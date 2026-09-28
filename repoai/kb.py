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

"""Offline knowledge base with tiny built-in full-text search.

Layout (all plain files, safe to ship in offline packages):
  <kb_dir>/kb.json          builtin entries
  <kb_dir>/kb.d/*.json      admin/offline-package incremental entries
Entry schema: {"id": "1024", "title": str, "body": str, "tags": [str]}

Search is a simple TF-style scoring over CJK-friendly bigrams plus ASCII
word tokens -- no external index libraries (zero-dependency constraint).
"""

import json
import os
import re


_BUILTIN_KB_PATH = os.path.join(os.path.dirname(__file__), "kb.json")

_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_./+-]*")
_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯]")

# English function words carry no signal here but appear inside KB bodies
# ("Unable to create ..."), so an unrelated English query like "how to brew
# espresso" would score a spurious hit on "to".  Dropping them keeps the
# honest "没有找到" answer for questions the KB truly cannot answer.
_STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has "
    "have how i if in is it its me my of on or please should so than that "
    "the their them then there these they this to too us was we were what "
    "when where which who will with would you your not no just like get "
    "need want".split()
)


def tokenize(text):
    """Split into lowercase ascii words + CJK unigrams/bigrams."""
    text = text.lower()
    tokens = [t for t in _ASCII_WORD.findall(text) if t not in _STOPWORDS]
    # Drop single-char ascii noise but keep meaningful short tokens like "j".
    cjk_runs = []
    run = []
    for ch in text:
        if _CJK.match(ch):
            run.append(ch)
        else:
            if run:
                cjk_runs.append("".join(run))
                run = []
    if run:
        cjk_runs.append("".join(run))
    for r in cjk_runs:
        tokens.extend(r)  # unigrams
        if len(r) > 1:
            tokens.extend(r[i : i + 2] for i in range(len(r) - 1))
    return tokens


class KbEntry:
    def __init__(self, data, origin="builtin"):
        self.id = str(data["id"])
        self.title = data.get("title", "")
        self.body = data.get("body", "")
        self.tags = [str(t) for t in data.get("tags", [])]
        self.origin = origin
        self._tokens = None

    @property
    def tokens(self):
        if self._tokens is None:
            self._tokens = tokenize(
                " ".join([self.title, self.body, " ".join(self.tags)])
            )
        return self._tokens

    def score(self, query_tokens):
        if not query_tokens:
            return 0.0
        if not self.tokens:
            return 0.0
        from collections import Counter

        counts = Counter(self.tokens)
        total = len(self.tokens)
        s = 0.0
        for t in set(query_tokens):
            tf = counts.get(t, 0)
            if tf:
                # Weight multi-char tokens higher than CJK unigrams.
                weight = 2.0 if len(t) > 1 else 0.5
                s += weight * (1.0 + tf) / total
        # Reward covering more of the query.
        if counts:
            hit_terms = sum(
                1 for t in set(query_tokens) if counts.get(t, 0) > 0
            )
            s *= 1.0 + 0.5 * hit_terms / len(set(query_tokens))
        return s


class KnowledgeBase:
    def __init__(self, entries=None):
        self._entries = list(entries or [])
        self._by_id = {}
        for e in self._entries:
            self._by_id[e.id] = e

    def __len__(self):
        return len(self._by_id)

    def __iter__(self):
        return iter(sorted(self._by_id.values(), key=lambda e: e.id))

    def get(self, entry_id):
        return self._by_id.get(str(entry_id))

    def ids(self):
        """All entry ids, for validating related "FAQ:<id>" references."""
        return set(self._by_id)

    def search(self, query, top=5):
        qt = tokenize(query)
        scored = []
        for entry in self._by_id.values():
            score = entry.score(qt)
            if score > 0:
                scored.append((entry, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored[:top]


def _load_entries(path, origin):
    with open(path, "r", encoding="utf-8") as fp:
        payload = json.load(fp)
    items = payload.get("entries", []) if isinstance(payload, dict) else payload
    out = []
    for item in items:
        try:
            out.append(KbEntry(item, origin=origin))
        except (KeyError, TypeError):
            continue
    return out


def load_kb(kb_dir=None):
    """Load builtin entries, layered by kb.d/*.json from state dir."""
    entries = []
    try:
        entries.extend(_load_entries(_BUILTIN_KB_PATH, "builtin"))
    except (OSError, ValueError):
        pass
    if kb_dir:
        kb_d = os.path.join(kb_dir, "kb.d")
        if os.path.isdir(kb_d):
            for name in sorted(os.listdir(kb_d)):
                if name.endswith(".json"):
                    try:
                        entries.extend(
                            _load_entries(os.path.join(kb_d, name), "offline")
                        )
                    except (OSError, ValueError):
                        continue
    return KnowledgeBase(entries)
