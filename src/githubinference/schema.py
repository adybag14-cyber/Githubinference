from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import CaretakerConfig
from .util import bounded_text

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Action:
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Decision:
    summary: str
    risk_notes: tuple[str, ...]
    actions: tuple[Action, ...]
    continuation: str
    continuation_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "risk_notes": list(self.risk_notes),
            "actions": [
                {"type": action.type, **action.payload} for action in self.actions
            ],
            "continuation": self.continuation,
            "continuation_reason": self.continuation_reason,
        }


def caretaker_decision_schema(config: CaretakerConfig) -> dict[str, Any]:
    """Return the strict generation schema enforced by the local model server."""
    text = {"type": "string"}
    action_variants = {
        "review_issue": {
            "type": "object",
            "properties": {
                "type": {"enum": ["review_issue"]},
                "issue_number": {"type": "integer", "minimum": 1},
                "comment": text,
            },
            "required": ["type", "issue_number", "comment"],
            "additionalProperties": False,
        },
        "open_issue": {
            "type": "object",
            "properties": {
                "type": {"enum": ["open_issue"]},
                "title": {"type": "string", "maxLength": 180},
                "body": text,
            },
            "required": ["type", "title", "body"],
            "additionalProperties": False,
        },
        "propose_change": {
            "type": "object",
            "properties": {
                "type": {"enum": ["propose_change"]},
                "title": {"type": "string", "maxLength": 180},
                "description": {"type": "string", "maxLength": 5000},
                "patch": {
                    "type": "string",
                    "maxLength": config.maximum_proposal_characters,
                },
            },
            "required": ["type", "title", "description", "patch"],
            "additionalProperties": False,
        },
        "propose_model": {
            "type": "object",
            "properties": {
                "type": {"enum": ["propose_model"]},
                "repository": {"type": "string", "maxLength": 160},
                "revision": {"type": "string", "maxLength": 80},
                "rationale": text,
                "evidence_urls": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 500},
                    "maxItems": 6,
                },
            },
            "required": ["type", "repository", "rationale"],
            "additionalProperties": False,
        },
        "request_subagent": {
            "type": "object",
            "properties": {
                "type": {"enum": ["request_subagent"]},
                "task": {"type": "string", "maxLength": 4000},
                "scope": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 300},
                    "maxItems": 12,
                },
            },
            "required": ["type", "task"],
            "additionalProperties": False,
        },
        "checkpoint": {
            "type": "object",
            "properties": {
                "type": {"enum": ["checkpoint"]},
                "note": {"type": "string", "maxLength": 4000},
            },
            "required": ["type", "note"],
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": {
            "summary": text,
            "risk_notes": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2000},
                "maxItems": 12,
            },
            "actions": {
                "type": "array",
                "items": {
                    "oneOf": [
                        action_variants[action_type]
                        for action_type in sorted(config.allowed_actions)
                    ]
                },
                "maxItems": config.maximum_actions_per_run,
            },
            "continuation": {"enum": ["continue", "stop"]},
            "continuation_reason": {"type": "string", "maxLength": 3000},
        },
        "required": [
            "summary",
            "risk_notes",
            "actions",
            "continuation",
            "continuation_reason",
        ],
        "additionalProperties": False,
    }


def extract_json_object(content: str) -> dict[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            candidate = candidate[first_newline + 1 : last_fence].strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response did not contain a JSON object") from None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"model response JSON is invalid: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def parse_decision(raw: dict[str, Any], config: CaretakerConfig) -> Decision:
    allowed_top = {
        "summary",
        "risk_notes",
        "actions",
        "continuation",
        "continuation_reason",
    }
    unknown_top = set(raw) - allowed_top
    if unknown_top:
        raise ValueError(f"unknown decision fields: {sorted(unknown_top)}")
    summary = bounded_text(
        raw.get("summary", ""), config.maximum_action_text_characters, field="summary"
    )
    risk_notes_raw = raw.get("risk_notes", [])
    if not isinstance(risk_notes_raw, list) or len(risk_notes_raw) > 12:
        raise ValueError("risk_notes must be a list with at most 12 entries")
    risk_notes = tuple(
        bounded_text(item, 2000, field="risk note") for item in risk_notes_raw
    )
    actions_raw = raw.get("actions", [])
    if not isinstance(actions_raw, list):
        raise ValueError("actions must be a list")
    if len(actions_raw) > config.maximum_actions_per_run:
        raise ValueError("decision exceeds the per-run action budget")
    actions = tuple(_parse_action(item, config) for item in actions_raw)
    continuation = raw.get("continuation", "stop")
    if continuation not in {"continue", "stop"}:
        raise ValueError("continuation must be continue or stop")
    continuation_reason = bounded_text(
        raw.get("continuation_reason", ""),
        3000,
        field="continuation_reason",
    )
    return Decision(
        summary=summary,
        risk_notes=risk_notes,
        actions=actions,
        continuation=continuation,
        continuation_reason=continuation_reason,
    )


def _parse_action(raw: Any, config: CaretakerConfig) -> Action:
    if not isinstance(raw, dict):
        raise ValueError("each action must be an object")
    action_type = raw.get("type")
    if action_type not in config.allowed_actions:
        raise ValueError(f"action type is not allowed: {action_type!r}")
    payload = {key: value for key, value in raw.items() if key != "type"}
    validators = {
        "review_issue": _review_issue,
        "open_issue": _open_issue,
        "propose_change": _propose_change,
        "propose_model": _propose_model,
        "request_subagent": _request_subagent,
        "checkpoint": _checkpoint,
    }
    validator = validators.get(action_type)
    if validator is None:
        raise ValueError(f"action type has no implemented validator: {action_type!r}")
    return Action(action_type, validator(payload, config))


def _require_keys(
    payload: dict[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing:
        raise ValueError(f"action is missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"action has unknown fields: {sorted(unknown)}")


def _issue_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("issue_number must be a positive integer")
    return value


def _review_issue(payload: dict[str, Any], config: CaretakerConfig) -> dict[str, Any]:
    _require_keys(payload, {"issue_number", "comment"})
    return {
        "issue_number": _issue_number(payload["issue_number"]),
        "comment": bounded_text(
            payload["comment"], config.maximum_action_text_characters, field="comment"
        ),
    }


def _open_issue(payload: dict[str, Any], config: CaretakerConfig) -> dict[str, Any]:
    _require_keys(payload, {"title", "body"})
    return {
        "title": bounded_text(payload["title"], 180, field="title").strip(),
        "body": bounded_text(
            payload["body"], config.maximum_action_text_characters, field="body"
        ),
    }


def _propose_change(payload: dict[str, Any], config: CaretakerConfig) -> dict[str, Any]:
    _require_keys(payload, {"title", "description", "patch"})
    return {
        "title": bounded_text(payload["title"], 180, field="proposal title").strip(),
        "description": bounded_text(
            payload["description"], 5000, field="proposal description"
        ),
        "patch": bounded_text(
            payload["patch"], config.maximum_proposal_characters, field="patch"
        ),
    }


def _propose_model(payload: dict[str, Any], config: CaretakerConfig) -> dict[str, Any]:
    _require_keys(payload, {"repository", "rationale"}, {"revision", "evidence_urls"})
    repository = bounded_text(payload["repository"], 160, field="repository")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("model repository must use owner/name form")
    revision = bounded_text(payload.get("revision", ""), 80, field="revision")
    evidence = payload.get("evidence_urls", [])
    if not isinstance(evidence, list) or len(evidence) > 6:
        raise ValueError("evidence_urls must contain at most six URLs")
    urls: list[str] = []
    for item in evidence:
        url = bounded_text(item, 500, field="evidence URL")
        if not url.startswith("https://"):
            raise ValueError("model evidence URLs must use HTTPS")
        urls.append(url)
    return {
        "repository": repository,
        "revision": revision,
        "rationale": bounded_text(
            payload["rationale"],
            config.maximum_action_text_characters,
            field="rationale",
        ),
        "evidence_urls": urls,
    }


def _request_subagent(
    payload: dict[str, Any], config: CaretakerConfig
) -> dict[str, Any]:
    _require_keys(payload, {"task"}, {"scope"})
    scope = payload.get("scope", [])
    if not isinstance(scope, list) or len(scope) > 12:
        raise ValueError("subagent scope must contain at most 12 paths")
    return {
        "task": bounded_text(payload["task"], 4000, field="subagent task"),
        "scope": [bounded_text(item, 300, field="scope item") for item in scope],
    }


def _checkpoint(payload: dict[str, Any], config: CaretakerConfig) -> dict[str, Any]:
    _require_keys(payload, {"note"})
    return {"note": bounded_text(payload["note"], 4000, field="checkpoint note")}
