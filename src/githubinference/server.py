from __future__ import annotations

from pathlib import Path

from .registry import ModelSpec


def llama_server_command(
    executable: str | Path,
    spec: ModelSpec,
    model_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    threads: int = 4,
    parallel: int = 1,
    draft_spec: ModelSpec | None = None,
    draft_path: str | Path | None = None,
) -> list[str]:
    if spec.kind != "chat":
        raise ValueError("the target model must be a chat model")
    if not 1 <= port <= 65535:
        raise ValueError("port is invalid")
    if threads < 1 or parallel < 1:
        raise ValueError("threads and parallel must be positive")
    command = [
        str(executable),
        "--model",
        str(model_path),
        "--alias",
        spec.model_id,
        "--host",
        host,
        "--port",
        str(port),
        "--ctx-size",
        str(spec.context_size),
        "--threads",
        str(threads),
        "--threads-batch",
        str(threads),
        "--parallel",
        str(parallel),
        "--jinja",
        "--reasoning",
        "off",
        "--reasoning-budget",
        "0",
        "--metrics",
        "--no-webui",
        "--temp",
        str(spec.temperature),
        "--top-p",
        str(spec.top_p),
        "--top-k",
        str(spec.top_k),
        "--repeat-penalty",
        str(spec.repeat_penalty),
    ]
    if draft_spec is not None or draft_path is not None:
        if draft_spec is None or draft_path is None:
            raise ValueError("both draft spec and draft path are required")
        if spec.draft_model != draft_spec.model_id or draft_spec.kind != "draft":
            raise ValueError("draft model is not the pinned companion for this target")
        command.extend(
            [
                "--spec-type",
                "draft-mtp",
                "--spec-draft-model",
                str(draft_path),
                "--spec-draft-n-max",
                "2",
                "--spec-draft-p-min",
                "0.8",
            ]
        )
    return command
