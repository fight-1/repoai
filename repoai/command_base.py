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

"""Shared plumbing for the optional AI subcommands.

All of these commands:
  - are gated by ai.enabled (policy can force-disable; then the command
    prints one short note and exits 1, like any unavailable command)
  - never touch the network except through the AI gateway helper (phase 2)
  - emit plain text (no pager, no color requirements)
"""

import sys

from command import Command

from repoai.config import AiConfig


DISABLED_MESSAGE = (
    "repo: AI features are disabled (ai.enabled=false).\n"
    "repo: Re-enable them in ~/.repoconfig/config, or ignore this --\n"
    "repo: the original commands are unaffected."
)


class AiCommand(Command):
    """Base class for repo ai.* commands."""

    COMMON = False
    MULTI_MANIFEST_SUPPORT = False

    # Marker so `repo help` can discover the AI family without hardcoding
    # command names; the hint section only shows when ai.enabled=true.
    IS_AI_COMMAND = True

    # Set True on subclasses that should still run when ai.enabled=false
    # (none in phase 1; kept for admin escape hatch).
    ALWAYS_ALLOWED = False

    def _ai_config(self):
        if getattr(self, "_aicfg", None) is None:
            self._aicfg = AiConfig(repodir=self.repodir)
        return self._aicfg

    def _check_enabled(self, opt):
        """Return the config, or None when AI is disabled.

        When None is returned the note has already been printed; callers
        should `return 1`.
        """
        cfg = self._ai_config()
        if cfg.enabled() or self.ALWAYS_ALLOWED:
            return cfg
        print(DISABLED_MESSAGE, file=sys.stderr)
        return None

    def _state_dir(self, cfg):
        state = cfg.state_dir()
        try:
            import os

            os.makedirs(state, exist_ok=True)
        except OSError:
            pass
        return state

    def _quiet(self, opt):
        return getattr(opt, "output_mode", None) is False

    def _info(self, opt, msg, *args):
        if not self._quiet(opt):
            print(msg % args if args else msg)

    def _err(self, msg, *args):
        print(msg % args if args else msg, file=sys.stderr)
