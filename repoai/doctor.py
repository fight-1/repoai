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

"""Workspace health checks for `repo doctor`.

Every check is read-only, offline and cheap.  Findings are data, never
exceptions: this must work on a half-broken workspace -- that is exactly
when it is needed.  Levels: ok / info / warn / fail.
"""

import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

MIN_FREE_GB = 2.0
FAIL_FREE_GB = 0.2
QUEUE_WARN = 500
RECURRENCE_WINDOW_DAYS = 7
RECURRENCE_MIN = 3
GIT_MIN = (2, 20)


def _check(check_id, level, title, detail=u"", hint=u"", rule=""):
    return {
        "id": check_id,
        "level": level,
        "title": title,
        "detail": detail,
        "hint": hint,
        "rule": rule,
    }


def _count_projects(path, seen, depth=0, extra_roots=()):
    """Count <project> entries, expanding repo's <include> chain.

    Return (ok, count_or_error_message).  repo resolves include names
    against the *manifest repo* directory when .repo/manifest.xml is a
    generated pointer file, and against the file's own directory when it
    is the real file; we try both bases (file dir first, then any
    `extra_roots`).  Cycles are skipped via `seen`, nesting is
    depth-capped: a broken manifest degrades to a finding, never hangs
    or raises.
    """
    if depth > 10:
        return False, u"include 嵌套超过 10 层（manifest 可能成环）"
    real = os.path.realpath(path)
    if real in seen:
        return True, 0
    seen.add(real)
    try:
        tree = ET.parse(real)
    except ET.ParseError as e:
        return False, u"XML 解析失败：%s" % e
    except OSError as e:
        return False, u"无法读取：%s" % e
    root = tree.getroot()
    n = len(root.findall(".//project"))
    bases = [os.path.dirname(real)] + [r for r in extra_roots if r]
    for inc in root.findall("include"):
        name = inc.get("name") or ""
        if not name:
            continue
        optional = (inc.get("optional") or "").lower() in (
            "true", "yes", "1")
        target = None
        for base in bases:
            cand = os.path.join(base, name)
            if os.path.isfile(cand):
                target = cand
                break
        if target is None:
            if optional:
                continue
            return False, (u"include 目标缺失：%s（在 %s 下都没找到）"
                           % (name, u"、".join(bases)))
        ok, sub = _count_projects(target, seen, depth + 1, extra_roots)
        if not ok:
            return False, sub
        n += sub
    return True, n


def _parse_xml(path, extra_roots=()):
    """Return (ok, message).  Never raises."""
    ok, value = _count_projects(path, set(), 0, tuple(extra_roots))
    if not ok:
        return False, value
    return True, u"%d 个 project" % value


def _check_disk(repodir):
    top = os.path.dirname(os.path.abspath(repodir)) or os.sep
    while not os.path.isdir(top) and top != os.sep:
        top = os.path.dirname(top)
    try:
        usage = shutil.disk_usage(top)
    except OSError as e:
        return _check("disk", "info", u"磁盘空间", u"无法读取：%s" % e)
    free_gb = usage.free / (1024.0 ** 3)
    text = u"剩余 %.1f GB" % free_gb
    if free_gb < FAIL_FREE_GB:
        return _check(
            "disk", "fail", u"磁盘空间", text,
            u"磁盘几乎已满，sync/init 必然失败；请先清理。", "R1008")
    if free_gb < MIN_FREE_GB:
        return _check(
            "disk", "warn", u"磁盘空间", text,
            u"大仓 sync 建议预留 2 GB 以上。", "R1008")
    return _check("disk", "ok", u"磁盘空间", text)


def _check_writable(repodir):
    if not os.path.isdir(repodir):
        return _check(
            "writable", "fail", u"工作区",
            u".repo 目录不存在：%s" % repodir,
            u"该目录尚未 init，或不在 repo 工作区内。", "R1003")
    if os.access(repodir, os.W_OK):
        return _check("writable", "ok", u".repo 可写")
    return _check(
        "writable", "fail", u".repo 可写",
        u"当前用户无法写入 %s" % repodir,
        u"检查目录属主/权限（常见于 sudo 与 su 混用之后）。")


def _check_manifest(repodir):
    out = []
    mfile = os.path.join(repodir, "manifest.xml")
    if not os.path.exists(mfile):
        if os.path.isdir(os.path.join(repodir, "manifests")):
            out.append(_check(
                "manifest", "fail", u"manifest 文件",
                u"manifest.xml 缺失（manifests/ 已存在）",
                u"init 可能中途失败；重新运行 repo init。", "R1003"))
        else:
            out.append(_check(
                "manifest", "warn", u"manifest 文件",
                u"manifest.xml 不存在",
                u"该 .repo 可能未 init 完成。", "R1003"))
        return out
    ok, msg = _parse_xml(
        mfile, extra_roots=[os.path.join(repodir, "manifests")])
    if not ok:
        out.append(_check(
            "manifest", "fail", u"manifest 文件", msg,
            u"运行 repo explain R1004 查看修复步骤。", "R1004"))
        return out
    level = "ok"
    hint = u""
    if msg == u"0 个 project":
        level = "warn"
        hint = u"manifest 中没有任何 project，检查 init 参数与分组。"
    out.append(_check("manifest", level, u"manifest 文件", msg, hint,
                      "R1004" if level == "warn" else ""))
    lm = os.path.join(repodir, "local_manifests")
    if os.path.isdir(lm):
        for name in sorted(os.listdir(lm)):
            path = os.path.join(lm, name)
            if not os.path.isfile(path):
                continue
            if not name.endswith(".xml"):
                out.append(_check(
                    "local_manifests", "warn",
                    u"本地片段 %s" % name,
                    u"非 .xml 文件会被 repo 忽略。", u"", "R1018"))
                continue
            ok, msg = _parse_xml(path)
            if not ok:
                out.append(_check(
                    "local_manifests", "fail",
                    u"本地片段 %s" % name, msg,
                    u"片段必须是合法 XML（见 R1018/R1004）。", "R1018"))
            else:
                out.append(_check(
                    "local_manifests", "ok",
                    u"本地片段 %s" % name, msg))
    return out


def _check_git():
    try:
        proc = subprocess.Popen(
            ["git", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _err = proc.communicate()
    except OSError:
        return _check(
            "git", "fail", u"git 可用", u"找不到 git 命令",
            u"安装 git（建议 2.20 以上）。", "R1016")
    ver = (out or b"").decode("utf-8", "replace").strip()
    parts = ver.split()
    num = parts[-1] if parts else ""
    try:
        major, minor = (int(x) for x in num.split(".")[:2])
        below = (major, minor) < GIT_MIN
    except ValueError:
        below = False
    if below:
        return _check(
            "git", "warn", u"git 版本", u"当前 %s" % ver,
            u"部分功能要求 git %d.%d 以上（见 R1016）。" % GIT_MIN,
            "R1016")
    return _check("git", "ok", u"git 版本", ver)


def _check_recurrence(cfg):
    path = os.path.join(cfg.state_dir(), "failure_history.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            lines = fp.read().splitlines()[-50:]
    except OSError:
        return _check("recurrence", "ok", u"近期失败模式",
                      u"暂无失败记录")
    cutoff = time.time() - RECURRENCE_WINDOW_DAYS * 86400.0
    counts = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", 0) < cutoff:
            continue
        rules = rec.get("rules") or []
        key = (rec.get("command", ""), rules[0] if rules
               else rec.get("exception", ""))
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if not counts:
        return _check("recurrence", "ok", u"近期失败模式",
                      u"近 %d 天无失败记录" % RECURRENCE_WINDOW_DAYS)
    (cmd, rid), n = max(counts.items(), key=lambda kv: kv[1])
    if n >= RECURRENCE_MIN and (cmd or rid):
        hint = u"同类失败 7 天内出现 %d 次；" % n
        hint += (u"先运行 repo explain %s。" % rid if rid
                 else u"运行 repo explain \"<报错原文>\" 分析。")
        return _check(
            "recurrence", "warn", u"近期失败模式",
            u"%s 失败 %d 次（规则 %s）" % (cmd or u"(未知命令)", n,
                                          rid or u"未命中"),
            hint, rid)
    return _check("recurrence", "ok", u"近期失败模式",
                  u"近 %d 天失败 %d 次，未见反复"
                  % (RECURRENCE_WINDOW_DAYS, total))


def _check_queue(cfg):
    from repoai import events

    try:
        q = events.EventQueue(cfg.state_dir())
        n = q.count_pending()
    except Exception as e:
        return _check(
            "queue", "warn", u"事件队列", u"无法读取：%s" % e,
            u"检查 ai.state-dir 磁盘/权限；必要时删除 events.sqlite* "
            u"（将丢弃未上报事件）。")
    if n > QUEUE_WARN:
        return _check(
            "queue", "warn", u"事件队列",
            u"待上报事件 %d 条" % n,
            u"运行 repo telemetry --flush（在线区）或 --export "
            u"（离线/文件交换区）。")
    return _check("queue", "ok", u"事件队列", u"待上报 %d 条" % n)


def _check_region(cfg):
    return _check(
        "region", "info", u"区域与通道",
        u"区域 %s，上报通道 %s" % (cfg.region(), cfg.telemetry_channel()))


def _check_rulelib(cfg):
    """Surface silently-dead rules: broken regexes / skipped entries.

    A rule with an uncompilable pattern matches nothing forever; before
    this check, broken_patterns was recorded but never shown anywhere.
    """
    from repoai import rules as _rules

    try:
        rs = _rules.load_rules(cfg.state_dir())
    except Exception as e:
        return _check("rulelib", "warn", u"规则库",
                      u"无法加载：%s" % e,
                      u"检查 ai.state-dir 下 rules.d/*.json。")
    problems = list(getattr(rs, "load_problems", []))
    seen = {(p["id"], p["detail"]) for p in problems}
    for r in rs:
        for pat in r.broken_patterns:
            if (r.id, pat) not in seen:
                seen.add((r.id, pat))
                problems.append({"id": r.id, "kind": "broken-pattern",
                                 "detail": pat})
    if not problems:
        return _check("rulelib", "ok", u"规则库",
                      u"%d 条规则，无失效项" % len(rs))
    bad = sorted({"%s(%s)" % (p["id"], p["kind"]) for p in problems})
    return _check(
        "rulelib", "warn", u"规则库",
        u"%d 条规则，%d 项失效/跳过：%s"
        % (len(rs), len(problems), u"、".join(bad[:5])),
        u"运行 repo rules --lint 查看明细并修复（问题规则永不命中）。")


def check_all(cfg):
    """Run every check; returns a list of finding dicts (never raises)."""
    checks = []
    repodir = cfg.repodir() or os.getcwd()
    for fn in (_check_disk, _check_writable):
        checks.append(fn(repodir))
    checks.extend(_check_manifest(repodir))
    checks.append(_check_git())
    for fn in (_check_recurrence, _check_queue):
        checks.append(fn(cfg))
    checks.append(_check_rulelib(cfg))
    checks.append(_check_region(cfg))
    return checks


_LABEL = {"ok": u"正常", "info": u"信息", "warn": u"警告", "fail": u"失败"}


def render(checks):
    """Human-readable report."""
    lines = [u"repo 工作区体检（只读，不修改任何文件）"]
    for c in checks:
        line = u"[%s] %s" % (_LABEL[c["level"]], c["title"])
        if c["detail"]:
            line += u"：%s" % c["detail"]
        lines.append(line)
        if c["hint"]:
            lines.append(u"    ↳ %s" % c["hint"])
    fails = sum(1 for c in checks if c["level"] == "fail")
    warns = sum(1 for c in checks if c["level"] == "warn")
    if fails or warns:
        lines.append(u"")
        lines.append(u"共 %d 项失败、%d 项警告；按上述提示处理后重试原命令。"
                     % (fails, warns))
    else:
        lines.append(u"")
        lines.append(u"未发现明显问题。")
    return u"\n".join(lines)
