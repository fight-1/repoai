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

"""`ai.*` settings resolution.

Sources, later ones win:
  1. built-in defaults
  2. admin policy file: /etc/repo/ai.conf  (git-config format, read-only)
  3. user config:      RepoConfig (~/.repoconfig/config)
  4. repo config:      .repo/config
  5. environment:      REPO_AI_* overrides (used by tests & per-invocation
                       overrides without touching any file)

Region (`ai.region`) decides which capability profile is in effect:
  auto          probe for an AI endpoint, fall back to offline
  online        AI gateway reachable (A/B level)
  isolated-r1   no AI API, internal log/monitoring system reachable
  isolated-r2   no AI API, internal file-exchange area only
  isolated-r3   no egress at all, manual shuttle
"""

import os

from git_config import GitConfig
from git_config import RepoConfig


# Capability regions.
REGION_AUTO = "auto"
REGION_ONLINE = "online"
REGION_ISOLATED_R1 = "isolated-r1"
REGION_ISOLATED_R2 = "isolated-r2"
REGION_ISOLATED_R3 = "isolated-r3"

REGIONS = (
    REGION_AUTO,
    REGION_ONLINE,
    REGION_ISOLATED_R1,
    REGION_ISOLATED_R2,
    REGION_ISOLATED_R3,
)

ISOLATED_REGIONS = (
    REGION_ISOLATED_R1,
    REGION_ISOLATED_R2,
    REGION_ISOLATED_R3,
)

TELEMETRY_AUTO = "auto"
TELEMETRY_ONLINE = "online"
TELEMETRY_FILE_EXCHANGE = "file-exchange"
TELEMETRY_OFFLINE = "offline"

# Admin-delivered policy (read-only, git-config format).
POLICY_PATHS = (
    "/etc/repo/ai.conf",
    os.path.expanduser("~/.repoai/policy.conf"),
)

_DEFAULTS = {
    "ai.enabled": "true",
    "ai.autoHint": "true",
    "ai.region": REGION_AUTO,
    "ai.telemetry.enabled": "false",
    "ai.telemetry.channel": TELEMETRY_AUTO,
    "ai.telemetry.endpoint": "",
    # Bearer token for the online collector (repoai/collector.py --token).
    "ai.telemetry.token": "",
    "ai.telemetry.exchange-dir": "",
    "ai.telemetry.batch-size": "200",
    "ai.telemetry.timeout": "10",
    # Best-effort auto push after diagnostics queue events (throttled,
    # short timeout; never blocks the command meaningfully).
    "ai.telemetry.auto-flush": "true",
    # Optional HTTP fallback collector for outage tolerance.  OFF by
    # default: partitioned sites (e.g. some users can only reach the
    # backup box, others only the primary) must not silently
    # cross-post -- that would mix the two groups' data.  A machine
    # that can reach BOTH boxes may opt in explicitly (e.g.
    # http://collector-backup.example:8080); the batch is then POSTed
    # there when the primary is down, events stay pending (primary
    # keeps the only ack), and the admin folds the backup back with
    # `repo ai-admin pull`.
    "ai.telemetry.fallback-endpoint": "",
    # Optional shuttle drop-off dir for machines that mount a share
    # directly (admins, R2 regions); tried before the backup endpoint.
    "ai.telemetry.fallback-dir": "",
    "ai.gateway.url": "",
    # Model name is deployment-specific: leave empty and let the admin
    # pick one from the live catalogue with `repo models` (any
    # OpenAI-compatible /v1 gateway works: vLLM, Ollama, LiteLLM...).
    "ai.gateway.model": "",
    # Used automatically when the configured model is unavailable
    # (bad name / unprovisioned tier).  Empty = no retry.
    "ai.gateway.model-fallback": "",
    "ai.gateway.token": "",
    "ai.admin.store": "",
    # Reasoning-style models spend part of the completion budget on
    # hidden chain-of-thought, so budget and timeout are generous: too
    # small a budget can return HTTP 200 with empty content (measured
    # ~2.5k tokens needed for the enrich prompt on some models).
    "ai.gateway.max-tokens": "2500",
    "ai.gateway.timeout": "30",
    "ai.ticket.endpoint": "",
    "ai.kb.path": "",
    "ai.rules.path": "",
    # One-key feedback prompt after an error hint; must stay opt-in:
    # the AI layer never adds interactive prompts by default.
    "ai.feedback.hint": "false",
    # Lightweight pre-flight warning before risky commands (opt-in).
    "ai.preflight": "false",
}

# Keys the admin policy is allowed to pin: user/repo config cannot turn these
# back on.  Values are "on" / "off" / "" (not pinned).
POLICY_LOCK_KEYS = ("ai.enabled", "ai.telemetry.enabled")


def _policy_config():
    """Return the first existing admin policy config, or None."""
    for path in POLICY_PATHS:
        if path and os.path.exists(path):
            try:
                return GitConfig(configfile=path)
            except Exception:  # pragma: no cover - defensive
                return None
    return None


class AiConfig:
    """Resolved view over the `ai.*` settings."""

    def __init__(self, repodir=None, user_config=None, repo_config=None):
        self._repodir = repodir
        self._policy = _policy_config()
        self._user = user_config
        self._repo = None
        if repo_config is not None:
            self._repo = repo_config
        elif repodir:
            try:
                self._repo = RepoConfig.ForRepository(repodir)
            except Exception:  # pragma: no cover - defensive
                self._repo = None
        if self._user is None:
            try:
                self._user = RepoConfig.ForUser()
            except Exception:  # pragma: no cover - defensive
                self._user = None

    # -- raw lookup -----------------------------------------------------
    def _raw(self, key):
        env_key = "REPO_" + key.upper().replace(".", "_")
        val = os.environ.get(env_key)
        if val is not None:
            return val
        for cfg in (self._repo, self._user):
            if cfg is None:
                continue
            try:
                val = cfg.GetString(key)
            except Exception:  # pragma: no cover - defensive
                val = None
            if val is not None:
                return val
        if self._policy is not None:
            try:
                val = self._policy.GetString(key)
            except Exception:  # pragma: no cover - defensive
                val = None
            if val is not None:
                return val
        return _DEFAULTS.get(key)

    def get(self, key, default=None):
        val = self._raw(key)
        if val is None or val == "":
            return default
        return val

    def get_effective(self, key):
        """Value as configured, or the built-in default for this key.

        Unlike get(), explicitly-empty user config counts as a value
        ("" disables built-in defaults like the backup endpoint).
        """
        val = self._raw(key)
        if val is None:
            return _DEFAULTS.get(key, "")
        return val

    def user_config(self):
        """The user's RepoConfig handle (writable), or None."""
        return self._user

    def get_bool(self, key, default=False):
        """Boolean getter.  Policy lock keys can only be forced off."""
        val = self._raw(key)
        if key in POLICY_LOCK_KEYS and self._policy is not None:
            pin = None
            try:
                pin = self._policy.GetString(key)
            except Exception:  # pragma: no cover - defensive
                pin = None
            if pin is not None and pin.strip().lower() in ("off", "false"):
                return False
        if val is None:
            return default
        return val.strip().lower() in ("true", "yes", "1", "on")

    def region(self):
        """Return the effective (probed) region name."""
        region = (self.get("ai.region", REGION_AUTO) or REGION_AUTO).strip()
        if region not in REGIONS:
            region = REGION_AUTO
        if region == REGION_AUTO:
            region = self._probe_region()
        return region

    def _probe_region(self):
        """Cheap capability probe used when ai.region == auto.

        Rules:
          - explicit gateway URL (config/env) that looks usable -> online
          - explicit telemetry endpoint -> at least isolated-r1
          - file-exchange directory configured and writable      -> isolated-r2
          - otherwise                                            -> isolated-r3
        Probing is intentionally static (no DNS/socket): isolated regions
        must not pay connect timeouts on every command.
        """
        if self.get("ai.gateway.url"):
            return REGION_ONLINE
        if self.get("ai.telemetry.endpoint"):
            return REGION_ISOLATED_R1
        exchange = self.get("ai.telemetry.exchange-dir")
        if exchange and os.path.isdir(exchange) and os.access(
            exchange, os.W_OK
        ):
            return REGION_ISOLATED_R2
        return REGION_ISOLATED_R3

    # -- derived flags --------------------------------------------------
    def enabled(self):
        return self.get_bool("ai.enabled", True)

    def auto_hint(self):
        return self.enabled() and self.get_bool("ai.autoHint", True)

    def telemetry_enabled(self):
        return self.enabled() and self.get_bool("ai.telemetry.enabled", True)

    def telemetry_channel(self):
        channel = (
            self.get("ai.telemetry.channel", TELEMETRY_AUTO) or TELEMETRY_AUTO
        ).strip()
        if channel != TELEMETRY_AUTO:
            return channel
        region = self.region()
        if region == REGION_ONLINE:
            # "online" region only guarantees the AI gateway; pushing
            # events additionally needs a telemetry endpoint.  Without
            # one we must not promise "在线直报" (pilot machine hit this:
            # gateway set, endpoint unset, note promised a direct push
            # and `--flush` then failed).  Events stay queued honestly.
            # The built-in backup endpoint deliberately does NOT count
            # here: configuring the primary is the upload opt-in.
            if self.get("ai.telemetry.endpoint"):
                return TELEMETRY_ONLINE
            return TELEMETRY_OFFLINE
        if region == REGION_ISOLATED_R1:
            return TELEMETRY_ONLINE
        if region == REGION_ISOLATED_R2:
            return TELEMETRY_FILE_EXCHANGE
        return TELEMETRY_OFFLINE

    def is_isolated(self):
        return self.region() in ISOLATED_REGIONS

    def online_ai(self):
        """True when a real AI endpoint may be used."""
        return self.enabled() and self.region() == REGION_ONLINE

    def state_dir(self):
        """Directory holding queue/KB/rule state (default under .repo)."""
        base = self.get("ai.state-dir")
        if not base:
            root = self._repodir or os.path.expanduser("~/.repo")
            base = os.path.join(root, "ai")
        return base

    def repodir(self):
        return self._repodir
