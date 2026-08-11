from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backend import ChatBackend
from .config import CaretakerConfig
from .deadline import Deadline
from .policy import validate_decision
from .prompts import CARETAKER_SYSTEM_PROMPT
from .schema import Action, Decision, caretaker_decision_schema, parse_decision
from .snapshot import snapshot_prompt
from .util import atomic_write_json, safe_slug, utc_now


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    run_id: str
    model_id: str | None
    decision: Decision
    turns: tuple[dict[str, Any], ...]
    checkpoint_required: bool
    next_run_requested: bool
    started_at: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "decision": self.decision.to_dict(),
            "turns": list(self.turns),
            "checkpoint_required": self.checkpoint_required,
            "next_run_requested": self.next_run_requested,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def run_analysis(
    *,
    backend: ChatBackend,
    snapshot: dict[str, Any],
    config: CaretakerConfig,
    runtime_minutes: int,
    output_directory: str | os.PathLike[str],
    run_id: str | None = None,
    model_id: str | None = None,
) -> AnalysisResult:
    selected_run_id = _run_id(run_id)
    started_at = utc_now()
    deadline = Deadline(
        runtime_minutes, min(config.deadline_reserve_minutes, runtime_minutes - 1)
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": CARETAKER_SYSTEM_PROMPT},
        {"role": "user", "content": snapshot_prompt(snapshot)},
    ]
    turns: list[dict[str, Any]] = []
    collected_actions: list[Action] = []
    collected_risks: list[str] = []
    summaries: list[str] = []
    continuation = "stop"
    continuation_reason = "turn budget completed"
    checkpoint_required = False
    next_run_requested = False

    for turn_number in range(1, config.maximum_turns + 1):
        if deadline.should_checkpoint(estimated_next_turn_seconds=180):
            checkpoint_required = True
            next_run_requested = continuation == "continue"
            continuation_reason = (
                "runtime boundary reached; state checkpointed for a later scheduled run"
            )
            break
        decision = _ask_for_valid_decision(backend, messages, config)
        if (
            len(collected_actions) + len(decision.actions)
            > config.maximum_actions_per_run
        ):
            checkpoint_required = True
            next_run_requested = True
            continuation_reason = (
                "cumulative action budget reached; the over-budget turn was discarded "
                "and prior state was checkpointed for the next normal schedule"
            )
            break
        verdicts = validate_decision(decision, config, snapshot)
        turns.append(
            {
                "turn": turn_number,
                "decision": decision.to_dict(),
                "policy_verdicts": [verdict.to_dict() for verdict in verdicts],
            }
        )
        collected_actions.extend(decision.actions)
        collected_risks.extend(decision.risk_notes)
        if decision.summary:
            summaries.append(decision.summary)
        continuation = decision.continuation
        continuation_reason = decision.continuation_reason
        if continuation == "stop":
            break
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": json.dumps(decision.to_dict(), separators=(",", ":")),
                },
                {
                    "role": "user",
                    "content": (
                        "Continue only if another bounded reasoning turn adds material value. "
                        "Policy verdicts for the previous turn: "
                        + json.dumps(
                            [verdict.to_dict() for verdict in verdicts],
                            separators=(",", ":"),
                        )
                    ),
                },
            ]
        )
    else:
        if continuation == "continue":
            checkpoint_required = True
            next_run_requested = True
            continuation_reason = (
                "maximum turn count reached; checkpointed for the next normal schedule"
            )

    aggregate = Decision(
        summary="\n\n".join(summaries)[: config.maximum_action_text_characters],
        risk_notes=tuple(dict.fromkeys(collected_risks))[:12],
        actions=tuple(collected_actions),
        continuation="continue" if next_run_requested else "stop",
        continuation_reason=continuation_reason,
    )
    result = AnalysisResult(
        run_id=selected_run_id,
        model_id=model_id,
        decision=aggregate,
        turns=tuple(turns),
        checkpoint_required=checkpoint_required,
        next_run_requested=next_run_requested,
        started_at=started_at,
        completed_at=utc_now(),
    )
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output / "snapshot.json", snapshot)
    atomic_write_json(output / "decision.json", aggregate.to_dict())
    atomic_write_json(output / "analysis.json", result.to_dict())
    return result


def _ask_for_valid_decision(
    backend: ChatBackend,
    messages: list[dict[str, str]],
    config: CaretakerConfig,
) -> Decision:
    repair_messages = list(messages)
    last_error: BaseException | None = None
    for attempt in range(2):
        raw = backend.chat_json(
            repair_messages,
            max_tokens=2048,
            response_schema=caretaker_decision_schema(config),
        )
        try:
            return parse_decision(raw, config)
        except ValueError as exc:
            last_error = exc
            if attempt == 0:
                repair_messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": json.dumps(raw, separators=(",", ":"))[:12000],
                        },
                        {
                            "role": "user",
                            "content": f"The JSON failed deterministic validation: {exc}. Return corrected JSON only.",
                        },
                    ]
                )
    raise ValueError(f"model could not produce a valid decision: {last_error}")


def _run_id(explicit: str | None) -> str:
    if explicit:
        return safe_slug(explicit, maximum=70)
    github_run = os.environ.get("GITHUB_RUN_ID")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if github_run:
        return safe_slug(f"{github_run}-{attempt}", maximum=70)
    return safe_slug(utc_now().replace(":", "-"), maximum=70)
