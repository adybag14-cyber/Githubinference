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
        self.assertIn(
            "github.ref_name == github.event.repository.default_branch", endpoint
        )
        self.assertIn("environment: inference", endpoint)
        self.assertIn("secrets.INFERENCE_API_KEY", endpoint)
        self.assertIn("secrets.CLOUDFLARE_TUNNEL_TOKEN", endpoint)
        self.assertIn("--token-file", endpoint)
        self.assertGreaterEqual(endpoint.count("--connect-timeout 2 --max-time 5"), 2)
        self.assertNotIn("Upload sanitized service logs", endpoint)

    def test_ci_and_model_smoke_cover_submitted_runtime_changes(self) -> None:
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", ci)
        self.assertIn('git diff --check "${BASE_SHA}" "${GITHUB_SHA}"', ci)
        smoke = (WORKFLOWS / "model-smoke.yml").read_text(encoding="utf-8")
        for path in ("config/caretaker.json", "src/githubinference/**"):
            self.assertIn(path, smoke)

    def test_cached_runtime_assets_are_cryptographically_revalidated(self) -> None:
        cloudflared = (ROOT / "scripts" / "install_cloudflared.sh").read_text(
            encoding="utf-8"
        )
        llama = (ROOT / "scripts" / "install_llama.sh").read_text(encoding="utf-8")
        self.assertIn('"${BINARY}" | sha256sum --check --status', cloudflared)
        self.assertIn('"${CACHED_ARCHIVE}" | sha256sum --check --status', llama)
        self.assertIn('VERIFIED_DIR="${INSTALL_DIR}/verified-${LLAMA_SHA256}"', llama)
        self.assertIn("-name 'verified-*'", llama)


if __name__ == "__main__":
    unittest.main()
