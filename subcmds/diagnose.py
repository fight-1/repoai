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

"""repo diagnose - analyze the most recent failure on demand.

The automatic hint after a failed command is deliberately short; this is
the manual, fuller version: every matched rule with its complete steps,
plus internal-AI analysis in online regions.  Read-only (the failure was
already recorded by the hook; nothing new runs).
"""

import sys
import time

from repoai.command_base import AiCommand
from repoai import diagnose as _diagnose
from repoai import gateway as _gateway
from repoai import kb as _kb
from repoai import rules as _rules


class Diagnose(AiCommand):
    COMMON = False
    helpSummary = "Analyze the last failed command in detail"
    helpUsage = """
%prog
"""
    helpDescription = """
The '%prog' command re-analyzes the most recent repo failure (stored in
the local state dir, redacted).  Unlike the automatic hint it prints
every matched rule with full steps, related FAQ entries, and -- online
regions only -- an internal-AI analysis.  It never re-runs anything.
"""

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        record = _diagnose.load_last_failure(cfg)
        if record is None:
            print(u"本地没有失败记录（只读命令失败不会记录，或状态目录"
                  u"被清理过）。")
            print(u"先复现一次失败，再运行 repo diagnose。")
            return 1

        state = self._state_dir(cfg)
        ruleset = _rules.load_rules(state)
        kbset = _kb.load_kb(state)
        ctx = {
            "command": record.command,
            "exception": record.exception,
            "text": record.text,
        }
        matches = _rules.diagnose(ruleset, ctx, top=3)

        print(u"最近一次失败分析（只读，不重新执行）")
        print(u"-" * 40)
        when = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(record.ts or 0))
        print(u"时间：%s    命令：repo %s" % (when, record.command or "?"))
        if record.exception:
            print(u"异常：%s    退出码：%s"
                  % (record.exception, record.exit_code))
        summary = (record.text or u"").strip().splitlines()
        if summary:
            print(u"错误摘要：")
            for line in summary[:5]:
                print(u"  %s" % line[:110])
        print(u"-" * 40)

        if not matches:
            print(u"规则库未命中（可换关键词 repo faq 搜索，或 repo "
                  u"report 生成诊断包提工单）。")
        for i, (rule, score) in enumerate(matches, 1):
            print(u"[%d] %s %s（匹配度 %.0f%%）"
                  % (i, rule.id, rule.title, score * 100))
            if rule.cause:
                print(u"    根因：%s" % rule.cause)
            for j, step in enumerate(rule.steps, 1):
                print(u"    %d. %s" % (j, step))
            if rule.related:
                print(u"    相关：%s" % u" ".join(rule.related))
            print(u"")

        entries = kbset.search(record.command + u" " + record.exception,
                               top=3)
        if not entries:
            entries = kbset.search(record.text[:120], top=3)
        if entries:
            print(u"相关 FAQ（repo faq --show <ID> 查看全文）：")
            for entry, _s in entries:
                print(u"  #%s %s" % (entry.id, entry.title))
            print(u"")

        ai_text = None
        if cfg.online_ai():
            ai_text = _gateway.diagnose_failure(
                cfg, record, matches or [], out=sys.stderr
            )
            if ai_text:
                print(u"AI 分析（AI 服务提供）：")
                for line in ai_text.strip().splitlines()[:16]:
                    print(u"  %s" % line.strip())
                print(u"")
        print(u"下一步：repo explain <规则ID> 看单条详情；repo report "
              u"生成诊断包；处理后 repo feedback 评价本次诊断。")
        return 0
