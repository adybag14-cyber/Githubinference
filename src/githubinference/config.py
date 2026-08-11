from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .util import load_json, parse_bool

_IMPLEMENTED_ACTIONS = frozenset(
    {
        "review_issue",
        "open_issue",
        "propose_change",
        "propose_model",
        "request_subagent",
        "checkpoint",
    }
)


@dataclass(frozen=True, slots=True)
class CaretakerConfig:
    schedule_runtime_minutes: int
    maximum_runtime_minutes: int
    deadline_reserve_minutes: int
    maximum_turns: int
    maximum_actions_per_run: int
    maximum_issue_comments_per_run: int
    maximum_new_issues_per_run: int
    maximum_subagents_per_run: int
    maximum_context_characters: int
    maximum_file_characters: int
    maximum_proposal_characters: int
    maximum_action_text_characters: int
    maximum_github_items: int
    review_label: str
    state_label: str
    write_environment_variable: str
    allowed_actions: frozenset[str]
    report_path_prefix: str
    proposal_path_prefix: str
    blocked_proposal_paths: tuple[str, ...]

    @classmethod
    def load(cls, path: str | Path = "config/caretaker.json") -> "CaretakerConfig":
        raw = load_json(path)
        if raw.pop("schema_version", None) != 1:
            raise ValueError("unsupported caretaker configuration schema")
        raw["allowed_actions"] = frozenset(raw["allowed_actions"])
        raw["blocked_proposal_paths"] = tuple(raw["blocked_proposal_paths"])
        config = cls(**raw)
        config.validate()
        return config

    def validate(self) -> None:
        if not 1 <= self.schedule_runtime_minutes <= self.maximum_runtime_minutes:
            raise ValueError("schedule runtime is outside allowed bounds")
        if not 1 <= self.maximum_runtime_minutes <= 340:
            raise ValueError(
                "maximum runtime cannot exceed the 340 minute safety boundary"
            )
        if not 1 <= self.deadline_reserve_minutes < self.schedule_runtime_minutes:
            raise ValueError("deadline reserve must fit inside scheduled runtime")
        if not 1 <= self.maximum_turns <= 12:
            raise ValueError("maximum_turns is invalid")
        if not 1 <= self.maximum_actions_per_run <= 30:
            raise ValueError("maximum_actions_per_run is invalid")
        if not 0 <= self.maximum_issue_comments_per_run <= self.maximum_actions_per_run:
            raise ValueError("issue comment budget is invalid")
        if not 0 <= self.maximum_new_issues_per_run <= self.maximum_actions_per_run:
            raise ValueError("new issue budget is invalid")
        if not 0 <= self.maximum_subagents_per_run <= 2:
            raise ValueError("subagents are capped at two")
        if not 10000 <= self.maximum_context_characters <= 250000:
            raise ValueError("context budget is invalid")
        if not 1000 <= self.maximum_file_characters <= self.maximum_context_characters:
            raise ValueError("file budget is invalid")
        if not 1000 <= self.maximum_proposal_characters <= 100000:
            raise ValueError("proposal budget is invalid")
        if not 500 <= self.maximum_action_text_characters <= 20000:
            raise ValueError("action text budget is invalid")
        if not 1 <= self.maximum_github_items <= 50:
            raise ValueError("GitHub item budget is invalid")
        if (
            not self.review_label
            or not self.state_label
            or not self.write_environment_variable
        ):
            raise ValueError("required policy settings are empty")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", self.write_environment_variable):
            raise ValueError("write environment variable name is invalid")
        if not self.allowed_actions or not self.allowed_actions <= _IMPLEMENTED_ACTIONS:
            raise ValueError("allowed_actions includes an unsupported action")
        if self.report_path_prefix != ".caretaker/reports/":
            raise ValueError("report path prefix must be .caretaker/reports/")
        if self.proposal_path_prefix != ".caretaker/proposals/":
            raise ValueError("proposal path prefix must be .caretaker/proposals/")
        for path in self.blocked_proposal_paths:
            if (
                not path
                or "\\" in path
                or path.startswith(("/", "../"))
                or "/../" in path
            ):
                raise ValueError("blocked proposal path is unsafe")

    def runtime_minutes(self, requested: int | None) -> int:
        value = self.schedule_runtime_minutes if requested is None else requested
        if value < 1 or value > self.maximum_runtime_minutes:
            raise ValueError(
                f"runtime must be between 1 and {self.maximum_runtime_minutes} minutes"
            )
        return value

    def write_enabled(self, explicit: bool | None = None) -> bool:
        environment_enabled = parse_bool(
            os.environ.get(self.write_environment_variable), default=False
        )
        if explicit is None:
            return environment_enabled
        return explicit and environment_enabled
