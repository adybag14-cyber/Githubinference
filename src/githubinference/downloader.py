from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .registry import ModelSpec
from .util import sha256_file

UrlOpen = Callable[..., object]


class ModelDownloadError(RuntimeError):
    pass


class _SafeModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: object,
        fp: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> object | None:
        if urllib.parse.urlparse(new_url).scheme != "https":
            return None
        redirected = super().redirect_request(
            request,
            fp,
            code,
            message,
            headers,
            new_url,  # type: ignore[arg-type]
        )
        if redirected is not None:
            old_host = urllib.parse.urlparse(request.full_url).hostname  # type: ignore[attr-defined]
            new_host = urllib.parse.urlparse(new_url).hostname
            if old_host != new_host:
                redirected.remove_header("Authorization")
        return redirected


def _safe_urlopen(request: object, *, timeout: int) -> object:
    return urllib.request.build_opener(_SafeModelRedirect()).open(
        request, timeout=timeout
    )


def model_url(spec: ModelSpec) -> str:
    filename = urllib.parse.quote(spec.filename, safe="")
    return (
        f"https://huggingface.co/{spec.repository}/resolve/{spec.revision}/{filename}"
    )


def download_model(
    spec: ModelSpec,
    output_directory: str | os.PathLike[str],
    *,
    retries: int = 4,
    timeout_seconds: int = 60,
    urlopen: UrlOpen = _safe_urlopen,
) -> Path:
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / spec.cache_filename
    partial = destination.with_name(destination.name + ".part")
    if destination.exists():
        if _valid_file(destination, spec):
            _log(f"verified cached model: {destination}")
            return destination
        quarantine = destination.with_name(destination.name + ".invalid")
        os.replace(destination, quarantine)
        _log(f"quarantined invalid cached model as {quarantine.name}")
    if partial.exists() and partial.stat().st_size == spec.size_bytes:
        if _valid_file(partial, spec):
            os.replace(partial, destination)
            _log(f"promoted verified completed partial model: {destination}")
            return destination
        invalid_partial = partial.with_name(partial.name + ".invalid")
        os.replace(partial, invalid_partial)
        _log(f"quarantined invalid completed partial as {invalid_partial.name}")

    free_bytes = shutil.disk_usage(output).free
    present_bytes = partial.stat().st_size if partial.exists() else 0
    needed_bytes = max(0, spec.size_bytes - present_bytes) + 256 * 1024**2
    if free_bytes < needed_bytes:
        raise ModelDownloadError(
            f"insufficient disk: need {needed_bytes} bytes including reserve, have {free_bytes}"
        )

    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            _download_once(
                spec,
                partial,
                timeout_seconds=timeout_seconds,
                urlopen=urlopen,
            )
            if partial.stat().st_size != spec.size_bytes:
                raise ModelDownloadError(
                    f"downloaded size {partial.stat().st_size} does not match {spec.size_bytes}"
                )
            digest = sha256_file(partial)
            if digest != spec.sha256:
                raise ModelDownloadError(
                    f"download SHA256 mismatch: expected {spec.sha256}, got {digest}"
                )
            os.replace(partial, destination)
            _log(f"downloaded and verified {spec.display_name}: {destination}")
            return destination
        except (OSError, urllib.error.URLError, ModelDownloadError) as exc:
            last_error = exc
            if isinstance(exc, ModelDownloadError) and "SHA256 mismatch" in str(exc):
                corrupt = partial.with_name(partial.name + ".invalid")
                if partial.exists():
                    os.replace(partial, corrupt)
            if attempt < retries:
                delay = min(20, 2**attempt)
                _log(f"download attempt {attempt} failed; retrying in {delay}s: {exc}")
                time.sleep(delay)
    raise ModelDownloadError(
        f"model download failed after {retries} attempts: {last_error}"
    )


def _download_once(
    spec: ModelSpec,
    partial: Path,
    *,
    timeout_seconds: int,
    urlopen: UrlOpen,
) -> None:
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > spec.size_bytes:
        invalid = partial.with_name(partial.name + ".oversize")
        os.replace(partial, invalid)
        offset = 0
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "githubinference/0.1 (+https://github.com/adybag14-cyber/Githubinference)",
    }
    token = os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(model_url(spec), headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:  # type: ignore[attr-defined]
        status = getattr(response, "status", 200)
        append = bool(offset and status == 206)
        mode = "ab" if append else "wb"
        if offset and not append:
            _log("server did not honor Range; restarting partial download")
        with partial.open(mode) as handle:
            written = offset if append else 0
            while True:
                remaining_with_guard = spec.size_bytes - written + 1
                chunk = response.read(min(1024 * 1024, remaining_with_guard))
                if not chunk:
                    break
                written += len(chunk)
                if written > spec.size_bytes:
                    raise ModelDownloadError(
                        "download exceeded the pinned artifact size"
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())


def _valid_file(path: Path, spec: ModelSpec) -> bool:
    return path.stat().st_size == spec.size_bytes and sha256_file(path) == spec.sha256


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
