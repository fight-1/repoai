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

"""Offline rule engine.

Rules are declarative JSON documents matched against a failure context
(command name, exception type, aggregated messages, raw stderr).  The engine
ships a builtin set derived from real-world repo failure messages and can be
extended at runtime by:

  - <state_dir>/rules.d/*.json   (admin-maintained, always merged)
  - offline packages imported with `repo ai-rules --import <file>`

Rule schema (unknown keys are ignored, all fields optional except id/patterns):
  id          stable rule id, e.g. "R1002"
  title       one line summary
  commands    list of fnmatch patterns for the subcommand ("sync", "*")
  exceptions  list of exception class names ("DownloadError")
  patterns    list of regexes matched against the failure text (ANY match)
  require_all if true, all patterns must match (default false)
  confidence  0..1 static confidence
  admin       bool, whether an admin is likely needed
  cause       root cause paragraph
  steps       list of remediation steps (shown in order)
  related     list of related rule ids / kb ids
"""

import fnmatch
import json
import os
import re


_BUILTIN_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")


class Rule:
    """One diagnostic rule."""

    def __init__(self, data, origin="builtin"):
        self.id = data["id"]
        self.title = data.get("title", "")
        self.commands = list(data.get("commands", ["*"]))
        self.exceptions = list(data.get("exceptions", []))
        self.patterns_raw = list(data.get("patterns", []))
        self.require_all = bool(data.get("require_all", False))
        self.confidence = float(data.get("confidence", 0.5))
        self.admin = bool(data.get("admin", False))
        self.cause = data.get("cause", "")
        self.steps = list(data.get("steps", []))
        self.related = list(data.get("related", []))
        self.origin = origin
        self.broken_patterns = []
        self._compiled = []
        bad = []
        for pat in self.patterns_raw:
            try:
                # IGNORECASE: the same upstream message differs in casing
                # across git/curl/ssh versions ("Connection refused" vs
                # "connection refused"); a case-sensitive match would just
                # silently miss the rule.
                self._compiled.append(
                    re.compile(pat, re.MULTILINE | re.IGNORECASE))
            except re.error:
                bad.append(pat)
        if bad:
            self.broken_patterns = bad

    def matches(self, ctx):
        """Return score (0 = no match).  Higher is better.

        Filters apply only when the context provides the field: matching an
        error text without knowing the command (repo explain) must still
        work, while a known command/exception narrows the candidates.
        """
        name = ctx.get("command") or ""
        exc = ctx.get("exception") or ""
        text = ctx.get("text") or ""

        if self.commands and name:
            if not any(fnmatch.fnmatch(name, c) for c in self.commands):
                return 0.0
        if self.exceptions and exc:
            if exc not in self.exceptions:
                return 0.0
        if not self._compiled:
            # Exception/command-only rule: usable only with those fields.
            if exc and self.exceptions and exc in self.exceptions:
                return 0.4
            return 0.0

        hits = 0
        for cre in self._compiled:
            if cre.search(text):
                hits += 1
        if hits == 0:
            return 0.0
        if self.require_all and hits != len(self._compiled):
            return 0.0
        # Patterns are alternatives (ANY), so matching one of them is a full
        # hit either way; requiring all of them is stronger evidence.  Static
        # confidence acts as a specificity tiebreak so that a narrow rule
        # (e.g. revision conflict) outranks a broad catch-all (Cannot fetch).
        score = 0.70 if not self.require_all else 0.90
        if name and self.commands and self.commands != ["*"]:
            score += 0.05
        if exc and self.exceptions:
            score += 0.05
        score += 0.15 * self.confidence
        return min(score, 1.0)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "origin": self.origin,
            "confidence": self.confidence,
            "commands": self.commands,
            "exceptions": self.exceptions,
            "patterns": self.patterns_raw,
            "require_all": self.require_all,
            "admin": self.admin,
            "cause": self.cause,
            "steps": self.steps,
            "related": self.related,
        }


class RuleSet:
    """Ordered collection of rules with lookup by id and keyword."""

    def __init__(self, rules=None):
        self._rules = list(rules or [])
        self._by_id = {}
        for r in self._rules:
            self._by_id[r.id] = r
        # Later definitions override earlier ones by id; keep list order for
        # stable listing but rebuild the index.
        self._by_id = {}
        for r in self._rules:
            self._by_id[r.id] = r

    def __len__(self):
        return len(self._by_id)

    def __iter__(self):
        return iter(sorted(self._by_id.values(), key=lambda r: r.id))

    def get(self, rule_id):
        return self._by_id.get(rule_id)

    def rules_for(self, ctx):
        """Return [(rule, score)] sorted by descending score (score>0)."""
        scored = []
        for rule in self._by_id.values():
            score = rule.matches(ctx)
            if score > 0:
                scored.append((rule, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored

    def search(self, keyword):
        """Loose keyword search over id/title/cause/steps (for explain)."""
        kw = keyword.lower().strip()
        if not kw:
            return []
        out = []
        for rule in self:
            hay = " ".join(
                [rule.id, rule.title, rule.cause, " ".join(rule.steps)]
            ).lower()
            if kw in hay:
                out.append(rule)
        return out

    def merge(self, other):
        """Return a new RuleSet where |other| overrides self by id."""
        data = [r.to_dict() for r in self]
        override = [r.to_dict() for r in other]
        merged = {}
        order = []
        for d in data + override:
            if d["id"] not in merged:
                order.append(d["id"])
            merged[d["id"]] = d
        rules = [Rule(merged[i], origin=merged[i].get("origin", "builtin"))
                 for i in order]
        return RuleSet(rules)


def _load_file(path, origin):
    """Return (rules, problems).  Malformed entries never abort the file;
    each one is reported so callers can surface it ("不许静默劣化")."""
    with open(path, "r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if isinstance(payload, dict):
        rules = payload.get("rules", [])
    else:
        rules = payload
    out = []
    problems = []
    for item in rules:
        try:
            rule = Rule(item, origin=origin)
        except (KeyError, TypeError, ValueError) as e:
            rid = "?"
            if isinstance(item, dict):
                rid = item.get("id", "?")
            problems.append(
                {"origin": origin, "file": path, "id": rid,
                 "kind": "malformed", "detail": str(e)})
            continue
        if rule.broken_patterns:
            problems.append(
                {"origin": origin, "file": path, "id": rule.id,
                 "kind": "broken-pattern",
                 "detail": ", ".join(rule.broken_patterns)})
        out.append(rule)
    return out, problems


def load_rules(state_dir=None, extra_files=None):
    """Load builtin rules then layer admin/offline rules on top.

    Returns a RuleSet; ``set.load_problems`` lists rules that were skipped
    or carry broken patterns (surfaced by `repo doctor` / `repo rules`).
    """
    rules = []
    problems = []
    try:
        got, probs = _load_file(_BUILTIN_RULES_PATH, "builtin")
        rules.extend(got)
        problems.extend(probs)
    except (OSError, ValueError):
        pass
    sources = []
    if state_dir:
        rules_d = os.path.join(state_dir, "rules.d")
        if os.path.isdir(rules_d):
            for name in sorted(os.listdir(rules_d)):
                if name.endswith(".json"):
                    sources.append(
                        (os.path.join(rules_d, name), "admin")
                    )
    for path in extra_files or []:
        sources.append((path, "offline-package"))
    for path, origin in sources:
        try:
            got, probs = _load_file(path, origin)
            rules.extend(got)
            problems.extend(probs)
        except OSError:
            problems.append({"origin": origin, "file": path, "id": "?",
                             "kind": "unreadable", "detail": ""})
            continue
        except ValueError as e:
            problems.append({"origin": origin, "file": path, "id": "?",
                             "kind": "invalid-json", "detail": str(e)})
            continue
    result = RuleSet(rules)
    result.load_problems = problems
    return result


def lint_ruleset(ruleset, known_commands=(), known_kb_ids=()):
    """Validate a RuleSet; return a list of human-readable problem lines.

    Catches what would otherwise be a silently dead rule: an uncompilable
    pattern, a rule id referenced but missing, a dangling FAQ: reference,
    an out-of-range confidence, or a commands filter naming a subcommand
    that does not exist (typos never match anything, forever).
    """
    problems = []
    for p in getattr(ruleset, "load_problems", []):
        where = "%s(%s)" % (p["file"], p["id"])
        if p["kind"] == "broken-pattern":
            problems.append(u"%s: 正则无法编译：%s" % (where, p["detail"]))
        else:
            problems.append(u"%s: 规则被跳过（%s：%s）"
                            % (where, p["kind"], p["detail"]))
    ids = {r.id for r in ruleset}
    kb_ids = set(str(x) for x in known_kb_ids)
    for rule in ruleset:
        if not 0.0 <= rule.confidence <= 1.0:
            problems.append(u"%s: confidence=%s 不在 0..1"
                            % (rule.id, rule.confidence))
        for pat in rule.broken_patterns:
            problems.append(u"%s: 正则无法编译：%s" % (rule.id, pat))
        for cmd in rule.commands:
            if cmd == "*":
                continue
            if known_commands and cmd not in known_commands:
                problems.append(u"%s: 未知子命令 %r 永远不会匹配"
                                % (rule.id, cmd))
        for ref in rule.related:
            if ref.startswith("FAQ:"):
                if str(ref[4:]) not in kb_ids:
                    problems.append(
                        u"%s: 相关条目 %s 在知识库中不存在" % (rule.id, ref))
            elif ref.startswith("R") and ref not in ids:
                problems.append(u"%s: 相关规则 %s 不存在" % (rule.id, ref))
    return problems


def diagnose(ruleset, ctx, top=3):
    """Return the top matching (rule, score) pairs for |ctx|."""
    return ruleset.rules_for(ctx)[:top]
