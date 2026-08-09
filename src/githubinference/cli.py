from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from .backend import LlamaCppClient
from .caretaker import run_analysis
from .config import CaretakerConfig
from .downloader import download_model
from .executor import apply_decision
from .gateway import serve_gateway
from .github_api import GitHubClient
from .registry import ModelRegistry, ModelSpec
from .scout import scout_models
from .server import llama_server_command
from .snapshot import build_snapshot
from .subagent import run_subagent
from .util import atomic_write_json, load_json


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.function(args) or 0)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safety-bounded GitHub Actions CPU caretaker"
    )
    parser.add_argument("--models", default="config/models.json")
    parser.add_argument("--config", default="config/caretaker.json")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate all checked-in configuration")
    validate.set_defaults(function=_validate)

    download = sub.add_parser(
        "download-model", help="download and verify a pinned GGUF"
    )
    _model_arguments(download)
    download.set_defaults(function=_download)

    serve = sub.add_parser(
        "serve", help="replace this process with pinned llama-server"
    )
    _model_arguments(serve)
    serve.add_argument("--llama-server", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--threads", type=int, default=max(1, min(4, os.cpu_count() or 1))
    )
    serve.add_argument("--parallel", type=int, default=1)
    serve.add_argument("--enable-mtp", action="store_true")
    serve.set_defaults(function=_serve)

    wait = sub.add_parser("wait", help="wait for llama.cpp readiness")
    wait.add_argument("--base-url", default="http://127.0.0.1:8080")
    wait.add_argument("--timeout", type=int, default=240)
    wait.add_argument(
        "--pid",
        type=int,
        default=None,
        help="fail early if this exact server PID exits",
    )
    wait.set_defaults(function=_wait)

    smoke = sub.add_parser("smoke", help="perform one JSON-mode model inference")
    smoke.add_argument("--base-url", default="http://127.0.0.1:8080")
    smoke.add_argument("--model-id", default="caretaker")
    smoke.set_defaults(function=_smoke)

    contract = sub.add_parser(
        "caretaker-smoke",
        help="verify one real model response against the caretaker contract",
    )
    contract.add_argument("--snapshot", required=True)
    contract.add_argument("--base-url", default="http://127.0.0.1:8080")
    contract.add_argument("--model-id", default=None)
    contract.add_argument("--output", default=None)
    contract.set_defaults(function=_caretaker_smoke)

    snapshot = sub.add_parser("snapshot", help="build a bounded repository snapshot")
    snapshot.add_argument("--root", default=".")
    snapshot.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY", "local/repository")
    )
    snapshot.add_argument("--ref", default=os.environ.get("GITHUB_SHA", "local"))
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument(
        "--github", action="store_true", help="include GitHub issue/PR/run data"
    )
    snapshot.add_argument(
        "--scout", action="store_true", help="include report-only model discovery"
    )
    snapshot.set_defaults(function=_snapshot)

    analyze = sub.add_parser("analyze", help="run bounded caretaker turns")
    analyze.add_argument("--snapshot", required=True)
    analyze.add_argument("--output-directory", required=True)
    analyze.add_argument("--base-url", default="http://127.0.0.1:8080")
    analyze.add_argument("--model-id", default=None)
    analyze.add_argument("--runtime-minutes", type=int, default=None)
    analyze.add_argument("--run-id", default=None)
    analyze.set_defaults(function=_analyze)

    apply = sub.add_parser(
        "apply", help="apply deterministic policy in the write-capable job"
    )
    apply.add_argument("--decision", required=True)
    apply.add_argument("--snapshot", required=True)
    apply.add_argument("--output-directory", required=True)
    apply.add_argument("--run-id", required=True)
    apply.add_argument("--write-enabled", action="store_true")
    apply.set_defaults(function=_apply)

    subagent = sub.add_parser("subagent", help="run one bounded read-only subagent")
    subagent.add_argument("--snapshot", required=True)
    subagent.add_argument("--base-url", default="http://127.0.0.1:8080")
    subagent.add_argument("--model-id", default=None)
    subagent.add_argument("--task", required=True)
    subagent.add_argument("--scope-json", default="[]")
    subagent.add_argument("--parent-run", required=True)
    subagent.add_argument("--task-id", required=True)
    subagent.add_argument("--output", required=True)
    subagent.set_defaults(function=_subagent)

    scout = sub.add_parser("scout", help="write a report-only model discovery snapshot")
    scout.add_argument("--scout-config", default="config/scout.json")
    scout.add_argument("--output", required=True)
    scout.set_defaults(function=_scout)

    gateway = sub.add_parser(
        "gateway", help="serve the authenticated loopback inference gateway"
    )
    gateway.add_argument("--host", default="127.0.0.1")
    gateway.add_argument("--port", type=int, default=8787)
    gateway.add_argument("--upstream", default="http://127.0.0.1:8080")
    gateway.set_defaults(function=_gateway)

    endpoint_smoke = sub.add_parser(
        "endpoint-smoke",
        help="verify the bearer-protected endpoint without printing its key",
    )
    endpoint_smoke.add_argument("--url", required=True)
    endpoint_smoke.set_defaults(function=_endpoint_smoke)

    benchmark = sub.add_parser("benchmark", help="measure one bounded chat completion")
    benchmark.add_argument("--base-url", default="http://127.0.0.1:8080")
    benchmark.add_argument("--model-id", default="caretaker")
    benchmark.add_argument("--output", required=True)
    benchmark.set_defaults(function=_benchmark)
    return parser


def _model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--cache-directory", default=".cache/models")
    parser.add_argument("--allow-manual-model", action="store_true")


def _registry(args: argparse.Namespace) -> ModelRegistry:
    return ModelRegistry.load(args.models)


def _selected_model(args: argparse.Namespace) -> tuple[ModelRegistry, ModelSpec]:
    registry = _registry(args)
    spec = registry.get(args.model_id)
    if not spec.automatic_eligible and not args.allow_manual_model:
        raise ValueError(f"{spec.model_id} is manual-only; pass --allow-manual-model")
    return registry, spec


def _validate(args: argparse.Namespace) -> int:
    registry = _registry(args)
    config = CaretakerConfig.load(args.config)
    print(
        json.dumps(
            {
                "models": sorted(registry.models),
                "allowed_actions": sorted(config.allowed_actions),
            }
        )
    )
    return 0


def _download(args: argparse.Namespace) -> int:
    _, spec = _selected_model(args)
    print(download_model(spec, args.cache_directory))
    return 0


def _serve(args: argparse.Namespace) -> int:
    registry, spec = _selected_model(args)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("llama-server must remain bound to loopback")
    executable = shutil.which(args.llama_server) or args.llama_server
    if not Path(executable).is_file():
        raise ValueError(f"llama-server executable was not found: {args.llama_server}")
    model_path = download_model(spec, args.cache_directory)
    draft_spec = None
    draft_path = None
    if args.enable_mtp:
        if not spec.draft_model:
            raise ValueError("selected model has no pinned MTP assistant")
        draft_spec = registry.get(spec.draft_model)
        draft_path = download_model(draft_spec, args.cache_directory)
    command = llama_server_command(
        executable,
        spec,
        model_path,
        host=args.host,
        port=args.port,
        threads=args.threads,
        parallel=args.parallel,
        draft_spec=draft_spec,
        draft_path=draft_path,
    )
    os.execv(str(executable), command)
    return 0


def _wait(args: argparse.Namespace) -> int:
    LlamaCppClient(args.base_url).wait_until_ready(
        timeout_seconds=args.timeout, process_id=args.pid
    )
    print("ready")
    return 0


def _smoke(args: argparse.Namespace) -> int:
    client = LlamaCppClient(args.base_url, model=args.model_id)
    result = client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "Output exactly one JSON object and nothing else. Do not explain, reason, "
                    "or use Markdown. The only allowed keys are status and engine."
                ),
            },
            {
                "role": "user",
                "content": 'Return exactly this object: {"status":"ok","engine":"cpu"}',
            },
        ],
        max_tokens=256,
    )
    if not isinstance(result, dict) or not result:
        raise RuntimeError("model smoke response was empty")
    print(json.dumps(result, sort_keys=True))
    return 0


def _caretaker_smoke(args: argparse.Namespace) -> int:
    config = CaretakerConfig.load(args.config)
    spec = _registry(args).get(args.model_id)
    snapshot = load_json(args.snapshot)
    client = LlamaCppClient(
        args.base_url,
        model=spec.model_id,
        temperature=spec.temperature,
        top_p=spec.top_p,
        top_k=spec.top_k,
        repeat_penalty=spec.repeat_penalty,
    )
    destination = (
        Path(args.output).parent if args.output else Path(".runtime/caretaker-smoke")
    )
    result = run_analysis(
        backend=client,
        snapshot=snapshot,
        config=replace(config, maximum_turns=1),
        runtime_minutes=15,
        output_directory=destination,
        run_id="caretaker-contract-smoke",
        model_id=spec.model_id,
    ).to_dict()
    if args.output:
        atomic_write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    config = CaretakerConfig.load(args.config)
    github_data: dict[str, Any] | None = None
    subagent_results: list[dict[str, Any]] | None = None
    if args.github:
        client = GitHubClient.from_environment(require_token=True)
        assert client is not None
        github_data = client.read_snapshot(config.maximum_github_items)
        subagent_results = client.collect_subagent_results()
    scout_data = scout_models() if args.scout else None
    value = build_snapshot(
        args.root,
        config,
        repository=args.repository,
        ref=args.ref,
        github_data=github_data,
        scout_data=scout_data,
        subagent_results=subagent_results,
    )
    atomic_write_json(args.output, value)
    print(args.output)
    return 0


def _analyze(args: argparse.Namespace) -> int:
    config = CaretakerConfig.load(args.config)
    registry = _registry(args)
    spec = registry.get(args.model_id)
    runtime = config.runtime_minutes(args.runtime_minutes)
    backend = LlamaCppClient(
        args.base_url,
        model=spec.model_id,
        temperature=spec.temperature,
        top_p=spec.top_p,
        top_k=spec.top_k,
        repeat_penalty=spec.repeat_penalty,
    )
    result = run_analysis(
        backend=backend,
        snapshot=load_json(args.snapshot),
        config=config,
        runtime_minutes=runtime,
        output_directory=args.output_directory,
        run_id=args.run_id,
        model_id=spec.model_id,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


def _apply(args: argparse.Namespace) -> int:
    config = CaretakerConfig.load(args.config)
    explicit = True if args.write_enabled else None
    result = apply_decision(
        decision_data=load_json(args.decision),
        snapshot=load_json(args.snapshot),
        config=config,
        output_directory=args.output_directory,
        run_id=args.run_id,
        write_enabled=explicit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _subagent(args: argparse.Namespace) -> int:
    scope = json.loads(args.scope_json)
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ValueError("scope-json must be a JSON array of strings")
    spec = _registry(args).get(args.model_id)
    result = run_subagent(
        backend=LlamaCppClient(
            args.base_url,
            model=spec.model_id,
            temperature=spec.temperature,
            top_p=spec.top_p,
            top_k=spec.top_k,
            repeat_penalty=spec.repeat_penalty,
        ),
        task=args.task,
        scope=scope,
        snapshot=load_json(args.snapshot),
        parent_run=args.parent_run,
        task_id=args.task_id,
        output_path=args.output,
        model_id=spec.model_id,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _scout(args: argparse.Namespace) -> int:
    result = scout_models(args.scout_config)
    atomic_write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _gateway(args: argparse.Namespace) -> int:
    key = os.environ.get("INFERENCE_API_KEY", "")
    if not key:
        raise ValueError("INFERENCE_API_KEY is required")
    serve_gateway(api_key=key, host=args.host, port=args.port, upstream=args.upstream)
    return 0


def _endpoint_smoke(args: argparse.Namespace) -> int:
    key = os.environ.get("INFERENCE_API_KEY", "")
    if len(key) < 32:
        raise ValueError("INFERENCE_API_KEY must contain at least 32 characters")
    parsed = urllib.parse.urlparse(args.url)
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if not (parsed.scheme == "https" or loopback_http):
        raise ValueError("endpoint smoke URL must be HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "endpoint smoke URL cannot contain credentials, query, or fragment"
        )
    url = f"{args.url.rstrip('/')}/v1/models"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "githubinference-endpoint-smoke/0.1",
        },
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"endpoint returned HTTP {exc.code}") from exc
    if len(body) > 1024 * 1024:
        raise RuntimeError("endpoint model response exceeded 1 MiB")
    envelope = json.loads(body)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise RuntimeError("endpoint did not return an OpenAI-compatible model list")
    print(json.dumps({"status": "ok", "model_count": len(envelope["data"])}))
    return 0


def _benchmark(args: argparse.Namespace) -> int:
    validated_client = LlamaCppClient(args.base_url, model=args.model_id)
    payload = {
        "model": args.model_id,
        "messages": [
            {"role": "user", "content": "In one sentence, explain why tests matter."}
        ],
        "max_tokens": 96,
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{validated_client.base_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    opener = urllib.request.build_opener(_RejectRedirects())
    with opener.open(request, timeout=300) as response:
        envelope = json.loads(response.read(4 * 1024 * 1024))
    elapsed = time.monotonic() - started
    usage = envelope.get("usage", {}) if isinstance(envelope, dict) else {}
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    result = {
        "elapsed_seconds": round(elapsed, 3),
        "completion_tokens": completion_tokens,
        "completion_tokens_per_second": round(completion_tokens / elapsed, 3)
        if completion_tokens
        else None,
        "usage": usage,
    }
    atomic_write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
