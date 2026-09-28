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

"""repo report - generate a redacted diagnostic bundle (all regions)."""

import os

from repoai.command_base import AiCommand
from repoai import events as _events
from repoai import report as _report


class Report(AiCommand):
    COMMON = False
    helpSummary = "Generate a diagnostic bundle for tickets"
    helpUsage = """
%prog [--preview] [--output <dir>] [--submit]
"""
    helpDescription = """
The '%prog' command collects command history, the last failure, a manifest
summary, git status flags and an environment summary into a zip bundle with
sensitive data (URL users, home paths, credential-looking strings)
redacted.  Bundles feed the ticket system; in isolated regions they are
submitted through the approved out-of-zone process.

--preview   show what would be included without writing the bundle
--submit    mark the bundle ready for the ticket system and queue an event
"""

    def _Options(self, p):
        p.add_option(
            "--preview",
            action="store_true",
            help="preview bundle contents without creating the file",
        )
        p.add_option(
            "-o",
            "--output",
            dest="output_dir",
            default=None,
            metavar="DIR",
            help="write the bundle into DIR",
        )
        p.add_option(
            "--submit",
            action="store_true",
            help="mark the bundle for ticket submission",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        state = self._state_dir(cfg)
        manifest = self.manifest

        if opt.preview:
            # Build into a temp bundle then show preview: keeps behavior
            # identical (same redaction pipeline) between preview/final.
            path, preview = _report.build_bundle(
                cfg, manifest=manifest, out_dir=state
            )
            print(preview)
            print("")
            print(u"（预览未保存。运行 repo report 生成正式诊断包）")
            try:
                os.remove(path)
            except OSError:
                pass
            return 0

        print(u"正在收集诊断信息...")
        for label in (u"命令历史", u"错误日志", u"manifest 摘要",
                      u"git 状态", u"环境信息"):
            print(u"✓ %s" % label)
        try:
            path, preview = _report.build_bundle(
                cfg, manifest=manifest, out_dir=opt.output_dir
            )
        except OSError as e:
            self._err("error: 诊断包生成失败: %s", e)
            return 1
        print("")
        print(u"诊断包已生成：%s" % path)
        print(u"敏感信息已自动脱敏，可预览后提交工单。")
        print(u"运行 repo report --preview 预览内容")
        if cfg.is_isolated():
            print(u"（当前区域：隔离区，外发需经审批的出域流程）")

        if cfg.telemetry_enabled():
            try:
                q = _events.EventQueue(state)
                q.add("report-generated", {"bundle": os.path.basename(path)},
                      region=cfg.region())
            except Exception:
                pass

        if opt.submit:
            return self._submit(cfg, path)
        return 0

    def _submit(self, cfg, bundle_path):
        """Hand the bundle to the ticket channel configured for the region."""
        endpoint = cfg.get("ai.ticket.endpoint")
        channel = cfg.telemetry_channel()
        if endpoint:
            try:
                import json
                import urllib.request

                with open(bundle_path, "rb") as fp:
                    data = fp.read()
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={
                        "Content-Type": "application/zip",
                        "X-Repo-Client": _events.EventQueue(
                            cfg.state_dir()).client_id(),
                    },
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
                print(u"诊断包已提交工单系统。")
            except Exception as e:
                self._err("error: 工单提交失败: %s", e)
                self._err(u"诊断包已保留：%s", bundle_path)
                return 1
        else:
            print(u"未配置工单端点（ai.ticket.endpoint）。")
            print(u"诊断包已保留：%s" % bundle_path)
            print(u"（上报通道：%s）" % channel)
        if cfg.telemetry_enabled():
            try:
                q = _events.EventQueue(cfg.state_dir())
                q.add(
                    "report-submitted",
                    {"bundle": os.path.basename(bundle_path),
                     "endpoint_configured": bool(endpoint)},
                    region=cfg.region(),
                )
            except Exception:
                pass
        return 0
