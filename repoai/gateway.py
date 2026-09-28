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

"""Client for the AI gateway (OpenAI-compatible /v1/chat/completions).

Design.md constraints this file implements:
  - stdlib only, py3.6 syntax (urllib.request); no third-party SDK
  - online calls are strictly optional: short timeout, every failure
    degrades to None and the caller must say so out loud (不许静默劣化)
  - only structured, redacted context is sent -- never raw logs or code
    (the payload here is built from redacted FailureRecords and rule
    metadata)
  - prompts/responses are echoed to the gateway for its own audit log;
    the client only tags requests with the anonymous client id
"""

import json
import sys
import urllib.error
import urllib.request

from repoai import diagnose as _diagnose


DEFAULT_TIMEOUT = 30.0
# No baked-in model name: deployments run different gateways (vLLM,
# Ollama, hosted APIs...).  Set ai.gateway.model from the live
# catalogue via `repo models`; unknown names usually answer 503/404.
# The client speaks the OpenAI-compatible /v1 protocol, NOT the SDK.
DEFAULT_MODEL = ""

_SYSTEM_PROMPT = (
    "你是 repo 工具（git 多仓库工作区管理）的技术支持助手。用户是在 Linux 上"
    "用 repo/git 管理多仓库工作区的研发工程师。根据提供的结构化错误上下文，"
    "用不超过 200 字的中文给出可执行的排查建议。不要臆测上下文之外的信息，"
    "不要输出任何敏感路径。"
)


class GatewayUnavailable(Exception):
    """Raised for any gateway failure; message is user-presentable."""


def _timeout(cfg):
    try:
        return float(cfg.get("ai.gateway.timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _budget(cfg):
    try:
        return max(64, int(cfg.get("ai.gateway.max-tokens", 2500)))
    except (TypeError, ValueError):
        return 2500


def _endpoint(url, path="/chat/completions"):
    """Accept both host roots and OpenAI-style base URLs ending in /v1."""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url + path
    return url + "/v1" + path


def _post_chat(cfg, messages, max_tokens=None, out=None):
    """POST to the gateway and return the assistant text.  Raises.

    Budget comes from ai.gateway.max-tokens and is deliberately generous:
    reasoning-style models spend part of the completion budget on hidden
    chain-of-thought, and too small a budget can return HTTP 200 with
    empty content.  Only `content` is ever returned -- the
    `reasoning_content` field is never shown to users.

    If the configured model is unavailable (HTTP 5xx / 404 -- typically
    a typo'd or retired model name, or an unprovisioned model) we retry
    once with ai.gateway.model-fallback and say so -- loudly, never
    silently.  `repo models` lists/verifies what actually works.
    """
    if max_tokens is None:
        max_tokens = _budget(cfg)
    model = cfg.get("ai.gateway.model", DEFAULT_MODEL)
    try:
        return _post_chat_once(cfg, model, messages, max_tokens)
    except GatewayUnavailable as e:
        fallback = cfg.get("ai.gateway.model-fallback", "")
        if not fallback or fallback == model or not _is_model_error(e):
            raise
        print(
            u"（模型 %s 暂不可用：%s；回退到 %s）" % (model, e, fallback),
            file=out or sys.stderr,
        )
        return _post_chat_once(cfg, fallback, messages, max_tokens)


def _is_model_error(err):
    return u"HTTP 5" in str(err) or u"HTTP 404" in str(err)


def _post_chat_once(cfg, model, messages, max_tokens):
    url = cfg.get("ai.gateway.url")
    if not url:
        raise GatewayUnavailable(u"未配置 ai.gateway.url")
    if not model:
        raise GatewayUnavailable(
            u"未配置 ai.gateway.model（用 repo models 查看并选择）")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    token = cfg.get("ai.gateway.token")
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(
        _endpoint(url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout(cfg)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GatewayUnavailable(
            u"网关返回 HTTP %s" % getattr(e, "code", "?")
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GatewayUnavailable(u"网关不可达：%s" % e)
    try:
        message = payload["choices"][0]["message"]
        text = message["content"]
    except (KeyError, IndexError, TypeError):
        raise GatewayUnavailable(u"网关响应格式异常")
    if not text or not text.strip():
        raise GatewayUnavailable(
            u"网关未返回正文（可能预算全部用于推理，请调大 max_tokens）"
        )
    return text.strip()


def diagnose_failure(cfg, record, matches, out=None):
    """Ask the gateway to interpret a redacted failure.

    Returns answer text, or None (printing why the fallback happened --
    never silently).  Input is metadata only: exception name, redacted
    error text (already capped), top matched rule ids/titles.
    """
    try:
        rule_lines = [
            u"%s %s（置信度 %.0f%%）" % (r.id, r.title, s * 100)
            for r, s in matches
        ]
        user = u"\n".join(
            [
                u"命令：repo %s" % record.command,
                u"异常类型：%s" % record.exception,
                u"退出码：%s" % record.exit_code,
                u"错误文本（已脱敏）：\n%s"
                % _diagnose.redact(record.text[:1500]),
                u"本地规则库命中：\n%s"
                % (u"\n".join(rule_lines) or u"（无）"),
            ]
        )
        return _post_chat(
            cfg,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            out=out,
        )
    except GatewayUnavailable as e:
        return _degrade(e, out)


def enrich_rule(cfg, rule, context, out=None):
    """Extra explanation for one rule; None (loudly) on failure."""
    try:
        user = u"\n".join(
            [
                u"请补充解释以下 repo 诊断规则，给出常见触发场景：",
                json.dumps(rule.to_dict(), ensure_ascii=False),
                u"当前环境摘要：%s"
                % json.dumps(context, ensure_ascii=False),
            ]
        )
        return _post_chat(
            cfg,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            out=out,
        )
    except GatewayUnavailable as e:
        return _degrade(e, out)


def _model_hint(err):
    """Steer users at `repo models` when the failure looks like a bad
    model name (listed-but-not-provisioned answers 404/503)."""
    err = str(err)
    if u"HTTP 404" in err or u"HTTP 503" in err:
        return u"；模型名可能不正确或未开通，用 repo models 查看/切换"
    return u""


def _degrade(err, out=None):
    # 不许静默劣化：告诉用户在线增强没成，正在用本地结果。
    print(
        u"（AI 服务暂不可用：%s%s；使用本地规则库结果）"
        % (err, _model_hint(err)),
        file=out or sys.stdout,
    )
    return None


def list_models(cfg, timeout=None):
    """GET /v1/models on the configured gateway.  Returns a list of
    model-id strings; raises GatewayUnavailable (never silent)."""
    url = cfg.get("ai.gateway.url")
    if not url:
        raise GatewayUnavailable(u"未配置 ai.gateway.url")
    headers = {}
    token = cfg.get("ai.gateway.token")
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(
        _endpoint(url, "/models"), headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=timeout or _timeout(cfg)
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GatewayUnavailable(
            u"网关返回 HTTP %s" % getattr(e, "code", "?")
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GatewayUnavailable(u"网关不可达：%s" % e)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise GatewayUnavailable(u"网关响应缺少 models 列表")
    return sorted(
        m["id"] for m in data
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    )


def probe_model(cfg, model, out=None):
    """Minimal completion to verify a model actually answers.  Returns
    None on success, or the GatewayUnavailable message (str) on failure.
    Reasoning models spend budget on hidden chain-of-thought, hence the
    deliberately generous budget for a one-word answer."""
    try:
        _post_chat_once(
            cfg,
            model,
            [{"role": "user", "content": u"回复：ok"}],
            max_tokens=1500,
        )
        return None
    except GatewayUnavailable as e:
        return str(e)


_ASK_SYSTEM_PROMPT = (
    "你是 repo 工具（git 多仓库工作区管理）的问答助手，用户在终端里提问。"
    "根据提供的仓库上下文，用中文给出简短（不超过 300 字）、"
    "可执行的回答：优先给出具体命令并解释关键参数。不臆测上下文之外的"
    "信息；与 repo/git 无关的问题简要说明无法解答。不要输出敏感路径。"
)


def ask_question(cfg, question, context, history=None, out=None):
    """Free-form Q&A grounded in redacted workspace context.

    |context| is assembled by the caller from metadata only (project
    count, repo version, local rule/KB hit ids, last-failure summary);
    nothing user-private goes over the wire.  Returns answer text, or
    None with a loud degradation note (never silent).
    """
    try:
        user = u"\n".join(
            [u"问题：%s" % question]
            + [u"%s：%s" % (k, v) for k, v in context]
        )
        messages = [{"role": "system", "content": _ASK_SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user})
        return _post_chat(cfg, messages, out=out)
    except GatewayUnavailable as e:
        # Different phrasing than _degrade: there is no local "rule
        # library result" here; the fallback is the offline KB.
        print(
            u"（AI 服务暂不可用：%s%s；以下改用本地知识库结果）"
            % (e, _model_hint(e)),
            file=out or sys.stdout,
        )
        return None
