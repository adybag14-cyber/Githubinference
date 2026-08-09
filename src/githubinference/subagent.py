from __future__ import annotations

from pathlib import Path
from typing import Any

from .backend import ChatBackend
from .prompts import SUBAGENT_SYSTEM_PROMPT
from .snapshot import snapshot_prompt
from .util import atomic_write_json, bounded_text, utc_now


def run_subagent(
    *,
    backend: ChatBackend,
    task: str,
    scope: list[str],
    snapshot: dict[str, Any],
    parent_run: str,
    task_id: str,
    output_path: str | Path,
    model_id: str | None = None,
) -> dict[str, Any]:
    bounded_task = bounded_text(task, 4000, field="subagent task")
    if len(scope) > 12:
        raise ValueError("subagent scope exceeds 12 entries")
    request = {
        "task": bounded_task,
        "scope": [bounded_text(item, 300, field="scope") for item in scope],
        "repository_snapshot": snapshot,
    }
    raw = backend.chat_json(
        [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": snapshot_prompt(request)},
        ],
        max_tokens=2048,
    )
    result = _validate_result(raw)
    envelope = {
        "schema_version": 1,
        "parent_run": bounded_text(parent_run, 80, field="parent_run"),
        "task_id": bounded_text(task_id, 80, field="task_id"),
        "model_id": bounded_text(model_id, 100, field="model_id") if model_id else None,
        "completed_at": utc_now(),
        **result,
    }
    atomic_write_json(output_path, envelope)
    return envelope


def _validate_result(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {"summary", "findings", "suggested_follow_up"}
    if set(raw) - allowed:
        raise ValueError("subagent result contains unknown fields")
    summary = bounded_text(raw.get("summary", ""), 5000, field="summary")
    follow_up = bounded_text(
        raw.get("suggested_follow_up", ""), 3000, field="suggested_follow_up"
    )
    findings_raw = raw.get("findings", [])
    if not isinstance(findings_raw, list) or len(findings_raw) > 8:
        raise ValueError("subagent findings must contain at most eight items")
    findings: list[dict[str, str]] = []
    for item in findings_raw:
        if not isinstance(item, dict) or set(item) != {
            "severity",
            "title",
            "evidence",
            "recommendation",
        }:
            raise ValueError("subagent finding has invalid fields")
        severity = item["severity"]
        if severity not in {"info", "low", "medium", "high"}:
            raise ValueError("subagent severity is invalid")
        findings.append(
            {
                "severity": severity,
                "title": bounded_text(item["title"], 300, field="finding title"),
                "evidence": bounded_text(
                    item["evidence"], 3000, field="finding evidence"
                ),
                "recommendation": bounded_text(
                    item["recommendation"], 3000, field="finding recommendation"
                ),
            }
        )
    return {"summary": summary, "findings": findings, "suggested_follow_up": follow_up}
