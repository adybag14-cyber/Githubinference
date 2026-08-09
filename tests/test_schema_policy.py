from __future__ import annotations

import unittest

from githubinference.config import CaretakerConfig
from githubinference.policy import validate_decision, validate_unified_diff
from githubinference.schema import extract_json_object, parse_decision


class SchemaPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = CaretakerConfig.load()

    def test_extracts_fenced_json_but_rejects_non_object(self) -> None:
        self.assertEqual(
            extract_json_object('```json\n{"ok": true}\n```'), {"ok": True}
        )
        with self.assertRaisesRegex(ValueError, "JSON object"):
            extract_json_object("[]")

    def test_unknown_action_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_decision(
                {
                    "summary": "x",
                    "risk_notes": [],
                    "actions": [
                        {"type": "open_issue", "title": "x", "body": "y", "shell": "id"}
                    ],
                    "continuation": "stop",
                    "continuation_reason": "done",
                },
                self.config,
            )

    def test_reviews_require_label_and_duplicate_targets_are_rejected(self) -> None:
        decision = parse_decision(
            {
                "summary": "review",
                "risk_notes": [],
                "actions": [
                    {"type": "review_issue", "issue_number": 7, "comment": "first"},
                    {"type": "review_issue", "issue_number": 7, "comment": "second"},
                    {"type": "review_issue", "issue_number": 8, "comment": "no label"},
                ],
                "continuation": "stop",
                "continuation_reason": "done",
            },
            self.config,
        )
        snapshot = {
            "issues": [{"number": 7, "labels": [{"name": "caretaker:review"}]}],
            "pull_requests": [],
        }
        verdicts = validate_decision(decision, self.config, snapshot)
        self.assertTrue(verdicts[0].accepted)
        self.assertFalse(verdicts[1].accepted)
        self.assertIn("duplicate", verdicts[1].reason)
        self.assertFalse(verdicts[2].accepted)
        self.assertIn("lacks", verdicts[2].reason)

    def test_patch_is_report_only_and_protected_paths_are_blocked(self) -> None:
        safe = """diff --git a/docs/note.md b/docs/note.md
--- a/docs/note.md
+++ b/docs/note.md
@@ -1 +1 @@
-old
+new
"""
        self.assertEqual(validate_unified_diff(safe, self.config), ("docs/note.md",))
        blocked = safe.replace("docs/note.md", ".github/workflows/pwn.yml")
        with self.assertRaisesRegex(ValueError, "protected path"):
            validate_unified_diff(blocked, self.config)

    def test_model_candidate_requires_https_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            parse_decision(
                {
                    "summary": "candidate",
                    "risk_notes": [],
                    "actions": [
                        {
                            "type": "propose_model",
                            "repository": "owner/model",
                            "rationale": "newer",
                            "evidence_urls": ["http://example.invalid"],
                        }
                    ],
                    "continuation": "stop",
                    "continuation_reason": "done",
                },
                self.config,
            )


if __name__ == "__main__":
    unittest.main()
