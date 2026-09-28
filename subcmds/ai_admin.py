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

"""repo ai-admin - ingest isolated-region batches, summarize (basic dashboard).

Admin utility for the offline data loop:
collect NDJSON batch files exported by isolated regions, de-duplicate into
a local event store, and print the hot-issues / region / feedback summary
that the phase-1 dashboard requires.
"""

import glob
import os
import urllib.error
import urllib.request

from repoai.command_base import AiCommand
from repoai import importer as _importer
from repoai import rules as _rules


class AiAdmin(AiCommand):
    COMMON = False
    helpSummary = "Admin: import isolated-region batches and summarize"
    helpUsage = """
%prog import <file-or-dir>...
%prog pull [URL]
%prog summary [--top N]
"""
    helpDescription = """
The '%prog' command maintains the admin event store (default
~/.repoai/store.sqlite; override with ai.admin.store).  Intended for
administrators' machines, not regular developer workstations.

  import PATH...   ingest NDJSON batch files (or directories of them)
                   exported by isolated regions; de-duplicated by event id
  pull [URL]       fetch the backup collector's store (GET /v1/export)
                   and fold it into the local store.  Use it after the
                   primary collector was down: events clients sent to
                   the backup during the outage merge back exactly once
                   (dedupe by event id).  URL defaults to the built-in
                   ai.telemetry.fallback-endpoint; token from
                   ai.telemetry.token.
  summary          print total/region/top-rules/feedback aggregates
"""

    def _Options(self, p):
        p.add_option(
            "--top",
            type="int",
            default=10,
            help="top N hot issues in summary (default 10)",
        )

    def _store(self, cfg):
        return cfg.get("ai.admin.store") or os.path.expanduser(
            "~/.repoai/store.sqlite"
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        if not args:
            self.OptionParser.error(
                "need a subcommand: import | pull | summary")
        sub = args[0]
        store = self._store(cfg)

        if sub == "import":
            return self._import(cfg, store, args[1:])
        if sub == "pull":
            return self._pull(cfg, store, args[1:])
        if sub == "summary":
            return self._summary(cfg, store, opt.top)
        self._err(
            "error: unknown subcommand %s (import | pull | summary)", sub)
        return 1

    def _pull(self, cfg, store, args):
        """Fold the backup collector's store back into ours."""
        url = args[0] if args else (cfg.get_effective(
            "ai.telemetry.fallback-endpoint") or "").strip()
        if not url:
            self._err("error: 未指定备用收集器地址，且未配置 "
                      "ai.telemetry.fallback-endpoint")
            return 1
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/export")
        token = cfg.get("ai.telemetry.token")
        if token:
            req.add_header("Authorization", "Bearer %s" % token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self._err("error: 备用收集器返回 HTTP %s"
                      % getattr(e, "code", "?"))
            return 1
        except (urllib.error.URLError, OSError) as e:
            self._err("error: 备用收集器不可达：%s" % e)
            return 1
        r = _importer.import_text(store, text, "backup-pull")
        print(u"从 %s 拉取：解析 %d，新增 %d，重复 %d，无效 %d"
              % (url, r.parsed, r.inserted, r.duplicates, r.bad))
        print(u"事件库：%s" % store)
        return 0 if r.ok() else 1

    def _import(self, cfg, store, paths):
        if not paths:
            self._err("error: import needs file or directory paths")
            return 1
        files = []
        for path in paths:
            if os.path.isdir(path):
                files.extend(
                    sorted(glob.glob(os.path.join(path, "*.ndjson")))
                )
            else:
                files.append(path)
        if not files:
            self._err("error: no batch files (*.ndjson) found")
            return 1
        total_ins = total_dups = total_bad = 0
        for path in files:
            try:
                r = _importer.import_file(store, path)
            except OSError as e:
                self._err("error: %s: %s", path, e)
                return 1
            total_ins += r.inserted
            total_dups += r.duplicates
            total_bad += r.bad
            print(u"  %s：解析 %d，新增 %d，重复 %d，无效 %d"
                  % (os.path.basename(path), r.parsed, r.inserted,
                     r.duplicates, r.bad))
        print(u"导入完成：%s（新增 %d，重复 %d，无效 %d）"
              % (store, total_ins, total_dups, total_bad))
        return 0 if total_bad == 0 else 1

    def _summary(self, cfg, store, top):
        if not os.path.exists(store):
            self._err("error: 事件存储不存在：%s（先 import 摆渡文件）",
                      store)
            return 1
        data = _importer.summary(store, top=top)
        print(_importer.render_summary(
            data, ruleset=_rules.load_rules(self._state_dir(cfg))))
        return 0
