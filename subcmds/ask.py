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

"""repo ask - natural-language Q&A about the repo tool.

Online regions: AI gateway, grounded in redacted workspace
context (project count, versions, local rule/KB hits, last failure).
Isolated regions: the question is answered locally from the rule
library + offline KB instead of failing -- same command shape, weaker
engine, loudly labelled.

Multi-turn: --continue replays the previous exchange (kept in the local
state dir, last N turns only).  Question/answer text never enters the
telemetry queue -- that stays metadata-only by policy.
"""

import json
import os
import sys

from repoai.command_base import AiCommand
from repoai import diagnose as _diagnose
from repoai import gateway as _gateway
from repoai import kb as _kb
from repoai import rules as _rules


_MAX_QUESTION = 500
_SESSION_NAME = "ask_session.json"
# Last 8 messages (4 exchanges) give continuity without prompt bloat.
_MAX_TURNS = 8


class Ask(AiCommand):
    COMMON = False
    helpSummary = "Ask the repo assistant a question (AI or offline fallback)"
    helpUsage = """
%prog [-c] [--reset] <问题>
"""
    helpDescription = """
The '%prog' command answers questions about the repo tool and this
workspace.  Online it uses the AI gateway with redacted
workspace context; in isolated regions it falls back to the local rule
library and offline knowledge base automatically.  The answer source is
always stated.  Read-only: this command never changes the workspace.

-c/--continue follows up on the previous question (multi-turn).
--reset forgets the stored conversation.  Q&A text is stored only in
the local state dir and is never reported.
"""

    def _Options(self, p):
        p.add_option(
            "-c",
            "--continue",
            dest="cont",
            action="store_true",
            help="follow up on the previous question",
        )
        p.add_option(
            "--reset",
            action="store_true",
            help="clear the stored conversation",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)
        session_path = os.path.join(state, _SESSION_NAME)

        if opt.reset:
            try:
                os.remove(session_path)
                print(u"已清除本地问答会话。")
            except OSError:
                print(u"没有已存储的问答会话。")
            return 0

        question = u" ".join(args).strip()
        if not question:
            self._err(
                u"error: 请带上问题（或 --reset 清除会话），如："
                u'repo ask "只想同步一个模块怎么做"'
            )
            return 1
        if len(question) > _MAX_QUESTION:
            question = question[:_MAX_QUESTION]

        ruleset = _rules.load_rules(state)
        kbset = _kb.load_kb(state)
        online = cfg.online_ai()
        history = _load_session(session_path) if opt.cont else []
        if opt.cont and not history:
            print(u"（没有找到上一轮会话，按新问题处理）")

        if online:
            answer = _gateway.ask_question(
                cfg,
                question,
                self._context(cfg, question, ruleset, kbset),
                history=history,
                out=sys.stderr,
            )
            if answer is not None:
                self._record(cfg, "ai")
                _save_session(session_path, history, question, answer)
                print(answer)
                turns = len(history) + 2
                tail = u"（来源：AI 服务 + 工作区上下文"
                if turns > 2:
                    tail += u"；第 %d 轮，-c 可继续追问" % (turns // 2)
                else:
                    tail += u"；-c 可继续追问"
                print(u"\n%s；本命令只读，不会执行任何变更）" % tail)
                return 0
            # Gateway failed (loudly); fall through to the local answer.
        rc = self._local_answer(question, ruleset, kbset, online)
        self._record(cfg, "local" if rc == 0 else "miss")
        return rc

    # -- online path ----------------------------------------------------
    def _context(self, cfg, question, ruleset, kbset):
        """Metadata-only grounding; redacted, capped, no private data."""
        ctx = []
        base = _diagnose.collect_context(cfg, self.manifest)
        bits = []
        if base.get("project_count") is not None:
            bits.append(u"project 数 %s" % base["project_count"])
        if base.get("repo_version"):
            bits.append(u"repo 版本 %s" % base["repo_version"])
        if base.get("manifest_name"):
            bits.append(u"manifest %s" % base["manifest_name"])
        if bits:
            ctx.append((u"工作区概况", u"，".join(bits)))
        rule_hits = ruleset.search(question)[:3]
        if rule_hits:
            ctx.append(
                (u"本地规则库相关条目",
                 u"；".join(r.id + u" " + r.title for r in rule_hits))
            )
        kb_hits = kbset.search(question, top=3)
        if kb_hits:
            ctx.append(
                (u"本地知识库相关条目",
                 u"；".join(e.title for e, _s in kb_hits))
            )
        last = _diagnose.load_last_failure(cfg)
        if last is not None and last.command:
            summary = u"最近一次失败：repo %s（%s）" % (
                last.command, last.exception or u"未知异常")
            ctx.append((summary, _diagnose.redact(last.text[:300])))
        return ctx

    # -- offline / fallback path ---------------------------------------
    def _local_answer(self, question, ruleset, kbset, online):
        rules = ruleset.search(question)[:3]
        entries = kbset.search(question, top=3)
        if not rules and not entries:
            print(u"本地知识库没有找到与这个问题直接相关的内容。")
            print(u"可以试试：repo faq <关键词> 换词搜索，"
                  u"repo rules 浏览规则库。")
            if online:
                print(u"（在线 AI 当前不可用；恢复后 repo ask 可获得"
                      u" AI 完整解答）")
            return 1
        print(u"本地知识库回答（离线规则库 + FAQ）：")
        for rule in rules:
            print(u"\n▶ %s %s" % (rule.id, rule.title))
            if rule.cause:
                print(u"  %s" % rule.cause)
            for step in rule.steps[:3]:
                print(u"  - %s" % step)
        for entry, _score in entries:
            print(u"\n▶ FAQ #%s %s" % (entry.id, entry.title))
            for line in entry.body.splitlines()[:8]:
                print(u"  %s" % line)
        print(u"\n（离线来源，可能不是最新；在线区可获得 AI 完整解答）")
        return 0

    def _record(self, cfg, source):
        """Queue an 'ask' event.  Metadata only: the question text is
        never queued or reported (privacy policy)."""
        try:
            if not cfg.telemetry_enabled():
                return
            from repoai import events

            q = events.EventQueue(self._state_dir(cfg))
            q.add("ask", {"source": source}, region=cfg.region())
        except Exception:
            return


def _load_session(path):
    """Prior turns as [{role, content}]; [] on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        turns = data.get("turns", [])
        if not isinstance(turns, list):
            return []
        return [
            t
            for t in turns[-_MAX_TURNS:]
            if isinstance(t, dict)
            and t.get("role") in ("user", "assistant")
            and isinstance(t.get("content"), str)
        ]
    except (OSError, ValueError):
        return []


def _save_session(path, history, question, answer):
    """Persist the conversation locally (state dir).  Never reported."""
    turns = list(history or [])
    turns.append({"role": "user", "content": question})
    turns.append({"role": "assistant", "content": answer})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    {"turns": turns[-_MAX_TURNS:]}, ensure_ascii=False
                )
            )
        os.replace(tmp, path)
    except OSError:
        pass
