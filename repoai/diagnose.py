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

"""Failure capture, redaction, rule-based diagnosis and hint rendering.

The main.py integration point calls:
  record_failure(...)  after a command failed, to persist the last failure
  maybe_print_hint(...) to append the diagnosis block after original output

Everything here is best-effort and must never raise into the caller's
failure path: repo's original error output and exit code are sacred.
"""

import json
import os
import re
import sys
import time

from repoai import events as _events
from repoai import rules as _rules


LAST_FAILURE_NAME = "last_failure.json"
HISTORY_NAME = "failure_history.jsonl"
_MAX_HISTORY = 200
_HINT_WIDTH = 68

# Redaction patterns applied to every free-text field before it is stored or
# rendered: user@host in URLs, bare emails, bare paths under /home or /Users,
# tokens.  Hostnames are kept on purpose -- they are needed for diagnosis.
_REDACTIONS = (
    (re.compile(r"(?P<scheme>ssh|https?)://(?P<user>[^/@\s]+)@"),
     lambda m: "%s://%s@" % (m.group("scheme"), "<user>")),
    (re.compile(r"(?<![\w/.-])(?P<user>[\w.+-]+)@(?P<host>[\w][\w.-]*)"),
     lambda m: "<user>@%s" % m.group("host")),
    (re.compile(r"/(?:home|Users|data)/[^/\s]+/"), "/<home>/"),
    (re.compile(r"(?i)\b(password|token|secret|api[-_]?key)\s*[=:]\s*\S+"),
     lambda m: m.group(0).split("=", 1)[0].split(":", 1)[0] + "=<redacted>"),
)


def redact(text):
    if not text:
        return text
    for cre, repl in _REDACTIONS:
        text = cre.sub(repl, text)
    return text


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
    os.replace(tmp, path)


class FailureRecord:
    """Serializable snapshot of one failed invocation."""

    def __init__(self, command=None, args=None, exception=None, text=None,
                 duration=None, region=None, exit_code=None):
        self.ts = time.time()
        self.command = command or ""
        self.args_redacted = [redact(a) for a in (args or [])][:8]
        self.exception = exception or ""
        self.text = redact(text or "")[:4000]
        self.duration = duration
        self.region = region or ""
        self.exit_code = exit_code
        # Rule ids matched at failure time (filled by record_failure).
        self.rules = []
        # Match scores parallel to rules (metadata: floats, no user data).
        self.scores = []

    def to_dict(self):
        return {
            "ts": self.ts,
            "command": self.command,
            "args": self.args_redacted,
            "exception": self.exception,
            "text": self.text,
            "duration": self.duration,
            "region": self.region,
            "exit_code": self.exit_code,
            "rules": list(self.rules),
            "scores": list(self.scores),
        }

    @classmethod
    def from_dict(cls, data):
        rec = cls(
            command=data.get("command"),
            args=data.get("args"),
            exception=data.get("exception"),
            text=data.get("text"),
            duration=data.get("duration"),
            region=data.get("region"),
            exit_code=data.get("exit_code"),
        )
        rec.ts = data.get("ts", rec.ts)
        rec.rules = list(data.get("rules", []))
        rec.scores = list(data.get("scores", []))
        return rec

    def context(self):
        return {
            "command": self.command,
            "exception": self.exception,
            "text": self.text,
        }


def _state_dirs(cfg):
    state = cfg.state_dir()
    try:
        os.makedirs(state, exist_ok=True)
    except OSError:
        pass
    return state


def record_failure(cfg, record, ruleset=None):
    """Persist last failure + history + queue a structured error event.

    Returns the list of (rule, score) matches computed at failure time so
    callers can reuse it.  Never raises.
    """
    try:
        state = _state_dirs(cfg)
        ruleset = ruleset or _rules.load_rules(state)
        matches = _rules.diagnose(ruleset, record.context(), top=3)
        record.rules = [r.id for r, _ in matches]
        record.scores = [round(float(s), 3) for _, s in matches]
        _atomic_write(
            os.path.join(state, LAST_FAILURE_NAME),
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True),
        )
        with open(os.path.join(state, HISTORY_NAME), "a",
                  encoding="utf-8") as fp:
            fp.write(
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
            )
            fp.write("\n")
        _trim_history(os.path.join(state, HISTORY_NAME))
        if cfg.telemetry_enabled():
            q = _events.EventQueue(state)
            q.add(
                "error",
                {
                    "command": record.command,
                    "exception": record.exception,
                    "rules": record.rules,
                    "scores": record.scores,
                    "exit_code": record.exit_code,
                    "duration": record.duration,
                },
                region=record.region,
            )
        return matches
    except Exception:
        return []


def _trim_history(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
    except OSError:
        return
    if len(lines) > _MAX_HISTORY:
        try:
            _atomic_write(path, "".join(lines[-_MAX_HISTORY:]))
        except OSError:
            pass


def load_last_failure(cfg):
    state = cfg.state_dir()
    path = os.path.join(state, LAST_FAILURE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return FailureRecord.from_dict(json.load(fp))
    except (OSError, ValueError):
        return None


def collect_context(cfg, manifest=None):
    """Structured context for prompts/diagnostic bundles (no secrets)."""
    import platform

    ctx = {
        "region": cfg.region(),
        "platform": platform.system().lower(),
        "python": platform.python_version(),
    }
    try:
        from git_command import RepoSourceVersion

        ctx["repo_version"] = RepoSourceVersion()
    except Exception:
        pass
    if manifest is not None:
        try:
            ctx["manifest_is_mirror"] = bool(manifest.IsMirror)
            projects = list(manifest.projects)
            ctx["project_count"] = len(projects)
            ctx["manifest_name"] = getattr(
                manifest, "manifestName", None)
        except Exception:
            pass
    return ctx


# -- rendering ---------------------------------------------------------

_SEP = "-" * 40


def render_hint(record, matches, region, source_label=None, ai_text=None):
    """Render the appended diagnosis block for stderr.

    |ai_text| is an optional internal-AI analysis (online regions); when
    present it is shown as its own labeled section and the header credits
    both sources.  Source labels must be truthful: the rule content always
    comes from the local library.
    """
    if source_label is None:
        if ai_text:
            source_label = u"本地规则库 + AI 服务"
        else:
            source_label = u"本地规则库"
    lines = []
    lines.append(_SEP)
    lines.append(u"💡 诊断建议（%s）" % source_label)
    if not matches and not ai_text:
        lines.append(u"未能匹配到已知规则。可运行 repo report 生成诊断包，")
        lines.append(u"或 repo faq <关键词> 搜索知识库。")
        lines.append(_SEP)
        return "\n".join(lines)
    if matches:
        rule, _score = matches[0]
        lines.append(u"%s：%s" % (rule.id, rule.title))
        if rule.cause:
            lines.append(u"根因：%s" % rule.cause)
        if rule.steps:
            lines.append(u"建议步骤：")
            for i, step in enumerate(rule.steps[:3], 1):
                lines.append(u"  %d. %s" % (i, step))
        lines.append(u"运行 repo explain %s 查看完整排查步骤" % rule.id)
    if ai_text:
        lines.append(u"AI 分析（AI 服务提供）：")
        for ln in ai_text.strip().splitlines()[:12]:
            lines.append(u"  %s" % ln.strip())
    # Online regions loop feedback back fast, so the accept action is
    # offered there; isolated regions only get the dismissal actions.
    if region == "online":
        lines.append(u"[采纳] [忽略] [不再显示]")
    else:
        lines.append(u"[忽略] [不再显示]")
    lines.append(_SEP)
    return "\n".join(lines)


def print_hint(text, out=None):
    """Best-effort render; never affects caller's exit status."""
    try:
        print(text, file=out or sys.stderr)
    except Exception:
        pass


def hint_disabled_key(rule_id):
    return "ai.hintOff.%s" % rule_id


def hint_disabled(cfg, rule_id):
    return cfg.get_bool(hint_disabled_key(rule_id), False)
