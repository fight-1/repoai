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

"""repo explain - explain a rule id, an error, or a command.

Regions share the exact same CLI shape; only the analysis source differs:
  online regions    rule engine + optional AI gateway enrichment
  isolated regions  rule engine + knowledge base only
"""

from command import PagedCommand

from repoai import gateway as _gateway
from repoai.command_base import AiCommand
from repoai import diagnose as _diagnose
from repoai import events as _events
from repoai import kb as _kb
from repoai import rules as _rules


class Explain(PagedCommand, AiCommand):
    COMMON = False
    helpSummary = "Explain a diagnostic rule, error text, or command"
    helpUsage = """
%prog <规则ID|命令名|错误关键词|错误原文>
"""
    helpDescription = """
The '%prog' command explains:
  - a rule id such as R1002: full remediation steps and related entries
  - a repo command name: usage summary from the command itself
  - free-form error text: matched against the offline rule library
In online regions the answer may be enriched by the AI gateway;
in isolated regions it is produced entirely by the local rule library.  The
output layout and available actions are identical everywhere.
"""

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)
        if not args:
            self.OptionParser.error("missing what to explain")
        subject = " ".join(args).strip()

        ruleset = _rules.load_rules(state)

        rule = ruleset.get(subject.upper())
        if rule is not None:
            self._print_rule(rule, cfg)
            self._hit(cfg, rule.id, "explain")
            return 0

        if self._try_command_help(subject):
            return 0

        # Error text / keyword: run it through the engine.
        ctx = {"command": "", "exception": "", "text": subject}
        matches = _rules.diagnose(ruleset, ctx, top=3)
        if matches:
            print(u"匹配到 %d 条规则：\n" % len(matches))
            for rule, score in matches:
                print(u"── %s（置信度 %.0f%%）%s"
                      % (rule.id, score * 100, rule.title))
                print(u"   详见: repo explain %s" % rule.id)
            self._hit(cfg, matches[0][0].id, "keyword")
            return 0

        kb = _kb.load_kb(state)
        results = kb.search(subject, top=3)
        if results:
            print(u"规则库未命中，知识库匹配 %d 条：" % len(results))
            for entry, _s in results:
                print(u"  #%s %s" % (entry.id, entry.title))
            print(u"运行 repo faq --show <ID> 查看完整内容")
            return 0

        print(u"未找到与「%s」相关的规则或知识条目。" % subject)
        print(u"可运行 repo report 生成诊断包提交工单。")
        return 1

    # -- printers -------------------------------------------------------
    def _print_rule(self, rule, cfg):
        print(u"规则 #%s：%s（来源：本地规则库）" % (rule.id, rule.title))
        pattern = rule.patterns_raw[0] if rule.patterns_raw else u"（按异常/命令匹配）"
        print(u"匹配模式：%s" % pattern)
        if rule.cause:
            print(u"\n根因：\n%s" % rule.cause)
        if rule.steps:
            print(u"\n排查步骤：")
            for i, step in enumerate(rule.steps, 1):
                print(u"%d. %s" % (i, step))
        if rule.related:
            # Resolve FAQ refs to titles: printing a raw "FAQ:1003" would
            # send users searching for an entry they cannot find.
            kb = None
            refs = []
            for ref in rule.related:
                if ref.startswith("FAQ:"):
                    if kb is None:
                        kb = _kb.load_kb(self._state_dir(cfg))
                    entry = kb.get(ref[4:])
                    if entry is not None:
                        refs.append(u"FAQ#%s %s（repo faq --show %s）"
                                    % (entry.id, entry.title, entry.id))
                    else:
                        refs.append(u"%s（缺失）" % ref)
                else:
                    refs.append(ref)
            print(u"\n相关：%s" % u"，".join(refs))
        if cfg.online_ai():
            answer = _gateway.enrich_rule(
                cfg, rule, _diagnose.collect_context(cfg)
            )
            if answer:
                print(u"\nAI 补充分析（AI 服务提供）：\n%s" % answer)
        print(u"\n运行 repo faq <关键词> 搜索更多资料。")

    def _try_command_help(self, name):
        from subcmds import all_commands

        cmd_cls = all_commands.get(name)
        if cmd_cls is None:
            return False
        summary = getattr(cmd_cls, "helpSummary", "").strip()
        print(u"repo %s —— %s" % (name, summary))
        usage = getattr(cmd_cls, "helpUsage", "")
        if usage:
            print(usage.strip().replace("%prog", "repo " + name))
        print(u"\n详细手册：repo help %s" % name)
        print(u"遇到问题：错误信息会附带诊断建议，或 repo faq 搜索资料。")
        return True

    def _hit(self, cfg, rule_id, how):
        if not cfg.telemetry_enabled():
            return
        try:
            q = _events.EventQueue(cfg.state_dir())
            q.add("rule-hit", {"rule": rule_id, "via": how},
                  region=cfg.region())
        except Exception:
            pass
