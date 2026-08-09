from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .util import load_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    display_name: str
    kind: str
    repository: str
    revision: str
    filename: str
    sha256: str
    size_bytes: int
    license: str
    publisher_trust: str
    automatic_eligible: bool
    context_size: int
    temperature: float = 0.1
    top_p: float = 0.95
    top_k: int = 50
    repeat_penalty: float = 1.0
    draft_model: str | None = None
    notes: str = ""

    @property
    def cache_filename(self) -> str:
        return f"{self.model_id}-{self.sha256[:12]}-{self.filename}"


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    default_model: str
    models: dict[str, ModelSpec]

    @classmethod
    def load(cls, path: str | Path = "config/models.json") -> "ModelRegistry":
        raw = load_json(path)
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported model registry schema")
        models: dict[str, ModelSpec] = {}
        for model_id, entry in raw.get("models", {}).items():
            if not re.fullmatch(r"[a-z0-9_]{3,80}", model_id):
                raise ValueError(f"invalid model id: {model_id!r}")
            spec = ModelSpec(model_id=model_id, **entry)
            _validate_spec(spec)
            models[model_id] = spec
        default_model = raw.get("default_model")
        if default_model not in models:
            raise ValueError("default model is absent from registry")
        for spec in models.values():
            if spec.draft_model and spec.draft_model not in models:
                raise ValueError(f"unknown draft model for {spec.model_id}")
            if spec.draft_model and models[spec.draft_model].kind != "draft":
                raise ValueError(f"draft model for {spec.model_id} is not kind=draft")
        return cls(default_model=default_model, models=models)

    def get(self, model_id: str | None = None) -> ModelSpec:
        selected = model_id or self.default_model
        try:
            return self.models[selected]
        except KeyError as exc:
            raise ValueError(
                f"model {selected!r} is not in the pinned registry"
            ) from exc


def _validate_spec(spec: ModelSpec) -> None:
    if spec.kind not in {"chat", "draft"}:
        raise ValueError(f"invalid model kind for {spec.model_id}")
    if not _REPOSITORY.fullmatch(spec.repository):
        raise ValueError(f"invalid repository for {spec.model_id}")
    if not _REVISION.fullmatch(spec.revision):
        raise ValueError(f"revision for {spec.model_id} must be a full commit SHA")
    if not _SHA256.fullmatch(spec.sha256):
        raise ValueError(f"sha256 for {spec.model_id} is invalid")
    if Path(spec.filename).name != spec.filename or not spec.filename.endswith(".gguf"):
        raise ValueError(f"filename for {spec.model_id} is unsafe")
    if isinstance(spec.size_bytes, bool) or not isinstance(spec.size_bytes, int):
        raise ValueError(f"size for {spec.model_id} must be an integer")
    if spec.size_bytes <= 0 or spec.size_bytes > 12 * 1024**3:
        raise ValueError(f"size for {spec.model_id} is outside the runner budget")
    if isinstance(spec.context_size, bool) or not isinstance(spec.context_size, int):
        raise ValueError(f"context size for {spec.model_id} must be an integer")
    if spec.context_size < 512 or spec.context_size > 262144:
        raise ValueError(f"context size for {spec.model_id} is invalid")
    if not isinstance(spec.automatic_eligible, bool):
        raise ValueError(f"automatic eligibility for {spec.model_id} must be boolean")
    if spec.automatic_eligible and (
        spec.kind != "chat" or spec.publisher_trust != "upstream"
    ):
        raise ValueError(
            f"automatic model {spec.model_id} must be an upstream chat model"
        )
    if spec.kind == "draft" and spec.automatic_eligible:
        raise ValueError(f"draft model {spec.model_id} cannot be automatic")
    if not isinstance(spec.license, str) or not spec.license.strip():
        raise ValueError(f"license for {spec.model_id} is empty")
    if not isinstance(spec.publisher_trust, str) or not spec.publisher_trust.strip():
        raise ValueError(f"publisher trust for {spec.model_id} is empty")
    if spec.draft_model is not None and not isinstance(spec.draft_model, str):
        raise ValueError(f"draft model id for {spec.model_id} is invalid")
    if not 0 <= spec.temperature <= 2:
        raise ValueError(f"temperature for {spec.model_id} is invalid")
    if not 0 < spec.top_p <= 1:
        raise ValueError(f"top_p for {spec.model_id} is invalid")
    if not 0 <= spec.top_k <= 1000:
        raise ValueError(f"top_k for {spec.model_id} is invalid")
