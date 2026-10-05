#!/usr/bin/env python3
"""Pipeline tests: is the GitHub Actions setup still safe to trust?

A reminder whose workflow silently stops running is worse than no reminder: you
believe you are covered and you are not. These tests fail loudly the moment the
workflows drift.

Structural parsing needs PyYAML (installed in CI). Every check that can be done
with plain text runs regardless, so this suite is useful even without it.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reminder.config import load_config, parse_hhmm  # noqa: E402

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
REMINDER_WORKFLOW = WORKFLOWS / "daily-reminder.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"

SHA = re.compile(r"^[0-9a-f]{40}$")

try:
    import yaml

    HAVE_YAML = True
except ImportError:  # structural checks are skipped, text checks still run
    HAVE_YAML = False


def triggers(doc: dict) -> dict:
    """`on:` parses as the boolean True under YAML 1.1."""
    return doc[True] if True in doc else doc["on"]


class ReminderWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = REMINDER_WORKFLOW.read_text(encoding="utf-8")

    def test_p1_every_action_is_pinned_to_a_commit_sha(self):
        """A tag like @v4 can be repointed at new code. A SHA cannot."""
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses, "no actions referenced")
        for ref in uses:
            repo, _, ref_part = ref.partition("@")
            with self.subTest(action=ref):
                self.assertTrue(
                    SHA.match(ref_part),
                    f"{repo} is not pinned to a SHA: {ref}. Pin it and note the version.",
                )

    def test_p2_runs_the_package_entry_point_that_actually_exists(self):
        """`python -m reminder` only works if the package is really there."""
        self.assertIn("python -m reminder --scheduled", self.text)
        self.assertTrue((PROJECT_ROOT / "reminder" / "__main__.py").exists())
        self.assertFalse(
            (PROJECT_ROOT / "reminder.py").exists(),
            "the flat module was replaced by the package; do not bring it back",
        )

    def test_p3_token_is_injected_at_run_time_and_never_stored_in_the_workflow(self):
        """The token comes from Infisical now, so it is in no repository at all.

        This used to assert `${{ secrets.TELEGRAM_BOT_TOKEN }}` was wired up.
        Secrets live in Infisical, so the stronger invariant is the opposite
        one: the workflow must name no GitHub secret for the token, because
        nothing here can leak with it. The only credential GitHub still holds
        is the machine identity that fetches the values.
        """
        self.assertNotIn("secrets.TELEGRAM_BOT_TOKEN", self.text)
        # \S after the negative lookahead stops backtracking from matching the
        # whitespace before a legitimate ${{ secrets... }} value.
        hardcoded = re.search(r"TELEGRAM_BOT_TOKEN:\s*(?!\$\{\{)\S", self.text)
        self.assertIsNone(
            hardcoded, f"token looks hardcoded: {hardcoded and hardcoded.group(0)}"
        )
        self.assertNotRegex(self.text, r"\d{8,10}:[A-Za-z0-9_-]{30,}")
        # ... and the values do arrive from somewhere, so deleting the fetch
        # step cannot pass by leaving no secret wiring at all.
        self.assertIn("Infisical/secrets-action@", self.text)
        referenced = set(re.findall(r"secrets\.(\w+)", self.text))
        self.assertLessEqual(
            referenced,
            {"INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"},
            f"only the Infisical identity belongs in GitHub Secrets, found {referenced}",
        )

    def test_p4_has_least_privilege_permissions(self):
        # (?m) inline: assertRegex's third argument is the failure message,
        # not the flags.
        self.assertRegex(self.text, r"(?m)^permissions:\s*\n\s+contents:\s*read\s*$")

    def test_p5_has_a_timeout_so_a_hang_cannot_burn_a_runner(self):
        self.assertRegex(self.text, r"timeout-minutes:\s*\d+")

    def test_p6_serialises_runs_so_two_reminders_cannot_overlap(self):
        self.assertIn("concurrency:", self.text)

    def test_p7_installs_tzdata_so_the_clock_cannot_fall_back_to_utc(self):
        self.assertIn("pip install tzdata", self.text)

    def test_p8_has_a_manual_trigger_for_recovery(self):
        self.assertIn("workflow_dispatch:", self.text)

    def test_p9_fails_fast_with_a_clear_error_when_the_secret_is_missing(self):
        self.assertIn("::error::", self.text)

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed; text checks still ran")
    def test_p10_cron_hour_matches_the_configured_cairo_time(self):
        """The drift that makes a reminder fire at the wrong hour, caught in CI."""
        doc = yaml.safe_load(self.text)
        match = re.search(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+\*"', self.text)
        self.assertIsNotNone(match, "no cron expression found")
        utc_hour = int(match.group(2))
        cairo_hour, cairo_minute = parse_hhmm(
            load_config(PROJECT_ROOT / "config.json")["reminder_time"]
        )
        self.assertEqual(int(match.group(1)), 0, "schedule must fire on the hour")
        self.assertEqual(utc_hour + 2, cairo_hour, "UTC hour does not match config.json")
        self.assertEqual(cairo_minute, 0)
        self.assertTrue(triggers(doc).get("schedule"))

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed; text checks still ran")
    def test_p11_job_shape_is_sane(self):
        doc = yaml.safe_load(self.text)
        job = doc["jobs"]["remind"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertIn("steps", job)
        self.assertGreaterEqual(len(job["steps"]), 4)


class CiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_c1_runs_on_push_and_pull_request(self):
        self.assertRegex(self.text, r"(?m)^on:\s*\n")
        self.assertIn("push:", self.text)
        self.assertIn("pull_request:", self.text)

    def test_c2_actually_runs_the_test_suite(self):
        self.assertIn("python -m unittest discover -s tests -t .", self.text)

    def test_c3_actions_are_pinned_to_shas(self):
        for ref in re.findall(r"uses:\s*(\S+)", self.text):
            _, _, ref_part = ref.partition("@")
            with self.subTest(action=ref):
                self.assertTrue(SHA.match(ref_part), f"{ref} is not pinned to a SHA")

    def test_c4_has_a_timeout(self):
        self.assertRegex(self.text, r"timeout-minutes:\s*\d+")

    def test_c5_cancels_stale_runs_of_the_same_branch(self):
        self.assertIn("cancel-in-progress: true", self.text)

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed; text checks still ran")
    def test_c6_smoke_tests_a_mode_that_needs_no_credentials(self):
        """--dry-run must stay runnable, or CI would need a secret to test."""
        doc = yaml.safe_load(self.text)
        runs = "\n".join(step.get("run", "") for step in doc["jobs"]["tests"]["steps"])
        self.assertIn("python -m reminder --dry-run", runs)


class RepositoryHygieneTest(unittest.TestCase):
    @staticmethod
    def _ignored(relative: str) -> bool:
        """Whether `.gitignore` covers this path.

        A secret inside an ignored file is not a leak: `.env` is *meant* to hold a
        real token locally and is never committed. So the scan has to ask the same
        question git asks, otherwise the check fires on the one file that is doing
        its job and teaches everyone to ignore it.
        """
        patterns = [
            line.strip()
            for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        return any(
            fnmatch.fnmatch(relative, pattern.rstrip("/"))
            or relative.startswith(pattern.rstrip("/") + "/")
            or fnmatch.fnmatch(Path(relative).name, pattern.rstrip("/"))
            for pattern in patterns
        )

    def test_h1_gitignore_covers_runtime_and_test_artifacts(self):
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("reminder.log", "state.json", "confirm.json", "__pycache__/"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)

    def test_h2_no_token_or_chat_id_is_committed(self):
        """A leaked token would be a real disclosure, not a lint nit.

        Scans code, config and workflows, skipping anything `.gitignore` covers.
        Markdown is deliberately excluded: the setup guide has to show a
        token-shaped example for the reader to recognise. Limitation: a token
        pasted into prose would not be caught here.
        """
        offenders = []
        for path in sorted(PROJECT_ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".json", ".yml", ""}:
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            if self._ignored(relative):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", text):
                offenders.append(relative)
        self.assertEqual(offenders, [], f"possible committed secrets: {offenders}")

    def test_h5_the_secret_scan_still_catches_a_leak(self):
        """Guards the guard.

        The scan now honours `.gitignore`, so prove it did not become a no-op: a
        token in a *tracked* file must still be reported, and `.env` must be
        recognised as ignored. Without this, "0 offenders" could mean "0 files
        examined".
        """
        token = "123456789:AA" + "z" * 32
        for probe in ("secrets_probe.py", "nested/probe.json", "workflow-probe.yml"):
            with self.subTest(probe=probe):
                self.assertFalse(
                    self._ignored(probe),
                    f"{probe} is tracked, so a token there must still be caught",
                )
                self.assertIsNotNone(
                    re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", f"TOKEN = '{token}'"),
                    "the pattern itself must still match a real token",
                )
        for ignored in (".env", "reminder.log", "state.json", "binding.json"):
            with self.subTest(ignored=ignored):
                self.assertTrue(
                    self._ignored(ignored),
                    f"{ignored} is gitignored and must not be scanned",
                )

    def test_h3_every_module_has_a_docstring(self):
        for module in sorted((PROJECT_ROOT / "reminder").glob("*.py")):
            with self.subTest(module=module.name):
                self.assertTrue(
                    module.read_text(encoding="utf-8").lstrip().startswith('"""'),
                    f"{module.name} has no module docstring",
                )

    def test_h4_source_and_docs_are_english_only(self):
        """Checked in Python on purpose: `grep -P '[\\x{0600}-\\x{06FF}]'` silently
        matches nothing under Git Bash, and once let Arabic through a green check.

        The range is written as \\u escapes so this file stays ASCII itself.
        """
        arabic = re.compile("[" + chr(0x600) + "-" + chr(0x6FF) + "]")
        offenders = []
        for path in sorted(PROJECT_ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".json", ".yml", ".md", ""}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if arabic.search(text):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [], f"non-English text found in: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
