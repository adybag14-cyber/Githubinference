from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from githubinference.backend import MockBackend
from githubinference.caretaker import run_analysis
from githubinference.config import CaretakerConfig
from githubinference.executor import apply_decision
from githubinference.snapshot import build_snapshot
from githubinference.subagent import _validate_result
from githubinference.util import append_job_summary


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def comment_once(self, number: int, body: str, marker: str) -> dict[str, Any]:
        self.calls.append(("comment", number, body, marker))
        return {"html_url": "https://example.test/comment"}

    def create_issue(self, title: str, body: str, marker: str) -> dict[str, Any]:
        self.calls.append(("issue", title, body, marker))
        return {"html_url": "https://example.test/issue"}

    def dispatch_subagent(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("subagent", kwargs))
        return {"dispatched": True}

    def create_report_pull_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("report", kwargs))
        return {"html_url": "https://example.test/pr", "draft": True}


class SnapshotCaretakerExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = CaretakerConfig.load()

    def test_snapshot_redacts_files_and_external_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "example.py").write_text(
                "api_key = supersecret\n", encoding="utf-8"
            )
            snapshot = build_snapshot(
                root,
                self.config,
                repository="owner/repo",
                ref="abc",
                github_data={"issues": [{"number": 1, "body": "token=badvalue"}]},
                subagent_results=[{"summary": "password: should-not-pass"}],
            )
        rendered = str(snapshot)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("supersecret", rendered)
        self.assertNotIn("badvalue", rendered)
        self.assertNotIn("should-not-pass", rendered)

    def test_snapshot_never_follows_file_symlinks(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temporary)
            secret = Path(outside) / "outside.py"
            secret.write_text("password=outside-secret\n", encoding="utf-8")
            link = root / "linked.py"
            try:
                link.symlink_to(secret)
            except OSError:
                self.skipTest("file symlinks are not available on this host")
            snapshot = build_snapshot(
                root,
                self.config,
                repository="owner/repo",
                ref="abc",
            )
        self.assertEqual(snapshot["files"], [])

    def test_snapshot_balances_repository_and_large_github_context(self) -> None:
        github_data = {
            "issues": [
                {
                    "number": index,
                    "title": f"Issue {index}",
                    "body": "x" * 10000,
                    "labels": [],
                }
                for index in range(1, 21)
            ],
            "pull_requests": [
                {
                    "number": index,
                    "title": f"PR {index}",
                    "body": "y" * 10000,
                    "labels": [],
                }
                for index in range(1, 21)
            ],
            "workflow_runs": [{"id": index, "name": "run"} for index in range(20)],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "repository evidence\n" * 1000, encoding="utf-8"
            )
            snapshot = build_snapshot(
                root,
                self.config,
                repository="owner/repo",
                ref="abc",
                github_data=github_data,
            )
        compact = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(compact), self.config.maximum_context_characters)
        self.assertGreaterEqual(len(snapshot["files"]), 1)
        self.assertGreaterEqual(len(snapshot["issues"]), 1)
        self.assertGreaterEqual(len(snapshot["pull_requests"]), 1)

    def test_snapshot_honors_configured_github_item_limit(self) -> None:
        config = replace(self.config, maximum_github_items=25)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = build_snapshot(
                temporary,
                config,
                repository="owner/repo",
                ref="abc",
                github_data={
                    "issues": [{"number": index, "title": "x"} for index in range(50)]
                },
            )
        self.assertEqual(len(snapshot["issues"]), 25)

    def test_analysis_writes_checkpoint_artifacts(self) -> None:
        backend = MockBackend(
            [
                {
                    "summary": "Repository is healthy.",
                    "risk_notes": [],
                    "actions": [],
                    "continuation": "stop",
                    "continuation_reason": "Nothing else to do.",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_analysis(
                backend=backend,
                snapshot={"issues": [], "pull_requests": []},
                config=self.config,
                runtime_minutes=15,
                output_directory=temporary,
                run_id="test-run",
            )
            self.assertEqual(result.run_id, "test-run")
            self.assertTrue((Path(temporary) / "analysis.json").is_file())
            self.assertTrue((Path(temporary) / "decision.json").is_file())
            self.assertEqual(len(backend.calls), 1)
            self.assertIsNotNone(backend.response_schemas[0])
            self.assertFalse(backend.response_schemas[0]["additionalProperties"])

    def test_analysis_discards_over_budget_turn_and_checkpoints(self) -> None:
        config = replace(self.config, maximum_actions_per_run=1)
        backend = MockBackend(
            [
                {
                    "summary": "first",
                    "risk_notes": [],
                    "actions": [{"type": "open_issue", "title": "one", "body": "x"}],
                    "continuation": "continue",
                    "continuation_reason": "more",
                },
                {
                    "summary": "second",
                    "risk_notes": [],
                    "actions": [{"type": "open_issue", "title": "two", "body": "y"}],
                    "continuation": "stop",
                    "continuation_reason": "done",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_analysis(
                backend=backend,
                snapshot={"issues": [], "pull_requests": []},
                config=config,
                runtime_minutes=15,
                output_directory=temporary,
                run_id="budget-test",
            )
        self.assertEqual(len(result.decision.actions), 1)
        self.assertTrue(result.checkpoint_required)
        self.assertTrue(result.next_run_requested)
        self.assertIn("discarded", result.decision.continuation_reason)

    def test_disabled_write_gate_keeps_patch_as_local_evidence(self) -> None:
        decision = {
            "summary": "A documentation update may help.",
            "risk_notes": [],
            "actions": [
                {
                    "type": "propose_change",
                    "title": "Improve note",
                    "description": "Report only",
                    "patch": (
                        "diff --git a/docs/note.md b/docs/note.md\n"
                        "--- a/docs/note.md\n+++ b/docs/note.md\n"
                        "@@ -1 +1 @@\n-old\n+new\n"
                    ),
                },
                {"type": "open_issue", "title": "Finding", "body": "Details"},
            ],
            "continuation": "continue",
            "continuation_reason": "Check later.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = apply_decision(
                decision_data=decision,
                snapshot={"issues": [], "pull_requests": []},
                config=self.config,
                output_directory=temporary,
                run_id="run-1",
                write_enabled=False,
            )
            generated = list((Path(temporary) / "generated").rglob("*.patch"))
            self.assertEqual(len(generated), 1)
            self.assertFalse(result["writes_enabled"])
            issue_operation = next(
                item for item in result["operations"] if item["type"] == "open_issue"
            )
            self.assertFalse(issue_operation["executed"])

    def test_enabled_gate_uses_only_fixed_client_methods(self) -> None:
        fake = _FakeGitHub()
        decision = {
            "summary": "Bounded actions",
            "risk_notes": [],
            "actions": [
                {"type": "review_issue", "issue_number": 5, "comment": "Reviewed."},
                {"type": "request_subagent", "task": "Inspect docs", "scope": ["docs"]},
                {"type": "checkpoint", "note": "Revisit later."},
            ],
            "continuation": "continue",
            "continuation_reason": "revisit on the next normal schedule",
        }
        snapshot = {
            "issues": [{"number": 5, "labels": [{"name": "caretaker:review"}]}],
            "pull_requests": [],
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {self.config.write_environment_variable: "true"},
                clear=False,
            ),
        ):
            result = apply_decision(
                decision_data=decision,
                snapshot=snapshot,
                config=self.config,
                output_directory=temporary,
                run_id="run-2",
                write_enabled=True,
                github=fake,  # type: ignore[arg-type]
            )
        self.assertTrue(result["writes_enabled"])
        self.assertEqual(
            [call[0] for call in fake.calls], ["comment", "subagent", "report"]
        )

    def test_explicit_write_flag_cannot_bypass_environment_gate(self) -> None:
        variable = self.config.write_environment_variable
        with patch.dict(os.environ, {variable: "false"}, clear=False):
            self.assertFalse(self.config.write_enabled(True))
        with patch.dict(os.environ, {variable: "true"}, clear=False):
            self.assertTrue(self.config.write_enabled(True))
            self.assertFalse(self.config.write_enabled(False))

    def test_required_labels_and_generated_prefixes_are_fixed(self) -> None:
        with self.assertRaisesRegex(ValueError, "required policy"):
            replace(self.config, state_label="").validate()
        with self.assertRaisesRegex(ValueError, "report path prefix"):
            replace(self.config, report_path_prefix=".caretaker/other/").validate()
        with self.assertRaisesRegex(ValueError, "proposal path prefix"):
            replace(self.config, proposal_path_prefix=".caretaker/other/").validate()

    def test_non_object_subagent_result_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an object"):
            _validate_result([{"summary": "bad"}])

    def test_job_summary_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "summary.md"
            with patch.dict(
                os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=False
            ):
                append_job_summary("token=should-not-appear")
            rendered = summary.read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("should-not-appear", rendered)


if __name__ == "__main__":
    unittest.main()
