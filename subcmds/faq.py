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

"""repo faq - search the offline knowledge base (works in every region)."""

from repoai.command_base import AiCommand
from repoai import events as _events
from repoai import kb as _kb


class Faq(AiCommand):
    COMMON = False
    helpSummary = "Search the offline knowledge base"
    helpUsage = """
%prog <关键词...>
%prog --show <条目ID>
"""
    helpDescription = """
The '%prog' command searches a locally packaged knowledge base of FAQs,
command manual excerpts, curated ticket resolutions and cross-region
analysis conclusions.  It is the offline counterpart of `repo ask` and is
fully available in isolated regions.
"""

    def _Options(self, p):
        p.add_option(
            "--show",
            dest="show_id",
            default=None,
            metavar="ID",
            help="show one knowledge base entry by id",
        )
        p.add_option(
            "-n",
            dest="top",
            type="int",
            default=5,
            help="number of results to show (default 5)",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)
        kb = _kb.load_kb(state)

        if opt.show_id:
            entry = kb.get(opt.show_id)
            if entry is None:
                self._err("error: 知识库中没有条目 #%s", opt.show_id)
                return 1
            self._print_entry(entry)
            return 0

        query = " ".join(args).strip()
        if not query:
            self.OptionParser.error("missing search keyword")
        results = kb.search(query, top=opt.top)
        if not results:
            print(u"未找到与「%s」相关的条目。" % query)
            print(u"可尝试更短的关键词，或 repo rules 查看已知问题规则。")
            self._record_search(cfg, query, None)
            return 0

        print(u"搜索结果（%d 条）：" % len(results))
        for i, (entry, _score) in enumerate(results, 1):
            snippet = entry.body.strip().splitlines()[0] if entry.body else ""
            if len(snippet) > 72:
                snippet = snippet[:72] + u"…"
            print(u"[%d] #%s %s" % (i, entry.id, entry.title))
            print(u"    %s" % snippet)
        best = results[0][0]
        self._record_search(cfg, query, best.id)
        print("")
        print(u"运行 repo faq --show <ID> 查看完整内容；repo feedback 评价结果。")
        return 0

    def _print_entry(self, entry):
        print(u"#%s %s" % (entry.id, entry.title))
        if entry.tags:
            print(u"标签: %s" % ", ".join(entry.tags))
        print("")
        for line in entry.body.splitlines():
            print(line)

    def _record_search(self, cfg, query, hit_id):
        if not cfg.telemetry_enabled():
            return
        try:
            q = _events.EventQueue(state_dir=cfg.state_dir())
            # Metadata only -- the search words themselves stay local
            # (same privacy line as `repo ask`).  Hit id + query length
            # alone still show admins which FAQ entries people find.
            q.add("kb-search", {"hit": hit_id, "chars": len(query)},
                  region=cfg.region())
        except Exception:
            pass
