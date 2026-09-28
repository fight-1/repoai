# Copyright (C) 2026 fight-1
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Telemetry upload channels.

Telemetry is intentionally decoupled from AI inference: isolated regions
may not reach an AI gateway but can still ship usage metadata out through
their allowed path.

Channels (config ai.telemetry.channel = auto resolves to one of):
  online        POST NDJSON batches to ai.telemetry.endpoint (/v1/events)
  file-exchange write batch NDJSON into ai.telemetry.exchange-dir (R2)
  offline       nothing to do; events wait in the local queue (R3)

Payload is NDJSON metadata only -- event ids let the server de-duplicate,
so retry/export is always safe (at-least-once delivery).  Nothing here
ever raises into the command path; callers get a SyncReport instead.
"""

import json
import os
import time
import urllib.error
import urllib.request

from repoai import events as _events


DEFAULT_BATCH_SIZE = 200
DEFAULT_TIMEOUT = 10.0


class SyncReport:
    """Outcome of one flush attempt (for command output/tests)."""

    def __init__(self, channel):
        self.channel = channel  # online / file-exchange / offline
        self.sent = 0           # events accepted by the remote/queue handoff
        self.pending = 0        # still queued after this attempt
        self.error = None       # user-presentable failure reason
        self.handoff_path = None  # R2: batch file written into exchange dir
        self.fallback_path = None  # primary down -> snapshot in fallback dir
        self.fallback_endpoint = None  # primary down -> backup collector
        self.fallback_reason = None  # why the primary path failed

    def ok(self):
        return self.error is None


def _batch_size(cfg):
    try:
        return max(1, int(cfg.get("ai.telemetry.batch-size",
                                  DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def _timeout(cfg):
    try:
        return float(cfg.get("ai.telemetry.timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _encode_batch(queue, rows):
    lines = [
        json.dumps(
            {
                "_meta": {
                    "client_id": queue.client_id(),
                    "exported_at": time.time(),
                    "count": len(rows),
                }
            },
            ensure_ascii=False,
        )
    ]
    for eid, ts, etype, region, obj in rows:
        lines.append(
            json.dumps(
                {
                    "id": eid,
                    "ts": ts,
                    "type": etype,
                    "region": region,
                    "client_id": queue.client_id(),
                    "payload": obj,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


def flush_online(cfg, queue, report, timeout=None):
    """POST pending events to ai.telemetry.endpoint, with fallbacks.

    Consent boundary: uploading requires a configured primary endpoint
    (the group-setup line); the built-in backup only makes that consent
    resilient, it never turns on upload by itself.

    Primary DOWN (unreachable / 5xx) -> try, in order:
      1. ai.telemetry.fallback-dir   shuttle snapshot into a mounted share
      2. ai.telemetry.fallback-endpoint  built-in backup collector
         (same protocol + token; runs on the second admin machine)
    Neither path ever marks events sent: the PRIMARY owns the ack, so a
    recovered 110 still receives everything and dedupes what the backup
    already took.  That is why an outage costs only dashboard latency.
    """
    endpoint = (cfg.get("ai.telemetry.endpoint") or "").strip()
    if not endpoint:
        report.error = u"未配置 ai.telemetry.endpoint"
        return report
    rows = queue.pending(limit=_batch_size(cfg))
    if not rows:
        return report
    to = (_timeout(cfg) if timeout is None
          else min(_timeout(cfg), max(0.5, float(timeout))))
    err = _post_batch(cfg, queue, rows, endpoint, to)
    if err is None:
        queue.mark_sent([r[0] for r in rows])
        report.sent = len(rows)
        return report
    report.fallback_reason = err
    fallback = (cfg.get("ai.telemetry.fallback-dir") or "").strip()
    if fallback:
        path = _write_fallback(cfg, queue, rows, fallback)
        if path:
            report.sent = len(rows)
            report.fallback_path = path
            return report
    backup = (cfg.get_effective(
        "ai.telemetry.fallback-endpoint") or "").strip()
    if backup and backup.rstrip("/") != endpoint.rstrip("/"):
        if _post_batch(cfg, queue, rows, backup, to) is None:
            report.sent = len(rows)
            report.fallback_endpoint = backup
            return report
    # Both fallbacks missed: keep the PRIMARY error (loud, actionable).
    report.error = err
    return report


def _post_batch(cfg, queue, rows, endpoint, timeout):
    """One POST of the batch; None on 2xx, else an error string."""
    data = _encode_batch(queue, rows).encode("utf-8")
    headers = {
        "Content-Type": "application/x-ndjson",
        "X-Repo-Client": queue.client_id(),
    }
    token = cfg.get("ai.telemetry.token")
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/events",
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        # 4xx = server rejected our data: don't retry blindly forever.
        return u"上报端点返回 HTTP %s" % getattr(e, "code", "?")
    except (urllib.error.URLError, OSError) as e:
        return u"上报端点不可达：%s" % e
    return None


def _write_fallback(cfg, queue, rows, fallback):
    """Snapshot the batch into the fallback dir; path or None.

    Deterministic name per client: each flush replaces the previous
    snapshot (pending grows monotonically until something imports it),
    so a down collector cannot pile up files on the share.  tmp+rename
    keeps readers (admin import) from seeing half-written files.
    """
    try:
        if not os.path.isdir(fallback):
            return None
        path = os.path.join(
            fallback, "repo-events-%s.ndjson" % queue.client_id()[:12])
        tmp = "%s.%s.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(_encode_batch(queue, rows))
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def flush_file_exchange(cfg, queue, report):
    """Write a batch file into the exchange dir (R2 shuttle).

    The directory must already exist and be writable -- same contract as
    the region probe.  Auto-creating it could silently mask an unmounted
    share and lose the shuttle data.  Files stay pending in the queue on
    purpose: the import side de-dupes by id, so re-exporting after a lost
    shuttle is safe.
    """
    exchange = cfg.get("ai.telemetry.exchange-dir")
    if not exchange:
        report.error = u"未配置 ai.telemetry.exchange-dir"
        return report
    rows = queue.pending(limit=_batch_size(cfg))
    if not rows:
        return report
    if not os.path.isdir(exchange):
        report.error = u"摆渡目录不存在或未挂载：%s" % exchange
        return report
    try:
        name = "repo-events-%s-%s.ndjson" % (
            time.strftime("%Y%m%d-%H%M%S"),
            queue.client_id()[:8],
        )
        path = os.path.join(exchange, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(_encode_batch(queue, rows))
        os.replace(tmp, path)
    except OSError as e:
        report.error = u"摆渡目录写入失败：%s" % e
        return report
    # Bookkeeping stays OUT of the pending queue on purpose: it would
    # re-export forever.  report.handoff_path carries it to the caller.
    report.sent = len(rows)
    report.handoff_path = path
    return report


def flush(cfg, queue=None, channel=None, timeout=None):
    """Flush one batch over the effective channel.  Never raises.

    `timeout` (seconds) caps the online POST for best-effort callers
    (auto-flush inside a failing command must not add noticeable
    latency); the manual --flush path keeps the configured timeout.
    """
    try:
        if queue is None:
            queue = _events.EventQueue(cfg.state_dir())
        channel = channel or cfg.telemetry_channel()
        report = SyncReport(channel)
        if channel == "online":
            flush_online(cfg, queue, report, timeout=timeout)
        elif channel == "file-exchange":
            flush_file_exchange(cfg, queue, report)
        # offline (and unknown channels): events simply stay queued.
        report.pending = queue.count_pending()
        return report
    except Exception as e:  # pragma: no cover - defensive
        report = SyncReport(channel or "unknown")
        report.error = u"上报异常：%s" % e
        return report


# Auto-flush: after diagnostics queue events, best-effort push them so
# the pilot dashboard stays fresh without user action.  Off-switch:
# ai.telemetry.auto-flush=false (or ai.telemetry.enabled=false, or the
# ai.enabled master switch).  Throttled by a marker (default 15 min)
# and capped to a 3s online timeout: at most one slightly-slower failing
# command per window, and only when the endpoint is actually down.
AUTO_FLUSH_THROTTLE = 900.0
AUTO_FLUSH_TIMEOUT = 3.0


def auto_flush(cfg, queue):
    """Best-effort background-style flush after queuing events.

    Returns the SyncReport when it actually tried, else None.  Never
    raises, never prints: the command output stays byte-identical.
    """
    try:
        if not cfg.telemetry_enabled():
            return None
        if not cfg.get_bool("ai.telemetry.auto-flush", True):
            return None
        if cfg.telemetry_channel() != "online":
            return None  # shuttle/offline channels are user-driven by design
        if queue.count_pending() <= 0:
            return None
        state = cfg.state_dir()
        marker = os.path.join(state, ".autoflush_last")
        now = time.time()
        try:
            if now - os.path.getmtime(marker) < AUTO_FLUSH_THROTTLE:
                return None
        except OSError:
            pass  # missing marker (or unreadable): first run, proceed
        try:
            with open(marker, "w", encoding="utf-8") as fp:
                fp.write("%.0f\n" % now)
        except OSError:
            return None  # state dir not writable: nothing sane to do
        return flush(cfg, queue, timeout=AUTO_FLUSH_TIMEOUT)
    except Exception:
        return None
