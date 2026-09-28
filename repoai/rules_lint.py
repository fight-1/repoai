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

"""Standalone validator for diagnostic rule files (offline, stdlib only).

Checks what would otherwise be a silently dead rule:

  - patterns that fail to compile (the rule then matches nothing, forever)
  - related/FAQ references pointing at entries that do not exist
  - commands naming a subcommand that does not exist (typo => never matches)
  - confidence outside 0..1, non-string fields, duplicate ids

The same engine backs ``repo rules --lint``; running the module directly
works without a repo workspace, e.g. on a build server before shipping an
offline package::

    python3 -m repoai.rules_lint package.json [more.json ...]
    python3 -m repoai.rules_lint --builtin            # the shipped set
    python3 -m repoai.rules_lint --json pkg.json      # machine-readable

Exit code: 0 when every file is clean, 1 when any problem is found.
"""

import argparse
import json
import os
import re
import sys

_BUILTIN_RULES = os.path.join(os.path.dirname(__file__), "rules.json")
_BUILTIN_KB = os.path.join(os.path.dirname(__file__), "kb.json")

_VALID_FIELDS = (
    "id", "title", "commands", "exceptions", "patterns", "require_all",
    "confidence", "admin", "cause", "steps", "related",
)


def known_command_names():
    """Real subcommand names, or () when subcmds cannot be imported.

    Returning () disables the unknown-command check rather than guessing --
    a false alarm would train admins to ignore the whole lint report.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from subcmds import all_commands  # noqa: F401
    except Exception:
        return ()
    return tuple(sorted(all_commands))


def load_kb_ids(path=None):
    """Knowledge-base entry ids used to resolve related "FAQ:<id>" refs."""
    path = path or _BUILTIN_KB
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, ValueError):
        return set()
    items = payload.get("entries", []) if isinstance(payload, dict) else payload
    return set(str(e.get("id")) for e in items if isinstance(e, dict))


def _check_regex(pattern, rid, out):
    if not isinstance(pattern, str):
        out.append(u"%s: pattern 不是字符串：%r" % (rid, pattern))
        return
    try:
        re.compile(pattern, re.MULTILINE | re.IGNORECASE)
    except re.error as e:
        out.append(u"%s: 正则无法编译：%s（%s）" % (rid, pattern, e))


def lint_items(items, where, known_commands, kb_ids):
    """Lint a list of rule dicts; return problem strings (never raises)."""
    out = []
    seen = {}
    for idx, item in enumerate(items):
        rid = (item or {}).get("id") if isinstance(item, dict) else None
        label = rid or u"第%d条" % (idx + 1)
        if not isinstance(item, dict):
            out.append(u"%s: 规则必须是 JSON 对象" % label)
            continue
        if not rid or not isinstance(rid, str):
            out.append(u"%s: 缺少字符串 id" % label)
        elif rid in seen:
            out.append(u"%s: id 重复（第 %d 条与第 %d 条）"
                       % (rid, seen[rid] + 1, idx + 1))
        else:
            seen[rid] = idx
        if u"id" not in item:
            out.append(u"%s: 缺 id 字段，引擎会整条跳过" % label)
        patterns = item.get("patterns")
        if patterns is None:
            out.append(u"%s: 缺 patterns 字段（仅按命令/异常匹配时，"
                       u"必须同时有 exceptions 或具体 commands）" % label)
            patterns = []
        if not isinstance(patterns, list):
            out.append(u"%s: patterns 必须是列表" % label)
            patterns = []
        for pat in patterns:
            _check_regex(pat, label, out)
        for field in ("title", "cause"):
            v = item.get(field, u"")
            if not isinstance(v, str):
                out.append(u"%s: %s 必须是字符串" % (label, field))
        conf = item.get("confidence", 0.5)
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            out.append(u"%s: confidence 必须是数字，当前 %r" % (label, conf))
        elif not 0.0 <= float(conf) <= 1.0:
            out.append(u"%s: confidence=%s 超出 0..1" % (label, conf))
        cmds = item.get("commands")
        if cmds is not None:
            if not isinstance(cmds, list):
                out.append(u"%s: commands 必须是列表" % label)
                cmds = []
            for c in cmds:
                if not isinstance(c, str):
                    out.append(u"%s: commands 含非字符串项 %r" % (label, c))
                elif c == "*":
                    continue
                elif known_commands and c not in known_commands:
                    out.append(u"%s: commands 含未知子命令 %r，该规则对此"
                               u"命令永不生效" % (label, c))
        for key in ("exceptions", "steps", "related"):
            v = item.get(key)
            if v is not None and not isinstance(v, list):
                out.append(u"%s: %s 必须是列表" % (label, key))
        for ref in item.get("related") or []:
            if not isinstance(ref, str):
                out.append(u"%s: related 含非字符串项 %r" % (label, ref))
            elif ref.startswith("FAQ:"):
                if str(ref[4:]) not in kb_ids:
                    out.append(u"%s: 相关条目 %s 在知识库中不存在"
                               % (label, ref))
            elif ref.startswith("R") and not ref[1:].isdigit():
                out.append(u"%s: related 引用格式异常：%s" % (label, ref))
        for key in item:
            if key not in _VALID_FIELDS:
                out.append(u"%s: 未知字段 %r（会被引擎忽略，可能拼写错误）"
                           % (label, key))
    if not items:
        out.append(u"%s: 文件不含任何规则" % where)
    return out


def builtin_rule_ids():
    """Rule ids shipped with the tool, for cross-file R: reference checks."""
    try:
        with open(_BUILTIN_RULES, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, ValueError):
        return set()
    items = payload.get("rules", []) if isinstance(payload, dict) else payload
    return set(i.get("id") for i in items if isinstance(i, dict))


def lint_file(path, known_commands=(), kb_ids=None):
    """Return (rules_count, problem_lines) for one rule file."""
    kb_ids = load_kb_ids() if kb_ids is None else kb_ids
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except OSError as e:
        return 0, [u"%s: 无法读取：%s" % (path, e)]
    except ValueError as e:
        return 0, [u"%s: 不是合法 JSON：%s" % (path, e)]
    if isinstance(payload, dict):
        items = payload.get("rules")
        if items is None:
            return 0, [u"%s: 缺少 rules 字段（应为 "
                       u"{\"rules\": [...]}）" % path]
    elif isinstance(payload, list):
        items = payload
    else:
        return 0, [u"%s: 顶层结构应为对象或列表" % path]
    if not isinstance(items, list):
        return 0, [u"%s: rules 字段必须是列表" % path]
    ids = set()
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    problems = lint_items(items, path, known_commands, kb_ids)
    # R: references may point at builtin rules, so resolve against the union.
    universe = ids | builtin_rule_ids() if path != _BUILTIN_RULES else ids
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        for ref in item.get("related") or []:
            if (isinstance(ref, str) and ref.startswith("R")
                    and ref[1:].isdigit() and ref not in universe):
                problems.append(u"%s: 相关规则 %s 在本文件与内置库中都不存在"
                                % (rid, ref))
    return len(items), problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m repoai.rules_lint",
        description="离线诊断规则库校验（只读，不修改文件）")
    parser.add_argument("paths", nargs="*", help="规则 JSON 文件")
    parser.add_argument("--builtin", action="store_true",
                        help="校验内置规则库")
    parser.add_argument("--kb", default=None, metavar="FILE",
                        help="知识库 JSON（解析 FAQ: 引用），默认内置")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="机器可读输出")
    opts = parser.parse_args(argv)

    paths = list(opts.paths)
    if opts.builtin:
        paths.append(_BUILTIN_RULES)
    if not paths:
        parser.error("给出规则文件路径，或使用 --builtin")

    kb_ids = load_kb_ids(opts.kb)
    known = known_command_names()
    report = []
    total_problems = 0
    total_rules = 0
    for path in paths:
        n, problems = lint_file(path, known, kb_ids)
        total_rules += n
        total_problems += len(problems)
        report.append({"file": path, "rules": n, "problems": problems})

    if opts.as_json:
        print(json.dumps({"files": report, "problems": total_problems},
                         ensure_ascii=False, indent=2))
    else:
        for item in report:
            if item["problems"]:
                print(u"%s: %d 条规则，%d 个问题"
                      % (item["file"], item["rules"], len(item["problems"])))
                for p in item["problems"]:
                    print(u"  - %s" % p)
            else:
                print(u"%s: %d 条规则，校验通过"
                      % (item["file"], item["rules"]))
        if total_problems:
            print(u"共 %d 个问题。问题规则会被引擎跳过或永不命中，"
                  u"请先修复再 --import。" % total_problems)
        else:
            print(u"全部通过。")
    return 1 if total_problems else 0


if __name__ == "__main__":
    sys.exit(main())
