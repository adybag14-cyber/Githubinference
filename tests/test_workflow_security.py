from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowSecurityTests(unittest.TestCase):
    def test_all_actions_are_pinned_to_full_commits(self) -> None:
        uses_pattern = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
        for workflow in WORKFLOWS.glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            for reference in uses_pattern.findall(text):
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)

    def test_no_pull_request_target_or_self_recursive_caretaker_dispatch(self) -> None:
        all_text = "\n".join(
            path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml")
        )
        self.assertNotIn("pull_request_target:", all_text)
        caretaker = (WORKFLOWS / "caretaker.yml").read_text(encoding="utf-8")
        self.assertNotIn("caretaker.yml/dispatches", caretaker)
        self.assertIn("CARETAKER_WRITE_ENABLED", caretaker)

    def test_public_endpoint_is_manual_only_and_uses_environment_secrets(self) -> None:
        endpoint = (WORKFLOWS / "endpoint.yml").read_text(encoding="utf-8")
        trigger_block = endpoint.split("concurrency:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger_block)
        self.assertNotIn("schedule:", trigger_block)
        self.assertNotIn("pull_request:", trigger_block)
        self.assertIn("environment: inference", endpoint)
        self.assertIn("secrets.INFERENCE_API_KEY", endpoint)
        self.assertIn("secrets.CLOUDFLARE_TUNNEL_TOKEN", endpoint)
        self.assertIn("--token-file", endpoint)
        self.assertNotIn("Upload sanitized service logs", endpoint)


if __name__ == "__main__":
    unittest.main()
