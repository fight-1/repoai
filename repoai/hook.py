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

"""The single integration point called from main.py.

Contract:
  - original repo output has already been printed when this runs
  - it must never raise and never change the exit code
  - it appends its block after a blank separator line
  - ai.enabled=false (or policy lock) => complete no-op
"""

import json
import os
import sys
import time

from repoai import diagnose as _diagnose
from repoai import events as _events
from repoai import gateway as _gateway
from repoai import rules as _rules
from repoai import telemetry as _telemetry
from repoai.config import AiConfig


# Commands whose failures are about the AI layer itself: no self-diagnosis.
_SKIP_COMMANDS = frozenset(
    ["rules", "faq", "explain", "report", "feedback", "telemetry",
     "ai-admin", "doctor", "ask", "diagnose", "models"]
)

# Implicit outcome judgement (adoption proxy): after a hint is shown we
# remember the top rule for this command; the next run of the *same*
# command within the window settles the pending entry -- success becomes
# "success_after_hint", a failure becomes "retry_failed", and no further
# run means no event at all (we never guess).  Only metadata leaves the
# machine: command, rule id, seconds, one bool.  This is a correlation,
# NOT a resolution rate; reports must label it 建议后成功率, never 解决率.
_PENDING_NAME = "pending_outcome.json"
_OUTCOME_WINDOW_DEFAULT = 1800.0

# One-key feedback prompt mapping.
_PROMPT = u"这条建议有用吗？[1]已解决 [2]未解决 [回车]跳过 > "
_VERDICTS = {
    "1": "resolved",
    "2": "unresolved",
    u"已解决": "resolved",
    u"未解决": "unresolved",
}


# Commands that mutate the workspace / hit the network: the only ones worth
# a pre-flight warning, and the ones `repo doctor` is suggested after.
_PREFLIGHT_COMMANDS = frozenset(["sync", "init", "upload", "download"])
_PREFLIGHT_THROTTLE = 3600.0  # seconds between repeated warnings


class _HookCtx:
    __slots__ = ("cfg", "name", "argv", "region", "user_text")

    def __init__(self, cfg, name, argv, region):
        self.cfg = cfg
        self.name = name
        self.argv = argv
        self.region = region
        # User-visible message for the failure, when the command
        # converts the exception into a fixed text (see
        # note_user_message).  Empty means "fall back to str(exc)".
        self.user_text = ""


def note_user_message(ctx, message):
    """Record the message a command prints for this failure.

    main.py converts some exception types into fixed user-visible
    messages (e.g. "project X not found"); str(exc) alone holds only
    the bare name, so rule patterns would miss the very text people
    see.  Callers pass the exact rendered message here.  No-op when
    ctx is None; never raises.
    """
    try:
        if ctx is not None:
            ctx.user_text = message
    except Exception:
        pass


def begin(repodir, name, argv):
    """Decide whether to observe this command for the AI layer.

    Returns a ctx to hand to maybe_print_hint()/note_success(), or None
    to stay completely silent (ai.enabled=false, autoHint off, or an AI
    command diagnosing itself).  The success-path outcome signal reuses
    the same gate: pending entries only ever exist for commands that
    were allowed to show a hint, so no extra telemetry-only state is
    needed and ai.enabled=false stays a strict no-op.
    Never raises: a broken AI layer must not affect normal command flow.
    """
    try:
        cfg = AiConfig(repodir=repodir)
        if not cfg.auto_hint():
            return None
        if name in _SKIP_COMMANDS:
            return None
        ctx = _HookCtx(cfg, name, argv, cfg.region())
        _preflight(ctx)
        return ctx
    except Exception:
        return None


def _preflight(ctx):
    """Optional, cheap, non-blocking warning before risky commands.

    Opt-in (ai.preflight=false by default) and warning-only: it never stops
    or alters the original command.  Two signals, both already available
    offline: the disk is (almost) full, or the same failure keeps recurring
    -- in which case repo doctor / repo explain is the faster path.
    """
    try:
        cfg = ctx.cfg
        if ctx.name not in _PREFLIGHT_COMMANDS:
            return
        if not cfg.get_bool("ai.preflight", False):
            return
        from repoai import doctor as _doctor

        disk = _doctor._check_disk(cfg.repodir() or ".")
        recur = _doctor._check_recurrence(cfg)
        if disk["level"] == "fail":
            msg = disk["detail"] or disk["title"]
        elif recur["level"] == "warn":
            msg = recur["detail"] or recur["title"]
        else:
            return
        # Throttle: one nudge per hour, not per command.
        marker = os.path.join(cfg.state_dir(), ".preflight_last")
        now = time.time()
        try:
            if now - os.path.getmtime(marker) < _PREFLIGHT_THROTTLE:
                return
        except OSError:
            pass
        try:
            with open(marker, "w", encoding="utf-8") as fp:
                fp.write("%.0f\n" % now)
        except OSError:
            pass
        sys.stderr.write(
            u"──── repo 预检提示（不影响本次执行）────\n"
            u"⚠ %s\n↳ %s\n" % (msg, u"先运行 repo doctor 查看完整体检。")
        )
        sys.stderr.flush()
        if cfg.telemetry_enabled():
            queue = _events.EventQueue(cfg.state_dir())
            queue.add(
                "preflight-warn",
                {"command": ctx.name, "disk": disk["level"],
                 "recurrence": recur["level"]},
                region=ctx.region,
            )
    except Exception:
        return


def _exception_name(exc):
    from error import RepoUnhandledExceptionError

    if isinstance(exc, RepoUnhandledExceptionError) and exc.error is not None:
        return type(exc.error).__name__
    return type(exc).__name__


def _failure_text(exc):
    parts = []
    try:
        parts.append(str(exc))
    except Exception:
        pass
    aggregate = getattr(exc, "aggregate_errors", None) or []
    for err in aggregate[:8]:
        try:
            parts.append(str(err))
        except Exception:
            pass
    return "\n".join(p for p in parts if p)


def _outcome_window(cfg):
    try:
        w = float(cfg.get("ai.outcome.window", _OUTCOME_WINDOW_DEFAULT))
    except (TypeError, ValueError):
        return _OUTCOME_WINDOW_DEFAULT
    return w if w > 0 else _OUTCOME_WINDOW_DEFAULT


def _pending_path(state):
    return os.path.join(state, _PENDING_NAME)


def _load_pending(state):
    try:
        with open(_pending_path(state), "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("command"):
        return data
    return None


def _clear_pending(state):
    try:
        os.remove(_pending_path(state))
    except OSError:
        pass


def _write_pending(cfg, state, name, argv, rule_id):
    """Remember the advice so a later run can settle it (local only)."""
    try:
        _diagnose._atomic_write(
            _pending_path(state),
            json.dumps(
                {
                    "command": name,
                    "rule": rule_id,
                    "ts": time.time(),
                    # Local only, already redacted; used solely to compute
                    # the one-bit args_changed flag at settle time.
                    "argv": [_diagnose.redact(a)
                             for a in (argv or [])][:8],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    except Exception:
        pass


def _settle_outcome(cfg, state, name, argv, verdict):
    """Turn this command's pending hint into one implicit outcome event.

    Drops (without reporting) when the window elapsed: absence of data
    beats a guessed verdict.  Payload is metadata only: command, top
    rule id, whole seconds, one bool.  Never raises.
    """
    try:
        data = _load_pending(state)
        if not data or data.get("command") != name:
            return
        _clear_pending(state)
        gap = time.time() - float(data.get("ts") or 0)
        if gap > _outcome_window(cfg):
            return
        if not cfg.telemetry_enabled():
            return
        prev = list(data.get("argv") or [])
        cur = [_diagnose.redact(a) for a in (argv or [])][:8]
        q = _events.EventQueue(state)
        q.add(
            "outcome",
            {
                "verdict": verdict,
                "rule": data.get("rule") or "",
                "command": name,
                "gap_seconds": int(gap),
                "args_changed": cur != prev,
                "source": "implicit",
            },
            region=cfg.region(),
        )
        _telemetry.auto_flush(cfg, q)
    except Exception:
        pass


def note_success(ctx):
    """Settle the pending hint after the same command succeeded.

    Called from main.py's success path (ctx may be None => strict
    no-op).  Same command succeeding within the window yields one
    success_after_hint event; outside the window the pending entry is
    dropped with no event.  Never raises, never prints.
    """
    try:
        if ctx is None:
            return
        cfg = ctx.cfg
        if not cfg.telemetry_enabled():
            return
        _settle_outcome(cfg, cfg.state_dir(), ctx.name, ctx.argv,
                       "success_after_hint")
    except Exception:
        pass


def maybe_print_hint(ctx, exc, exit_code):
    """Append the diagnosis block for a failed command.  Never raises.

    |ctx| is the value returned by begin() (may be None => no-op).
    """
    try:
        if ctx is None:
            return
        cfg = ctx.cfg
        name = ctx.name
        region = ctx.region
        text = _failure_text(exc)
        # Prepend the exact user-visible message (when the caller
        # supplied one via note_user_message): rule patterns are written
        # against what people actually see in the terminal.
        user_text = getattr(ctx, "user_text", "") or ""
        if user_text and user_text not in text:
            text = user_text + "\n" + text
        record = _diagnose.FailureRecord(
            command=name,
            args=list(ctx.argv or []),
            exception=_exception_name(exc),
            text=text,
            region=region,
            exit_code=exit_code,
        )
        state = cfg.state_dir()
        ruleset = _rules.load_rules(state)
        matches = _diagnose.record_failure(cfg, record, ruleset=ruleset)

        # Per-rule mute: ai.hintOff.<RULEID> = true
        visible = [
            (r, s)
            for (r, s) in matches
            if not _diagnose.hint_disabled(cfg, r.id)
        ]
        if not visible and matches:
            # Everything matched is muted: stay silent rather than nag.
            return
        shown = visible or matches
        ai_text = None
        if cfg.online_ai():
            # Bounded, best-effort enrichment; on failure the gateway says
            # so itself and we fall back to the local hint (never silent).
            ai_text = _gateway.diagnose_failure(
                cfg, record, shown, out=sys.stderr
            )
        _diagnose.print_hint(
            _diagnose.render_hint(record, shown, region, ai_text=ai_text)
        )
        if cfg.telemetry_enabled() and visible:
            q = _events.EventQueue(state)
            q.add(
                "hint-shown",
                {
                    "command": name,
                    "rules": [r.id for r, _ in visible],
                    "scores": [round(float(s), 3) for _, s in visible],
                    "exception": record.exception,
                },
                region=region,
            )
            _maybe_ask_feedback(cfg, q, name, visible, region)
            # Implicit outcome signal: a later run of this same command
            # settles it (success_after_hint / retry_failed / dropped).
            _settle_outcome(cfg, state, name, ctx.argv, "retry_failed")
            _write_pending(cfg, state, name, ctx.argv, visible[0][0].id)
            _telemetry.auto_flush(cfg, q)
    except Exception:
        # Diagnostics must never break the primary failure path.
        return


def _maybe_ask_feedback(cfg, queue, name, visible, region):
    """Optional one-key feedback right after the hint.

    Off by default (ai.feedback.hint=false): the AI layer must never add
    interactive prompts to the original command experience unless the user
    opts in.  When on, an interactive terminal only, and anything other
    than an explicit 1/2 is treated as "skip".
    """
    if not cfg.get_bool("ai.feedback.hint", False):
        return
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    try:
        sys.stderr.write(_PROMPT)
        sys.stderr.flush()
        answer = sys.stdin.readline()
    except Exception:
        return
    verdict = _VERDICTS.get(answer.strip())
    if not verdict:
        return
    try:
        queue.add(
            "feedback",
            {
                "verdict": verdict,
                "rule": visible[0][0].id,
                "score": round(float(visible[0][1]), 3),
                "command": name,
                "source": "error-hint",
            },
            region=region,
        )
        sys.stderr.write(u"已记录，感谢反馈。\n")
    except Exception:
        return
