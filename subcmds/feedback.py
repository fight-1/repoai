# Copyright (C) 2026 The Android Open Source Project
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

"""repo feedback - rate the last AI/rule suggestion (all regions).

Feedback lands in the local queue first; the region's channel decides when it
reaches the remote store (online / near-realtime / batch / shuttle).
"""

import sys

from repoai.command_base import AiCommand
from repoai import diagnose as _diagnose
from repoai import events as _events


CHOICES = {
    "1": "resolved",
    "2": "unresolved",
    "3": "partial",
    "4": "inaccurate",
}


class Feedback(AiCommand):
    COMMON = False
    helpSummary = "Give feedback on the last diagnosis"
    helpUsage = """
%prog [--resolved|--unresolved|--partial|--inaccurate] [--rule <ID>]
"""
    helpDescription = """
The '%prog' command records feedback about the most recent diagnosis hint
or `repo explain` answer.  Run without options for the interactive prompt.
Feedback is queued locally and reported according to the region's telemetry
channel; it never contains code or raw logs.
"""

    def _Options(self, p):
        for flag, verdict in (
            ("--resolved", "resolved"),
            ("--unresolved", "unresolved"),
            ("--partial", "partial"),
            ("--inaccurate", "inaccurate"),
        ):
            p.add_option(
                flag,
                dest="verdict",
                action="store_const",
                const=verdict,
                help="record this verdict directly",
            )
        p.add_option(
            "--rule",
            default=None,
            metavar="ID",
            help="rule id the feedback refers to (default: last hint)",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)

        verdict = opt.verdict
        rule_id = opt.rule

        last = _diagnose.load_last_failure(cfg)
        if rule_id is None and last is not None and last_rules(last):
            rule_id = last_rules(last)[0]

        if not self._stdin_tty() and not verdict:
            self._err(
                "error: non-interactive use requires one of "
                "--resolved/--unresolved/--partial/--inaccurate"
            )
            return 1

        if not verdict:
            verdict = self._prompt(last, rule_id)
            if verdict is None:
                print(u"已取消。")
                return 0

        payload = {
            "verdict": verdict,
            "rule": rule_id or "",
            "source": "feedback-command",
        }
        if last is not None and last.command:
            payload["command"] = last.command
        # Attach the match score of the rated rule (one float, metadata
        # only) so the admin report can correlate verdicts with confidence.
        if last is not None and rule_id and rule_id in last.rules:
            idx = last.rules.index(rule_id)
            if idx < len(last.scores):
                payload["score"] = last.scores[idx]
        q = _events.EventQueue(state)
        eid = q.add("feedback", payload, region=cfg.region())
        if eid is None:
            self._err("warning: 反馈未能写入本地队列（磁盘/权限问题）")
            return 1

        if cfg.telemetry_enabled():
            channel = cfg.telemetry_channel()
            if channel == "online":
                note = u"将同步到监控系统（在线直报）。"
            elif channel == "file-exchange":
                note = u"已入本地队列，将随批量导出上报（文件交换区）。"
            else:
                note = u"已入本地队列，将随离线报表人工上报。"
            print(u"反馈已记录，%s" % note)
        else:
            print(u"反馈已记录到本地队列。")
        return 0

    def _prompt(self, last, rule_id):
        if last is not None and last.text:
            snippet = last.text.strip().splitlines()[0]
            print(u"最近一次诊断（%s）：" % (last.command or "explain"))
            print(u'"%s"' % snippet[:100])
        if rule_id:
            print(u"关联规则：%s" % rule_id)
        print("")
        print(u"请评价：")
        print(u"[1] 已解决  [2] 未解决  [3] 部分有用  [4] 不准确")
        try:
            choice = input(u"选择（回车取消）：").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not choice:
            return None
        return CHOICES.get(choice)

    @staticmethod
    def _stdin_tty():
        try:
            return sys.stdin.isatty()
        except Exception:
            return False


def last_rules(record):
    raw = record.to_dict().get("rules") or []
    return raw
