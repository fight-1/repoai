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

"""AI assistance support package (offline-first, stdlib only).

This package implements the local rule engine, offline knowledge base,
event queue and diagnostic bundle generation described in the AI
enablement design.  Hard constraints:

- Python 3.6 compatible syntax only (no dataclasses / walrus / match).
- Standard library only; isolated regions have no pip.
- Never changes the behavior of pre-existing repo commands.
"""
