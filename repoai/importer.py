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

"""Admin-side ingestion of shuttle NDJSON batches.

Batches exported by isolated regions (repo telemetry --export / flush
file-exchange) are collected here, de-duplicated by event id, stored in a
local sqlite store, and summarized (top rules, region distribution,
feedback verdicts).  This is the minimum useful "dashboard" that works
fully offline; Grafana/OTel come later.

Store schema mirrors the client queue plus an ingestion timestamp; payloads
stay structured metadata only.
"""

import json
import os
import sqlite3
import time


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  ts          REAL NOT NULL,
  ingested_at REAL NOT NULL,
  type        TEXT NOT NULL,
  region      TEXT,
  client_id   TEXT,
  payload     TEXT NOT NULL,
  source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


class ImportResult:
    def __init__(self, path):
        self.path = path
        self.parsed = 0
        self.inserted = 0
        self.duplicates = 0
        self.bad = 0

    def ok(self):
        return self.bad == 0


def _connect(store_path):
    os.makedirs(os.path.dirname(os.path.abspath(store_path)), exist_ok=True)
    conn = sqlite3.connect(store_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def import_text(store_path, text, source=""):
    """Ingest NDJSON text; returns ImportResult.  Shared by file import
    and the online collector (one schema, one dedupe path)."""
    result = ImportResult(source)
    conn = _connect(store_path)
    try:
        now = time.time()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                result.bad += 1
                continue
            if "_meta" in rec:
                continue  # batch header
            if not all(k in rec for k in ("id", "ts", "type", "payload")):
                result.bad += 1
                continue
            result.parsed += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO events"
                " (id, ts, ingested_at, type, region, client_id,"
                "  payload, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["ts"],
                    now,
                    rec["type"],
                    rec.get("region", ""),
                    rec.get("client_id", ""),
                    json.dumps(rec["payload"], ensure_ascii=False,
                               sort_keys=True),
                    source,
                ),
            )
            if cur.rowcount:
                result.inserted += 1
            else:
                result.duplicates += 1
        conn.commit()
    finally:
        conn.close()
    return result


def import_file(store_path, path):
    """Import one NDJSON batch; returns ImportResult.  Raises OSError."""
    with open(path, "r", encoding="utf-8") as fp:
        text = fp.read()
    return import_text(store_path, text, os.path.basename(path))


def _rows(store_path, sql, params=()):
    conn = _connect(store_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def summary(store_path, top=10):
    """Aggregates for the admin report (metadata-only fields)."""
    (total,) = _rows(store_path, "SELECT COUNT(*) FROM events")[0]
    clients = _rows(
        store_path, "SELECT COUNT(DISTINCT client_id) FROM events"
    )[0][0]

    by_region = _rows(
        store_path,
        "SELECT region, COUNT(*) FROM events GROUP BY region"
        " ORDER BY COUNT(*) DESC",
    )
    top_rules = _rows(
        store_path, "SELECT payload FROM events WHERE type IN"
        " ('error','hint-shown','rule-hit')"
    )
    rule_counts = {}
    unmatched = {}  # radar: failures NO rule covered -> next rule to write
    for (payload,) in top_rules:
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        hits = list(obj.get("rules", [])[:1])  # top match only
        for rid in hits:
            rule_counts[rid] = rule_counts.get(rid, 0) + 1
        rid = obj.get("rule")
        if rid:
            rule_counts[rid] = rule_counts.get(rid, 0) + 1
            hits = [rid]
        if not hits and obj.get("command"):
            key = (obj.get("command"), obj.get("exception") or "?")
            unmatched[key] = unmatched.get(key, 0) + 1
    ranked = sorted(rule_counts.items(), key=lambda kv: -kv[1])[:top]
    gap = sorted(unmatched.items(), key=lambda kv: (-kv[1], kv[0]))[:top]

    fb = {}
    rule_fb = {}  # rule_id -> {verdict: count}: which rules actually help
    for (payload,) in _rows(
        store_path, "SELECT payload FROM events WHERE type = 'feedback'"
    ):
        try:
            obj = json.loads(payload)
        except ValueError:
            obj = {}
        verdict = obj.get("verdict") or "?"
        fb[verdict] = fb.get(verdict, 0) + 1
        rid = obj.get("rule")
        if rid:
            per = rule_fb.setdefault(rid, {})
            per[verdict] = per.get(verdict, 0) + 1

    # Implicit outcomes (adoption proxy): after a hint, did the very next
    # run of the same command succeed?  Correlation, NOT a resolution
    # rate -- network hiccups recovering on their own look identical to
    # "the advice worked", so reports must label this 建议后成功率 and
    # always show it next to the explicit feedback rate.
    rule_outcome = {}
    for (payload,) in _rows(
        store_path, "SELECT payload FROM events WHERE type = 'outcome'"
    ):
        try:
            obj = json.loads(payload)
        except ValueError:
            obj = {}
        rid = obj.get("rule")
        verdict = obj.get("verdict")
        if not rid or not verdict:
            continue
        per = rule_outcome.setdefault(rid, {})
        per[verdict] = per.get(verdict, 0) + 1

    by_type = _rows(
        store_path,
        "SELECT type, COUNT(*) FROM events GROUP BY type"
        " ORDER BY COUNT(*) DESC",
    )
    return {
        "total": total,
        "clients": clients,
        "by_region": by_region,
        "top_rules": ranked,
        "top_unmatched": gap,
        "rule_feedback": rule_fb,
        "rule_outcome": rule_outcome,
        "feedback": fb,
        "by_type": by_type,
    }


def render_summary(data, ruleset=None):
    """Human-readable admin report."""
    lines = []
    lines.append(u"事件存储：共 %d 条，来自 %d 个客户端"
                 % (data["total"], data["clients"]))
    if data["by_region"]:
        lines.append(u"区域分布：")
        for region, n in data["by_region"]:
            lines.append(u"  %-12s %d" % (region or u"(未知)", n))
    if data["top_rules"]:
        lines.append(u"Top %d 高频问题：" % len(data["top_rules"]))
        rule_fb = data.get("rule_feedback") or {}
        rule_out = data.get("rule_outcome") or {}
        for rid, n in data["top_rules"]:
            title = u""
            if ruleset is not None:
                rule = ruleset.get(rid)
                if rule is not None:
                    title = u" %s" % rule.title
            lines.append(u"  %-7s %5d 次%s" % (rid, n, title))
            per = rule_fb.get(rid) or {}
            total_r = sum(per.values())
            flag = u""
            if total_r:
                resolved = per.get("resolved", 0)
                detail = u"、".join(
                    u"%s %d" % (v, per[v]) for v in sorted(per)
                )
                bad = per.get("inaccurate", 0) + per.get("unresolved", 0)
                if total_r >= 3 and 2.0 * bad >= total_r:
                    flag = u"  ⚠ 建议复核该规则"
                lines.append(u"          反馈 %d 条（%s）：解决率 %.0f%%%s"
                             % (total_r, detail,
                                100.0 * resolved / total_r, flag))
            out = rule_out.get(rid) or {}
            ok = out.get("success_after_hint", 0)
            bad_n = out.get("retry_failed", 0)
            tries = ok + bad_n
            out_rate = 100.0 * ok / tries if tries else None
            # Flag for review on either signal: explicit feedback says it
            # is bad, or (too few explicit verdicts but) the implicit
            # retry outcome is bad.  Explicit wins when both exist.
            if not flag and total_r < 3 and tries >= 3 and out_rate < 40.0:
                flag = u"  ⚠ 建议复核该规则（重试多失败）"
            if tries:
                lines.append(u"          建议后重试 %d 次：建议后成功率 "
                             u"%.0f%%%s（隐式口径，含自愈，非解决率）"
                             % (tries, out_rate, flag))
                if total_r >= 3 and tries >= 3 and out_rate >= 70.0:
                    expl = 100.0 * per.get("resolved", 0) / total_r
                    if expl <= 30.0:
                        lines.append(
                            u"          ⚠ 隐式成功但显式反馈差：疑似环境"
                            u"自愈，先别归功于该规则")
    gap = data.get("top_unmatched") or []
    if gap:
        lines.append(u"未命中缺口（无规则覆盖，建议补充规则库）：")
        for (cmd, exc), n in gap:
            lines.append(u"  %-10s %-28s %4d 次" % (cmd, exc, n))
    if data["feedback"]:
        total_fb = sum(data["feedback"].values())
        lines.append(u"反馈（共 %d 条）：" % total_fb)
        for verdict, n in sorted(data["feedback"].items(),
                                 key=lambda kv: -kv[1]):
            lines.append(u"  %-12s %d (%.0f%%)"
                         % (verdict, n, 100.0 * n / total_fb))
        resolved = data["feedback"].get("resolved", 0)
        if total_fb:
            lines.append(u"问题解决率：%.0f%%" % (100.0 * resolved / total_fb))
    if not data["total"]:
        lines.append(u"（存储为空）")
    return "\n".join(lines)
