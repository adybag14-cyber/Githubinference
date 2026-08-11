from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .util import load_json, utc_now

UrlOpen = Callable[..., object]


def scout_models(
    config_path: str | Path = "config/scout.json",
    *,
    urlopen: UrlOpen = urllib.request.urlopen,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    config = load_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("scout configuration must be an object")
    if config.get("schema_version") != 1:
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
    params = urllib.parse.urlencode(
        {
            "filter": "gguf",
            "pipeline_tag": "text-generation",
            "sort": "lastModified",
            "direction": "-1",
            "limit": int(config["query_limit"]),
            "full": "true",
        }
    )
    request = urllib.request.Request(
        f"https://huggingface.co/api/models?{params}",
        headers={"Accept": "application/json", "User-Agent": "githubinference/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # type: ignore[attr-defined]
            maximum = int(config["maximum_response_bytes"])
            raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("Hugging Face response exceeded configured size")
        models = json.loads(raw)
        if not isinstance(models, list):
            raise ValueError("Hugging Face response was not a list")
        candidates = _filter_models(models, config)
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


def _filter_models(models: list[Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    publishers = set(config["publishers"])
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
        if len(candidates) >= int(config["maximum_candidates"]):
            break
    return candidates
