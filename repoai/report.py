# Copyright (C) 2026 fight-1
#
# Licensed under the License, Version 2.0 (the "License");
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

"""Diagnostic bundle generation.

Collects: command + failure record, manifest summary (NOT contents), git
status per project (paths + flags only), environment summary, recent local
event stats.  All free text goes through diagnose.redact.  The bundle is a
zip under <state_dir>/diagnostics/ ready for the ticket system.

Bundle contents:
  meta.json          versions, region, timestamps, tool versions
  failure.json       last failure record (redacted) or absent
  manifest_summary.json  project count/remote hosts/revision kinds
  git_status.json    per-project dirty/branch flags (redacted paths)
  events_summary.json    local counters since install
  PREVIEW.txt        human readable preview shown by --preview
"""

import json
import os
import time
import zipfile

from repoai import diagnose
from repoai import events as _events


def _manifest_summary(manifest):
    out = {"available": False}
    if manifest is None:
        return out
    try:
        projects = list(manifest.projects)
        hosts = set()
        rev_kinds = {"branch": 0, "tag": 0, "sha": 0, "other": 0}
        for p in projects:
            try:
                url = p.remote.fetchUrl or p.remote.url or ""
            except Exception:
                url = ""
            if "://" in url:
                hosts.add(url.split("://", 1)[1].split("/", 1)[0])
            rev = p.revisionExpr or ""
            if len(rev) == 40:
                rev_kinds["sha"] += 1
            elif rev.startswith("refs/"):
                rev_kinds["branch"] += 1
            else:
                rev_kinds["other"] += 1
        out = {
            "available": True,
            "project_count": len(projects),
            "remote_hosts": sorted(diagnose.redact(h) for h in hosts),
            "revision_kinds": rev_kinds,
            "is_mirror": bool(manifest.IsMirror),
            "groups": getattr(manifest, "groups", None),
        }
    except Exception:
        pass
    return out


def _git_status(cfg, manifest, limit=50):
    """Dirty flags only -- never file contents or full paths."""
    rows = []
    if manifest is None:
        return rows
    topdir = getattr(manifest, "topdir", None)
    if not topdir:
        return rows
    try:
        projects = list(manifest.projects)
    except Exception:
        return rows
    for p in projects[:limit]:
        try:
            wt = p.worktree
        except Exception:
            continue
        row = {"project": diagnose.redact(p.name)}
        try:
            row["exists"] = bool(p.Exists)
            row["dirty"] = bool(p.IsDirty())
            row["branch"] = p.CurrentBranch or "DETACHED"
        except Exception:
            row["error"] = "probe-failed"
        rows.append(row)
    return rows


def _events_summary(state_dir):
    q = _events.EventQueue(state_dir)
    pending = q.pending(limit=500)
    by_type = {}
    recent_rules = {}
    for _eid, _ts, etype, _region, obj in pending:
        by_type[etype] = by_type.get(etype, 0) + 1
        if etype in ("error", "rule-hit", "feedback"):
            for rid in (obj or {}).get("rules", []) or []:
                recent_rules[rid] = recent_rules.get(rid, 0) + 1
            rid = (obj or {}).get("rule")
            if rid:
                recent_rules[rid] = recent_rules.get(rid, 0) + 1
    return {
        "pending_events": len(pending),
        "client_id": q.client_id(),
        "by_type": by_type,
        "recent_rules": recent_rules,
    }


def build_bundle(cfg, manifest=None, out_dir=None, include_env=True):
    """Create the zip bundle; returns (path, preview_text)."""
    state = cfg.state_dir()
    diag_dir = out_dir or os.path.join(state, "diagnostics")
    try:
        os.makedirs(diag_dir, exist_ok=True)
    except OSError:
        diag_dir = state
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(diag_dir, "%s.zip" % stamp)

    meta = diagnose.collect_context(cfg, manifest=manifest)
    meta["generated_at"] = time.time()
    failure = diagnose.load_last_failure(cfg)
    manifest_summary = _manifest_summary(manifest)
    git_rows = _git_status(cfg, manifest)
    events_summary = _events_summary(state)

    preview_lines = [
        "repo 诊断包预览 (%s)" % stamp,
        "=" * 40,
        "区域: %s   平台: %s   python: %s"
        % (meta.get("region"), meta.get("platform"), meta.get("python")),
        "repo 版本: %s" % meta.get("repo_version", "unknown"),
        "",
    ]
    if failure:
        preview_lines += [
            "最近失败: repo %s (%s)"
            % (failure.command, time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(failure.ts))),
            "异常: %s" % failure.exception,
            "错误摘要: %s" % failure.text.splitlines()[0][:120]
            if failure.text else "-",
            "",
        ]
    if manifest_summary.get("available"):
        preview_lines += [
            "manifest: %d 个项目, 远端: %s"
            % (manifest_summary["project_count"],
               ", ".join(manifest_summary["remote_hosts"]) or "-"),
        ]
    dirty = [r for r in git_rows if r.get("dirty")]
    preview_lines += [
        "工作区: %d/%d 个 project 有未提交修改" % (len(dirty), len(git_rows)),
        "待上报事件: %d" % events_summary["pending_events"],
        "",
        "敏感信息已脱敏：URL 用户名、家目录路径、凭据样式字符串。",
    ]
    preview = "\n".join(preview_lines)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "meta.json", json.dumps(meta, ensure_ascii=False, indent=2)
        )
        if failure is not None:
            zf.writestr(
                "failure.json",
                json.dumps(failure.to_dict(), ensure_ascii=False, indent=2),
            )
        zf.writestr(
            "manifest_summary.json",
            json.dumps(manifest_summary, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "git_status.json",
            json.dumps(git_rows, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "events_summary.json",
            json.dumps(events_summary, ensure_ascii=False, indent=2),
        )
        zf.writestr("PREVIEW.txt", preview)
    return path, preview
