#!/usr/bin/env python3
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

"""pilot_check.py -- pre-flight self-check for the AI assistant layer.

Read-only in effect: it never touches workspace files or repo state;
the only writes are creating the (already-due) AI state dir and a
probe file that is removed immediately.  Run it once after installing
the AI layer:

    python3 .repo/repo/contrib/pilot_check.py [--offline] [--json]

Exit code 0 = no FAIL findings (WARNs are printed for review), 1 = at
least one FAIL.  It checks the same things the onboarding docs walk
through by hand: python version, config precedence, rules/KB loading,
gateway and collector reachability, queue hygiene, privacy spot-check.
"""

import argparse
import json
import os
import subprocess
import sys
import time

TOPDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOPDIR not in sys.path:
    sys.path.insert(0, TOPDIR)

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
_LABEL = {OK: u"[OK ]", WARN: u"[WARN]", FAIL: u"[FAIL]", INFO: u"[INFO]"}


class Report:
    def __init__(self, as_json=False):
        self.rows = []
        self.as_json = as_json

    def add(self, level, name, detail=u"", counted=True):
        self.rows.append({"level": level, "name": name,
                          "detail": detail, "counted": counted})
        if not self.as_json:
            line = u"%s %s" % (_LABEL[level], name)
            if detail:
                line += u"：%s" % detail
            print(line)

    def count(self, level):
        return len([r for r in self.rows
                    if r["level"] == level and r["counted"]])

    def exit_code(self):
        return 1 if self.count(FAIL) else 0


def _find_repodir(start=None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, ".repo")):
            return os.path.join(d, ".repo")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def check_python(r):
    v = sys.version_info
    if v < (3, 6):
        r.add(FAIL, u"Python 版本", u"%d.%d 低于 3.6，AI 层无法运行" %
              (v[0], v[1]))
    elif v < (3, 7):
        r.add(INFO, u"Python 版本", u"%d.%d.%d（3.6 兼容路径，正常）" %
              (v[0], v[1], v[2]))
    else:
        r.add(OK, u"Python 版本", u"%d.%d.%d" % (v[0], v[1], v[2]))


def check_git(r):
    try:
        out = subprocess.check_output(
            ["git", "--version"], stderr=subprocess.STDOUT)
        r.add(OK, u"git 可用", out.decode("utf-8", "replace").strip())
    except Exception as e:
        r.add(FAIL, u"git 可用", u"git 命令不可用：%s" % e)


def check_repoai(r):
    try:
        from repoai import config as _c  # noqa: F401
        from repoai import gateway, rules, kb  # noqa: F401
        r.add(OK, u"repoai 包导入", u"gateway/rules/kb 全部可用")
    except Exception as e:
        r.add(FAIL, u"repoai 包导入", u"%s：%s" % (type(e).__name__, e))
        return None
    try:
        import subcmds
        ai = [n for n, c in subcmds.all_commands.items()
              if getattr(c, "IS_AI_COMMAND", False)]
        if len(ai) >= 11:
            r.add(OK, u"命令注册", u"共 %d 个，AI 命令 %d 个" %
                  (len(subcmds.all_commands), len(ai)))
        else:
            r.add(FAIL, u"命令注册", u"AI 命令仅 %d 个（应为 11）：%s" %
                  (len(ai), u",".join(sorted(ai))))
    except Exception as e:
        r.add(FAIL, u"命令注册", u"导入 subcmds 失败：%s" % e)
    return True


def check_content(r):
    try:
        from repoai import kb as _kb
        from repoai import rules as _rules
        rs = _rules.load_rules()
        ks = _kb.load_kb()
        n_rules = len(rs)
        n_kb = len(ks)
        if n_rules >= 20 and n_kb >= 5:
            r.add(OK, u"内置规则/知识库", u"规则 %d 条、FAQ %d 条" %
                  (n_rules, n_kb))
        else:
            r.add(FAIL, u"内置规则/知识库",
                  u"规则 %d 条、FAQ %d 条（过少，内置数据可能损坏）" %
                  (n_rules, n_kb))
    except Exception as e:
        r.add(FAIL, u"内置规则/知识库", u"加载失败：%s" % e)


def check_config(r):
    try:
        from repoai.config import AiConfig
        cfg = AiConfig(repodir=_find_repodir())
    except Exception as e:
        r.add(FAIL, u"配置加载", u"%s：%s" % (type(e).__name__, e))
        return None
    if not cfg.enabled():
        r.add(INFO, u"AI 开关", u"ai.enabled=false（AI 层关闭，属合法配置；"
              u"如需启用：git config --file ~/.repoconfig/config "
              u"--unset ai.enabled）")
        return cfg
    r.add(OK, u"AI 开关", u"已启用（区域：%s）" % cfg.region())
    return cfg


def check_state_dir(r, cfg):
    state = cfg.state_dir()
    try:
        if not os.path.isdir(state):
            os.makedirs(state)
            r.add(INFO, u"状态目录", u"已创建 %s" % state)
        probe = os.path.join(state, ".pilot_check_probe")
        with open(probe, "w") as fp:
            fp.write("x")
        os.remove(probe)
        r.add(OK, u"状态目录可写", state)
    except OSError as e:
        r.add(FAIL, u"状态目录可写", u"%s：%s" % (state, e))


def check_gateway(r, cfg, offline):
    url = cfg.get("ai.gateway.url")
    if not url:
        r.add(INFO, u"网关", u"未配置 ai.gateway.url：将按隔离区运行"
              u"（本地规则库/FAQ 仍全部可用）")
        return
    if not cfg.get("ai.gateway.token"):
        r.add(WARN, u"网关 token", u"已配置 url 但缺少 ai.gateway.token，"
              u"请求预期会被拒（401）")
    if offline:
        r.add(INFO, u"网关连通", u"--offline 已跳过")
        return
    from repoai import gateway as _g
    try:
        models = _g.list_models(cfg)
    except Exception as e:
        r.add(FAIL, u"网关连通", u"GET /v1/models 失败：%s" % e)
        return
    cur = cfg.get("ai.gateway.model") or ""
    r.add(OK, u"网关连通", u"GET /v1/models 返回 %d 个模型" % len(models))
    if cur in models:
        r.add(OK, u"当前模型", u"%s 在网关清单中" % cur)
    else:
        r.add(FAIL, u"当前模型",
              u"配置的 %s 不在清单中（503/404 高危；用 repo models "
              u"查看并 --use 切换）" % cur)


def check_telemetry(r, cfg, offline):
    if not cfg.telemetry_enabled():
        r.add(INFO, u"遥测", u"ai.telemetry.enabled=false（合法配置）")
        return
    endpoint = cfg.get("ai.telemetry.endpoint")
    if not endpoint:
        r.add(INFO, u"遥测上报", u"未配置 endpoint：事件留在本地队列，"
              u"摆渡/导出通道仍可用")
        return
    if cfg.get("ai.telemetry.token"):
        r.add(OK, u"遥测鉴权", u"token 已配置")
    else:
        r.add(WARN, u"遥测鉴权", u"收集器启用了 token 时此处必须配置，"
              u"否则 flush 将 401")
    if offline:
        r.add(INFO, u"遥测连通", u"--offline 已跳过")
        return
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/healthz", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            r.add(OK, u"遥测连通", u"%s healthz 正常" % endpoint)
        else:
            r.add(WARN, u"遥测连通", u"响应异常：%s" % body)
    except Exception as e:
        r.add(WARN, u"遥测连通", u"%s 不可达：%s（事件保留队列，"
              u"不丢数据）" % (endpoint, e))


def check_privacy(r, cfg):
    """Pending events must carry structured metadata only -- never raw
    question/log text.  Cheap schema spot-check."""
    try:
        from repoai import events as _events
        q = _events.EventQueue(cfg.state_dir())
        pending = q.pending()
        banned_keys = set(["question", "text", "args", "log", "stderr"])
        leaks = []
        for _i, _ts, etype, _region, payload in pending:
            bad = banned_keys.intersection(payload.keys())
            if bad:
                leaks.append(u"%s:%s" % (etype, u",".join(sorted(bad))))
        if leaks:
            r.add(FAIL, u"隐私抽查", u"待上报事件含原文字段（不应出现）：%s" %
                  u"；".join(leaks[:3]))
        else:
            r.add(OK, u"隐私抽查",
                  u"待上报事件 %d 条，均为结构化元数据" % len(pending))
    except Exception as e:
        r.add(WARN, u"隐私抽查", u"无法读取队列：%s" % e)


def check_doctor(r, cfg):
    try:
        from repoai import doctor as _doctor
        bad = [c for c in _doctor.check_all(cfg)
               if c["level"] in ("fail", "warn")]
        fails = len([c for c in bad if c["level"] == "fail"])
        if fails:
            r.add(FAIL, u"repo doctor", u"%d 项失败（明细见下）" % fails)
        elif bad:
            r.add(WARN, u"repo doctor", u"%d 项警告（明细见下）" % len(bad))
        else:
            r.add(OK, u"repo doctor", u"全部通过")
        for c in bad:
            detail = c["detail"] or u""
            if c["hint"]:
                detail = (detail + u"；" if detail else u"") + c["hint"]
            r.add(u"FAIL" if c["level"] == "fail" else u"WARN",
                  u"  · " + c["title"], detail, counted=False)
    except Exception as e:
        r.add(WARN, u"repo doctor", u"体检不可用：%s" % e)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true",
                    help="skip all network probes")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable output")
    opts = ap.parse_args(argv)

    r = Report(as_json=opts.as_json)
    check_python(r)
    check_git(r)
    if check_repoai(r) is None:
        return _finish(r, opts)
    check_content(r)
    cfg = check_config(r)
    if cfg is not None and cfg.enabled():
        check_state_dir(r, cfg)
        check_gateway(r, cfg, opts.offline)
        check_telemetry(r, cfg, opts.offline)
        check_privacy(r, cfg)
        check_doctor(r, cfg)
    return _finish(r, opts)


def _finish(r, opts):
    summary = {
        "ts": time.time(),
        "counts": {lv: r.count(lv) for lv in (OK, WARN, FAIL, INFO)},
        "rows": r.rows,
    }
    if opts.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        c = summary["counts"]
        print(u"\n小结：%d 项通过 / %d 警告 / %d 失败 / %d 信息"
              % (c["OK"], c["WARN"], c["FAIL"], c["INFO"]))
        if c["FAIL"]:
            print(u"存在 FAIL：请先按上方建议解决，再重新运行本脚本。")
        elif c["WARN"]:
            print(u"仅 WARN：逐条确认原因即可，WARN 不阻塞使用。")
        else:
            print(u"全部通过，可以正常使用了。")
    return r.exit_code()


if __name__ == "__main__":
    sys.exit(main())
