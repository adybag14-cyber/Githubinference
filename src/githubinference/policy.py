from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import CaretakerConfig
from .schema import Action, Decision

_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_AUTHORITY_REQUEST = re.compile(
    r"\b(?:CARETAKER_WRITE_ENABLED|write[- ]gate|enable(?:d|ment|ing)? writes?|"
    r"workflow permissions?|repository settings?|environment secrets?|"
    r"maximum_(?:runtime_minutes|turns|actions_per_run|issue_comments_per_run|"
    r"new_issues_per_run|subagents_per_run|context_characters|file_characters|"
    r"proposal_characters|action_text_characters|github_items)|"
    r"deadline_reserve_minutes|report[- ]only (?:mode|operation|caretaker))\b|"
    r"\bcaretaker.{0,80}\bactivat(?:e|ed|ing|ion)\b|"
    r"\bactivat(?:e|ed|ing|ion).{0,80}\bcaretaker\b|"
    r"\b(?:caretaker|policy|safety).{0,80}\b(?:limits?|budgets?|bounds?|caps?)\b"
    r".{0,40}\b(?:too restrictive|insufficient)\b",
    re.IGNORECASE,
)
_FAILURE_CLAIM = re.compile(r"(?i)\b(?:failed|failure|failures|failing)\b")
_NEGATED_FAILURE_CLAIM = re.compile(
    r"\b(?:no|zero)\s+"
    r"(?:(?:current|exact-ref|workflow|workflows|ci|test|tests|check|checks|"
    r"job|jobs|run|runs)\s+){0,3}(?:failed|failure|failures|failing)\b|"
    r"\b(?:no|zero)\s+(?:failed|failure|failures|failing)\s+"
    r"(?:workflow|workflows|ci|test|tests|check|checks|job|jobs|run|runs)\b|"
    r"\b(?:did|does|do|is|are|was|were|has|have|had)\s+not\s+"
    r"(?:fail|failed|failing)\b|"
    r"\bnot\s+(?:a\s+)?(?:failure|failing)\b",
    re.IGNORECASE,
)
_MODEL_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


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
    reviewable = reviewable_numbers(snapshot, config.review_label)
    model_candidates = model_candidate_repositories(snapshot)
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
            elif requests_authority(action.payload["comment"]):
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
            elif requests_authority(
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
            if action.payload["repository"] not in model_candidates:
                accepted, reason = (
                    False,
                    "model candidate is absent from the current scout evidence",
                )
            else:
                reason = "report-only model candidate; benchmark and maintainer review required"
        elif action.type == "checkpoint":
            if decision.continuation != "continue":
                accepted, reason = (
                    False,
                    "checkpoint is unnecessary when continuation is stop",
                )
            else:
                reason = "checkpoint is report-only state for a later normal schedule"
        else:
            accepted, reason = False, "unsupported action type rejected by policy"
        verdicts.append(ActionVerdict(index, action, accepted, reason))
    return tuple(verdicts)


def validate_unified_diff(patch: str, config: CaretakerConfig) -> tuple[str, ...]:
    if not patch.strip():
        raise ValueError("proposal patch is empty")
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise ValueError("binary patches are not accepted")
    paths: list[str] = []
    targets: list[str] = []
    current_path: str | None = None
    awaiting_new_target = False
    target_seen = False
    for line in patch.splitlines():
        match = _DIFF_HEADER.match(line)
        if match:
            if current_path is not None and not target_seen:
                raise ValueError("proposal diff is missing a +++ target")
            old_path, new_path = match.groups()
            if old_path != new_path:
                raise ValueError("renames are not accepted in autonomous proposals")
            _validate_proposal_path(new_path, config)
            paths.append(new_path)
            current_path = new_path
            awaiting_new_target = False
            target_seen = False
            continue
        if current_path is None or target_seen:
            continue
        if line.startswith("--- "):
            old_target = _parse_diff_target(line, prefix="--- ", side="a/")
            if old_target is not None:
                _validate_proposal_path(old_target, config)
                if old_target != current_path:
                    raise ValueError("proposal --- target does not match diff header")
            awaiting_new_target = True
            continue
        if awaiting_new_target:
            if not line.startswith("+++ "):
                raise ValueError("proposal diff is missing a +++ target")
            new_target = _parse_diff_target(line, prefix="+++ ", side="b/")
            effective_target = current_path if new_target is None else new_target
            _validate_proposal_path(effective_target, config)
            if effective_target != current_path:
                raise ValueError("proposal +++ target does not match diff header")
            targets.append(effective_target)
            target_seen = True
            awaiting_new_target = False
    if current_path is not None and not target_seen:
        raise ValueError("proposal diff is missing a +++ target")
    if not paths:
        raise ValueError("proposal is not a unified git diff")
    if len(paths) != len({path.casefold() for path in paths}):
        raise ValueError("proposal repeats a diff path")
    if len(targets) != len({path.casefold() for path in targets}):
        raise ValueError("proposal repeats a +++ target")
    if len(targets) != len(paths):
        raise ValueError("proposal diff target count does not match headers")
    if len(paths) > 20:
        raise ValueError("proposal changes more than 20 paths")
    return tuple(paths)


def _parse_diff_target(line: str, *, prefix: str, side: str) -> str | None:
    target = line[len(prefix) :].split("\t", 1)[0]
    if target == "/dev/null":
        return None
    if not target.startswith(side):
        raise ValueError(f"proposal target must begin with {side!r}")
    return target[len(side) :]


def _validate_proposal_path(path: str, config: CaretakerConfig) -> None:
    if not _safe_relative_path(path):
        raise ValueError(f"unsafe proposal path: {path!r}")
    if is_protected_path(path, config):
        raise ValueError(f"proposal targets protected path: {path}")


def is_protected_path(path: str, config: CaretakerConfig) -> bool:
    candidate = path.casefold()
    for blocked in config.blocked_proposal_paths:
        blocked_folded = blocked.casefold()
        if candidate == blocked_folded.rstrip("/") or (
            blocked_folded.endswith("/") and candidate.startswith(blocked_folded)
        ):
            return True
    return False


def _safe_relative_path(path: str) -> bool:
    if not path or "\\" in path or "\x00" in path or path.startswith(("/", "~")):
        return False
    normalized = posixpath.normpath(path)
    return (
        normalized == path
        and normalized not in {".", ".."}
        and not normalized.startswith("../")
    )


def reviewable_numbers(snapshot: dict[str, Any], label: str) -> set[int]:
    numbers: set[int] = set()
    for collection in ("issues", "pull_requests"):
        items = snapshot.get(collection, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_labels = item.get("labels", [])
            if not isinstance(raw_labels, list):
                continue
            labels: set[str] = set()
            for entry in raw_labels:
                name = entry.get("name") if isinstance(entry, dict) else entry
                if isinstance(name, str):
                    labels.add(name)
            if label in labels and isinstance(item.get("number"), int):
                numbers.add(item["number"])
    return numbers


def model_candidate_repositories(snapshot: dict[str, Any]) -> set[str]:
    scout = snapshot.get("model_scout", {})
    candidates = scout.get("candidates", []) if isinstance(scout, dict) else []
    if not isinstance(candidates, list):
        return set()
    return {
        item["id"]
        for item in candidates
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and _MODEL_REPOSITORY.fullmatch(item["id"]) is not None
    }


def current_failed_workflows(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    ref = snapshot.get("ref")
    if not isinstance(ref, str) or not ref:
        return ()
    workflow_runs = snapshot.get("workflow_runs", [])
    if not isinstance(workflow_runs, list):
        return ()
    failures: list[dict[str, Any]] = []
    for item in workflow_runs:
        if (
            isinstance(item, dict)
            and item.get("head_sha") == ref
            and item.get("status") == "completed"
            and item.get("conclusion")
            not in {"success", "skipped", "neutral", None, ""}
        ):
            failures.append(item)
    return tuple(failures)


def decision_requests_authority(decision: Decision) -> bool:
    return any(
        _any_text(value, requests_authority) for value in _decision_values(decision)
    )


def decision_mentions_failure(decision: Decision) -> bool:
    return any(
        _any_text(value, _mentions_affirmative_failure)
        for value in _decision_values(decision)
    )


def _decision_values(decision: Decision) -> tuple[Any, ...]:
    return (
        decision.summary,
        decision.risk_notes,
        decision.continuation_reason,
        [action.payload for action in decision.actions],
    )


def requests_authority(text: str) -> bool:
    return _AUTHORITY_REQUEST.search(text) is not None


def _any_text(value: Any, predicate: Callable[[str], bool]) -> bool:
    if isinstance(value, str):
        return predicate(value)
    if isinstance(value, dict):
        return any(_any_text(item, predicate) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_any_text(item, predicate) for item in value)
    return False


def _mentions_affirmative_failure(text: str) -> bool:
    without_negated_claims = _NEGATED_FAILURE_CLAIM.sub("", text)
    return _FAILURE_CLAIM.search(without_negated_claims) is not None
