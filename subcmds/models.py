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

"""repo models - list / probe / switch gateway models.

Discovery is gateway-driven (GET /v1/models), never hardcoded, so the
model catalogue follows whatever the interface owner provisions.
--use persists the choice to the user config (same key the gateway
reads); --probe verifies a model actually answers (the /v1/models list
advertises models the account may not be able to call).
"""

import sys

from repoai.command_base import AiCommand
from repoai import gateway as _gateway


class Models(AiCommand):
    COMMON = False
    helpSummary = "List, probe, or switch AI gateway models"
    helpUsage = """
%prog [--probe] [--use NAME]
"""
    helpDescription = """
The '%prog' command talks to the configured AI gateway:

  repo models            list models the gateway advertises
  repo models --probe    also verify each model answers (slow, one
                         request per model)
  repo models --use N    verify N answers, then save it as your
                         ai.gateway.model

A model that is listed but 404/503 on --probe is provisioned on the
gateway but not for your account (e.g. plus tiers need the interface
owner to enable them); the tool falls back to ai.gateway.model-fallback
automatically when that happens at runtime.
"""

    def _Options(self, p):
        p.add_option(
            "--probe",
            action="store_true",
            help="verify every advertised model answers (slow)",
        )
        p.add_option(
            "--use",
            metavar="NAME",
            help="probe NAME, then set it as ai.gateway.model",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        if not cfg.get("ai.gateway.url"):
            self._err(u"error: 未配置 ai.gateway.url，无法探测网关模型。")
            self._err(u"  git config --file ~/.repoconfig/config "
                      u"ai.gateway.url <网关地址>")
            return 1
        current = cfg.get("ai.gateway.model") or ""
        fallback = cfg.get("ai.gateway.model-fallback") or ""

        if opt.use:
            return self._use(cfg, opt.use)

        try:
            models = _gateway.list_models(cfg)
        except _gateway.GatewayUnavailable as e:
            self._err(u"error: 获取模型列表失败：%s" % e)
            return 1
        print(u"网关可用模型（%d 个）：" % len(models))
        for m in models:
            marks = []
            if m == current:
                marks.append(u"当前")
            if m == fallback:
                marks.append(u"回退")
            tag = u"  [%s]" % u",".join(marks) if marks else u""
            note = u""
            if opt.probe:
                err = _gateway.probe_model(cfg, m, out=None)
                note = u"  OK" if err is None else u"  不可用：%s" % err
            print(u"  %-34s%s%s" % (m, tag, note))
        if not opt.probe:
            print(u"\n--probe 逐个探活（较慢）；--use <名字> 验证并切换。")
        return 0

    def _use(self, cfg, name):
        err = _gateway.probe_model(cfg, name, out=None)
        if err is not None:
            self._err(u"error: 模型 %s 探活失败：%s" % (name, err))
            self._err(u"  未切换。可能是账户未开通该模型（联系接口人），"
                      u"repo models --probe 可查看全部模型状态。")
            return 1
        user = cfg.user_config()
        if user is None:
            self._err(u"error: 无法写入用户配置，请手动执行："
                      u"git config --file ~/.repoconfig/config "
                      u"ai.gateway.model %s" % name)
            return 1
        user.SetString("ai.gateway.model", name)
        print(u"已切换默认模型为 %s（写入 ~/.repoconfig/config）。" % name)
        print(u"立即生效；后续 repo ask / diagnose 的在线回答都用它。")
        return 0
