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

"""repo rules - inspect the offline diagnostic rule library.

Available in every region (core in isolated regions).  Optionally imports an
offline rule package, or exports one for redistribution to isolated zones.
"""

import os
import sys
import time

from repoai.command_base import AiCommand
from repoai import rules as _rules
from repoai import rules_lint as _lint


class Rules(AiCommand):
    COMMON = False
    helpSummary = "List, import, export or lint offline diagnostic rules"
    helpUsage = """
%prog [--import <file.json>] [--export <file.json>] [--local-only]
%prog --lint [<file.json> ...]
"""
    helpDescription = """
The '%prog' command lists the rules used by the offline diagnostic engine
(repo explain / automatic error hints).  Rules ship with the tool and can be
extended with admin-maintained or offline-package rule files.

--import installs an incremental rule package (JSON) into the local state
directory, merged over the builtin set by rule id.
--export writes the currently active rules as a package file, so an admin can
review/tune them and --import the result on isolated machines.
--local-only with --export: export only admin/offline-package rules.

--lint validates rule files without installing them (run it BEFORE
--import): broken regexes, dangling rule/FAQ references, unknown
subcommand names, bad confidence values.  With file arguments it checks
those packages; with none it checks the currently active rule set.
Exit code: 0 when clean, 1 when problems were found.
"""

    def _Options(self, p):
        p.add_option(
            "--import",
            dest="import_file",
            default=None,
            metavar="FILE",
            help="import an offline rule package (JSON)",
        )
        p.add_option(
            "--export",
            dest="export_file",
            default=None,
            metavar="FILE",
            help="export the active rules as a JSON package",
        )
        p.add_option(
            "--lint",
            action="store_true",
            help="validate rule files given as arguments (or the active "
                 "rules when none) without installing; exit 1 on problems",
        )
        p.add_option(
            "--local-only",
            action="store_true",
            help="with --export: only admin/offline-package rules",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)

        if opt.import_file and opt.export_file:
            self._err("error: --import and --export cannot be combined")
            return 1
        if opt.lint and (opt.import_file or opt.export_file):
            self._err("error: --lint cannot be combined with "
                      "--import/--export")
            return 1

        if opt.lint:
            return self._do_lint(args)

        if opt.import_file:
            self._do_import(opt, cfg, state)

        ruleset = _rules.load_rules(state)

        if opt.export_file:
            return self._do_export(opt, ruleset)

        total = len(ruleset)
        print(u"离线规则库：共 %d 条（Top %d 高频）" % (total, min(total, 20)))
        for rule in list(ruleset)[:20]:
            flag = "  " if rule.origin == "builtin" else "*"
            print(
                "%s %-7s %s" % (flag, rule.id, rule.title)
            )
        print("")
        print(u"标 * 为管理员/离线包补充规则。")
        print(u"运行 repo explain <规则ID> 查看详情，如：repo explain R1002")
        problems = getattr(ruleset, "load_problems", [])
        if problems:
            print(u"注意：%d 条规则失效或被跳过，运行 repo rules --lint "
                  u"查看明细。" % len(problems))

    def _do_lint(self, args):
        if args:
            known = _lint.known_command_names()
            kb_ids = _lint.load_kb_ids()
            bad = False
            for path in args:
                _n, problems = _lint.lint_file(path, known, kb_ids)
                if problems:
                    bad = True
                    print(u"%s: %d 个问题" % (path, len(problems)))
                    for p in problems:
                        print(u"  - %s" % p)
                else:
                    print(u"%s: 校验通过" % path)
            if bad:
                print(u"存在问题的规则会被引擎跳过或永不命中，"
                      u"请修复后再 --import。")
                return 1
            return 0
        # No file given: validate the active (builtin + admin) rule set.
        ruleset = _rules.load_rules(self._state_dir(self._ai_config()))
        problems = _rules.lint_ruleset(
            ruleset,
            known_commands=_lint.known_command_names(),
            known_kb_ids=_lint.load_kb_ids())
        if problems:
            print(u"当前规则库存在 %d 个问题：" % len(problems))
            for p in problems:
                print(u"  - %s" % p)
            return 1
        print(u"当前规则库 %d 条，校验通过。" % len(ruleset))
        return 0

    def _do_import(self, opt, cfg, state):
        src = opt.import_file
        if not os.path.exists(src):
            self._err("error: rule package not found: %s", src)
            sys.exit(1)
        try:
            import json

            with open(src, "r", encoding="utf-8") as fp:
                json.load(fp)  # validate before installing
        except ValueError as e:
            self._err("error: invalid rule package %s: %s", src, e)
            sys.exit(1)
        rules_d = os.path.join(state, "rules.d")
        try:
            os.makedirs(rules_d, exist_ok=True)
            base = os.path.basename(src)
            if not base.endswith(".json"):
                base += ".json"
            dst = os.path.join(rules_d, base)
            with open(src, "rb") as fp:
                data = fp.read()
            with open(dst, "wb") as fp:
                fp.write(data)
        except OSError as e:
            self._err("error: cannot install rule package: %s", e)
            sys.exit(1)
        print(u"规则包已导入：%s" % dst)

    def _do_export(self, opt, ruleset):
        import json

        rules = [r.to_dict() for r in ruleset]
        if opt.local_only:
            rules = [r for r in rules if r.get("origin") != "builtin"]
        payload = {
            "version": 1,
            "source": "repo rules --export",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rules": rules,
        }
        path = opt.export_file
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                fp.write(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                )
            os.replace(tmp, path)
        except OSError as e:
            self._err("error: cannot export rules: %s", e)
            return 1
        scope = u"管理员规则" if opt.local_only else u"全部规则"
        print(u"已导出%s %d 条：%s" % (scope, len(rules), path))
        print(u"可编辑后经 repo rules --import 安装到其它工作区/隔离区。")
        return 0
