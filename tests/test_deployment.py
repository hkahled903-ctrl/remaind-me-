from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTest(unittest.TestCase):
    def test_vercel_routes_all_paths_to_the_wsgi_entrypoint(self):
        config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["builds"][0]["src"], "api/index.py")
        self.assertEqual(config["routes"][0]["dest"], "api/index.py")

    def test_only_ci_and_manual_webhook_registration_workflows_remain(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertEqual(
            {workflow.name for workflow in workflows},
            {"ci.yml", "register-webhook.yml"},
        )
        self.assertNotIn("schedule:", "\n".join(path.read_text(encoding="utf-8") for path in workflows))

    def test_ci_runs_against_disposable_redis_for_real_lua_scripts(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("image: redis:7-alpine", ci)
        self.assertIn("TIMER_TEST_REDIS_URL: redis://127.0.0.1:6379/0", ci)
        self.assertIn("python -m unittest discover -s tests -t . -v", ci)
        self.assertIn("permissions:\n  contents: read", ci)

    def test_qstash_schedule_is_setup_documentation_not_runtime_code(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        scheduler = (ROOT / "reminder" / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn("`* * * * *`", readme)
        self.assertIn("Create exactly one recurring schedule", readme)
        self.assertNotIn("qstash.upstash.io", scheduler)
        self.assertNotIn("create_schedule", scheduler)

    def test_runtime_has_no_legacy_product_imports(self):
        forbidden = (
            "reminder.binding", "reminder.commands", "reminder.confirm",
            "reminder.policy", "reminder.settings", "reminder.webapp",
        )
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for folder in (ROOT / "reminder", ROOT / "api")
            for path in folder.glob("*.py")
        )
        for module in forbidden:
            with self.subTest(module=module):
                self.assertNotIn(module, source)


if __name__ == "__main__":
    unittest.main()
