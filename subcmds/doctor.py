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

"""repo doctor - read-only workspace health check (offline, all regions)."""

import json

from repoai.command_base import AiCommand
from repoai import doctor as _doctor


class Doctor(AiCommand):
    COMMON = False
    helpSummary = "Check the repo workspace for common failure causes"
    helpUsage = """
%prog [--json]
"""
    helpDescription = """
The '%prog' command performs a read-only health check of the workspace
(disk, permissions, manifest files, git version, recent failure patterns,
event queue).  It works without network and never modifies anything, so
it is safe on a half-broken workspace -- which is exactly when to run it.

Exit code: 0 when there is no warning/failure, 1 otherwise.
"""

    def _Options(self, p):
        p.add_option(
            "--json",
            dest="as_json",
            action="store_true",
            help="machine-readable output",
        )

    def Execute(self, opt, args):
        cfg = self._check_enabled(opt)
        if cfg is None:
            return 1
        checks = _doctor.check_all(cfg)
        if opt.as_json:
            print(json.dumps({"checks": checks}, ensure_ascii=False,
                             indent=2))
        else:
            print(_doctor.render(checks))
        bad = any(c["level"] in ("warn", "fail") for c in checks)
        return 1 if bad else 0
