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

"""repo telemetry - inspect and flush the local event queue."""

import os
import time

from repoai import events as _events
from repoai import telemetry as _telemetry
from repoai.command_base import AiCommand


class Telemetry(AiCommand):
    COMMON = False
    helpSummary = "Inspect or flush local telemetry events"
    helpUsage = """
%prog [--flush] [--export <file>] [--all]
"""
    helpDescription = """
The '%prog' command shows the local event queue state and (optionally)
flushes queued events over the channel configured for this region:
online endpoint, R2 file-exchange, or offline shuttle.

  (no args)   show queue and channel status
  --flush     upload one batch over the effective channel
  --export    write pending events to an NDJSON file (R2/R3 shuttle)
  --all       with --flush: keep flushing until the queue is empty

Events are structured metadata only (commands, exception names, rule ids,
durations) -- never code, raw logs or manifest contents.
"""

    _FLUSH_ROUNDS = 50  # safety bound for --all

    def _Options(self, p):
        p.add_option(
            "--flush",
            action="store_true",
            help="upload pending events over the effective channel",
        )
        p.add_option(
            "--export",
            metavar="FILE",
            default=None,
            help="write pending events to FILE (NDJSON) for shuttle export",
        )
        p.add_option(
            "--all",
            action="store_true",
            help="with --flush, keep flushing until the queue is empty",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)
        queue = _events.EventQueue(state)

        if opt.export:
            return self._export(cfg, queue, opt.export)

        if opt.flush:
            return self._flush(cfg, queue, opt.all)

        self._status(cfg, queue)
        return 0

    # -- status ----------------------------------------------------------
    def _status(self, cfg, queue):
        stats = queue.stats()
        print(u"AI 遥测状态")
        print(u"  区域：%s" % cfg.region())
        print(u"  上报开关：%s" % (u"启用" if cfg.telemetry_enabled()
                                   else u"已关闭"))
        print(u"  通道：%s" % cfg.telemetry_channel())
        endpoint = cfg.get("ai.telemetry.endpoint")
        exchange = cfg.get("ai.telemetry.exchange-dir")
        if endpoint:
            print(u"  上报端点：%s" % endpoint)
        if exchange:
            print(u"  摆渡目录：%s" % exchange)
        fallback = cfg.get("ai.telemetry.fallback-dir")
        if fallback:
            print(u"  回退目录：%s（主端点不可达时转入）" % fallback)
        if cfg.telemetry_enabled():
            print(u"  自动上报：%s"
                  % (u"开（节流 15 分钟）" if cfg.get_bool(
                      "ai.telemetry.auto-flush", True) else u"关"))
        print(u"  待上报事件：%d（累计 %d）" % (stats["pending"],
                                               stats["total"]))
        if not cfg.telemetry_enabled():
            print(u"  （ai.telemetry.enabled=false，事件仅保留本地）")
        elif stats["pending"]:
            print(u"  运行 repo telemetry --flush 立即上报")

    # -- flush -----------------------------------------------------------
    def _flush(self, cfg, queue, flush_all):
        if not cfg.telemetry_enabled():
            self._err("error: 上报已关闭（ai.telemetry.enabled=false），"
                      u"事件仅保留本地。")
            return 1
        total_sent = 0
        report = None
        for _ in range(self._FLUSH_ROUNDS if flush_all else 1):
            report = _telemetry.flush(cfg, queue)
            total_sent += report.sent
            if not report.ok() or report.sent == 0:
                break
            if getattr(report, "fallback_path", None):
                break  # snapshot keeps events pending: never loop
            # --all loop bound: online drains the queue (sent marks rows),
            # but shuttle channels keep events pending by design, so a
            # single batch is one shuttle file -- never loop there.
            if not flush_all or report.channel != "online":
                break
        if getattr(report, "fallback_path", None):
            # --all must not loop here: the snapshot keeps events pending.
            print(u"主端点不可达（%s），已转摆渡目录：\n  %s"
                  % (report.fallback_reason, report.fallback_path))
            print(u"（事件保留队列，管理员导入端按事件 ID 去重，不丢不重）")
            return 0
        if getattr(report, "fallback_endpoint", None):
            print(u"已上报 %d 条事件（主端点不可达，已转备用收集器 %s），"
                  u"剩余 %d 条。" % (report.sent,
                                     report.fallback_endpoint,
                                     report.pending))
            print(u"（事件保留队列：主端点恢复后仍会补报，服务端按事件 "
                  u"ID 去重，不丢不重）")
            return 0
        if not report.ok():
            self._err("error: 上报失败：%s", report.error)
            self._err(u"  事件仍保留在本地队列（%d 条），稍后可重试。",
                      report.pending)
            return 1
        if report.channel == "offline":
            print(u"当前区域通道为 offline（R3），事件保留本地队列（%d 条）。"
                  % report.pending)
            print(u"可用 repo telemetry --export <文件> 导出摆渡文件。")
            return 0
        print(u"已上报 %d 条事件（通道：%s），剩余 %d 条。"
              % (total_sent, report.channel, report.pending))
        handoff = getattr(report, "handoff_path", None)
        if handoff:
            print(u"摆渡文件：%s" % handoff)
            print(u"（事件保留队列，导入端按事件 ID 去重）")
        return 0

    # -- export ----------------------------------------------------------
    def _export(self, cfg, queue, path):
        if os.path.isdir(path):
            name = "repo-events-%s-%s.ndjson" % (
                time.strftime("%Y%m%d-%H%M%S"),
                queue.client_id()[:8],
            )
            path = os.path.join(path, name)
        try:
            n = queue.export_batch(path)
        except OSError as e:
            self._err("error: 导出失败：%s", e)
            return 1
        if n == 0:
            print(u"队列中没有待上报事件，未生成文件。")
            return 0
        print(u"已导出 %d 条事件：%s" % (n, path))
        print(u"（事件保留队列，导入端按事件 ID 去重，重复导出安全）")
        return 0
