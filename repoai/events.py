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

"""Local durable event queue (sqlite3, stdlib only).

All telemetry/feedback events land here first so nothing is lost when the
region has no egress.  A per-channel cursor enables resumable sync; the
remote end de-duplicates by the global event id.  Only structured metadata is
stored -- never code, raw logs, or manifest contents.

Schema is intentionally minimal and forward compatible.
"""

import json
import os
import sqlite3
import time
import uuid


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id         TEXT PRIMARY KEY,
  ts         REAL NOT NULL,
  type       TEXT NOT NULL,
  region     TEXT,
  client_id  TEXT,
  payload    TEXT NOT NULL,
  sent       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_sent ON events(sent, ts);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

_MAX_ROWS = 50000  # soft cap; oldest sent rows are pruned past this


def _client_id(state_dir):
    path = os.path.join(state_dir, "client_id")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            val = fp.read().strip()
        if val:
            return val
    except OSError:
        pass
    val = uuid.uuid4().hex
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(val)
    except OSError:
        pass
    return val


class EventQueue:
    """Append-mostly queue with channel-tagged send bookkeeping."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        try:
            os.makedirs(state_dir, exist_ok=True)
        except OSError:
            pass
        self._db_path = os.path.join(state_dir, "events.sqlite")
        self._client = _client_id(state_dir)

    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def client_id(self):
        return self._client

    # -- write ----------------------------------------------------------
    def add(self, etype, payload, region=None):
        """Enqueue one event; returns its id.  Never raises on full disk."""
        eid = uuid.uuid4().hex
        row = (
            eid,
            time.time(),
            etype,
            region or "",
            self._client,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO events"
                    " (id, ts, type, region, client_id, payload, sent)"
                    " VALUES (?,?,?,?,?,?,0)",
                    row,
                )
                conn.commit()
                self._prune(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            # Queue must never crash the primary command path.
            return None
        return eid

    def _prune(self, conn):
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
            if n > _MAX_ROWS:
                conn.execute(
                    "DELETE FROM events WHERE sent = 1 AND id IN ("
                    " SELECT id FROM events WHERE sent = 1"
                    " ORDER BY ts ASC LIMIT ?)",
                    (n - _MAX_ROWS,),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    # -- read / sync bookkeeping ---------------------------------------
    def pending(self, limit=1000):
        """Return [(id, ts, type, region, payload_dict)] not yet sent."""
        out = []
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                cur = conn.execute(
                    "SELECT id, ts, type, region, payload FROM events"
                    " WHERE sent = 0 ORDER BY ts ASC LIMIT ?",
                    (limit,),
                )
                for eid, ts, etype, region, payload in cur.fetchall():
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        obj = {"raw": payload}
                    out.append((eid, ts, etype, region, obj))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        return out

    def mark_sent(self, ids):
        if not ids:
            return
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.executemany(
                    "UPDATE events SET sent = 1 WHERE id = ?",
                    [(i,) for i in ids],
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    def count_pending(self):
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                (n,) = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE sent = 0"
                ).fetchone()
                return n
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def stats(self):
        """Return {'pending': N, 'total': M}; zeros when unreadable."""
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                (pend,) = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE sent = 0"
                ).fetchone()
                (total,) = conn.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()
                return {"pending": pend, "total": total}
            finally:
                conn.close()
        except sqlite3.Error:
            return {"pending": 0, "total": 0}

    # -- offline export (R2/R3) ----------------------------------------
    def export_batch(self, path):
        """Write pending events as NDJSON for file-exchange / manual shuttle.

        Returns number of events exported.  Leaves them unsent (import side
        de-dupes by id) so a lost shuttle does not drop data.
        """
        events = self.pending(limit=100000)
        if not events:
            return 0
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            meta = {
                "_meta": {
                    "client_id": self._client,
                    "exported_at": time.time(),
                    "count": len(events),
                }
            }
            fp.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for eid, ts, etype, region, obj in events:
                rec = {
                    "id": eid,
                    "ts": ts,
                    "type": etype,
                    "region": region,
                    "client_id": self._client,
                    "payload": obj,
                }
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return len(events)
