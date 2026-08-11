from __future__ import annotations

import unittest
from dataclasses import replace

from githubinference.config import CaretakerConfig
from githubinference.policy import validate_decision, validate_unified_diff
from githubinference.schema import (
    Action,
    Decision,
    caretaker_decision_schema,
    extract_json_object,
    parse_decision,
)


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

    def test_generation_schema_is_strict_and_covers_configured_actions(self) -> None:
        schema = caretaker_decision_schema(self.config)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "summary",
                "risk_notes",
                "actions",
                "continuation",
                "continuation_reason",
            },
        )
        variants = schema["properties"]["actions"]["items"]["oneOf"]
        action_types = {
            variant["properties"]["type"]["enum"][0] for variant in variants
        }
        self.assertEqual(action_types, self.config.allowed_actions)
        self.assertTrue(all(not item["additionalProperties"] for item in variants))
        self.assertEqual(
            schema["properties"]["summary"]["maxLength"],
            self.config.maximum_action_text_characters,
        )
        self.assertNotIn("maxLength", schema["properties"]["risk_notes"]["items"])

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

        case_variant = safe.replace("docs/note.md", "security.md")
        with self.assertRaisesRegex(ValueError, "protected path"):
            validate_unified_diff(case_variant, self.config)

        mismatched_target = safe.replace("+++ b/docs/note.md", "+++ b/docs/other.md")
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_unified_diff(mismatched_target, self.config)

        added = safe.replace("--- a/docs/note.md", "--- /dev/null")
        deleted = safe.replace("+++ b/docs/note.md", "+++ /dev/null")
        self.assertEqual(validate_unified_diff(added, self.config), ("docs/note.md",))
        self.assertEqual(validate_unified_diff(deleted, self.config), ("docs/note.md",))

    def test_unknown_implemented_action_mappings_fail_closed(self) -> None:
        divergent = replace(
            self.config, allowed_actions=self.config.allowed_actions | {"unhandled"}
        )
        with self.assertRaisesRegex(ValueError, "no implemented validator"):
            parse_decision(
                {
                    "summary": "x",
                    "risk_notes": [],
                    "actions": [{"type": "unhandled"}],
                    "continuation": "stop",
                    "continuation_reason": "done",
                },
                divergent,
            )

        decision = Decision(
            summary="x",
            risk_notes=(),
            actions=(Action("unhandled", {}),),
            continuation="stop",
            continuation_reason="done",
        )
        verdict = validate_decision(decision, self.config, {"issues": []})[0]
        self.assertFalse(verdict.accepted)
        self.assertIn("unsupported", verdict.reason)

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

    def test_direct_writes_cannot_request_more_authority(self) -> None:
        decision = parse_decision(
            {
                "summary": "do not expand authority",
                "risk_notes": [],
                "actions": [
                    {
                        "type": "open_issue",
                        "title": "Verify write gate status",
                        "body": "Set CARETAKER_WRITE_ENABLED to true.",
                    },
                    {
                        "type": "review_issue",
                        "issue_number": 4,
                        "comment": "Please change repository settings to allow writes.",
                    },
                ],
                "continuation": "stop",
                "continuation_reason": "done",
            },
            self.config,
        )
        snapshot = {
            "issues": [{"number": 4, "labels": [{"name": "caretaker:review"}]}],
            "pull_requests": [],
        }
        verdicts = validate_decision(decision, self.config, snapshot)
        self.assertFalse(verdicts[0].accepted)
        self.assertFalse(verdicts[1].accepted)
        self.assertTrue(all("authority" in verdict.reason for verdict in verdicts))


if __name__ == "__main__":
    unittest.main()
