from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .util import load_json, utc_now

UrlOpen = Callable[..., object]

_INTEGER_RANGES = {
    "query_limit": (1, 1_000),
    "maximum_response_bytes": (1, 16 * 1024 * 1024),
    "maximum_candidates": (0, 1_000),
}


def scout_models(
    config_path: str | Path = "config/scout.json",
    *,
    urlopen: UrlOpen = urllib.request.urlopen,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    config = load_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("scout configuration must be an object")
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported scout configuration schema")
    required = {
        "query_limit",
        "maximum_response_bytes",
        "publishers",
        "maximum_candidates",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"scout configuration is missing keys: {sorted(missing)}")
    validated_integers: dict[str, int] = {}
    for key, (minimum, maximum) in _INTEGER_RANGES.items():
        value = config[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(
                f"{key} must be an integer between {minimum} and {maximum}"
            )
        validated_integers[key] = value
    publishers = config["publishers"]
    if not isinstance(publishers, list) or not all(
        isinstance(publisher, str) and publisher for publisher in publishers
    ):
        raise ValueError("publishers must be a list of non-empty strings")
    query_limit = validated_integers["query_limit"]
    maximum_response_bytes = validated_integers["maximum_response_bytes"]
    maximum_candidates = validated_integers["maximum_candidates"]
    params = urllib.parse.urlencode(
        {
            "filter": "gguf",
            "pipeline_tag": "text-generation",
            "sort": "lastModified",
            "direction": "-1",
            "limit": query_limit,
            "full": "true",
        }
    )
    request = urllib.request.Request(
        f"https://huggingface.co/api/models?{params}",
        headers={"Accept": "application/json", "User-Agent": "githubinference/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # type: ignore[attr-defined]
            raw = response.read(maximum_response_bytes + 1)
        if len(raw) > maximum_response_bytes:
            raise ValueError("Hugging Face response exceeded configured size")
        models = json.loads(raw)
        if not isinstance(models, list):
            raise ValueError("Hugging Face response was not a list")
        candidates = _filter_models(
            models,
            publishers=set(publishers),
            maximum_candidates=maximum_candidates,
        )
        return {
            "status": "ok",
            "captured_at": utc_now(),
            "source": "https://huggingface.co/api/models",
            "trust_boundary": "Discovery metadata is untrusted and cannot directly change config/models.json.",
            "candidates": candidates,
        }
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "captured_at": utc_now(),
            "error": str(exc)[:500],
            "candidates": [],
        }


def _filter_models(
    models: list[Any], *, publishers: set[str], maximum_candidates: int
) -> list[dict[str, Any]]:
    if maximum_candidates == 0:
        return []
    candidates: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("modelId")
        if not isinstance(model_id, str) or "/" not in model_id:
            continue
        owner = model_id.split("/", 1)[0]
        if (
            owner not in publishers
            or item.get("private")
            or item.get("gated") not in {False, None}
        ):
            continue
        tags = [tag for tag in item.get("tags", []) if isinstance(tag, str)][:30]
        candidates.append(
            {
                "id": model_id[:200],
                "sha": str(item.get("sha", ""))[:80],
                "last_modified": str(item.get("lastModified", ""))[:80],
                "downloads": int(item.get("downloads", 0) or 0),
                "likes": int(item.get("likes", 0) or 0),
                "pipeline_tag": str(item.get("pipeline_tag", ""))[:80],
                "library_name": str(item.get("library_name", ""))[:80],
                "tags": tags,
            }
        )
        if len(candidates) >= maximum_candidates:
            break
    return candidates
