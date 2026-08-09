from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import CaretakerConfig
from .github_api import GitHubClient
from .policy import ActionVerdict, validate_decision
from .schema import Decision, parse_decision
from .util import (
    append_job_summary,
    atomic_write_json,
    atomic_write_text,
    safe_slug,
    utc_now,
)


def apply_decision(
    *,
    decision_data: dict[str, Any],
    snapshot: dict[str, Any],
    config: CaretakerConfig,
    output_directory: str | os.PathLike[str],
    run_id: str,
    write_enabled: bool | None = None,
    github: GitHubClient | None = None,
) -> dict[str, Any]:
    """Apply only the deterministic, bounded subset of an LLM decision.

    Patch and model-upgrade actions are always converted to report files. They are
    never applied to the working tree. GitHub writes require both the explicit
    repository variable and a write-capable token in the separate apply job.
    """

    selected_run = safe_slug(run_id, maximum=70)
    decision = parse_decision(decision_data, config)
    verdicts = validate_decision(decision, config, snapshot)
    writes = config.write_enabled(write_enabled)
    client = github
    if writes and client is None:
        client = GitHubClient.from_environment(require_token=True)

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    operations: list[dict[str, Any]] = []
    report_files: dict[str, str] = {}

    for verdict in verdicts:
        operation = _handle_action(
            verdict,
            config=config,
            run_id=selected_run,
            writes=writes,
            github=client,
            report_files=report_files,
        )
        operations.append(operation)

    if decision.continuation == "continue" and not any(
        verdict.accepted and verdict.action.type == "checkpoint" for verdict in verdicts
    ):
        report_files[
            f"{config.report_path_prefix}{selected_run}/automatic-checkpoint.md"
        ] = (
            "# Automatic caretaker checkpoint\n\n"
            f"## Assessment\n\n{decision.summary or 'No summary was produced.'}\n\n"
            f"## Why another scheduled turn was requested\n\n"
            f"{decision.continuation_reason or 'No reason was supplied.'}\n\n"
            "This state is revisited only by an ordinary later schedule or a maintainer dispatch.\n"
        )

    for path, content in report_files.items():
        atomic_write_text(output / "generated" / path, content)

    report_pr: dict[str, Any] | None = None
    if writes and report_files:
        assert client is not None
        report_pr = client.create_report_pull_request(
            run_id=selected_run,
            title=f"caretaker: review report for {selected_run}",
            body=(
                "This is an automatically generated **draft, report-only** pull request. "
                "No proposed patch was applied. Treat all model-authored content as untrusted "
                "until a maintainer reviews and tests it.\n\n"
                f"Source run: `{selected_run}`.\n\n"
                f"Assessment: {decision.summary or 'No summary was produced.'}\n\n"
                f"Continuation: `{decision.continuation}` — "
                f"{decision.continuation_reason or 'no reason supplied'}"
            ),
            files=report_files,
        )

    result = {
        "schema_version": 1,
        "run_id": selected_run,
        "completed_at": utc_now(),
        "writes_enabled": writes,
        "policy_verdicts": [verdict.to_dict() for verdict in verdicts],
        "operations": operations,
        "generated_report_files": sorted(report_files),
        "report_pull_request": report_pr,
        "continuation": decision.continuation,
        "continuation_reason": decision.continuation_reason,
        "continuation_policy": (
            "A continue request is checkpointed for the next ordinary schedule. "
            "It never recursively starts another hosted-runner job."
        ),
    }
    atomic_write_json(output / "apply-result.json", result)
    atomic_write_text(output / "apply-summary.md", _render_summary(decision, result))
    append_job_summary(_render_summary(decision, result))
    return result


def _handle_action(
    verdict: ActionVerdict,
    *,
    config: CaretakerConfig,
    run_id: str,
    writes: bool,
    github: GitHubClient | None,
    report_files: dict[str, str],
) -> dict[str, Any]:
    action = verdict.action
    operation: dict[str, Any] = {
        "index": verdict.index,
        "type": action.type,
        "accepted": verdict.accepted,
        "reason": verdict.reason,
        "executed": False,
    }
    if not verdict.accepted:
        return operation

    marker = _operation_marker(action.type, action.payload)
    if action.type == "review_issue":
        if writes:
            assert github is not None
            operation["result"] = github.comment_once(
                action.payload["issue_number"], action.payload["comment"], marker
            )
            operation["executed"] = True
        else:
            operation["reason"] += "; write gate disabled"
    elif action.type == "open_issue":
        if writes:
            assert github is not None
            operation["result"] = github.create_issue(
                action.payload["title"], action.payload["body"], marker
            )
            operation["executed"] = True
        else:
            operation["reason"] += "; write gate disabled"
    elif action.type == "request_subagent":
        if writes:
            assert github is not None
            task_id = safe_slug(f"{run_id}-{verdict.index}", maximum=70)
            operation["result"] = github.dispatch_subagent(
                parent_run=run_id,
                task_id=task_id,
                task=action.payload["task"],
                scope=action.payload["scope"],
            )
            operation["executed"] = True
        else:
            operation["reason"] += "; write gate disabled"
    elif action.type == "propose_change":
        slug = safe_slug(action.payload["title"], maximum=50)
        root = f"{config.proposal_path_prefix}{run_id}"
        report_files[f"{root}/{verdict.index:02d}-{slug}.patch"] = action.payload[
            "patch"
        ]
        report_files[f"{root}/{verdict.index:02d}-{slug}.md"] = (
            f"# {action.payload['title']}\n\n"
            f"{action.payload['description']}\n\n"
            "This patch is model-authored evidence only. It has not been applied or tested.\n"
        )
        operation["executed"] = True
        operation["result"] = {
            "mode": "report_only",
            "file": f"{root}/{verdict.index:02d}-{slug}.patch",
        }
    elif action.type == "propose_model":
        path = f"{config.report_path_prefix}{run_id}/{verdict.index:02d}-model-candidate.md"
        evidence = (
            "\n".join(f"- {url}" for url in action.payload["evidence_urls"])
            or "- None supplied"
        )
        report_files[path] = (
            "# Model candidate (benchmark required)\n\n"
            f"- Repository: `{action.payload['repository']}`\n"
            f"- Revision: `{action.payload['revision'] or 'not supplied'}`\n\n"
            f"## Rationale\n\n{action.payload['rationale']}\n\n"
            f"## Evidence URLs\n\n{evidence}\n\n"
            "This candidate cannot enter the automatic registry without an immutable revision, "
            "artifact digest, compatible license, CPU smoke/quality benchmarks, and a rollback plan.\n"
        )
        operation["executed"] = True
        operation["result"] = {"mode": "report_only", "file": path}
    elif action.type == "checkpoint":
        path = f"{config.report_path_prefix}{run_id}/{verdict.index:02d}-checkpoint.md"
        report_files[path] = f"# Caretaker checkpoint\n\n{action.payload['note']}\n"
        operation["executed"] = True
        operation["result"] = {"mode": "report_only", "file": path}
    return operation


def _operation_marker(action_type: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(f"{action_type}\0{canonical}".encode("utf-8")).hexdigest()[
        :20
    ]
    return f"<!-- githubinference:{action_type}:{digest} -->"


def _render_summary(decision: Decision, result: dict[str, Any]) -> str:
    accepted = sum(1 for item in result["policy_verdicts"] if item["accepted"])
    rejected = len(result["policy_verdicts"]) - accepted
    lines = [
        "# Githubinference caretaker result",
        "",
        f"- Run: `{result['run_id']}`",
        f"- GitHub writes enabled: `{str(result['writes_enabled']).lower()}`",
        f"- Accepted actions: `{accepted}`",
        f"- Rejected actions: `{rejected}`",
        f"- Continuation: `{decision.continuation}`",
        "",
        "## Assessment",
        "",
        decision.summary or "No summary was produced.",
        "",
        "## Continuation",
        "",
        decision.continuation_reason or "No continuation reason was produced.",
    ]
    if decision.risk_notes:
        lines.extend(["", "## Risk notes", ""])
        lines.extend(f"- {note}" for note in decision.risk_notes)
    lines.extend(
        [
            "",
            "> A request to continue waits for the next normal cron/manual run; it does not "
            "recursively claim another GitHub-hosted runner window.",
            "",
        ]
    )
    return "\n".join(lines)
