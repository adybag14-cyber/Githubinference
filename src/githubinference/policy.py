from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any

from .config import CaretakerConfig
from .schema import Action, Decision

_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_AUTHORITY_REQUEST = re.compile(
    r"(?i)\b(?:CARETAKER_WRITE_ENABLED|write[- ]gate|enable(?:d|ment|ing)? writes?|"
    r"workflow permissions?|repository settings?|environment secrets?)\b"
)


@dataclass(frozen=True, slots=True)
class ActionVerdict:
    index: int
    action: Action
    accepted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "type": self.action.type,
            "accepted": self.accepted,
            "reason": self.reason,
            "payload": self.action.payload,
        }


def validate_decision(
    decision: Decision,
    config: CaretakerConfig,
    snapshot: dict[str, Any],
) -> tuple[ActionVerdict, ...]:
    reviewable = _reviewable_numbers(snapshot, config.review_label)
    comments = 0
    new_issues = 0
    subagents = 0
    seen_comments: set[int] = set()
    verdicts: list[ActionVerdict] = []
    for index, action in enumerate(decision.actions):
        accepted = True
        reason = "accepted by deterministic policy"
        if action.type == "review_issue":
            number = action.payload["issue_number"]
            comments += 1
            if comments > config.maximum_issue_comments_per_run:
                accepted, reason = False, "issue comment budget exceeded"
            elif number not in reviewable:
                accepted, reason = False, f"issue #{number} lacks {config.review_label}"
            elif number in seen_comments:
                accepted, reason = False, "duplicate comment target in one run"
            elif not action.payload["comment"].strip():
                accepted, reason = False, "issue comment is empty"
            elif _requests_authority(action.payload["comment"]):
                accepted, reason = (
                    False,
                    "model comments cannot request authority or settings changes",
                )
            seen_comments.add(number)
        elif action.type == "open_issue":
            new_issues += 1
            if new_issues > config.maximum_new_issues_per_run:
                accepted, reason = False, "new issue budget exceeded"
            elif not action.payload["title"]:
                accepted, reason = False, "issue title is empty"
            elif _requests_authority(
                f"{action.payload['title']}\n{action.payload['body']}"
            ):
                accepted, reason = (
                    False,
                    "model-created issues cannot request authority or settings changes",
                )
        elif action.type == "propose_change":
            try:
                paths = validate_unified_diff(action.payload["patch"], config)
            except ValueError as exc:
                accepted, reason = False, str(exc)
            else:
                reason = f"report-only proposal for {len(paths)} path(s)"
        elif action.type == "request_subagent":
            subagents += 1
            if subagents > config.maximum_subagents_per_run:
                accepted, reason = False, "subagent budget exceeded"
            elif not action.payload["task"].strip():
                accepted, reason = False, "subagent task is empty"
            elif any(not _safe_relative_path(item) for item in action.payload["scope"]):
                accepted, reason = False, "subagent scope contains an unsafe path"
        elif action.type == "propose_model":
            reason = (
                "report-only model candidate; benchmark and maintainer review required"
            )
        elif action.type == "checkpoint":
            if decision.continuation != "continue":
                accepted, reason = (
                    False,
                    "checkpoint is unnecessary when continuation is stop",
                )
            else:
                reason = "checkpoint is report-only state for a later normal schedule"
        verdicts.append(ActionVerdict(index, action, accepted, reason))
    return tuple(verdicts)


def validate_unified_diff(patch: str, config: CaretakerConfig) -> tuple[str, ...]:
    if not patch.strip():
        raise ValueError("proposal patch is empty")
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise ValueError("binary patches are not accepted")
    paths: list[str] = []
    for line in patch.splitlines():
        match = _DIFF_HEADER.match(line)
        if not match:
            continue
        old_path, new_path = match.groups()
        if old_path != new_path:
            raise ValueError("renames are not accepted in autonomous proposals")
        if not _safe_relative_path(new_path):
            raise ValueError(f"unsafe proposal path: {new_path!r}")
        if any(
            new_path == blocked.rstrip("/") or new_path.startswith(blocked)
            for blocked in config.blocked_proposal_paths
        ):
            raise ValueError(f"proposal targets protected path: {new_path}")
        paths.append(new_path)
    if not paths:
        raise ValueError("proposal is not a unified git diff")
    if len(paths) != len(set(paths)):
        raise ValueError("proposal repeats a diff path")
    if len(paths) > 20:
        raise ValueError("proposal changes more than 20 paths")
    return tuple(paths)


def _safe_relative_path(path: str) -> bool:
    if not path or "\\" in path or "\x00" in path or path.startswith(("/", "~")):
        return False
    normalized = posixpath.normpath(path)
    return (
        normalized == path
        and normalized not in {".", ".."}
        and not normalized.startswith("../")
    )


def _reviewable_numbers(snapshot: dict[str, Any], label: str) -> set[int]:
    numbers: set[int] = set()
    for collection in ("issues", "pull_requests"):
        for item in snapshot.get(collection, []):
            labels = {
                entry.get("name") if isinstance(entry, dict) else entry
                for entry in item.get("labels", [])
            }
            if label in labels and isinstance(item.get("number"), int):
                numbers.add(item["number"])
    return numbers


def _requests_authority(text: str) -> bool:
    return _AUTHORITY_REQUEST.search(text) is not None
