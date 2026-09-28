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

"""Minimal telemetry collector: receives client batches over HTTP.

The client (repoai/telemetry.py, online channel) POSTs NDJSON batches to
<v1/events> on ai.telemetry.endpoint.  This is the receiving end for the
S6 online channel -- stdlib only, so any admin machine in an online zone
can run it today:

    python3 -m repoai.collector --port 8080 --token <secret>

Design points:
  - storage is the SAME sqlite store as `repo ai-admin summary`
    (de-duplicated by event id, so at-least-once client retries are safe)
  - optional bearer token (must then match the client's
    ai.telemetry.token); no token configured = open intake, print a
    loud warning at startup (never silently insecure)
  - bounded request body; malformed lines counted, never poison the DB
  - GET /healthz for ops; GET / serves a read-only dashboard whose data
    endpoints stay token-protected
"""

import glob
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

from repoai import dashboard
from repoai import importer
from repoai import rules as _rules


try:  # py3.7+
    from http.server import ThreadingHTTPServer
except ImportError:  # pragma: no cover - py3.6
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True


MAX_BODY = 8 * 1024 * 1024  # batches are metadata; 8 MB is very generous


class Collector:
    """Shared state for request handlers (one per server process).

    Retention: events live in the sqlite store for --rotate-days days;
    on the first write after the date changes, older rows are archived
    to <store-dir>/archive/events-YYYYMMDD.ndjson (append; never lost)
    and pruned from the live store.  The check is lazy (at most once a
    day, on write) -- no timers, no threads.
    """

    def __init__(self, store_path, token=None, rotate_days=7,
                 ingest_dir=None, ingest_interval=60.0, role="primary"):
        self.store_path = store_path
        self.token = token or ""
        self.rotate_days = max(1, rotate_days)
        # Display-only identity ("primary"/"backup") so a viewer can
        # never read the backup board as the primary one.  Intake
        # behaviour is identical either way.
        self.role = role if role in ("primary", "backup") else "primary"
        self.received = 0
        self.rejected = 0
        self._last_rotate = ""
        # Share-drop ingestion (fallback channel): clients whose primary
        # endpoint was down snapshot batches into a shared directory
        # (e.g. a team NFS share).  The admin mounts the same share
        # here and we fold those files into the store on a lazy timer --
        # no new network path, no new deps, import de-dupes by event id.
        self.ingest_dir = (ingest_dir or "").strip() or None
        self.ingest_interval = max(1.0, ingest_interval)
        self.ingested = 0
        self.ingest_error = None
        self._last_ingest = 0.0

    def _maybe_ingest(self):
        """Scan the ingest dir at most once per ingest_interval seconds."""
        if not self.ingest_dir:
            return
        now = time.time()
        if now - self._last_ingest < self.ingest_interval:
            return
        self._last_ingest = now
        try:
            self._ingest_now()
        except Exception as e:  # a broken share must not break intake
            self.ingest_error = str(e)

    def _ingest_now(self):
        for path in sorted(glob.glob(
                os.path.join(self.ingest_dir, "*.ndjson"))):
            try:
                result = importer.import_file(self.store_path, path)
            except OSError as e:
                # Share hiccups / half-open files: retry next tick.
                self.ingest_error = "%s: %s" % (
                    os.path.basename(path), e)
                continue
            self.ingest_error = None
            if result.inserted:
                self.received += result.inserted
                self.ingested += result.inserted
                sys.stderr.write(
                    "[collector] 共享目录收编 %s：新增 %d 条"
                    "（重复 %d 条已忽略）\n"
                    % (os.path.basename(path), result.inserted,
                       result.duplicates))

    def ingest(self, text, source):
        self._maybe_rotate()
        result = importer.import_text(self.store_path, text, source)
        self.received += result.inserted
        return result

    def _maybe_rotate(self):
        today = time.strftime("%Y%m%d")
        if self._last_rotate == today:
            return
        self._last_rotate = today
        try:
            self._rotate(today)
        except Exception as e:  # rotation must never break intake
            sys.stderr.write("[collector] 轮转失败（不影响接收）：%s\n" % e)

    def _rotate(self, today):
        cutoff = time.time() - self.rotate_days * 86400.0
        conn = importer._connect(self.store_path)
        try:
            rows = conn.execute(
                "SELECT id, ts, type, region, client_id, payload"
                " FROM events WHERE ingested_at < ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                return
            archive_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.store_path)), "archive"
            )
            os.makedirs(archive_dir, exist_ok=True)
            path = os.path.join(archive_dir, "events-%s.ndjson" % today)
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as fp:
                for rid, ts, typ, region, client, payload in rows:
                    fp.write(
                        json.dumps(
                            {
                                "id": rid,
                                "ts": ts,
                                "type": typ,
                                "region": region,
                                "client_id": client,
                                "payload": json.loads(payload),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            if new:
                sys.stderr.write("[collector] 归档目录：%s\n" % path)
            conn.execute("DELETE FROM events WHERE ingested_at < ?",
                         (cutoff,))
            conn.commit()
            sys.stderr.write(
                "[collector] 轮转：归档并清理 %d 条（保留 %d 天）\n"
                % (len(rows), self.rotate_days)
            )
        finally:
            conn.close()

    def feedback_summary(self):
        """Verdict counters + rule hit ranking for the dashboard.

        Same counting semantics as `repo ai-admin summary` (top rule of
        error/hint-shown + explicit rule field).  Small data (pilot
        scale), so a scan beats a second schema.
        """
        out = {"ok": True, "feedback": {}, "feedback_total": 0,
               "resolved_rate": None, "top_rules": [],
               "top_unmatched": [], "rule_feedback": {},
               "rule_outcome": {}}
        try:
            conn = importer._connect(self.store_path)
            try:
                rules = {}
                gap = {}  # no rule covered this failure -> next rule to add
                rows = conn.execute(
                    "SELECT type, payload FROM events"
                    " WHERE type IN ('error','hint-shown','rule-hit',"
                    " 'feedback','outcome')")
                for rtype, payload in rows:
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if rtype == "feedback":
                        verdict = data.get("verdict") or "unknown"
                        fb = out["feedback"]
                        fb[verdict] = fb.get(verdict, 0) + 1
                        rid = data.get("rule")
                        if rid:
                            per = out["rule_feedback"].setdefault(rid, {})
                            per[verdict] = per.get(verdict, 0) + 1
                        continue
                    if rtype == "outcome":
                        # Implicit adoption proxy: next run of the same
                        # command after the hint.  Correlation only --
                        # label it 建议后成功率, never 解决率.
                        rid = data.get("rule")
                        verdict = data.get("verdict")
                        if rid and verdict:
                            per = out["rule_outcome"].setdefault(rid, {})
                            per[verdict] = per.get(verdict, 0) + 1
                        continue
                    hits = list(data.get("rules") or [])[:1]
                    for rid in hits:
                        rules[rid] = rules.get(rid, 0) + 1
                    rid = data.get("rule")
                    if rid:
                        rules[rid] = rules.get(rid, 0) + 1
                        hits = [rid]
                    if not hits and data.get("command"):
                        key = (data.get("command"),
                               data.get("exception") or "?")
                        gap[key] = gap.get(key, 0) + 1
                out["top_rules"] = [[k, v] for k, v in sorted(
                    rules.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
                out["top_unmatched"] = [
                    [c, e, n] for (c, e), n in sorted(
                        gap.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
                total = sum(out["feedback"].values())
                out["feedback_total"] = total
                if total:
                    out["resolved_rate"] = round(
                        100.0 * out["feedback"].get("resolved", 0) / total, 1)
            finally:
                conn.close()
        except Exception as e:
            out["error"] = str(e)
        return out

    def rules_index(self):
        """{rule_id: title} for the dashboard's hit ranking.  Titles are
        static library text (no user data), so no auth needed."""
        try:
            rs = _rules.load_rules()
            return {r.id: r.title for r in rs}
        except Exception:
            return {}

    def export_ndjson(self):
        """Whole live store as NDJSON (same record shape as intake).

        The backup collector exposes this so the primary can fold
        outage-period events back with `repo ai-admin pull` (dedupe by
        id makes the merge exact).  Pilot scale: a full dump is small.
        """
        lines = []
        conn = importer._connect(self.store_path)
        try:
            cur = conn.execute(
                "SELECT id, ts, type, region, client_id, payload"
                " FROM events ORDER BY ts")
            for eid, ts, etype, region, client, payload in cur:
                try:
                    body = json.loads(payload)
                except ValueError:
                    continue
                lines.append(json.dumps(
                    {"id": eid, "ts": ts, "type": etype,
                     "region": region or "", "client_id": client or "",
                     "payload": body}, ensure_ascii=False))
        finally:
            conn.close()
        return ("\n".join(lines) + "\n") if lines else ""

    def stats(self):
        """Compact JSON facts for dashboards (/v1/stats).  Structure
        mirrors repo ai-admin summary so Grafana can read either."""
        out = {
            "ok": True,
            "store": os.path.basename(self.store_path),
            "role": self.role,
            "rotate_days": self.rotate_days,
            "process": {
                "received": self.received,
                "rejected": self.rejected,
                "ingested": self.ingested,
            },
        }
        if self.ingest_dir:
            out["ingest_dir"] = self.ingest_dir
            if self.ingest_error:
                out["ingest_error"] = self.ingest_error
        try:
            conn = importer._connect(self.store_path)
            try:
                out["total"] = conn.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0]
                out["clients"] = conn.execute(
                    "SELECT COUNT(DISTINCT client_id)"
                    " FROM events").fetchone()[0]
                out["by_region"] = dict(
                    conn.execute(
                        "SELECT region, COUNT(*) FROM events"
                        " GROUP BY region").fetchall())
                out["by_type"] = dict(
                    conn.execute(
                        "SELECT type, COUNT(*) FROM events"
                        " GROUP BY type").fetchall())
                daily = conn.execute(
                    "SELECT strftime('%Y-%m-%d', ts, 'unixepoch'),"
                    " COUNT(*) FROM events"
                    " GROUP BY 1 ORDER BY 1 DESC LIMIT 30").fetchall()
                out["daily"] = [[d, n] for d, n in daily]
            finally:
                conn.close()
        except Exception as e:  # stats never 500 the ops plane
            out["error"] = str(e)
        return out


def make_handler(collector):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "repo-telemetry-collector/1"
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------
        def log_message(self, fmt, *args):
            sys.stderr.write(
                "[collector] %s %s\n"
                % (self.client_address[0], fmt % args)
            )

        def _respond(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_html(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type",
                             "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not collector.token:
                return True
            want = "Bearer " + collector.token
            got = self.headers.get("Authorization", "")
            return got == want

        # -- routes ---------------------------------------------------
        def do_GET(self):
            collector._maybe_ingest()  # fold share drops into the store
            if self.path == "/healthz":
                self._respond(
                    200,
                    {
                        "ok": True,
                        "role": collector.role,
                        "received": collector.received,
                        "store": os.path.basename(collector.store_path),
                    },
                )
            elif self.path == "/" or self.path.startswith("/index"):
                # Static shell only; the data endpoints it calls are
                # still token-protected, so serving this freely is safe.
                self._respond_html(200, dashboard.page())
            elif self.path == "/v1/rules":
                # Static rule titles (library text, no user data).
                self._respond(200, collector.rules_index())
            elif self.path in ("/v1/stats", "/v1/summary", "/v1/export"):
                if not self._authorized():
                    collector.rejected += 1
                    self._respond(401, {"error": "unauthorized"})
                    return
                if self.path == "/v1/stats":
                    self._respond(200, collector.stats())
                elif self.path == "/v1/export":
                    body = collector.export_ndjson().encode("utf-8")
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._respond(200, collector.feedback_summary())
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/events":
                self._respond(404, {"error": "not found"})
                return
            if not self._authorized():
                collector.rejected += 1
                self._respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self._respond(400, {"error": "empty body"})
                return
            if length > MAX_BODY:
                collector.rejected += 1
                self._respond(413, {"error": "body too large"})
                return
            body = self.rfile.read(length)
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                self._respond(400, {"error": "not utf-8"})
                return
            source = "online:%s" % self.client_address[0]
            result = collector.ingest(text, source)
            # 200 even with bad lines: the DB is not poisoned, and the
            # client's at-least-once retry must not spin on them.
            self._respond(
                200,
                {
                    "ok": True,
                    "inserted": result.inserted,
                    "duplicates": result.duplicates,
                    "bad": result.bad,
                },
            )

    return _Handler


def make_server(host, port, store_path, token=None, rotate_days=7,
                ingest_dir=None, role="primary"):
    collector = Collector(store_path, token, rotate_days,
                          ingest_dir=ingest_dir, role=role)
    httpd = ThreadingHTTPServer((host, port), make_handler(collector))
    httpd.repo_collector = collector
    return httpd


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="repo telemetry online collector (S6)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="~/.repoai/store.sqlite",
                        help="sqlite store path (same one ai-admin reads)")
    parser.add_argument("--token", default=os.environ.get(
        "REPO_TELEMETRY_TOKEN", ""),
        help="bearer token; empty = open intake (warns loudly)")
    parser.add_argument("--rotate-days", type=int, default=7,
                        help="keep live events this many days, then"
                        " archive to NDJSON and prune (default 7)")
    parser.add_argument("--ingest-dir", default="",
                        help="also fold *.ndjson share drops (client"
                        " fallback snapshots) into the store; point at"
                        " the mounted share, e.g. /mnt/repo-share/...")
    parser.add_argument("--role", default="primary",
                        choices=("primary", "backup"),
                        help="display-only identity for the dashboard"
                        " header; intake behaviour is identical")
    opts = parser.parse_args(argv)

    store = os.path.expanduser(opts.store)
    if not opts.token:
        sys.stderr.write(
            "[collector] 警告：未配置 --token，任何能访问本端口的客户端"
            "都可写入。生产请使用：--token <secret>（或环境变量 "
            "REPO_TELEMETRY_TOKEN），并确保客户端 ai.telemetry.token "
            "一致。\n"
        )
    httpd = make_server(opts.host, opts.port, store, opts.token,
                        opts.rotate_days, ingest_dir=opts.ingest_dir,
                        role=opts.role)
    sys.stderr.write(
        "[collector] 监听 http://%s:%d/v1/events，事件库 %s"
        "（保留 %d 天，过期归档 NDJSON）\n"
        % (opts.host, opts.port, store, max(1, opts.rotate_days))
    )
    if opts.role != "primary":
        sys.stderr.write(
            "[collector] 身份：%s（看板页眉标注；主收集器恢复后用 "
            "repo ai-admin pull 并库）\n" % opts.role)
    if opts.ingest_dir:
        sys.stderr.write(
            "[collector] 共享目录收编：%s（每 60 秒扫一次 *.ndjson）\n"
            % opts.ingest_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[collector] 已停止\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
