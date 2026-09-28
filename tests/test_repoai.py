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

"""Unit tests for the repoai support package (offline, stdlib only).

Written with stdlib unittest so they also run in environments without pytest;
pytest (used by CI) collects unittest.TestCase classes as well.
"""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

from git_config import GitConfig
from git_config import RepoConfig

from repoai import config
from repoai import diagnose
from repoai import doctor
from repoai import events
from repoai import gateway
from repoai import hook
from repoai import importer
from repoai import kb
from repoai import report
from repoai import rules
from repoai import telemetry
from repoai.config import AiConfig


TOPDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AiTestCase(unittest.TestCase):
    """Base class providing an isolated AiConfig (no user state touched)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repoai-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        # Isolate config: repo's RepoConfig honors REPO_CONFIG_DIR, and the
        # XDG/gitconfig lookups key off HOME.
        os.environ["HOME"] = self.tmp
        os.environ["REPO_CONFIG_DIR"] = self.tmp
        # Gateway model is deployment-specific (no baked-in default);
        # tests that exercise the online path simulate a configured
        # deployment this way -- env wins in _raw() and never persists.
        os.environ["REPO_AI_GATEWAY_MODEL"] = "model-a"
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, ".config")
        os.makedirs(os.path.join(self.tmp, ".repoconfig"), exist_ok=True)
        # Both config classes cache a user singleton; clear them so every
        # test resolves against this test's isolated HOME/REPO_CONFIG_DIR.
        GitConfig._ForUser = None
        RepoConfig._ForUser = None
        self.repodir = os.path.join(self.tmp, ".repo")
        self.user_cfg = GitConfig(
            configfile=os.path.join(self.tmp, ".repoconfig", "config")
        )
        self.cfg = AiConfig(repodir=self.repodir, user_config=self.user_cfg)
        # Never probe a real admin policy file from the host.
        self.cfg._policy = None

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        GitConfig._ForUser = None
        RepoConfig._ForUser = None

    def capture_output(self):
        """Capture stdout+stderr into a buffer for the rest of the test."""
        buf = io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf

        def _restore():
            sys.stdout, sys.stderr = real_out, real_err

        self.addCleanup(_restore)
        return buf

    @contextlib.contextmanager
    def stub_urlopen(self, handler):
        """Replace urllib.request.urlopen for the duration of the block.

        handler(request) -> bytes body, or an exception instance to raise.
        Records requests on the returned object's .requests list.
        """

        class _Recorder:
            def __init__(self):
                self.requests = []

        rec = _Recorder()

        class _Resp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            rec.requests.append(req)
            out = handler(req)
            if isinstance(out, Exception):
                raise out
            return _Resp(out if isinstance(out, bytes) else out.encode())

        real = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            yield rec
        finally:
            urllib.request.urlopen = real


class TestPy36Compat(AiTestCase):
    """Guard the hard compatibility floor of the codebase."""

    def test_sources_parse_on_py36(self):
        """Every repoai/subcmds source must parse as Python 3.6 syntax.

        The project supports python 3.6+ (requirements.json) and some
        company hosts pin 3.9.12, so 3.7+ only syntax is a regression.
        """
        targets = []
        for sub in ("repoai", "subcmds", "contrib"):
            d = os.path.join(TOPDIR, sub)
            targets += [
                os.path.join(d, f)
                for f in sorted(os.listdir(d))
                if f.endswith(".py")
            ]
        self.assertTrue(targets)
        failures = []
        for path in targets:
            with open(path, encoding="utf-8") as fp:
                src = fp.read()
            try:
                ast.parse(src, filename=path, feature_version=(3, 6))
            except SyntaxError as e:
                failures.append("%s:%s %s" % (path, e.lineno, e.msg))
        self.assertFalse(
            failures, "py3.6 syntax violations:\n" + "\n".join(failures)
        )

    def test_no_third_party_imports(self):
        """The AI layer must stay stdlib-only for isolated regions."""
        stdlib = set(sys.builtin_module_names) | {
            "argparse",
            "collections",
            "difflib",
            "fnmatch",
            "glob",
            "http",
            "json",
            "logging",
            "optparse",
            "os",
            "platform",
            "re",
            "shutil",
            "signal",
            "socketserver",
            "sqlite3",
            "subprocess",
            "sys",
            "textwrap",
            "time",
            "urllib",
            "uuid",
            "xml",
            "zipfile",
            # repo's own modules, imported by absolute name.
            "color",
            "command",
            "error",
            "event_log",
            "git_command",
            "git_config",
            "manifest_xml",
            "platform_utils",
            "repo_trace",
            "repoai",
            "subcmds",
        }
        offenders = []
        d = os.path.join(TOPDIR, "repoai")
        for fname in sorted(os.listdir(d)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(d, fname)
            with open(path, encoding="utf-8") as fp:
                tree = ast.parse(fp.read(), filename=path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                for name in names:
                    if name and name not in stdlib:
                        offenders.append("%s: %s" % (fname, name))
        self.assertFalse(offenders, "non-stdlib imports: %s" % offenders)

    def test_new_commands_registered(self):
        """The optional commands must be discoverable by the loader."""
        import subcmds

        for name in ("rules", "faq", "explain", "report", "feedback",
                     "telemetry", "ai-admin", "doctor", "ask",
                     "diagnose"):
            self.assertIn(name, subcmds.all_commands)

    def test_builtin_data_files_valid(self):
        """rules.json / kb.json must stay parseable and well formed."""
        with open(
            os.path.join(TOPDIR, "repoai", "rules.json"), encoding="utf-8"
        ) as fp:
            data = json.load(fp)
        self.assertTrue(data["rules"])
        for rule in data["rules"]:
            self.assertIn("id", rule)
            self.assertIn("title", rule)
        with open(
            os.path.join(TOPDIR, "repoai", "kb.json"), encoding="utf-8"
        ) as fp:
            data = json.load(fp)
        self.assertTrue(data["entries"])
        for entry in data["entries"]:
            self.assertIn("id", entry)
            self.assertIn("title", entry)


class TestRules(AiTestCase):
    def test_builtin_library_loads(self):
        ruleset = rules.load_rules()
        self.assertGreaterEqual(len(ruleset), 20)
        for rid in ("R1001", "R1002", "R1003", "R1005", "R1009", "R1016"):
            self.assertIsNotNone(ruleset.get(rid))

    # Hit cases use exact message templates from the repo sources.
    def test_phase2_rule_hits(self):
        ruleset = rules.load_rules()
        cases = [
            # (command, exception, text, expected top rule)
            ("upload", "UploadError",
             "remote rejected: refs/for/master (pre-receive hook declined)",
             "R1009"),
            ("init", "DownloadError", "fatal: cannot obtain manifest ssh://x/y",
             "R1010"),
            ("init", "RepoExitError", "fatal: double check your --repo-rev.",
             "R1011"),
            ("sync", "RepoExitError",
             "error: cannot smart sync: no manifest server defined", "R1012"),
            ("upload", "RepoExitError",
             "error: project foo has a detached HEAD", "R1013"),
            ("sync", "SyncError",
             "error: foo: Cannot remove project: uncommitted changes",
             "R1015"),
            ("init", "RepoExitError", "fatal: git 2.20 or later required",
             "R1016"),
            ("sync", "DownloadError", "error: Cannot fetch foo from ssh://g/x",
             "R1008"),
            ("download", "RepoExitError", "error: cannot abandon 12345",
             "R1019"),
        ]
        for cmd, exc, text, want in cases:
            top = rules.diagnose(
                ruleset,
                {"command": cmd, "exception": exc, "text": text},
                top=1,
            )
            got = top[0][0].id if top else None
            self.assertEqual(want, got, "ctx=%r" % (text,))

    def test_revision_conflict_wins_over_generic(self):
        ruleset = rules.load_rules()
        ctx = {
            "command": "sync",
            "exception": "SyncError",
            "text": "error: Cannot checkout project foo/bar: revision "
            "conflict",
        }
        top = rules.diagnose(ruleset, ctx, top=1)
        self.assertEqual(1, len(top))
        self.assertEqual("R1002", top[0][0].id)

    def test_command_filter_applies_only_when_known(self):
        """repo explain passes text only: the command filter must not block."""
        ruleset = rules.load_rules()
        ctx = {"command": "", "exception": "", "text": "revision conflict"}
        ids = [r.id for r, _ in rules.diagnose(ruleset, ctx, top=3)]
        self.assertIn("R1002", ids)

    def test_wrong_command_is_rejected(self):
        ruleset = rules.load_rules()
        ctx = {
            "command": "status",
            "exception": "",
            "text": "no branches ready for upload",
        }
        ids = [r.id for r, _ in rules.diagnose(ruleset, ctx, top=3)]
        self.assertNotIn("R1005", ids)

    def test_no_match_for_unrelated_text(self):
        ruleset = rules.load_rules()
        ctx = {"command": "status", "exception": "", "text": "all good"}
        self.assertEqual([], rules.diagnose(ruleset, ctx))

    def test_admin_rules_override_builtin_by_id(self):
        state = self.cfg.state_dir()
        os.makedirs(os.path.join(state, "rules.d"))
        pkg = {
            "rules": [
                {
                    "id": "R1002",
                    "title": "custom override",
                    "patterns": ["revision conflict"],
                    "confidence": 0.9,
                    "steps": ["admin step"],
                }
            ]
        }
        path = os.path.join(state, "rules.d", "admin.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(pkg, fp)
        rule = rules.load_rules(state).get("R1002")
        self.assertEqual("custom override", rule.title)
        self.assertEqual("admin", rule.origin)
        self.assertEqual(["admin step"], rule.steps)

    def test_malformed_rule_file_is_skipped(self):
        state = self.cfg.state_dir()
        os.makedirs(os.path.join(state, "rules.d"))
        with open(
            os.path.join(state, "rules.d", "bad.json"), "w", encoding="utf-8"
        ) as fp:
            fp.write("{not json")
        self.assertGreaterEqual(len(rules.load_rules(state)), 10)

    def test_broken_regex_does_not_break_library(self):
        state = self.cfg.state_dir()
        os.makedirs(os.path.join(state, "rules.d"))
        pkg = {"rules": [{"id": "R9999", "patterns": ["([unclosed"]}]}
        with open(
            os.path.join(state, "rules.d", "re.json"), "w", encoding="utf-8"
        ) as fp:
            json.dump(pkg, fp)
        ruleset = rules.load_rules(state)
        self.assertIsNotNone(ruleset.get("R9999"))
        # Surfaced, not swallowed: the load problem is recorded for
        # `repo doctor` / `repo rules --lint` to show.
        kinds = [(p["id"], p["kind"]) for p in ruleset.load_problems]
        self.assertIn(("R9999", "broken-pattern"), kinds)


class TestRuleLibraryV2(AiTestCase):
    """Guards for the expanded rule library (R1027-R1040) and lint."""

    # (rule id, command, exception, error text) positive samples taken from
    # the real error templates in subcmds/*.py.
    POSITIVES = [
        ("R1027", "forall", "", "Aborting due to previous error"),
        ("R1027", "forall", "Exception",
         "forall: unhandled error, terminating the pool: ValueError: x"),
        ("R1028", "forall", "", "sh: 1: mytool: not found"),
        ("R1029", "forall", "", "no projects matched"),
        ("R1030", "checkout", "MissingBranchError",
         "error: no project has branch featureX"),
        ("R1031", "rebase", "", "error: project foo has a detached HEAD"),
        ("R1031", "rebase", "", "error: project foo does not track any"
                                " remote branches"),
        ("R1032", "abandon", "", "error: branch topic1 is not fully merged"),
        ("R1033", "sync", "GitError",
         "fatal: Unable to create '/x/.git/index.lock': File exists."),
        ("R1034", "start", "",
         "error: Your local changes to the following files would be"
         " overwritten: a.c"),
        ("R1034", "wipe", "", "error: The following untracked working tree"
                              " files would be removed by checkout"),
        ("R1035", "forall", "GitError",
         "fatal: not a git repository (or any of the parent directories)"),
        ("R1036", "cherry-pick", "",
         "CONFLICT (content): Merge conflict in a.c"),
        ("R1037", "manifest", "ManifestParseError", "no manifest found"),
        ("R1038", "smartsync", "SmartSyncError", "no manifest server"),
        ("R1040", "diff", "",
         "fatal: ambiguous argument 'HEAD1': unknown revision"),
    ]

    def _top1(self, ruleset, command, exception, text):
        ctx = {"command": command, "exception": exception, "text": text}
        matches = rules.diagnose(ruleset, ctx, top=1)
        return matches[0][0].id if matches else None

    def test_new_rules_are_top1_for_their_own_errors(self):
        ruleset = rules.load_rules()
        for want, cmd, exc, text in self.POSITIVES:
            got = self._top1(ruleset, cmd, exc, text)
            self.assertEqual(want, got, "top-1 for %r" % text[:40])

    def test_new_rules_reject_unrelated_text(self):
        ruleset = rules.load_rules()
        new_ids = {p[0] for p in self.POSITIVES}
        for cmd, exc, text in (
            ("sync", "DownloadError", "Connection timed out while fetching"),
            ("upload", "UploadError", "error: no branches ready for upload"),
            ("forall", "", "nothing to commit, working tree clean"),
            ("status", "", "project foo/ bar/ up to date"),
        ):
            hit = {r.id for r, _ in rules.diagnose(
                ruleset, {"command": cmd, "exception": exc, "text": text},
                top=3)}
            self.assertFalse(hit & new_ids,
                             "new rules must not fire on %r" % text[:40])

    def test_hot_paths_keep_their_top1(self):
        """Regression guard: old scenarios must not be outscored."""
        ruleset = rules.load_rules()
        self.assertEqual(
            "R1001", self._top1(ruleset, "sync", "DownloadError",
                                "Connection timed out while fetching"))
        self.assertEqual(
            "R1005", self._top1(ruleset, "upload", "UploadError",
                                "error: no branches ready for upload"))
        self.assertEqual(
            "R1015", self._top1(ruleset, "checkout", "SyncError",
                                "error: Your local changes to the following"
                                " files would be overwritten by checkout"))

    def test_matching_is_case_insensitive(self):
        ruleset = rules.load_rules()
        # git/curl/ssh vary the casing of the same message across versions.
        self.assertEqual(
            "R1001", self._top1(ruleset, "sync", "",
                                "resulting in connection refused"))
        self.assertEqual(
            "R1001", self._top1(ruleset, "sync", "",
                                "CONNECTION REFUSED"))

    def test_builtin_library_references_resolve(self):
        """No dangling R:/FAQ: refs, no unknown command names, clean lint."""
        from subcmds import all_commands
        from repoai import rules_lint
        ruleset = rules.load_rules()
        problems = rules.lint_ruleset(
            ruleset,
            known_commands=tuple(all_commands),
            known_kb_ids=kb.load_kb().ids())
        self.assertEqual([], problems)

    def test_lint_ruleset_flags_broken_refs_and_confidence(self):
        bad = rules.RuleSet([rules.Rule({
            "id": "R9001",
            "patterns": ["([oops", "fine"],
            "commands": ["sync", "teleport"],
            "confidence": 12,
            "related": ["FAQ:9999", "R8888"],
        })])
        problems = "\n".join(
            rules.lint_ruleset(bad, known_commands=("sync",),
                               known_kb_ids=("1001",)))
        self.assertIn("R9001", problems)
        self.assertIn("正则无法编译", problems)
        self.assertIn("teleport", problems)
        self.assertIn("FAQ:9999", problems)
        self.assertIn("R8888", problems)
        self.assertIn("confidence", problems)

    def test_rules_lint_module_main(self):
        from repoai import rules_lint

        self.assertEqual(0, rules_lint.main(["--builtin"]))
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fp:
            json.dump({"rules": [
                {"id": "R9002", "patterns": ["(oops"],
                 "commands": ["teleport"], "related": ["FAQ:9999"]},
                {"patterns": ["no id"]},
            ]}, fp)
        self.assertEqual(1, rules_lint.main([broken]))

    def test_rules_lint_module_flags_unknown_field(self):
        from repoai import rules_lint
        path = os.path.join(self.tmp, "typo.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"rules": [{"id": "R9003", "patterns": ["x"],
                                  "cofidance": 0.9}]}, fp)
        buf = self.capture_output()
        self.assertEqual(1, rules_lint.main([path]))
        self.assertIn("cofidance", buf.getvalue())

    def test_faq_refs_have_no_legacy_dangling_ids(self):
        """The old rules cited FAQ:2031/1087, which never existed."""
        ids = kb.load_kb().ids()
        for rule in rules.load_rules():
            for ref in rule.related:
                if ref.startswith("FAQ:"):
                    self.assertIn(ref[4:], ids,
                                  "%s cites %s" % (rule.id, ref))

    def test_kb_stopwords_do_not_fabricate_hits(self):
        # English function words inside KB bodies ("unable to create") must
        # not make an unrelated English query look answerable.
        self.assertEqual(["brew", "espresso"],
                         kb.tokenize("how to brew espresso"))
        results = kb.load_kb().search("how to brew espresso", top=3)
        self.assertEqual([], results)

    def test_rule_and_kb_counts(self):
        self.assertEqual(40, len(rules.load_rules()))
        self.assertEqual(18, len(kb.load_kb()))

    def test_explain_resolves_faq_references(self):
        """R1001 cites FAQ:1003; explain must print the real title."""
        from subcmds import all_commands

        cmd = all_commands["explain"](repodir=self.repodir, manifest=None)
        opt, args = cmd.OptionParser.parse_args(["R1001"])
        cmd.CommonValidateOptions(opt, args)
        buf = self.capture_output()
        rc = cmd.Execute(opt, args)
        out = buf.getvalue()
        self.assertEqual(0, rc)
        self.assertIn(u"FAQ#1003", out)
        self.assertIn(u"同步超时", out)
        self.assertNotIn(u"FAQ:2031", out)

    def test_rules_lint_cli_clean_and_broken(self):
        from subcmds import all_commands

        def run(argv):
            cmd = all_commands["rules"](repodir=self.repodir, manifest=None)
            opt, args = cmd.OptionParser.parse_args(argv)
            cmd.CommonValidateOptions(opt, args)
            return cmd.Execute(opt, args)

        buf = self.capture_output()
        self.assertEqual(0, run(["--lint"]))
        self.assertIn(u"校验通过", buf.getvalue())

        path = os.path.join(self.tmp, "bad-pkg.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"rules": [{"id": "R9010", "patterns": ["([bad"]}]},
                      fp)
        buf2 = self.capture_output()
        self.assertEqual(1, run(["--lint", path]))
        self.assertIn(u"正则无法编译", buf2.getvalue())

    def test_doctor_flags_dead_rules(self):
        state = self.cfg.state_dir()
        os.makedirs(os.path.join(state, "rules.d"))
        with open(os.path.join(state, "rules.d", "broken.json"), "w",
                  encoding="utf-8") as fp:
            json.dump({"rules": [{"id": "R9011",
                                  "patterns": ["([oops"]}]}, fp)
        checks = doctor.check_all(self.cfg)
        rulelib = [c for c in checks if c["id"] == "rulelib"]
        self.assertEqual(1, len(rulelib))
        self.assertEqual("warn", rulelib[0]["level"])
        self.assertIn("R9011", rulelib[0]["detail"])
        # And a clean library reports ok, not warn.
        os.remove(os.path.join(state, "rules.d", "broken.json"))
        checks2 = doctor.check_all(self.cfg)
        ok = [c for c in checks2 if c["id"] == "rulelib"]
        self.assertEqual("ok", ok[0]["level"])


class TestDataLoop(AiTestCase):
    """C batch: rule hits carry scores; verdicts correlate per rule.

    The loop closes the "which rule actually helps?" question for the
    weekly rule-iteration review: error/hint-shown events keep the match
    score, feedback keeps the rated rule + its score, and the admin
    report renders per-rule resolution rates.
    """

    def test_record_failure_stores_and_queues_scores(self):
        rec = diagnose.FailureRecord(
            command="sync", exception="DownloadError",
            text="Connection timed out while fetching")
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        matches = diagnose.record_failure(self.cfg, rec)
        self.assertTrue(matches)
        self.assertEqual(len(rec.rules), len(rec.scores))
        self.assertTrue(all(0.0 < s <= 1.0 for s in rec.scores))
        loaded = diagnose.load_last_failure(self.cfg)
        self.assertEqual(rec.rules, loaded.rules)
        self.assertEqual(rec.scores, loaded.scores)
        payloads = [p for _i, _t, t, _r, p in
                    events.EventQueue(self.cfg.state_dir()).pending()]
        err = [p for p in payloads if "rules" in p]
        self.assertTrue(err)
        self.assertEqual(rec.rules, err[0]["rules"])
        self.assertEqual(rec.scores, err[0]["scores"])

    def test_feedback_attaches_score_of_rated_rule(self):
        from subcmds import all_commands

        self.user_cfg.SetString("ai.enabled", "true")
        diagnose.record_failure(
            self.cfg,
            diagnose.FailureRecord(
                command="sync", exception="DownloadError",
                text="Connection timed out while fetching"))
        cmd = all_commands["feedback"](repodir=self.repodir, manifest=None)
        opt, args = cmd.OptionParser.parse_args(["--resolved"])
        cmd.CommonValidateOptions(opt, args)
        buf = self.capture_output()
        self.assertEqual(0, cmd.Execute(opt, args))
        fb = [p for _i, _t, t, _r, p in
              events.EventQueue(self.cfg.state_dir()).pending()
              if t == "feedback"]
        self.assertEqual(1, len(fb))
        self.assertEqual("R1001", fb[0]["rule"])
        self.assertIn("score", fb[0])
        self.assertTrue(0.0 < fb[0]["score"] <= 1.0)

    def test_summary_rule_feedback_correlates_verdicts(self):
        import sqlite3

        store = os.path.join(self.tmp, "store.sqlite")
        q = events.EventQueue(self.cfg.state_dir())
        q.add("hint-shown", {"command": "forall", "rules": ["R1027"],
                             "scores": [0.86]})
        q.add("feedback", {"verdict": "resolved", "rule": "R1027",
                           "score": 0.86})
        q.add("feedback", {"verdict": "unresolved", "rule": "R1027",
                           "score": 0.86})
        q.add("feedback", {"verdict": "resolved", "rule": "R1001"})
        conn = sqlite3.connect(store)
        conn.executescript(events._SCHEMA)
        for _id, _ts, etype, region, payload in q.pending():
            conn.execute(
                "INSERT INTO events"
                " (id, ts, type, region, client_id, payload, sent)"
                " VALUES (?,?,?,?,?,?,1)",
                (_id, _ts, etype, region, "c1", json.dumps(payload)))
        conn.commit()
        conn.close()
        data = importer.summary(store)
        self.assertEqual({"resolved": 1, "unresolved": 1},
                         data["rule_feedback"]["R1027"])
        text = importer.render_summary(data, ruleset=rules.load_rules())
        self.assertIn(u"R1027", text)
        self.assertIn(u"解决率 50%", text)

    def test_low_resolution_rate_flagged_for_review(self):
        import sqlite3

        store = os.path.join(self.tmp, "store2.sqlite")
        conn = sqlite3.connect(store)
        conn.executescript(events._SCHEMA)
        conn.execute(
            "INSERT INTO events (id, ts, type, region, client_id,"
            " payload, sent) VALUES (?,?,?,?,?,?,1)",
            ("hint1", 1.0, "hint-shown", "", "c1",
             json.dumps({"command": "sync", "rules": ["R1021"]})))
        for n, v in enumerate(("inaccurate", "unresolved", "unresolved")):
            conn.execute(
                "INSERT INTO events (id, ts, type, region, client_id,"
                " payload, sent) VALUES (?,?,?,?,?,?,1)",
                ("fb%d" % n, 1.0, "feedback", "", "c1",
                 json.dumps({"verdict": v, "rule": "R1021"})))
        conn.commit()
        conn.close()
        data = importer.summary(store)
        text = importer.render_summary(data)
        self.assertIn(u"建议复核", text)

    def test_scores_are_not_privacy_fields(self):
        """The new event fields stay on the pilot_check allow-list spirit:
        no raw text keys introduced."""
        banned = set(["question", "text", "args", "log", "stderr"])
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync", "rules": ["R1001"],
                        "scores": [0.85]})
        for _i, _ts, _t, _r, payload in q.pending():
            self.assertFalse(banned & set(payload))


class TestImplicitOutcome(AiTestCase):
    """Implicit outcome signal (adoption proxy).

    A shown hint writes a local pending entry; the next run of the SAME
    command settles it: success -> success_after_hint, failure ->
    retry_failed, window elapsed -> no event at all.  Payload stays
    metadata-only (command, rule, seconds, one bool) and the field is
    labelled a correlation, never a resolution rate.
    """

    def _fail_sync(self, argv=("-j6",)):
        from error import SyncError

        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", list(argv))
        self.assertIsNotNone(ctx)
        err = SyncError(
            "sync failed",
            aggregate_errors=[Exception(
                "Connection timed out while fetching")],
        )
        hook.maybe_print_hint(ctx, err, 1)
        return ctx

    def _events_of(self, etype):
        out = []
        for _i, _ts, t, _r, payload in events.EventQueue(
                self.cfg.state_dir()).pending():
            if t == etype:
                out.append(payload)
        return out

    def _pending_path(self):
        return os.path.join(self.cfg.state_dir(), "pending_outcome.json")

    def test_hint_writes_pending(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self._fail_sync()
        with open(self._pending_path(), encoding="utf-8") as fp:
            data = json.load(fp)
        self.assertEqual("sync", data["command"])
        self.assertEqual("R1001", data["rule"])
        self.assertTrue(data["ts"] > 0)

    def test_success_within_window_setstles_event(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        ctx = self._fail_sync()
        hook.note_success(ctx)
        out = self._events_of("outcome")
        self.assertEqual(1, len(out))
        payload = out[0]
        self.assertEqual("success_after_hint", payload["verdict"])
        self.assertEqual("R1001", payload["rule"])
        self.assertEqual("sync", payload["command"])
        self.assertIsInstance(payload["gap_seconds"], int)
        self.assertFalse(payload["args_changed"])
        # Settled: the pending file is gone, no double counting.
        self.assertFalse(os.path.exists(self._pending_path()))
        hook.note_success(ctx)
        self.assertEqual(1, len(self._events_of("outcome")))

    def test_retry_failure_setstles_with_verdict(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self._fail_sync()
        self._fail_sync(argv=("-c", "-j6"))
        out = self._events_of("outcome")
        self.assertEqual(1, len(out))
        self.assertEqual("retry_failed", out[0]["verdict"])
        self.assertTrue(out[0]["args_changed"])

    def test_window_expiry_drops_without_event(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self._fail_sync()
        with open(self._pending_path(), encoding="utf-8") as fp:
            data = json.load(fp)
        data["ts"] -= 3700.0  # beyond the default 1800s window
        with open(self._pending_path(), "w", encoding="utf-8") as fp:
            json.dump(data, fp)
        ctx = hook.begin(self.repodir, "sync", ["-j6"])
        hook.note_success(ctx)
        self.assertEqual([], self._events_of("outcome"))
        # Expired entries are consumed, not left to rot.
        self.assertFalse(os.path.exists(self._pending_path()))

    def test_other_command_does_not_settle(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self._fail_sync()
        ctx = hook.begin(self.repodir, "upload", [])
        hook.note_success(ctx)
        self.assertEqual([], self._events_of("outcome"))
        self.assertTrue(os.path.exists(self._pending_path()))

    def test_ai_commands_never_write_pending(self):
        # The AI surface must not pollute its own outcome signal.
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self.assertIsNone(hook.begin(self.repodir, "diagnose", []))
        hook.note_success(None)  # tolerated, no crash
        self.assertFalse(os.path.exists(self._pending_path()))

    def test_disabled_flags_are_noop(self):
        # ai.enabled=false: begin() returns None, nothing is written and
        # note_success(None) is a strict no-op (red line).
        self.user_cfg.SetString("ai.enabled", "false")
        self.assertIsNone(hook.begin(self.repodir, "sync", ["-j6"]))
        hook.note_success(None)
        self.assertFalse(os.path.exists(self._pending_path()))
        # Telemetry off, hints on: the hint itself works, but no pending
        # file is written (maybe_print_hint only writes under
        # telemetry_enabled), and _settle_outcome refuses to queue.
        # begin() re-resolves config through the user singleton, so it
        # must be dropped after toggling (same trick as setUp).
        self.user_cfg.SetString("ai.enabled", "true")
        self.user_cfg.SetString("ai.telemetry.enabled", "false")
        GitConfig._ForUser = None
        RepoConfig._ForUser = None
        from error import SyncError

        self.capture_output()
        ctx = hook.begin(self.repodir, "sync", ["-j6"])
        self.assertIsNotNone(ctx)
        err = SyncError("sync failed",
                        aggregate_errors=[
                            Exception("Connection timed out while fetching")])
        hook.maybe_print_hint(ctx, err, 1)
        hook._settle_outcome(self.cfg, self.cfg.state_dir(), "sync",
                             ["-j6"], "success_after_hint")
        self.assertFalse(os.path.exists(self._pending_path()))

    def test_pending_payload_leaks_nothing(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "true")
        self._fail_sync()
        with open(self._pending_path(), encoding="utf-8") as fp:
            raw = fp.read()
        # argv is kept only locally to compute args_changed, and only in
        # redacted form; nothing else may ride along.
        self.assertNotIn("timed out", raw)

    def test_outcome_event_privacy_shape(self):
        payload = self._events_of("outcome") or [None]
        banned = set(["question", "text", "args", "log", "stderr"])
        for p in payload:
            if p is not None:
                self.assertFalse(banned & set(p))

    def test_summary_rule_outcome(self):
        import sqlite3

        store = os.path.join(self.tmp, "store.sqlite")
        conn = sqlite3.connect(store)
        conn.executescript(events._SCHEMA)

        def ins(n, etype, payload):
            conn.execute(
                "INSERT INTO events (id, ts, type, region, client_id,"
                " payload, sent) VALUES (?,?,?,?,?,?,1)",
                (n, 1.0, etype, "", "c1", json.dumps(payload)))

        ins("h1", "hint-shown", {"command": "sync", "rules": ["R1021"]})
        ins("h2", "hint-shown", {"command": "sync", "rules": ["R1021"]})
        ins("o1", "outcome", {"verdict": "success_after_hint",
                              "rule": "R1021", "command": "sync",
                              "gap_seconds": 30, "args_changed": False})
        ins("o2", "outcome", {"verdict": "retry_failed",
                              "rule": "R1021", "command": "sync",
                              "gap_seconds": 12, "args_changed": True})
        ins("o3", "outcome", {"verdict": "success_after_hint",
                              "rule": "R1021", "command": "sync",
                              "gap_seconds": 40, "args_changed": False})
        conn.commit()
        conn.close()
        data = importer.summary(store)
        self.assertEqual({"success_after_hint": 2, "retry_failed": 1},
                         data["rule_outcome"]["R1021"])
        text = importer.render_summary(data)
        self.assertIn(u"建议后成功率 67%", text)
        self.assertIn(u"非解决率", text)

    def test_collector_summary_exposes_rule_outcome(self):
        import sqlite3

        from repoai import collector

        store = os.path.join(self.tmp, "store2.sqlite")
        conn = sqlite3.connect(store)
        conn.executescript(events._SCHEMA)
        rows = [
            ("h1", "hint-shown",
             {"command": "sync", "rules": ["R1021"]}),
            ("o1", "outcome",
             {"verdict": "success_after_hint", "rule": "R1021",
              "command": "sync", "gap_seconds": 5,
              "args_changed": False}),
        ]
        for rid, etype, payload in rows:
            conn.execute(
                "INSERT INTO events (id, ts, type, region, client_id,"
                " payload, sent) VALUES (?,?,?,?,?,?,1)",
                (rid, 1.0, etype, "", "c1", json.dumps(payload)))
        conn.commit()
        conn.close()
        data = collector.Collector(store).feedback_summary()
        self.assertTrue(data["ok"])
        self.assertEqual({"success_after_hint": 1},
                         data["rule_outcome"]["R1021"])


class TestKb(AiTestCase):
    def test_builtin_kb_search_cjk(self):
        base = kb.load_kb()
        self.assertGreaterEqual(len(base), 5)
        results = base.search("部分同步")
        self.assertTrue(results)
        self.assertEqual("1001", results[0][0].id)

    def test_builtin_kb_search_ascii(self):
        results = kb.load_kb().search("no branches ready for upload")
        self.assertEqual("1004", results[0][0].id)

    def test_no_results(self):
        self.assertEqual([], kb.load_kb().search("zzz-nomatch-xyz"))

    def test_offline_package_layering(self):
        state = self.cfg.state_dir()
        os.makedirs(os.path.join(state, "kb.d"))
        with open(
            os.path.join(state, "kb.d", "pkg.json"), "w", encoding="utf-8"
        ) as fp:
            json.dump(
                {
                    "entries": [
                        {
                            "id": "9001",
                            "title": "专用镜像说明",
                            "body": "离线包补充的镜像使用说明",
                            "tags": ["镜像"],
                        }
                    ]
                },
                fp,
            )
        base = kb.load_kb(state)
        self.assertIsNotNone(base.get("9001"))
        self.assertEqual("9001", base.search("专用镜像")[0][0].id)


class TestEvents(AiTestCase):
    def setUp(self):
        super().setUp()
        self.state = self.cfg.state_dir()

    def test_roundtrip_and_pending(self):
        q = events.EventQueue(self.cfg.state_dir())
        eid = q.add(
            "error",
            {"command": "sync", "rules": ["R1002"]},
            region="isolated-r3",
        )
        self.assertTrue(eid)
        pending = q.pending()
        self.assertEqual(1, len(pending))
        self.assertEqual(eid, pending[0][0])
        self.assertEqual("sync", pending[0][4]["command"])
        self.assertEqual(1, q.count_pending())
        q.mark_sent([eid])
        self.assertEqual(0, q.count_pending())

    def test_sent_bookkeeping(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("feedback", {"verdict": "resolved"})
        q.add("feedback", {"verdict": "unresolved"})
        self.assertEqual(2, q.count_pending())
        first = q.pending(limit=1)[0][0]
        q.mark_sent([first])
        self.assertEqual(1, q.count_pending())

    def test_client_id_stable(self):
        self.assertEqual(
            events.EventQueue(self.state).client_id(),
            events.EventQueue(self.state).client_id(),
        )

    def test_export_batch_format(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("feedback", {"verdict": "resolved", "rule": "R1002"})
        path = os.path.join(self.state, "batch.ndjson")
        self.assertEqual(1, q.export_batch(path))
        with open(path, encoding="utf-8") as fp:
            lines = fp.read().strip().splitlines()
        meta = json.loads(lines[0])
        rec = json.loads(lines[1])
        self.assertEqual(1, meta["_meta"]["count"])
        self.assertEqual("resolved", rec["payload"]["verdict"])
        # Shuttle safety: exporting must not consume the queue.
        self.assertEqual(1, q.count_pending())

    def test_export_empty(self):
        self.assertEqual(
            0, events.EventQueue(self.state).export_batch("unused")
        )

    def test_payload_is_structured_only(self):
        """Stored payloads are JSON metadata; verify serializable round trip."""
        q = events.EventQueue(self.cfg.state_dir())
        q.add("kb-search", {"hit": "1001", "chars": 4})
        payload = q.pending()[0][4]
        self.assertEqual({"hit": "1001", "chars": 4}, payload)


class TestDiagnose(AiTestCase):
    def test_redaction(self):
        text = (
            "ssh://alice@gerrit.corp:29418/repo /home/alice/x token=abc123"
        )
        out = diagnose.redact(text)
        self.assertNotIn("alice@gerrit", out)
        self.assertNotIn("/home/alice", out)
        self.assertNotIn("abc123", out)
        # The host itself stays useful for diagnosis.
        self.assertIn("gerrit.corp", out)

    def test_record_and_load_last_failure(self):
        rec = diagnose.FailureRecord(
            command="sync",
            args=["-j6"],
            exception="SyncError",
            text="error: Cannot checkout project foo/bar: revision conflict",
            region="isolated-r2",
            exit_code=1,
        )
        matches = diagnose.record_failure(self.cfg, rec)
        self.assertTrue(matches)
        self.assertEqual("R1002", matches[0][0].id)
        loaded = diagnose.load_last_failure(self.cfg)
        self.assertIsNotNone(loaded)
        self.assertEqual("sync", loaded.command)
        self.assertIn("revision conflict", loaded.text)

    def test_record_failure_queues_event(self):
        diagnose.record_failure(
            self.cfg,
            diagnose.FailureRecord(
                command="sync",
                exception="SyncError",
                text="revision conflict",
            ),
        )
        types = [t for _i, _t, t, _r, _p in
                 events.EventQueue(self.cfg.state_dir()).pending()]
        self.assertIn("error", types)

    def test_history_is_bounded(self):
        for i in range(diagnose._MAX_HISTORY + 30):
            diagnose.record_failure(
                self.cfg,
                diagnose.FailureRecord(
                    command="sync", exception="SyncError", text="x%d" % i
                ),
            )
        path = os.path.join(self.cfg.state_dir(), diagnose.HISTORY_NAME)
        with open(path, encoding="utf-8") as fp:
            self.assertLessEqual(
                len(fp.readlines()), diagnose._MAX_HISTORY
            )

    def test_render_hint_isolated(self):
        ruleset = rules.load_rules()
        matches = rules.diagnose(
            ruleset,
            {"command": "sync", "exception": "", "text": "revision conflict"},
            top=1,
        )
        out = diagnose.render_hint(
            diagnose.FailureRecord(command="sync"), matches, "isolated-r3"
        )
        self.assertIn("R1002", out)
        self.assertIn(u"本地规则库", out)
        self.assertIn(u"[忽略] [不再显示]", out)
        self.assertNotIn(u"[采纳]", out)

    def test_render_hint_online_without_ai(self):
        """Source label stays truthful before the gateway answers."""
        ruleset = rules.load_rules()
        matches = rules.diagnose(
            ruleset,
            {"command": "sync", "exception": "", "text": "revision conflict"},
            top=1,
        )
        out = diagnose.render_hint(
            diagnose.FailureRecord(command="sync"), matches, "online"
        )
        self.assertIn(u"本地规则库", out)
        self.assertNotIn(u"AI 服务提供", out)
        self.assertIn(u"[采纳]", out)

    def test_render_hint_online_with_ai(self):
        ruleset = rules.load_rules()
        matches = rules.diagnose(
            ruleset,
            {"command": "sync", "exception": "", "text": "revision conflict"},
            top=1,
        )
        out = diagnose.render_hint(
            diagnose.FailureRecord(command="sync"),
            matches,
            "online",
            ai_text=u"建议先执行 git fsck 检查对象库。",
        )
        self.assertIn(u"本地规则库 + AI 服务", out)
        self.assertIn(u"AI 分析（AI 服务提供）", out)
        self.assertIn(u"git fsck", out)
        self.assertIn(u"[采纳]", out)

    def test_render_hint_ai_only_without_match(self):
        """No rule hit but AI answered: still show the AI section."""
        out = diagnose.render_hint(
            diagnose.FailureRecord(command="sync"),
            [],
            "online",
            ai_text=u"看起来是磁盘空间不足。",
        )
        self.assertIn(u"磁盘空间不足", out)

    def test_render_hint_without_match(self):
        out = diagnose.render_hint(
            diagnose.FailureRecord(command="sync"), [], "isolated-r1"
        )
        self.assertIn("repo report", out)


class TestHook(AiTestCase):
    def test_disabled_is_noop(self):
        self.user_cfg.SetString("ai.enabled", "false")
        self.assertIsNone(hook.begin(self.repodir, "sync", ["-j6"]))
        # maybe_print_hint tolerates the None ctx.
        hook.maybe_print_hint(None, Exception("boom"), 1)

    def test_hint_skips_ai_commands(self):
        """The AI commands must not diagnose their own failures."""
        for name in (
            "faq", "rules", "explain", "report", "feedback", "telemetry",
            "ai-admin", "doctor", "ask", "diagnose", "models",
        ):
            self.assertIsNone(hook.begin(self.repodir, name, []))

    def test_hint_printed_on_failure(self):
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", ["-j6"])
        self.assertIsNotNone(ctx)
        from error import SyncError

        err = SyncError(
            "sync failed",
            aggregate_errors=[
                Exception(
                    "error: Cannot checkout project foo/bar: revision conflict"
                )
            ],
        )
        hook.maybe_print_hint(ctx, err, 1)
        out = buf.getvalue()
        self.assertIn(u"诊断建议", out)
        self.assertIn("R1002", out)

    def test_swallowed_errors_still_diagnose(self):
        """main.py converts NoSuchProjectError into an exit code inside
        _Run()/_RunLong(); the hook must fire there with the exact
        user-visible message (pilot machine regression: no hint appeared
        at all, and str(exc) alone lacks "project X not found").
        """
        from error import NoSuchProjectError

        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", ["no_such_project_xyz"])
        try:
            raise NoSuchProjectError("no_such_project_xyz")
        except NoSuchProjectError as e:
            hook.note_user_message(
                ctx, "error: project no_such_project_xyz not found")
            hook.maybe_print_hint(ctx, e, 1)
        out = buf.getvalue()
        self.assertIn(u"诊断建议", out)
        self.assertIn("R1021", out)

    def _write_manifest(self):
        """Minimal readable workspace so RepoClient init succeeds."""
        os.makedirs(os.path.join(self.repodir, "manifests.git"),
                    exist_ok=True)
        with open(os.path.join(self.repodir, "manifest.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n<manifest>\n'
                '  <remote name="o" fetch=".."/>\n'
                '  <default remote="o" revision="main"/>\n'
                '  <project name="p/one" path="one"/>\n</manifest>\n')
        with open(
            os.path.join(self.repodir, "manifests.git", "config"), "w",
            encoding="utf-8",
        ) as fp:
            fp.write('[remote "origin"]\n\turl = http://localhost/m\n')

    def test_run_forwards_ctx_no_nameerror(self):
        """End-to-end: a real _Run() call on a failing command must
        neither crash (NameError -- the ai_ctx kwarg was once dropped in
        the _Run->_RunLong lambda) nor leak a traceback.  This drives the
        actual code path, unlike a source grep, so signature drift fails
        here.  Uses `repo list` (no network) with an unknown project.
        """
        import main

        # Precise wiring assertion: a past regression dropped the ai_ctx
        # kwarg in the _Run -> _RunLong lambda (NameError on the pilot
        # machine).  Grep the exact call so signature drift names itself.
        main_src = open(os.path.join(TOPDIR, "main.py"),
                        encoding="utf-8").read()
        self.assertRegex(
            main_src, r"self\._RunLong\(\s*name, gopts, argv,\s*"
                      r"git_trace2_event_log,\s*ai_ctx=ai_ctx")

        self._write_manifest()
        buf = self.capture_output()
        gopts, _ = main.global_options.parse_args([])
        rc = None
        for ai_ctx in (hook.begin(self.repodir, "list", ["no_such_xyz"]),
                       None):
            out_before = len(buf.getvalue())
            rc = main._Repo(self.repodir)._Run(
                "list", gopts, ["no_such_xyz"], ai_ctx=ai_ctx)
            chunk = buf.getvalue()[out_before:]
            self.assertNotIn("Traceback", chunk)
            self.assertNotIn("NameError", chunk)
            self.assertEqual(1, rc)
            # Hook actually fired only when a ctx was supplied.
            self.assertIn(u"诊断建议" if ai_ctx else "", chunk)

    def test_note_user_message_tolerates_none_ctx(self):
        hook.note_user_message(None, "anything")  # must not raise

    def test_per_rule_mute(self):
        buf = self.capture_output()
        self.user_cfg.SetString("ai.hintOff.R1002", "true")
        ctx = hook.begin(self.repodir, "sync", [])
        from error import SyncError

        hook.maybe_print_hint(
            ctx,
            SyncError(
                "x", aggregate_errors=[Exception("revision conflict")]
            ),
            1,
        )
        self.assertNotIn("R1002", buf.getvalue())

    def test_fallback_hint_when_no_rule_matches(self):
        """Unmatched failures still get the self-service pointer block."""
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "status", [])
        hook.maybe_print_hint(ctx, Exception("totally unknown state"), 1)
        out = buf.getvalue()
        self.assertIn(u"未能匹配到已知规则", out)
        self.assertIn("repo report", out)

    def test_no_feedback_prompt_by_default(self):
        """Default ai.feedback.hint=false: never any interaction."""
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", [])
        from error import SyncError

        hook.maybe_print_hint(
            ctx,
            SyncError("x", aggregate_errors=[Exception("revision conflict")]),
            1,
        )
        self.assertNotIn(u"这条建议有用吗", buf.getvalue())
        # Default off even with tty streams present.

    def _tty(self, data=""):
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        return _TTY(data)

    def test_one_key_feedback_when_enabled(self):
        """ai.feedback.hint=true + tty: pressing 1 records the verdict."""
        self.user_cfg.SetString("ai.feedback.hint", "true")
        real_in, real_out, real_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = self._tty("1\n")
        sys.stdout = sys.stderr = self._tty()
        ctx = hook.begin(self.repodir, "sync", [])
        from error import SyncError

        try:
            hook.maybe_print_hint(
                ctx,
                SyncError(
                    "x", aggregate_errors=[Exception("revision conflict")]
                ),
                1,
            )
            out = sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = real_in, real_out, real_err
        q = events.EventQueue(self.cfg.state_dir())
        feedbacks = [
            p for _i, _t, t, _r, p in q.pending() if t == "feedback"
        ]
        self.assertEqual(1, len(feedbacks))
        self.assertEqual("resolved", feedbacks[0]["verdict"])
        self.assertEqual("R1002", feedbacks[0]["rule"])
        self.assertEqual(u"error-hint", feedbacks[0]["source"])
        self.assertIn(u"感谢反馈", out)

    def test_one_key_feedback_skips_on_blank(self):
        """Anything but an explicit choice is a no-op."""
        self.user_cfg.SetString("ai.feedback.hint", "true")
        real_in, real_out, real_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = self._tty("\n")
        sys.stdout = sys.stderr = self._tty()
        ctx = hook.begin(self.repodir, "sync", [])
        from error import SyncError

        try:
            hook.maybe_print_hint(
                ctx,
                SyncError(
                    "x", aggregate_errors=[Exception("revision conflict")]
                ),
                1,
            )
        finally:
            sys.stdin, sys.stdout, sys.stderr = real_in, real_out, real_err
        q = events.EventQueue(self.cfg.state_dir())
        self.assertEqual(
            [], [p for _i, _t, t, _r, p in q.pending() if t == "feedback"]
        )

    def test_feedback_prompt_skipped_when_not_tty(self):
        """Piped output (CI, logs): prompt never even printed."""
        self.user_cfg.SetString("ai.feedback.hint", "true")
        buf = self.capture_output()  # StringIO: isatty() is False
        ctx = hook.begin(self.repodir, "sync", [])
        from error import SyncError

        hook.maybe_print_hint(
            ctx,
            SyncError("x", aggregate_errors=[Exception("revision conflict")]),
            1,
        )
        self.assertNotIn(u"这条建议有用吗", buf.getvalue())
        self.assertIn("R1002", buf.getvalue())  # hint itself still shown

    def test_hook_never_raises(self):
        """A broken state dir must not propagate out of the hook."""
        ctx = hook.begin(self.repodir, "sync", [])
        self.assertIsNotNone(ctx)
        # Point state at a path that cannot be created.
        ctx.cfg = AiConfig(
            repodir=os.path.join(os.sep, "proc", "nonexistent-ai"),
            user_config=self.user_cfg,
        )
        hook.maybe_print_hint(ctx, Exception("revision conflict"), 1)

    # -- pre-flight --------------------------------------------------
    def _seed_recurring_failures(self, n=3):
        os.makedirs(self.cfg.state_dir(), exist_ok=True)
        path = os.path.join(self.cfg.state_dir(), "failure_history.jsonl")
        with open(path, "w", encoding="utf-8") as fp:
            for i in range(n):
                fp.write(
                    json.dumps(
                        {
                            "ts": 1e12 + i,
                            "command": "sync",
                            "exception": "SyncError",
                            "rules": ["R1002"],
                        }
                    )
                    + "\n"
                )

    def test_preflight_off_by_default(self):
        """Default: begin() never prints anything before a command."""
        self._seed_recurring_failures()
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", [])
        self.assertIsNotNone(ctx)
        self.assertNotIn(u"预检", buf.getvalue())

    def test_preflight_warns_on_recurrence(self):
        self.user_cfg.SetString("ai.preflight", "true")
        self._seed_recurring_failures()
        buf = self.capture_output()
        hook.begin(self.repodir, "sync", [])
        out = buf.getvalue()
        self.assertIn(u"预检提示", out)
        self.assertIn("repo doctor", out)
        q = events.EventQueue(self.cfg.state_dir())
        kinds = [t for _i, _t, t, _r, _p in q.pending()]
        self.assertIn("preflight-warn", kinds)

    def test_preflight_throttled(self):
        """Second begin() within the window: warned only once."""
        self.user_cfg.SetString("ai.preflight", "true")
        self._seed_recurring_failures()
        buf = self.capture_output()
        hook.begin(self.repodir, "sync", [])
        hook.begin(self.repodir, "sync", [])
        self.assertEqual(1, buf.getvalue().count(u"预检提示"))

    def test_preflight_skips_safe_commands(self):
        self.user_cfg.SetString("ai.preflight", "true")
        self._seed_recurring_failures()
        buf = self.capture_output()
        hook.begin(self.repodir, "status", [])
        self.assertNotIn(u"预检", buf.getvalue())


class TestDoctor(AiTestCase):
    """repo doctor: read-only health checks."""

    def _run(self, name, argv):
        from subcmds import all_commands

        cmd = all_commands[name](repodir=self.repodir, manifest=None)
        opt, args = cmd.OptionParser.parse_args(argv)
        cmd.CommonValidateOptions(opt, args)
        return cmd.Execute(opt, args)

    _GOOD_MANIFEST = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest>\n'
        '  <remote name="origin" fetch=".."/>\n'
        '  <default remote="origin" revision="master"/>\n'
        '  <project path="p1" name="platform/p1"/>\n'
        '  <project path="p2" name="platform/p2"/>\n'
        "</manifest>\n"
    )

    def test_manifest_include_chain_counted(self):
        """repo manifests commonly <include> fragments; doctor must
        follow them or every real workspace looks empty (pilot bug)."""
        os.makedirs(self.repodir, exist_ok=True)
        snips = os.path.join(self.repodir, "manifests", "snips")
        os.makedirs(snips, exist_ok=True)
        with open(os.path.join(self.repodir, "manifests", "default.xml"),
                  "w", encoding="utf-8") as fp:
            fp.write(
                '<manifest><include name="snips/main.xml"/></manifest>')
        with open(os.path.join(snips, "main.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write('<manifest><project path="a" name="x/a"/>'
                     '<project path="b" name="x/b"/></manifest>')
        link = os.path.join(self.repodir, "manifest.xml")
        if not os.path.exists(link):
            os.symlink(os.path.join("manifests", "default.xml"), link)
        ok, msg = doctor._parse_xml(link)
        self.assertTrue(ok)
        self.assertEqual(u"2 个 project", msg)

    def test_manifest_include_cycle_terminates(self):
        os.makedirs(self.repodir, exist_ok=True)
        path = os.path.join(self.repodir, "manifest.xml")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write('<manifest><include name="manifest.xml"/>'
                     '<project path="a" name="x/a"/></manifest>')
        ok, msg = doctor._parse_xml(path)
        self.assertTrue(ok)
        self.assertEqual(u"1 个 project", msg)

    def test_manifest_missing_include_is_finding(self):
        os.makedirs(self.repodir, exist_ok=True)
        path = os.path.join(self.repodir, "manifest.xml")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write('<manifest><include name="gone.xml"/></manifest>')
        ok, msg = doctor._parse_xml(path)
        self.assertFalse(ok)
        self.assertIn(u"include", msg)

    def test_manifest_include_resolves_against_manifests_dir(self):
        """repo's own generated pointer file includes relative to the
        manifests/ dir, not to .repo/ (pilot machine found both modes).
        """
        os.makedirs(self.repodir, exist_ok=True)
        os.makedirs(os.path.join(self.repodir, "manifests"), exist_ok=True)
        with open(os.path.join(self.repodir, "manifest.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write('<manifest><include name="default.xml"/></manifest>')
        with open(os.path.join(self.repodir, "manifests",
                               "default.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write('<manifest><project path="u" name="a/u"/>'
                     '<project path="d" name="a/d"/></manifest>')
        ok, msg = doctor._parse_xml(
            os.path.join(self.repodir, "manifest.xml"),
            extra_roots=[os.path.join(self.repodir, "manifests")])
        self.assertTrue(ok)
        self.assertEqual(u"2 个 project", msg)

    def _healthy(self):
        os.makedirs(self.repodir, exist_ok=True)
        with open(
            os.path.join(self.repodir, "manifest.xml"), "w",
            encoding="utf-8",
        ) as fp:
            fp.write(self._GOOD_MANIFEST)

    def test_flags_missing_manifest(self):
        buf = self.capture_output()
        self.assertEqual(1, self._run("doctor", []))
        out = buf.getvalue()
        self.assertIn(u"manifest", out)
        self.assertIn(u"警告", out)

    def test_healthy_workspace_exit_0(self):
        self._healthy()
        buf = self.capture_output()
        self.assertEqual(0, self._run("doctor", []))
        self.assertIn(u"未发现明显问题", buf.getvalue())

    def test_json_output(self):
        self._healthy()
        buf = self.capture_output()
        self._run("doctor", ["--json"])
        data = json.loads(buf.getvalue())
        ids = {c["id"] for c in data["checks"]}
        self.assertIn("manifest", ids)
        self.assertIn("disk", ids)
        self.assertIn("git", ids)

    def test_bad_local_manifest_is_fail(self):
        self._healthy()
        lm = os.path.join(self.repodir, "local_manifests")
        os.makedirs(lm)
        with open(os.path.join(lm, "broken.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write("<manifest><project")
        with open(os.path.join(lm, "notes.txt"), "w",
                  encoding="utf-8") as fp:
            fp.write("x")
        buf = self.capture_output()
        self.assertEqual(1, self._run("doctor", []))
        out = buf.getvalue()
        self.assertIn(u"失败", out)
        self.assertIn("broken.xml", out)
        self.assertIn("notes.txt", out)  # non-xml flagged as warn

    def test_empty_manifest_warns(self):
        os.makedirs(self.repodir, exist_ok=True)
        with open(os.path.join(self.repodir, "manifest.xml"), "w",
                  encoding="utf-8") as fp:
            fp.write('<manifest>\n</manifest>\n')
        buf = self.capture_output()
        self.assertEqual(1, self._run("doctor", []))
        self.assertIn(u"没有任何 project", buf.getvalue())

    def test_recurrence_check(self):
        os.makedirs(self.cfg.state_dir(), exist_ok=True)
        path = os.path.join(self.cfg.state_dir(), "failure_history.jsonl")
        with open(path, "w", encoding="utf-8") as fp:
            for _ in range(3):
                fp.write(json.dumps(
                    {"ts": 1e12, "command": "sync",
                     "exception": "SyncError", "rules": ["R1002"]}) + "\n")
        c = doctor._check_recurrence(self.cfg)
        self.assertEqual("warn", c["level"])
        self.assertIn("R1002", c["rule"])
        # Old entries fall out of the window.
        with open(path, "w", encoding="utf-8") as fp:
            for _ in range(5):
                fp.write(json.dumps(
                    {"ts": 1.0, "command": "sync", "exception": "SyncError",
                     "rules": ["R1002"]}) + "\n")
        self.assertEqual("ok", doctor._check_recurrence(self.cfg)["level"])


class TestGateway(AiTestCase):
    """Gateway client: payload hygiene, timeout/degrade behavior."""

    def setUp(self):
        super().setUp()
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")

    def _record(self):
        return diagnose.FailureRecord(
            command="sync",
            exception="SyncError",
            text="error: Cannot checkout project foo/bar: revision conflict",
            region="online",
            exit_code=1,
        )

    def _matches(self):
        return rules.diagnose(
            rules.load_rules(),
            {"command": "sync", "exception": "", "text": "revision conflict"},
            top=1,
        )

    def test_diagnose_success_and_payload(self):
        answer = {"choices": [{"message": {"content": "先检查 manifest。"}}]}
        with self.stub_urlopen(
            lambda req: json.dumps(answer)
        ) as rec:
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertEqual(u"先检查 manifest。", out)
        self.assertEqual(1, len(rec.requests))
        req = rec.requests[0]
        self.assertEqual(
            "http://ai.internal:8080/v1/chat/completions", req.full_url
        )
        self.assertEqual("application/json", req.get_header("Content-type"))
        body = json.loads(req.data.decode("utf-8"))
        self.assertIn("messages", body)
        self.assertEqual("user", body["messages"][-1]["role"])
        self.assertIn("R1002", body["messages"][-1]["content"])

    # -- model fallback (model-a not yet provisioned) ---------------
    def _http_error(self, code):
        return urllib.error.HTTPError(
            "http://ai.internal:8080/v1/chat/completions", code,
            "Service Unavailable", {}, None)

    def test_model_fallback_on_503(self):
        self.user_cfg.SetString("ai.gateway.model", "model-a")
        self.user_cfg.SetString("ai.gateway.model-fallback", "model-b")
        seen = []

        def handler(req):
            body = json.loads(req.data.decode("utf-8"))
            seen.append(body["model"])
            if body["model"] == "model-a":
                return self._http_error(503)
            return json.dumps(
                {"choices": [{"message": {"content": "回退后的回答"}}]})

        buf = self.capture_output()
        with self.stub_urlopen(handler) as rec:
            out = gateway.ask_question(
                self.cfg, u"怎么同步", [], out=sys.stderr)
        self.assertEqual(u"回退后的回答", out)
        self.assertEqual(["model-a", "model-b"], seen)  # loud retry
        self.assertIn(u"暂不可用", buf.getvalue())
        self.assertIn(u"回退", buf.getvalue())

    def test_model_fallback_disabled_keeps_error(self):
        self.user_cfg.SetString("ai.gateway.model", "model-a")
        self.user_cfg.SetString("ai.gateway.model-fallback", "")
        calls = []

        def handler(req):
            calls.append(1)
            return self._http_error(503)

        with self.stub_urlopen(handler):
            out = gateway.ask_question(self.cfg, u"?", [], out=None)
        self.assertIsNone(out)
        self.assertEqual(1, len(calls))  # no retry, one loud degrade

    def test_connection_error_does_not_retry(self):
        """Network down is not a model problem: no fallback spin."""
        self.user_cfg.SetString("ai.gateway.model", "model-a")
        calls = []

        def handler(req):
            calls.append(1)
            return urllib.error.URLError("refused")

        with self.stub_urlopen(handler):
            out = gateway.ask_question(self.cfg, u"?", [], out=None)
        self.assertIsNone(out)
        self.assertEqual(1, len(calls))

    def test_redacted_text_only(self):
        """What goes over the wire must already be redacted."""
        rec = self._record()
        rec.text = "/home/bob/x alice@corp"
        seen = {}

        def handler(req):
            seen["body"] = req.data.decode("utf-8")
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            )

        with self.stub_urlopen(handler):
            gateway.diagnose_failure(self.cfg, rec, self._matches())
        self.assertNotIn("/home/bob", seen["body"])
        self.assertNotIn("alice@corp", seen["body"])

    def test_no_url_raises_nothing_but_degrades(self):
        self.user_cfg.SetString("ai.gateway.url", "")
        buf = self.capture_output()
        with self.stub_urlopen(lambda req: b"should not be called"):
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertIsNone(out)
        self.assertIn(u"AI 服务暂不可用", buf.getvalue())

    def test_unreachable_degrades_loudly(self):
        err = urllib.error.URLError("connection refused")
        buf = self.capture_output()
        with self.stub_urlopen(lambda req: err):
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertIsNone(out)
        text = buf.getvalue()
        self.assertIn(u"AI 服务暂不可用", text)
        self.assertIn(u"使用本地规则库结果", text)

    def test_http_error_degrades(self):
        err = urllib.error.HTTPError(
            "http://x", 503, "down", {}, None
        )
        buf = self.capture_output()
        with self.stub_urlopen(lambda req: err):
            self.assertIsNone(
                gateway.diagnose_failure(
                    self.cfg, self._record(), self._matches()
                )
            )
        self.assertIn("503", buf.getvalue())

    def test_openai_style_base_url(self):
        """ai.gateway.url may be a host root OR an OpenAI /v1 base URL."""
        self.user_cfg.SetString("ai.gateway.url", "http://gw.internal/v1")
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            )
        ) as rec:
            gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertEqual(
            "http://gw.internal/v1/chat/completions",
            rec.requests[0].full_url,
        )

    def test_default_model_is_unset(self):
        """No vendor model baked in: unset degrades loudly, never fires."""
        self.assertEqual("", config._DEFAULTS["ai.gateway.model"])
        os.environ.pop("REPO_AI_GATEWAY_MODEL", None)
        buf = self.capture_output()
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            )
        ) as rec:
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertIsNone(out)
        self.assertEqual(0, len(rec.requests))  # never fired at the wire
        self.assertIn(u"ai.gateway.model", buf.getvalue())
        self.assertIn(u"repo models", buf.getvalue())

    def test_configured_model_sends_generous_budget(self):
        self.user_cfg.SetString("ai.gateway.model", "model-a")
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            )
        ) as rec:
            gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        body = json.loads(rec.requests[0].data.decode("utf-8"))
        self.assertEqual("model-a", body["model"])
        # Reasoning-style models: budget must be generous, not the old 400.
        self.assertGreaterEqual(body["max_tokens"], 1500)

    def test_reasoning_model_budget_configurable(self):
        self.user_cfg.SetString("ai.gateway.max-tokens", "3000")
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            )
        ) as rec:
            gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        body = json.loads(rec.requests[0].data.decode("utf-8"))
        self.assertEqual(3000, body["max_tokens"])

    def test_reasoning_content_never_shown(self):
        """Only `content` is surfaced; chain-of-thought stays hidden."""
        answer = {
            "choices": [
                {
                    "message": {
                        "content": "先看网络。",
                        "reasoning_content": "内部思考过程，绝不该展示",
                    }
                }
            ]
        }
        with self.stub_urlopen(lambda req: json.dumps(answer)):
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertEqual(u"先看网络。", out)
        self.assertNotIn(u"思考", out)

    def test_empty_content_degrades_with_hint(self):
        """Reasoning ate the whole budget: HTTP 200, empty content."""
        answer = {"choices": [{"message": {"content": ""}}]}
        buf = self.capture_output()
        with self.stub_urlopen(lambda req: json.dumps(answer)):
            out = gateway.diagnose_failure(
                self.cfg, self._record(), self._matches()
            )
        self.assertIsNone(out)
        self.assertIn(u"max_tokens", buf.getvalue())

    def test_malformed_answer_degrades(self):
        with self.stub_urlopen(lambda req: b'{"unexpected": 1}'):
            self.assertIsNone(
                gateway.diagnose_failure(
                    self.cfg, self._record(), self._matches()
                )
            )

    def test_token_header(self):
        self.user_cfg.SetString("ai.gateway.token", "sekret")
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "x"}}]}
            )
        ) as rec:
            gateway.diagnose_failure(self.cfg, self._record(), self._matches())
        self.assertEqual(
            "Bearer sekret", rec.requests[0].get_header("Authorization")
        )

    def test_enrich_rule_success(self):
        rule = rules.load_rules().get("R1002")
        with self.stub_urlopen(
            lambda req: json.dumps(
                {"choices": [{"message": {"content": "常见于 manifest 改分支"}}]}
            )
        ):
            out = gateway.enrich_rule(self.cfg, rule, {"repo": "2.29"})
        self.assertIn(u"manifest", out)

    def test_hook_uses_gateway_when_online(self):
        """S3: online failure hint credits both sources."""
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", ["-j6"])
        self.assertIsNotNone(ctx)
        from error import SyncError

        answer = json.dumps(
            {"choices": [{"message": {"content": "先核对 manifest 分支。"}}]}
        )
        with self.stub_urlopen(lambda req: answer):
            hook.maybe_print_hint(
                ctx,
                SyncError(
                    "sync failed",
                    aggregate_errors=[Exception("revision conflict")],
                ),
                1,
            )
        out = buf.getvalue()
        self.assertIn("R1002", out)
        self.assertIn(u"AI 服务", out)
        self.assertIn(u"先核对 manifest 分支", out)

    def test_hook_survives_gateway_outage(self):
        buf = self.capture_output()
        ctx = hook.begin(self.repodir, "sync", [])
        err = urllib.error.URLError("no route")
        from error import SyncError

        with self.stub_urlopen(lambda req: err):
            hook.maybe_print_hint(
                ctx,
                SyncError(
                    "x",
                    aggregate_errors=[Exception("revision conflict")],
                ),
                1,
            )
        out = buf.getvalue()
        # Local rule hint must still be there, plus a visible fallback note.
        self.assertIn("R1002", out)
        self.assertIn(u"AI 服务暂不可用", out)


class TestTelemetry(AiTestCase):
    """Upload channels: online flush, R2 file exchange, R3 offline."""

    def _seed(self, n=3):
        q = events.EventQueue(self.cfg.state_dir())
        for i in range(n):
            q.add("error", {"command": "sync", "rules": ["R1002"]})
        return q

    # -- online ----------------------------------------------------------
    def test_online_flush_marks_sent(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        q = self._seed(3)
        with self.stub_urlopen(lambda req: b'{"ok":true}') as rec:
            report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok())
        self.assertEqual(3, report.sent)
        self.assertEqual(0, report.pending)
        self.assertEqual(0, q.count_pending())
        req = rec.requests[0]
        self.assertEqual(
            "http://logs.internal:4317/v1/events", req.full_url
        )
        self.assertEqual(
            "application/x-ndjson", req.get_header("Content-type")
        )
        self.assertEqual(q.client_id(), req.get_header("X-repo-client"))
        lines = req.data.decode("utf-8").strip().splitlines()
        self.assertEqual(4, len(lines))  # _meta + 3 events
        self.assertIn("_meta", json.loads(lines[0]))
        self.assertEqual("error", json.loads(lines[1])["type"])

    def test_online_batch_size(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        self.user_cfg.SetString("ai.telemetry.batch-size", "2")
        q = self._seed(5)
        with self.stub_urlopen(lambda req: b"{}"):
            report = telemetry.flush(self.cfg, q)
        self.assertEqual(2, report.sent)
        self.assertEqual(3, report.pending)

    def test_online_unreachable_keeps_events(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        q = self._seed(2)
        with self.stub_urlopen(lambda req: urllib.error.URLError("nope")):
            report = telemetry.flush(self.cfg, q)
        self.assertFalse(report.ok())
        self.assertEqual(0, report.sent)
        self.assertIn(u"不可达", report.error)
        self.assertEqual(2, q.count_pending())

    def test_online_down_falls_back_to_share_dir(self):
        # backup-endpoint fallback: primary collector unreachable,
        # batch snapshot lands in the share dir, nothing marked sent,
        # no error surfaced (data kept flowing via the other path).
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317")
        drop = os.path.join(self.tmp, "share")
        os.makedirs(drop)
        self.user_cfg.SetString("ai.telemetry.fallback-dir", drop)
        q = self._seed(3)
        with self.stub_urlopen(lambda req: urllib.error.URLError("down")):
            report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(3, report.sent)
        self.assertEqual(3, q.count_pending())  # import dedupes by id
        self.assertIsNotNone(report.fallback_path)
        self.assertIn(u"不可达", report.fallback_reason)
        files = [f for f in os.listdir(drop) if f.endswith(".ndjson")]
        self.assertEqual(1, len(files))
        with open(os.path.join(drop, files[0]), encoding="utf-8") as fp:
            lines = [json.loads(x) for x in fp if x.strip()]
        events = [x for x in lines if "_meta" not in x]
        self.assertEqual(3, len(events))
        self.assertNotIn("command", events[0])  # payload only, no raw args
        # Snapshot is per-client deterministic: a second flush while the
        # endpoint is down replaces the file instead of piling up copies.
        q.add("error", {"command": "sync", "rules": ["R1001"]},
              region="online")
        with self.stub_urlopen(lambda req: urllib.error.URLError("down")):
            report2 = telemetry.flush(self.cfg, q)
        self.assertEqual(4, report2.sent)
        files = [f for f in os.listdir(drop) if f.endswith(".ndjson")]
        self.assertEqual(1, len(files))

    def test_online_down_without_fallback_still_errors(self):
        # No share configured: the loud degradation path is unchanged.
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317")
        q = self._seed(2)
        with self.stub_urlopen(lambda req: urllib.error.URLError("nope")):
            report = telemetry.flush(self.cfg, q)
        self.assertFalse(report.ok())
        self.assertIsNone(report.fallback_path)

    def test_auto_flush_default_on_and_throttled(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317")
        q = self._seed(1)
        # Default: enabled; endpoint down but no share -> report exists,
        # error stays internal (auto path never raises/prints).
        with self.stub_urlopen(lambda req: urllib.error.URLError("down")):
            first = telemetry.auto_flush(self.cfg, q)
        self.assertIsNotNone(first)
        self.assertEqual(1, q.count_pending())
        # Throttled inside the window: second call is a no-op.
        self.assertIsNone(telemetry.auto_flush(self.cfg, q))
        # Off-switch respected.
        self.user_cfg.SetString("ai.telemetry.auto-flush", "false")
        os.remove(os.path.join(self.cfg.state_dir(), ".autoflush_last"))
        with self.stub_urlopen(lambda req: urllib.error.URLError("down")):
            self.assertIsNone(telemetry.auto_flush(self.cfg, q))

    def test_auto_flush_sends_when_endpoint_up(self):
        store, base = self._collector_server()
        self.user_cfg.SetString("ai.telemetry.endpoint", base)
        self.user_cfg.SetString("ai.telemetry.channel", "online")
        q = self._seed(2)
        report = telemetry.auto_flush(self.cfg, q)
        self.assertIsNotNone(report)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(2, report.sent)
        self.assertEqual(0, q.count_pending())
        conn = importer._connect(store)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, total)

    def _collector_server(self):
        from repoai import collector as _collector

        store = os.path.join(self.tmp, "store.sqlite")
        httpd = _collector.make_server("127.0.0.1", 0, store, "")
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return store, "http://127.0.0.1:%d" % httpd.server_address[1]

    def test_online_http_error_keeps_events(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        q = self._seed(1)
        err = urllib.error.HTTPError("http://x", 400, "bad", {}, None)
        with self.stub_urlopen(lambda req: err):
            report = telemetry.flush(self.cfg, q)
        self.assertFalse(report.ok())
        self.assertIn("400", report.error)
        self.assertEqual(1, q.count_pending())

    def test_online_missing_endpoint(self):
        q = self._seed(1)
        report = telemetry.flush(self.cfg, q, channel="online")
        self.assertFalse(report.ok())
        self.assertIn("ai.telemetry.endpoint", report.error)

    def test_empty_queue_no_request(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        q = events.EventQueue(self.cfg.state_dir())
        with self.stub_urlopen(lambda req: b"{}") as rec:
            report = telemetry.flush(self.cfg, q)
        self.assertEqual([], rec.requests)
        self.assertEqual(0, report.sent)

    # -- R2 file exchange -------------------------------------------------
    def test_file_exchange_writes_batch(self):
        exchange = os.path.join(self.tmp, "exchange")
        os.makedirs(exchange)
        self.user_cfg.SetString("ai.telemetry.exchange-dir", exchange)
        q = self._seed(2)
        report = telemetry.flush(self.cfg, q)
        self.assertEqual("file-exchange", report.channel)
        self.assertTrue(report.ok())
        self.assertEqual(2, report.sent)
        files = os.listdir(exchange)
        self.assertEqual(1, len(files))
        self.assertTrue(files[0].startswith("repo-events-"))
        with open(os.path.join(exchange, files[0]), encoding="utf-8") as fp:
            lines = fp.read().strip().splitlines()
        self.assertEqual(3, len(lines))
        # Shuttle safety: events stay queued for retry/dedupe.
        self.assertEqual(2, q.count_pending())

    def test_file_exchange_missing_dir_setting(self):
        q = self._seed(1)
        report = telemetry.flush(self.cfg, q, channel="file-exchange")
        self.assertFalse(report.ok())
        self.assertIn("exchange-dir", report.error)

    def test_file_exchange_dir_not_mounted(self):
        # Configured but absent (e.g. share not mounted): must NOT create
        # the directory locally -- that would silently lose the shuttle.
        missing = os.path.join(self.tmp, "not-mounted")
        self.user_cfg.SetString("ai.telemetry.exchange-dir", missing)
        q = self._seed(1)
        report = telemetry.flush(self.cfg, q, channel="file-exchange")
        self.assertFalse(report.ok())
        self.assertIn(u"不存在或未挂载", report.error)
        self.assertFalse(os.path.exists(missing))

    def test_file_exchange_unwritable(self):
        # An existing dir we cannot write into (chmod 000; skipped for
        # root where permissions are not enforced).
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        self.addCleanup(os.chmod, ro, 0o700)
        self.user_cfg.SetString("ai.telemetry.exchange-dir", ro)
        q = self._seed(1)
        report = telemetry.flush(self.cfg, q, channel="file-exchange")
        self.assertFalse(report.ok())
        self.assertIn(u"摆渡目录写入失败", report.error)

    # -- R3 offline -------------------------------------------------------
    def test_offline_is_noop(self):
        q = self._seed(2)
        report = telemetry.flush(self.cfg, q, channel="offline")
        self.assertTrue(report.ok())
        self.assertEqual(0, report.sent)
        self.assertEqual(2, report.pending)

    def test_channel_resolution(self):
        # No endpoint / exchange => offline by probe.
        self.assertEqual("offline", self.cfg.telemetry_channel())
        self.user_cfg.SetString("ai.telemetry.endpoint", "http://x:1")
        self.assertEqual("online", self.cfg.telemetry_channel())
        self.user_cfg.SetString("ai.telemetry.channel", "offline")
        self.assertEqual("offline", self.cfg.telemetry_channel())

    def test_flush_never_raises(self):
        q = events.EventQueue(os.path.join(os.sep, "proc", "nope-ai"))
        self.user_cfg.SetString("ai.telemetry.endpoint", "http://x:1")
        report = telemetry.flush(self.cfg, q)
        self.assertIsNotNone(report)


class TestReport(AiTestCase):
    def test_bundle_contents_and_redaction(self):
        diagnose.record_failure(
            self.cfg,
            diagnose.FailureRecord(
                command="sync",
                args=["ssh://bob@corp/x"],
                exception="DownloadError",
                text="fatal: Cannot fetch /home/bob/secret from "
                "ssh://bob@corp:29418/x",
                region="isolated-r2",
            ),
        )
        path, preview = report.build_bundle(self.cfg)
        self.assertTrue(os.path.exists(path))
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for expected in (
                "meta.json",
                "manifest_summary.json",
                "git_status.json",
                "events_summary.json",
                "PREVIEW.txt",
                "failure.json",
            ):
                self.assertIn(expected, names)
            failure = zf.read("failure.json").decode("utf-8")
        self.assertNotIn("bob@corp", failure)
        self.assertNotIn("/home/bob", failure)
        self.assertIn(u"诊断包预览", preview)

    def test_bundle_without_manifest(self):
        path, preview = report.build_bundle(self.cfg, manifest=None)
        self.assertTrue(os.path.exists(path))
        self.assertIn(u"区域", preview)

    def test_meta_has_version_and_region(self):
        path, _preview = report.build_bundle(self.cfg)
        with zipfile.ZipFile(path) as zf:
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
        self.assertIn("region", meta)
        self.assertIn("python", meta)


class TestConfig(AiTestCase):
    def test_defaults(self):
        self.assertTrue(self.cfg.enabled())
        self.assertTrue(self.cfg.auto_hint())
        self.assertTrue(self.cfg.telemetry_enabled())

    def test_region_probe_isolated_without_endpoints(self):
        """No gateway / exchange dir configured => isolated-r3."""
        self.assertEqual("isolated-r3", self.cfg.region())
        self.assertTrue(self.cfg.is_isolated())
        self.assertFalse(self.cfg.online_ai())
        self.assertEqual("offline", self.cfg.telemetry_channel())

    def test_region_online_with_gateway(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        self.assertEqual("online", self.cfg.region())
        self.assertTrue(self.cfg.online_ai())
        # Gateway alone enables the AI, NOT event pushing: without a
        # telemetry endpoint we must not promise "在线直报".
        self.assertEqual("offline", self.cfg.telemetry_channel())
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://collector:8080")
        self.assertEqual("online", self.cfg.telemetry_channel())

    def test_region_file_exchange(self):
        exchange = os.path.join(self.tmp, "exchange")
        os.makedirs(exchange)
        self.user_cfg.SetString("ai.telemetry.exchange-dir", exchange)
        self.assertEqual("isolated-r2", self.cfg.region())
        self.assertEqual("file-exchange", self.cfg.telemetry_channel())

    def test_region_r1_with_telemetry_endpoint(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        self.assertEqual("isolated-r1", self.cfg.region())
        self.assertEqual("online", self.cfg.telemetry_channel())

    def test_env_override(self):
        os.environ["REPO_AI_REGION"] = "isolated-r1"
        self.assertEqual("isolated-r1", self.cfg.region())

    def test_invalid_region_falls_back(self):
        self.user_cfg.SetString("ai.region", "not-a-region")
        self.assertIn(
            self.cfg.region(),
            ("online", "isolated-r1", "isolated-r2", "isolated-r3"),
        )

    def test_auto_hint_follows_master_switch(self):
        self.user_cfg.SetString("ai.enabled", "false")
        self.assertFalse(self.cfg.auto_hint())
        self.assertFalse(self.cfg.telemetry_enabled())

    def test_telemetry_independent_switch(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "false")
        self.assertTrue(self.cfg.enabled())
        self.assertFalse(self.cfg.telemetry_enabled())

    def test_state_dir_under_repodir(self):
        self.assertTrue(
            self.cfg.state_dir().endswith(os.path.join(".repo", "ai"))
        )

    def test_policy_lock_can_force_disable(self):
        """An admin policy pinning ai.enabled off wins over user config."""
        policy_path = os.path.join(self.tmp, "policy.conf")
        with open(policy_path, "w", encoding="utf-8") as fp:
            fp.write("[ai]\n\tenabled = false\n")
        self.cfg._policy = GitConfig(configfile=policy_path)
        self.user_cfg.SetString("ai.enabled", "true")
        self.assertFalse(self.cfg.enabled())


class TestImporter(AiTestCase):
    """Admin-side shuttle ingestion store (S7 basic)."""

    def _batch(self, name, events):
        path = os.path.join(self.tmp, name)
        meta = json.dumps(
            {"_meta": {"client_id": "c1", "count": len(events)}}
        )
        lines = [meta]
        for i, ev in enumerate(events):
            lines.append(
                json.dumps(
                    {
                        "id": ev.get("id", "e%d" % i),
                        "ts": 1000.0 + i,
                        "type": ev.get("type", "error"),
                        "region": ev.get("region", "isolated-r2"),
                        "client_id": "c1",
                        "payload": ev.get("payload", {}),
                    },
                    ensure_ascii=False,
                )
            )
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
        return path

    def _store(self):
        return os.path.join(self.tmp, "store.sqlite")

    def test_import_and_dedupe(self):
        path = self._batch(
            "b1.ndjson",
            [
                {"payload": {"command": "sync", "rules": ["R1002"]}},
                {"payload": {"command": "sync", "rules": ["R1001"]}},
            ],
        )
        r1 = importer.import_file(self._store(), path)
        self.assertEqual(2, r1.inserted)
        self.assertEqual(0, r1.duplicates)
        # Re-import of the same shuttle file is a pure dedupe.
        r2 = importer.import_file(self._store(), path)
        self.assertEqual(0, r2.inserted)
        self.assertEqual(2, r2.duplicates)

    def test_bad_lines_counted(self):
        path = self._batch("b2.ndjson", [{"payload": {}}])
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("{not json\n")
            fp.write('{"id":"x","type":"error"}\n')  # missing fields
        r = importer.import_file(self._store(), path)
        self.assertEqual(1, r.inserted)
        self.assertEqual(2, r.bad)
        self.assertFalse(r.ok())

    def test_summary_aggregates(self):
        path = self._batch(
            "b3.ndjson",
            [
                {"payload": {"command": "sync", "rules": ["R1002"]}},
                {"payload": {"command": "sync", "rules": ["R1002"]}},
                {"type": "feedback", "payload": {"verdict": "resolved",
                                                 "rule": "R1002"}},
                {"type": "feedback", "payload": {"verdict": "unresolved",
                                                 "rule": "R1002"}},
                {"region": "isolated-r3",
                 "payload": {"command": "upload", "rules": ["R1009"]}},
            ],
        )
        importer.import_file(self._store(), path)
        data = importer.summary(self._store())
        self.assertEqual(5, data["total"])
        self.assertEqual(1, data["clients"])
        self.assertEqual("R1002", data["top_rules"][0][0])
        self.assertEqual(2, data["top_rules"][0][1])
        self.assertEqual(1, data["feedback"]["resolved"])
        self.assertEqual(
            {"isolated-r2", "isolated-r3"},
            {region for region, _ in data["by_region"]},
        )
        text = importer.render_summary(data, ruleset=rules.load_rules())
        self.assertIn("R1002", text)
        self.assertIn(u"问题解决率：50%", text)
        self.assertIn(u"高频问题", text)

    def test_summary_empty_store(self):
        data = importer.summary(self._store())
        self.assertEqual(0, data["total"])
        self.assertIn(u"存储为空", importer.render_summary(data))


def _encode_batch_for_test(queue):
    from repoai import telemetry as _tl

    return _tl._encode_batch(queue, queue.pending())


class TestDashboard(AiTestCase):
    """Built-in collector dashboard: routes and auth boundaries."""

    def _serve(self, token=""):
        from repoai import collector

        store = os.path.join(self.tmp, "store.sqlite")
        httpd = collector.make_server("127.0.0.1", 0, store, token)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return store, "http://127.0.0.1:%d" % httpd.server_address[1]

    def _get(self, url, token=None):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_index_served_without_token(self):
        _store, base = self._serve(token="tok1")
        code, body = self._get(base + "/")
        self.assertEqual(200, code)
        html = body.decode("utf-8")
        self.assertIn("repo AI 遥测看板", html)
        self.assertNotIn("tok1", html)  # never bake secrets into the page

    def test_data_endpoints_still_need_token(self):
        _store, base = self._serve(token="tok1")
        self.assertEqual(401, self._get(base + "/v1/stats")[0])
        self.assertEqual(401, self._get(base + "/v1/summary")[0])
        self.assertEqual(200, self._get(base + "/v1/stats",
                                        token="tok1")[0])
        # Rule titles are static library text: public on purpose.
        code, body = self._get(base + "/v1/rules")
        self.assertEqual(200, code)
        titles = json.loads(body.decode("utf-8"))
        self.assertTrue(titles.get("R1002"))

    def test_gap_radar_and_role_badge(self):
        # Rule-gap radar: failures with NO rule coverage surface as the
        # next rules to write; role badge keeps primary/backup boards
        # from being read interchangeably.
        from repoai import collector as _collector, importer as _imp

        store, base = self._serve(token="tok1")
        hit = json.dumps({"id": "g1", "ts": 1.0, "type": "error",
                          "region": "online", "client_id": "c",
                          "payload": {"command": "sync",
                                      "rules": ["R1021"]}})
        gap1 = json.dumps({"id": "g2", "ts": 1.0, "type": "error",
                           "region": "online", "client_id": "c",
                           "payload": {"command": "checkout",
                                       "exception": "CheckoutError"}})
        gap2 = json.dumps({"id": "g3", "ts": 1.0, "type": "error",
                           "region": "online", "client_id": "c",
                           "payload": {"command": "checkout",
                                       "exception": "CheckoutError"}})
        _imp.import_text(store, hit + "\n" + gap1 + "\n" + gap2 + "\n",
                         "seed")
        code, body = self._get(base + "/v1/summary", token="tok1")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual([["checkout", "CheckoutError", 2]],
                         data["top_unmatched"])
        self.assertEqual([["R1021", 1]], data["top_rules"])
        code, body = self._get(base + "/v1/stats", token="tok1")
        self.assertEqual("primary",
                         json.loads(body.decode("utf-8"))["role"])

        httpd2 = _collector.make_server("127.0.0.1", 0,
                                        os.path.join(self.tmp, "sB.sqlite"),
                                        "tok1", role="backup")
        t2 = threading.Thread(target=httpd2.serve_forever)
        t2.daemon = True
        t2.start()
        self.addCleanup(httpd2.server_close)
        self.addCleanup(httpd2.shutdown)
        base2 = "http://127.0.0.1:%d" % httpd2.server_address[1]
        code, body = self._get(base2 + "/healthz")  # public probe
        self.assertEqual("backup",
                         json.loads(body.decode("utf-8"))["role"])
        code, body = self._get(base2 + "/")
        self.assertIn("备用收集器", body.decode("utf-8"))

    def test_ingest_dir_folds_share_drops(self):
        # The shared-directory path end to end: a client snapshot written
        # while the collector was down gets folded into the store once
        # an admin (mounted share + --ingest-dir) polls any GET route.
        from repoai import collector as _collector

        store = os.path.join(self.tmp, "store2.sqlite")
        drop = os.path.join(self.tmp, "share2")
        os.makedirs(drop)
        httpd = _collector.make_server("127.0.0.1", 0, store, "",
                                       ingest_dir=drop)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync", "rules": ["R1021"]},
              region="online")
        q.add("error", {"command": "sync", "rules": ["R1021"]},
              region="online")
        with open(os.path.join(drop, "repo-events-abc.ndjson"), "w",
                  encoding="utf-8") as fp:
            fp.write(_encode_batch_for_test(q))
        self._get(base + "/healthz")  # any GET triggers the lazy scan
        code, body = self._get(base + "/v1/stats")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(2, data["total"])
        self.assertEqual(2, data["process"]["ingested"])
        # Second poll re-reads the same file: dedupe by id keeps 2.
        httpd.repo_collector._last_ingest = 0.0
        httpd.repo_collector._ingest_now()
        code, body = self._get(base + "/v1/stats")
        self.assertEqual(2, json.loads(body.decode("utf-8"))["total"])

    def test_summary_counts_rules_and_feedback(self):
        store, base = self._serve()
        self.user_cfg.SetString("ai.telemetry.endpoint", base)
        self.user_cfg.SetString("ai.telemetry.channel", "online")
        q = events.EventQueue(self.cfg.state_dir())
        q.add("hint-shown", {"command": "sync", "rules": ["R1021"]})
        q.add("hint-shown", {"command": "sync", "rules": ["R1021"]})
        q.add("hint-shown", {"command": "sync", "rules": ["R1002"]})
        q.add("feedback", {"verdict": "resolved", "rule": "R1021"})
        report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(4, report.sent)
        code, body = self._get(base + "/v1/summary")
        self.assertEqual(200, code)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual([["R1021", 2], ["R1002", 1]],
                         data["top_rules"])
        self.assertEqual(1, data["feedback_total"])
        self.assertEqual(100.0, data["resolved_rate"])


class TestCollector(AiTestCase):
    """Online telemetry collector: real HTTP round-trip (S6 server side)."""

    def _serve(self, token=""):
        from repoai import collector

        store = os.path.join(self.tmp, "store.sqlite")
        httpd = collector.make_server("127.0.0.1", 0, store, token)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        return store, "http://127.0.0.1:%d" % port

    def _seed(self, n):
        q = events.EventQueue(self.cfg.state_dir())
        for i in range(n):
            q.add("error", {"command": "sync", "rules": ["R100%d" % i]},
                  region="online")
        return q

    def test_flush_online_end_to_end(self):
        store, base = self._serve()
        self.user_cfg.SetString("ai.telemetry.endpoint", base)
        self.user_cfg.SetString("ai.telemetry.channel", "online")
        q = self._seed(3)
        report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(3, report.sent)
        # Events were actually marked sent client-side...
        self.assertEqual(0, q.count_pending())
        # ...and landed in the admin store (dedupe schema shared).
        data = importer.summary(store)
        self.assertEqual(3, data["total"])

    def test_at_least_once_dedupe(self):
        """Client retry of an identical batch must not double-insert."""
        store, base = self._serve()
        self.user_cfg.SetString("ai.telemetry.endpoint", base)
        self.user_cfg.SetString("ai.telemetry.channel", "online")
        q = self._seed(2)
        rows = q.pending()
        # Send the same encoded batch twice via raw urllib.
        body = telemetry._encode_batch(q, rows).encode("utf-8")
        for _ in range(2):
            req = urllib.request.Request(base + "/v1/events", data=body)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        data = importer.summary(store)
        self.assertEqual(2, data["total"])

    def test_token_required(self):
        store, base = self._serve(token="s3cret")
        self.user_cfg.SetString("ai.telemetry.endpoint", base)
        self.user_cfg.SetString("ai.telemetry.channel", "online")
        q = self._seed(2)
        # No token configured on the client -> rejected, queue kept.
        report = telemetry.flush(self.cfg, q)
        self.assertFalse(report.ok())
        self.assertIn("401", report.error)
        self.assertEqual(2, q.count_pending())
        # Correct token -> accepted.
        self.user_cfg.SetString("ai.telemetry.token", "s3cret")
        report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(0, q.count_pending())
        self.assertEqual(2, importer.summary(store)["total"])

    def test_healthz_and_bad_request(self):
        _store, base = self._serve()
        with urllib.request.urlopen(base + "/healthz", timeout=10) as r:
            self.assertEqual(200, r.status)
        err = None
        try:
            req = urllib.request.Request(base + "/v1/nope", data=b"x")
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            err = e.code
        self.assertEqual(404, err)

    def test_bad_lines_counted_not_fatal(self):
        _store, base = self._serve()
        body = (
            '{"id":"a","ts":1,"type":"error","payload":{"x":1}}\n'
            "{not json}\n"
        ).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/events",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read().decode("utf-8"))
        self.assertTrue(out["ok"])
        self.assertEqual(1, out["inserted"])
        self.assertEqual(1, out["bad"])

    # -- hardening: rotation + stats ------------------------------------
    def _post_line(self, base, ev_id, ts, token=None):
        line = json.dumps(
            {"id": ev_id, "ts": ts, "type": "error", "region": "online",
             "client_id": "c1", "payload": {"command": "sync"}}
        ).encode("utf-8")
        headers = {"Content-Type": "application/x-ndjson"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(base + "/v1/events", data=line,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _live_ids(self, store):
        rows = importer._rows(
            store, "SELECT id FROM events ORDER BY id")
        return [r[0] for r in rows]

    def test_rotation_archives_old_events(self):
        from repoai import collector

        store = os.path.join(self.tmp, "store.sqlite")
        c = collector.Collector(store, rotate_days=1)
        c.ingest(json.dumps({"id": "old1", "ts": time.time(),
                             "type": "error",
                             "payload": {"k": 1}}), "test")
        c.ingest(json.dumps({"id": "new1", "ts": time.time(),
                             "type": "error",
                             "payload": {"k": 2}}), "test")
        # Retention is by age-in-store (an offline client flushing last
        # week's events keeps them live); age old1 in the store itself.
        conn = importer._connect(store)
        conn.execute(
            "UPDATE events SET ingested_at = ingested_at - ?"
            " WHERE id = 'old1'",
            (5 * 86400,))
        conn.commit()
        conn.close()
        # Force rotation as if a new day arrived.
        c._last_rotate = ""
        c._maybe_rotate()
        ids = self._live_ids(store)
        self.assertNotIn("old1", ids)
        self.assertIn("new1", ids)
        arch_dir = os.path.join(self.tmp, "archive")
        files = os.listdir(arch_dir)
        self.assertEqual(1, len(files))
        arch = os.path.join(arch_dir, files[0])
        with open(arch, encoding="utf-8") as fp:
            lines = [x for x in fp if x.strip()]
        self.assertEqual(["old1"],
                         [json.loads(x)["id"] for x in lines])
        # Second rotation same day: no-op, archive not re-written.
        c._maybe_rotate()
        with open(arch, encoding="utf-8") as fp:
            self.assertEqual(1, len([x for x in fp if x.strip()]))
        # Archived data stays re-importable (same NDJSON contract).
        r = importer.import_text(store, "".join(lines), "reimport")
        self.assertEqual(1, r.inserted)

    def test_stats_requires_token_when_set(self):
        _store, base = self._serve(token="tok9")
        self._post_line(base, "s1", time.time(), token="tok9")
        req = urllib.request.Request(
            base + "/v1/stats", headers={"Authorization": "Bearer nope"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("stats must require the token")
        except urllib.error.HTTPError as e:
            self.assertEqual(401, e.code)
        req = urllib.request.Request(
            base + "/v1/stats",
            headers={"Authorization": "Bearer tok9"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read().decode("utf-8"))
        self.assertEqual(1, out["total"])

    def test_stats_reports_counts(self):
        _store, base = self._serve()
        self._post_line(base, "s3", time.time())
        with urllib.request.urlopen(base + "/v1/stats", timeout=10) as r:
            out = json.loads(r.read().decode("utf-8"))
        self.assertTrue(out["ok"])
        self.assertEqual(1, out["total"])
        self.assertEqual(1, out["clients"])
        self.assertEqual(1, out["by_region"].get("online"))
        self.assertEqual(1, len(out["daily"]))
        with urllib.request.urlopen(base + "/healthz", timeout=10) as r:
            self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])


class TestModels(AiTestCase):
    """repo models: gateway discovery, probe, and switching."""

    def setUp(self):
        super().setUp()
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")

    def _models_payload(self, *ids):
        return json.dumps({"data": [
            {"id": i, "object": "model"} for i in ids]})

    def _run(self, argv):
        from subcmds import all_commands

        cmd = all_commands["models"](repodir=self.repodir, manifest=None)
        opt, args = cmd.OptionParser.parse_args(argv)
        cmd.CommonValidateOptions(opt, args)
        return cmd.Execute(opt, args)

    def test_list_models(self):
        with self.stub_urlopen(
            lambda req: self._models_payload("model-a", "model-b")
        ) as rec:
            buf = self.capture_output()
            rc = self._run([])
        self.assertEqual(0, rc)
        out = buf.getvalue()
        self.assertIn(u"model-a", out)
        self.assertIn(u"model-b", out)
        self.assertIn(u"2 个", out)
        # URL joining: /models on a /v1-style base.
        self.assertEqual(
            "http://ai.internal:8080/v1/models",
            rec.requests[0].full_url)

    def test_host_root_base_joins_models(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        with self.stub_urlopen(
            lambda req: self._models_payload("m1")
        ) as rec:
            self.capture_output()
            self._run([])
        self.assertEqual(
            "http://ai.internal:8080/v1/models",
            rec.requests[0].full_url)

    def test_marks_current_and_fallback(self):
        self.user_cfg.SetString("ai.gateway.model", "model-a")
        with self.stub_urlopen(
            lambda req: self._models_payload("model-a", "other")
        ):
            buf = self.capture_output()
            self._run([])
        out = buf.getvalue()
        line = [l for l in out.splitlines()
                if l.strip().split(u" ")[0].split(u"[")[0] == "model-a"]
        self.assertTrue(line and u"当前" in line[0], out)

    def test_probe_flags_unavailable(self):
        def handler(req):
            if req.full_url.endswith("/models"):
                return self._models_payload("good", "bad")
            body = json.loads(req.data.decode("utf-8"))
            if body["model"] == "bad":
                return urllib.error.HTTPError(
                    req.full_url, 503, "no", {}, None)
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]})

        with self.stub_urlopen(handler):
            buf = self.capture_output()
            rc = self._run(["--probe"])
        self.assertEqual(0, rc)
        out = buf.getvalue()
        lines = {l.split()[0]: l for l in out.splitlines()
                 if l.strip().startswith(("good", "bad"))}
        self.assertIn("OK", lines["good"])
        self.assertIn(u"不可用", lines["bad"])

    def test_use_probes_then_saves(self):
        def handler(req):
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]})

        with self.stub_urlopen(handler):
            buf = self.capture_output()
            rc = self._run(["--use", "model-b"])
        self.assertEqual(0, rc)
        self.assertIn(u"已切换", buf.getvalue())
        # Persisted to the user config (same key the gateway reads);
        # a *fresh* GitConfig, the fixture's instance has a stale cache.
        fresh = GitConfig(configfile=self.user_cfg.file)
        self.assertEqual("model-b",
                         fresh.GetString("ai.gateway.model"))

    def test_use_refuses_to_save_broken_model(self):
        def handler(req):
            return urllib.error.HTTPError(
                req.full_url, 503, "no", {}, None)

        with self.stub_urlopen(handler):
            buf = self.capture_output()
            rc = self._run(["--use", "model-b"])
        self.assertEqual(1, rc)
        self.assertIn(u"未切换", buf.getvalue())
        fresh = GitConfig(configfile=self.user_cfg.file)
        self.assertIsNone(fresh.GetString("ai.gateway.model"))

    def test_no_url_is_clear_error(self):
        self.user_cfg.SetString("ai.gateway.url", "")
        buf = self.capture_output()
        rc = self._run([])
        self.assertEqual(1, rc)
        self.assertIn(u"ai.gateway.url", buf.getvalue())

    def test_gateway_down_friendly_error(self):
        with self.stub_urlopen(
            lambda req: urllib.error.URLError("refused")):
            buf = self.capture_output()
            rc = self._run([])
        self.assertEqual(1, rc)
        self.assertIn(u"获取模型列表失败", buf.getvalue())

    def test_degrade_hint_mentions_models_command(self):
        """404/503 on a real ask must point the user at repo models."""
        self.user_cfg.SetString("ai.gateway.model-fallback", "")
        with self.stub_urlopen(
            lambda req: urllib.error.HTTPError(
                req.full_url, 503, "no", {}, None)):
            buf = self.capture_output()
            out = gateway.ask_question(self.cfg, u"x", [],
                                       out=sys.stderr)
        self.assertIsNone(out)
        self.assertIn(u"repo models", buf.getvalue())


class TestPilotCheck(AiTestCase):
    """contrib/pilot_check.py self-check script behavior."""

    def _load(self):
        import importlib.util

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "contrib", "pilot_check.py")
        spec = importlib.util.spec_from_file_location("pilot_check", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_main_offline_json_exit0(self):
        mod = self._load()
        buf = self.capture_output()
        rc = mod.main(["--offline", "--json"])
        self.assertEqual(0, rc)
        data = json.loads(buf.getvalue())
        names = {r["name"] for r in data["rows"]}
        self.assertIn(u"Python 版本", names)
        self.assertIn(u"命令注册", names)
        self.assertEqual(0, data["counts"]["FAIL"])

    def test_privacy_spot_check_flags_raw_text(self):
        mod = self._load()
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync", "question": "raw text"})
        r = mod.Report()
        mod.check_privacy(r, self.cfg)
        self.assertEqual(1, r.count("FAIL"))

    def test_privacy_spot_check_passes_clean(self):
        mod = self._load()
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync", "rules": ["R1002"]})
        r = mod.Report()
        mod.check_privacy(r, self.cfg)
        self.assertEqual(1, r.count("OK"))
        self.assertEqual(0, r.count("FAIL"))

    def test_content_counts_builtin_data(self):
        mod = self._load()
        r = mod.Report()
        mod.check_content(r)
        self.assertEqual(0, r.count("FAIL"), r.rows)


class TestDiscoverability(AiTestCase):
    """`repo help` integration + man pages stay in sync with commands."""

    def test_help_lists_ai_commands_when_enabled(self):
        self.user_cfg.SetString("ai.enabled", "true")
        from subcmds import all_commands

        buf = self.capture_output()
        all_commands["help"](repodir=self.repodir,
                             manifest=None).PrintCommonCommandsBody()
        out = buf.getvalue()
        self.assertIn("AI assistant commands", out)
        self.assertIn("diagnose", out)

    def test_help_original_when_ai_disabled(self):
        self.user_cfg.SetString("ai.enabled", "false")
        from subcmds import all_commands

        buf = self.capture_output()
        all_commands["help"](repodir=self.repodir,
                             manifest=None).PrintCommonCommandsBody()
        out = buf.getvalue()
        # Frozen view: original common list, no AI section at all.
        self.assertNotIn("AI assistant", out)
        for name in ("sync", "upload", "status"):
            self.assertIn("\n  %s  " % name, out)
        self.assertIn("See 'repo help <command>'", out)

    def test_man_pages_exist_for_ai_commands(self):
        """Every AI command ships a man page (repo-<name>.1) and is
        listed in the repo.1 index; run release/gen-ai-manpages.py."""
        from subcmds import all_commands

        mandir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "man")
        with open(os.path.join(mandir, "repo.1"), encoding="utf-8") as fp:
            index = fp.read()
        names = [
            n for n, c in all_commands.items()
            if getattr(c, "IS_AI_COMMAND", False)
        ]
        self.assertGreaterEqual(len(names), 10)
        for name in names:
            path = os.path.join(mandir, "repo-%s.1" % name)
            self.assertTrue(os.path.exists(path), path)
            with open(path, encoding="utf-8") as fp:
                page = fp.read()
            self.assertIn("manual page for repo %s" % name, page)
            self.assertIn("\\fI\\,%s\\/\\fR" % name, page)
            # Index entry: bare name line in repo.1
            self.assertIn("\n%s\n" % name, index)


class TestCommands(AiTestCase):
    """Smoke coverage for the five new subcommands' Execute paths."""

    def _run(self, name, argv):
        from subcmds import all_commands

        cmd = all_commands[name](repodir=self.repodir, manifest=None)
        opt, args = cmd.OptionParser.parse_args(argv)
        cmd.CommonValidateOptions(opt, args)
        return cmd.Execute(opt, args)

    def test_rules_lists(self):
        buf = self.capture_output()
        self.assertIsNone(self._run("rules", []))
        self.assertIn("R1002", buf.getvalue())

    def test_rules_export_roundtrip(self):
        path = os.path.join(self.tmp, "pkg.json")
        buf = self.capture_output()
        self.assertEqual(0, self._run("rules", ["--export", path]))
        with open(path, encoding="utf-8") as fp:
            pkg = json.load(fp)
        self.assertTrue(pkg["rules"])
        self.assertIn(u"已导出", buf.getvalue())
        # Exported packages are importable elsewhere (isolated zones).
        buf2 = self.capture_output()
        self.assertIsNone(self._run("rules", ["--import", path]))
        self.assertIn(u"规则包已导入", buf2.getvalue())

    def test_rules_export_local_only_empty(self):
        path = os.path.join(self.tmp, "local.json")
        buf = self.capture_output()
        self.assertEqual(0, self._run("rules", ["--export", path,
                                                "--local-only"]))
        with open(path, encoding="utf-8") as fp:
            self.assertEqual([], json.load(fp)["rules"])
        self.assertIn(u"管理员规则", buf.getvalue())

    def test_rules_import_export_conflict(self):
        buf = self.capture_output()
        self.assertEqual(
            1, self._run("rules", ["--import", "a.json",
                                   "--export", "b.json"])
        )

    def test_faq_search(self):
        buf = self.capture_output()
        self.assertEqual(0, self._run("faq", ["部分同步"]))
        self.assertIn("1001", buf.getvalue())

    def test_faq_does_not_queue_query_text(self):
        """Privacy invariant: search words stay local, only metadata."""
        buf = self.capture_output()
        self._run("faq", [u"部分同步"])
        q = events.EventQueue(self.cfg.state_dir())
        kb_events = [p for _i, _t, t, _r, p in q.pending()
                     if t == "kb-search"]
        self.assertTrue(kb_events)
        blob = json.dumps(kb_events, ensure_ascii=False)
        self.assertNotIn(u"部分同步", blob)
        for p in kb_events:
            self.assertNotIn("query", p)

    def test_faq_requires_query(self):
        with self.assertRaises(SystemExit):
            self._run("faq", [])

    def test_explain_rule_id(self):
        buf = self.capture_output()
        self.assertEqual(0, self._run("explain", ["R1002"]))
        self.assertIn(u"排查步骤", buf.getvalue())

    def test_explain_command_name(self):
        buf = self.capture_output()
        self.assertEqual(0, self._run("explain", ["sync"]))
        self.assertIn("repo help sync", buf.getvalue())

    def test_explain_error_text(self):
        buf = self.capture_output()
        self.assertEqual(
            0,
            self._run(
                "explain",
                ["error: Cannot checkout foo/bar: revision conflict"],
            ),
        )
        self.assertIn("R1002", buf.getvalue())

    def test_explain_unknown(self):
        buf = self.capture_output()
        self.assertEqual(1, self._run("explain", ["zzz-nomatch-xyz"]))

    def test_report_bundle(self):
        buf = self.capture_output()
        self.assertEqual(0, self._run("report", []))
        self.assertIn(".zip", buf.getvalue())

    def test_feedback_flag(self):
        buf = self.capture_output()
        self.assertEqual(0, self._run("feedback", ["--resolved"]))
        q = events.EventQueue(self.cfg.state_dir())
        self.assertIn(
            "feedback",
            [t for _i, _t, t, _r, _p in q.pending()],
        )

    def test_telemetry_status(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync"})
        buf = self.capture_output()
        self.assertEqual(0, self._run("telemetry", []))
        out = buf.getvalue()
        self.assertIn(u"待上报事件：1", out)
        self.assertIn(u"通道", out)

    def test_telemetry_export(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync"})
        path = os.path.join(self.tmp, "out.ndjson")
        buf = self.capture_output()
        self.assertEqual(0, self._run("telemetry", ["--export", path]))
        self.assertTrue(os.path.exists(path))
        # Export must not consume the queue (import side de-dupes).
        self.assertEqual(1, q.count_pending())

    def test_telemetry_export_empty(self):
        path = os.path.join(self.tmp, "out.ndjson")
        buf = self.capture_output()
        self.assertEqual(0, self._run("telemetry", ["--export", path]))
        self.assertFalse(os.path.exists(path))
        self.assertIn(u"没有待上报事件", buf.getvalue())

    def test_telemetry_flush_offline(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync"})
        buf = self.capture_output()
        self.assertEqual(0, self._run("telemetry", ["--flush"]))
        self.assertIn("offline", buf.getvalue())
        self.assertEqual(1, q.count_pending())

    def test_telemetry_flush_blocked_when_disabled(self):
        self.user_cfg.SetString("ai.telemetry.enabled", "false")
        buf = self.capture_output()
        self.assertEqual(1, self._run("telemetry", ["--flush"]))
        self.assertIn(u"上报已关闭", buf.getvalue())

    def test_telemetry_flush_online_via_command(self):
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://logs.internal:4317"
        )
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync"})
        buf = self.capture_output()
        with self.stub_urlopen(lambda req: b'{"ok":true}') as rec:
            self.assertEqual(0, self._run("telemetry", ["--flush"]))
        self.assertEqual(1, len(rec.requests))
        self.assertEqual(0, q.count_pending())
        self.assertIn(u"已上报 1 条事件", buf.getvalue())

    def test_backup_endpoint_off_by_default(self):
        # Sites are partitioned (only-60 vs only-110 users), so the
        # fallback collector is OFF unless explicitly configured: a
        # primary outage must NOT silently cross-post to another group.
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://collector.example:8080")
        urls = []

        def down(req):
            urls.append(req.full_url)
            raise urllib.error.URLError("110 down")

        with self.stub_urlopen(down):
            report = telemetry.flush(self.cfg, self._seed2())
        self.assertFalse(report.ok())
        self.assertIsNone(report.fallback_endpoint)
        self.assertEqual(1, len(urls))  # primary only, never 60/13

    def test_backup_endpoint_opt_in_fallback(self):
        # Explicit opt-in (machine reachable from BOTH boxes): 110
        # down -> the batch goes to the configured backup; events stay
        # pending because only the primary owns the ack.
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://collector.example:8080")
        self.user_cfg.SetString(
            "ai.telemetry.fallback-endpoint",
            "http://collector-backup.example:8080")
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync", "rules": ["R1021"]},
              region="online")
        q.add("hint-shown", {"command": "sync", "rules": ["R1021"]},
              region="online")
        urls = []

        def handler(req):
            urls.append(req.full_url)
            if "collector-backup.example" in req.full_url:
                return b'{"ok":true}'
            raise urllib.error.URLError("110 down")

        with self.stub_urlopen(handler):
            report = telemetry.flush(self.cfg, q)
        self.assertTrue(report.ok(), report.error)
        self.assertEqual(2, report.sent)
        self.assertEqual(2, q.count_pending())  # only 110 owns the ack
        self.assertIn("collector-backup.example:8080", report.fallback_endpoint)
        self.assertEqual(2, len(urls))  # tried primary first

    def test_backup_endpoint_relocate(self):
        # Settable: policy can point the fallback at another box.
        self.user_cfg.SetString(
            "ai.telemetry.endpoint", "http://collector.example:8080")
        self.user_cfg.SetString(
            "ai.telemetry.fallback-endpoint", "http://backup2:8081")
        urls = []

        def down(req):
            urls.append(req.full_url)
            raise urllib.error.URLError("down")

        with self.stub_urlopen(down):
            report = telemetry.flush(self.cfg, self._seed2())
        self.assertFalse(report.ok())  # both down -> loud error
        self.assertIn("http://backup2:8081/v1/events", urls[-1])

    def _seed2(self):
        q = events.EventQueue(self.cfg.state_dir())
        q.add("error", {"command": "sync"}, region="online")
        return q

    def test_aiadmin_pull_merges_backup_store(self):
        # Closed loop: outage events landed on the backup (its own
        # store); after 110 recovers, ONE command folds them back --
        # shared ids dedupe, new ones land, board is complete again.
        from repoai import collector as _collector

        bstore = os.path.join(self.tmp, "storeB.sqlite")
        shared = json.dumps({"id": "a1", "ts": 1.0, "type": "error",
                             "region": "online", "client_id": "c1",
                             "payload": {"command": "sync"}})
        outage = json.dumps({"id": "b2", "ts": 2.0, "type": "error",
                             "region": "online", "client_id": "c1",
                             "payload": {"command": "sync"}})
        _importer_text = __import__(
            "repoai.importer", fromlist=["importer"])
        _importer_text.import_text(bstore, shared + "\n", "seed")
        httpd = _collector.make_server("127.0.0.1", 0, bstore, "tokP")
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        # Backup got the outage batch (simulates client fallback).
        import urllib.request as _rq
        req = _rq.Request(base + "/v1/events",
                          data=(shared + "\n" + outage + "\n")
                          .encode("utf-8"))
        req.add_header("Authorization", "Bearer tokP")
        with _rq.urlopen(req, timeout=10) as resp:
            self.assertEqual(200, resp.status)

        astore = os.path.join(self.tmp, "storeA.sqlite")
        _importer_text.import_text(astore, shared + "\n", "seed")
        self.user_cfg.SetString("ai.admin.store", astore)
        self.user_cfg.SetString("ai.telemetry.token", "tokP")
        self.user_cfg.SetString("ai.telemetry.fallback-endpoint", base)
        buf = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["pull"]))
        out = buf.getvalue()
        self.assertIn(u"新增 1", out)   # outage-only event lands
        self.assertIn(u"重复 1", out)   # shared one dedupes
        # Wrong token must not leak the backup store.  Env var wins
        # over the (class-cached) RepoConfig file for this override.
        os.environ["REPO_AI_TELEMETRY_TOKEN"] = "nope"
        self.addCleanup(os.environ.pop, "REPO_AI_TELEMETRY_TOKEN", None)
        buf2 = self.capture_output()
        self.assertEqual(1, self._run("ai-admin", ["pull"]))
        self.assertIn(u"HTTP 401", buf2.getvalue())

    def test_summary_shows_rule_gap_radar(self):
        store = os.path.join(self.tmp, "store.sqlite")
        hit = json.dumps({"id": "s1", "ts": 1.0, "type": "error",
                          "region": "online", "client_id": "c",
                          "payload": {"command": "sync",
                                      "rules": ["R1021"]}})
        gap = json.dumps({"id": "s2", "ts": 1.0, "type": "error",
                          "region": "online", "client_id": "c",
                          "payload": {"command": "checkout",
                                      "exception": "CheckoutError"}})
        from repoai import importer as _imp
        _imp.import_text(store, hit + "\n" + gap + "\n", "seed")
        self.user_cfg.SetString("ai.admin.store", store)
        buf = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["summary"]))
        out = buf.getvalue()
        self.assertIn(u"未命中缺口", out)
        self.assertIn("CheckoutError", out)
        self.assertLess(out.index("R1021"), out.index(u"未命中缺口"))

    def test_aiadmin_pull_twice_folds_once(self):
        # Second pull must be all-duplicates: the fold is idempotent, so
        # a cron line is safe and an impatient admin cannot double-count.
        from repoai import collector as _collector
        from repoai import importer as _imp

        bstore = os.path.join(self.tmp, "storeB.sqlite")
        ev = json.dumps({"id": "p1", "ts": 1.0, "type": "error",
                         "region": "online", "client_id": "c",
                         "payload": {"command": "sync"}})
        _imp.import_text(bstore, ev + "\n", "seed")
        httpd = _collector.make_server("127.0.0.1", 0, bstore, "tokP")
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        self.user_cfg.SetString(
            "ai.admin.store", os.path.join(self.tmp, "storeA.sqlite"))
        self.user_cfg.SetString("ai.telemetry.token", "tokP")
        self.user_cfg.SetString("ai.telemetry.fallback-endpoint", base)
        buf = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["pull"]))
        self.assertIn(u"新增 1", buf.getvalue())
        buf2 = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["pull"]))
        out2 = buf2.getvalue()
        self.assertIn(u"新增 0", out2)
        self.assertIn(u"重复 1", out2)

    def test_aiadmin_import_dir_and_summary(self):
        # One batch file in a directory, plus an unrelated file that must
        # be ignored by the directory glob.
        batch = os.path.join(self.tmp, "exchange")
        os.makedirs(batch)
        ev = json.dumps(
            {"id": "x1", "ts": 1.0, "type": "error", "region": "isolated-r2",
             "client_id": "c", "payload": {"command": "sync",
                                           "rules": ["R1002"]}}
        )
        with open(os.path.join(batch, "a.ndjson"), "w",
                  encoding="utf-8") as fp:
            fp.write(ev + "\n")
        with open(os.path.join(batch, "notes.txt"), "w",
                  encoding="utf-8") as fp:
            fp.write("ignore me\n")
        store = os.path.join(self.tmp, "store.sqlite")
        self.user_cfg.SetString("ai.admin.store", store)
        buf = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["import", batch]))
        self.assertIn(u"新增 1", buf.getvalue())
        buf2 = self.capture_output()
        self.assertEqual(0, self._run("ai-admin", ["summary"]))
        out = buf2.getvalue()
        self.assertIn("R1002", out)
        self.assertIn(u"区域分布", out)

    def test_aiadmin_summary_missing_store(self):
        self.user_cfg.SetString(
            "ai.admin.store", os.path.join(self.tmp, "nope.sqlite"))
        buf = self.capture_output()
        self.assertEqual(1, self._run("ai-admin", ["summary"]))
        self.assertIn(u"事件存储不存在", buf.getvalue())

    def test_aiadmin_unknown_subcommand(self):
        buf = self.capture_output()
        self.assertEqual(1, self._run("ai-admin", ["bogus"]))

    # -- repo ask ------------------------------------------------------
    def test_ask_offline_answers_from_kb(self):
        """Isolated region: same command shape, local rule/KB answer."""
        self.user_cfg.SetString("ai.gateway.url", "")
        buf = self.capture_output()
        rc = self._run("ask", ["upload", "被", "拒绝", "怎么排查"])
        out = buf.getvalue()
        self.assertEqual(0, rc)
        self.assertIn(u"本地知识库回答", out)
        self.assertIn("R1009", out)

    def test_ask_offline_miss_exit_1(self):
        self.user_cfg.SetString("ai.gateway.url", "")
        buf = self.capture_output()
        rc = self._run("ask", ["how", "to", "brew", "espresso"])
        self.assertEqual(1, rc)
        self.assertIn(u"没有找到", buf.getvalue())

    def test_ask_online_ai_path(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        answer = {
            "choices": [{"message": {"content": "用 repo sync foo/bar"}}]
        }
        with self.stub_urlopen(lambda req: json.dumps(answer)):
            buf = self.capture_output()
            rc = self._run("ask", ["只想同步一个模块怎么办"])
        out = buf.getvalue()
        self.assertEqual(0, rc)
        self.assertIn("repo sync foo/bar", out)
        self.assertIn(u"来源：AI 服务", out)

    def test_ask_question_text_never_queued(self):
        """Privacy: the 'ask' event carries the source, not the text."""
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        answer = {"choices": [{"message": {"content": "ok"}}]}
        with self.stub_urlopen(lambda req: json.dumps(answer)):
            buf = self.capture_output()
            self._run("ask", ["我的秘密项目代号是菠萝123 怎么同步"])
        q = events.EventQueue(self.cfg.state_dir())
        ask_events = [
            p for _i, _t, t, _r, p in q.pending() if t == "ask"
        ]
        self.assertEqual(1, len(ask_events))
        blob = json.dumps(ask_events[0], ensure_ascii=False)
        self.assertNotIn(u"菠萝", blob)
        self.assertNotIn("question", blob)

    def test_ask_gateway_down_falls_back_local(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        err = urllib.error.URLError("refused")
        with self.stub_urlopen(lambda req: err):
            buf = self.capture_output()
            rc = self._run("ask", ["upload 被 拒绝 怎么办"])
        out = buf.getvalue()
        self.assertIn(u"AI 服务暂不可用", out)  # loud...
        self.assertEqual(0, rc)  # ...but the local answer still works
        self.assertIn(u"本地知识库回答", out)

    # -- repo ask: multi-turn ------------------------------------------
    def _answer_with(self, text):
        return lambda req: json.dumps(
            {"choices": [{"message": {"content": text}}]}
        )

    def test_ask_multi_turn_carries_history(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        with self.stub_urlopen(self._answer_with(u"回答一")):
            buf1 = self.capture_output()
            self._run("ask", [u"第一个问题"])
        self.assertIn(u"-c 可继续追问", buf1.getvalue())
        # Follow-up with -c must include the previous exchange.
        with self.stub_urlopen(self._answer_with(u"回答二")) as rec:
            buf2 = self.capture_output()
            self._run("ask", ["-c", u"那第二步呢"])
        body = json.loads(rec.requests[0].data.decode("utf-8"))
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(
            ["system", "user", "assistant", "user"], roles
        )
        self.assertIn(u"第一个问题", body["messages"][1]["content"])
        self.assertEqual(u"回答一", body["messages"][2]["content"])
        self.assertIn(u"第 2 轮", buf2.getvalue())

    def test_ask_continue_without_history_noted(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        with self.stub_urlopen(self._answer_with(u"x")) as rec:
            buf = self.capture_output()
            self._run("ask", ["-c", u"继续"])
        self.assertIn(u"没有找到上一轮会话", buf.getvalue())
        # ...and it still asked a fresh single-turn question.
        body = json.loads(rec.requests[0].data.decode("utf-8"))
        self.assertEqual(
            ["system", "user"],
            [m["role"] for m in body["messages"]],
        )

    def test_ask_session_reset(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        with self.stub_urlopen(self._answer_with(u"a1")):
            buf = self.capture_output()
            self._run("ask", [u"问题一"])
        path = os.path.join(self.cfg.state_dir(), "ask_session.json")
        self.assertTrue(os.path.exists(path))
        buf2 = self.capture_output()
        self.assertEqual(0, self._run("ask", ["--reset"]))
        self.assertIn(u"已清除", buf2.getvalue())
        self.assertFalse(os.path.exists(path))

    def test_ask_session_capped_turns(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        for i in range(8):
            argv = [u"q0"] if i == 0 else ["-c", u"q%d" % i]
            with self.stub_urlopen(self._answer_with(u"a%d" % i)):
                self.capture_output()
                self._run("ask", argv)
        path = os.path.join(self.cfg.state_dir(), "ask_session.json")
        with open(path, encoding="utf-8") as fp:
            turns = json.load(fp)["turns"]
        self.assertLessEqual(len(turns), 8)
        self.assertEqual(u"a7", turns[-1]["content"])

    def test_ask_session_not_reported(self):
        """Session file exists locally but nothing of it is queued."""
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        with self.stub_urlopen(self._answer_with(u"本地留存")):
            buf = self.capture_output()
            self._run("ask", [u"仅本地的问题"])
        q = events.EventQueue(self.cfg.state_dir())
        blob = json.dumps(
            [p for _i, _t, t, _r, p in q.pending()], ensure_ascii=False
        )
        self.assertNotIn(u"仅本地", blob)
        self.assertNotIn(u"本地留存", blob)

    def test_ask_requires_question(self):
        buf = self.capture_output()
        self.assertEqual(1, self._run("ask", []))
        self.assertIn(u"请带上问题", buf.getvalue())

    # -- repo diagnose --------------------------------------------------
    def test_diagnose_no_history(self):
        buf = self.capture_output()
        self.assertEqual(1, self._run("diagnose", []))
        self.assertIn(u"没有失败记录", buf.getvalue())

    def _seed_failure(self):
        from error import SyncError

        ctx = hook.begin(self.repodir, "sync", [])
        hook.maybe_print_hint(
            ctx,
            SyncError("x", aggregate_errors=[Exception("revision conflict")]),
            1,
        )

    def test_diagnose_offline_full_steps(self):
        self.user_cfg.SetString("ai.gateway.url", "")  # offline path
        self._seed_failure()
        q = events.EventQueue(self.cfg.state_dir())
        before = q.count_pending()
        buf = self.capture_output()
        self.assertEqual(0, self._run("diagnose", []))
        out = buf.getvalue()
        self.assertIn("R1002", out)
        self.assertIn(u"根因", out)
        # Read-only: re-analysis must not queue new events.
        self.assertEqual(before, q.count_pending())

    def test_diagnose_online_adds_ai_section(self):
        self.user_cfg.SetString("ai.gateway.url", "http://ai.internal:8080")
        self._seed_failure()
        answer = {"choices": [{"message": {"content": "检查 manifest 分支"}}]}
        with self.stub_urlopen(lambda req: json.dumps(answer)):
            buf = self.capture_output()
            self.assertEqual(0, self._run("diagnose", []))
        out = buf.getvalue()
        self.assertIn(u"AI 分析（AI 服务提供）", out)
        self.assertIn(u"检查 manifest 分支", out)

    def test_all_commands_disabled_exit_1(self):
        self.user_cfg.SetString("ai.enabled", "false")
        for name in (
            "rules", "faq", "explain", "report", "feedback", "telemetry",
            "ai-admin", "doctor", "ask", "diagnose", "models",
        ):
            buf = self.capture_output()
            self.assertEqual(1, self._run(name, []), name)


if __name__ == "__main__":
    unittest.main()
