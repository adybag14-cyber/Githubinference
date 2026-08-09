from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any

from .util import bounded_text, redact_secrets, safe_slug

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_GENERATED_PATH = re.compile(r"^\.caretaker/(?:reports|proposals)/[a-z0-9._/-]+$")


class GitHubApiError(RuntimeError):
    pass


class _StripCrossHostAuthorization(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if urllib.parse.urlparse(newurl).scheme != "https":
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old_host = urllib.parse.urlparse(req.full_url).hostname
            new_host = urllib.parse.urlparse(newurl).hostname
            if old_host != new_host:
                redirected.remove_header("Authorization")
        return redirected


@dataclass(slots=True)
class GitHubClient:
    repository: str
    token: str
    api_url: str = "https://api.github.com"
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository must use owner/name form")
        parsed = urllib.parse.urlparse(self.api_url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            raise ValueError("GitHub API URL must be https://api.github.com")
        if not self.token:
            raise ValueError("GitHub token is required")
        self.api_url = self.api_url.rstrip("/")

    @classmethod
    def from_environment(cls, *, require_token: bool = True) -> "GitHubClient | None":
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not repository or not token:
            if require_token:
                raise ValueError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
            return None
        return cls(repository, token)

    def repository_info(self) -> dict[str, Any]:
        return self._request("GET", "")

    def read_snapshot(self, maximum_items: int = 20) -> dict[str, Any]:
        maximum_items = max(1, min(50, maximum_items))
        issues_raw = self._request(
            "GET",
            f"/issues?state=open&sort=updated&direction=desc&per_page={maximum_items}",
        )
        pulls_raw = self._request(
            "GET",
            f"/pulls?state=open&sort=updated&direction=desc&per_page={maximum_items}",
        )
        runs_raw = self._request(
            "GET", f"/actions/runs?per_page={maximum_items}&exclude_pull_requests=false"
        )
        issues = [
            self._normalize_issue(item)
            for item in issues_raw
            if isinstance(item, dict) and "pull_request" not in item
        ]
        pulls = [
            self._normalize_pull(item) for item in pulls_raw if isinstance(item, dict)
        ]
        runs = [
            {
                "id": item.get("id"),
                "name": str(item.get("name", ""))[:200],
                "event": str(item.get("event", ""))[:80],
                "status": str(item.get("status", ""))[:80],
                "conclusion": str(item.get("conclusion", ""))[:80],
                "head_branch": str(item.get("head_branch", ""))[:200],
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "html_url": item.get("html_url"),
            }
            for item in runs_raw.get("workflow_runs", [])
            if isinstance(item, dict)
        ]
        return {"issues": issues, "pull_requests": pulls, "workflow_runs": runs}

    def collect_subagent_results(
        self, maximum_results: int = 6
    ) -> list[dict[str, Any]]:
        maximum_results = max(0, min(10, maximum_results))
        if maximum_results == 0:
            return []
        artifacts = self._request("GET", "/actions/artifacts?per_page=40")
        results: list[dict[str, Any]] = []
        for artifact in artifacts.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            name = artifact.get("name")
            artifact_id = artifact.get("id")
            if (
                not isinstance(name, str)
                or not name.startswith("subagent-result-")
                or not isinstance(artifact_id, int)
                or artifact.get("expired")
            ):
                continue
            try:
                archive = self._request_bytes(
                    f"/actions/artifacts/{artifact_id}/zip", maximum=1024 * 1024
                )
                result = _read_subagent_archive(archive)
            except (GitHubApiError, ValueError, zipfile.BadZipFile):
                continue
            result["artifact_name"] = name[:240]
            result["artifact_created_at"] = artifact.get("created_at")
            results.append(result)
            if len(results) >= maximum_results:
                break
        return results

    def comment_once(self, issue_number: int, body: str, marker: str) -> dict[str, Any]:
        comments = self._request("GET", f"/issues/{issue_number}/comments?per_page=100")
        if any(
            marker in str(item.get("body", ""))
            for item in comments
            if isinstance(item, dict)
        ):
            return {"skipped": True, "reason": "marker already present"}
        return self._request(
            "POST",
            f"/issues/{issue_number}/comments",
            {"body": f"{body.rstrip()}\n\n{marker}"},
            expected=(201,),
        )

    def create_issue(self, title: str, body: str, marker: str) -> dict[str, Any]:
        existing = self._request("GET", "/issues?state=open&per_page=100")
        for item in existing:
            if isinstance(item, dict) and marker in str(item.get("body", "")):
                return {
                    "skipped": True,
                    "reason": "marker already present",
                    "html_url": item.get("html_url"),
                }
        return self._request(
            "POST",
            "/issues",
            {"title": title, "body": f"{body.rstrip()}\n\n{marker}"},
            expected=(201,),
        )

    def create_report_pull_request(
        self,
        *,
        run_id: str,
        title: str,
        body: str,
        files: dict[str, str],
    ) -> dict[str, Any]:
        if not files:
            return {"skipped": True, "reason": "no report files"}
        for path, content in files.items():
            if not _SAFE_GENERATED_PATH.fullmatch(path) or ".." in path.split("/"):
                raise ValueError(f"unsafe generated report path: {path!r}")
            bounded_text(content, 200000, field=f"generated file {path}")
        info = self.repository_info()
        default_branch = info.get("default_branch", "main")
        owner = self.repository.split("/", 1)[0]
        branch = f"caretaker/report-{safe_slug(run_id, maximum=45)}"
        existing = self._request(
            "GET",
            "/pulls?state=open&head="
            + urllib.parse.quote(f"{owner}:{branch}", safe="")
            + "&per_page=10",
        )
        if existing:
            return {
                "skipped": True,
                "reason": "report PR already exists",
                "html_url": existing[0].get("html_url"),
            }

        reference = self._request(
            "GET", f"/git/ref/heads/{urllib.parse.quote(default_branch, safe='')}"
        )
        base_sha = reference["object"]["sha"]
        commit = self._request("GET", f"/git/commits/{base_sha}")
        base_tree = commit["tree"]["sha"]
        tree_entries: list[dict[str, str]] = []
        for path, content in sorted(files.items()):
            blob = self._request(
                "POST",
                "/git/blobs",
                {"content": content, "encoding": "utf-8"},
                expected=(201,),
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )
        tree = self._request(
            "POST",
            "/git/trees",
            {"base_tree": base_tree, "tree": tree_entries},
            expected=(201,),
        )
        new_commit = self._request(
            "POST",
            "/git/commits",
            {
                "message": f"caretaker: add report for {run_id}",
                "tree": tree["sha"],
                "parents": [base_sha],
            },
            expected=(201,),
        )
        self._request(
            "POST",
            "/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]},
            expected=(201,),
        )
        return self._request(
            "POST",
            "/pulls",
            {
                "title": title,
                "head": branch,
                "base": default_branch,
                "body": body,
                "draft": True,
            },
            expected=(201,),
        )

    def dispatch_subagent(
        self,
        *,
        parent_run: str,
        task_id: str,
        task: str,
        scope: list[str],
    ) -> dict[str, Any]:
        default_branch = self.repository_info().get("default_branch", "main")
        self._request(
            "POST",
            "/actions/workflows/subagent.yml/dispatches",
            {
                "ref": default_branch,
                "inputs": {
                    "parent_run": parent_run[:80],
                    "task_id": task_id[:80],
                    "task": task[:4000],
                    "scope_json": json.dumps(scope, separators=(",", ":"))[:4000],
                },
            },
            expected=(204,),
        )
        return {"dispatched": True, "task_id": task_id}

    def _normalize_issue(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": item.get("number"),
            "title": redact_secrets(str(item.get("title", ""))[:500]),
            "body": redact_secrets(str(item.get("body") or "")[:12000]),
            "labels": [
                {"name": str(label.get("name", ""))[:100]}
                for label in item.get("labels", [])
                if isinstance(label, dict)
            ],
            "author": str((item.get("user") or {}).get("login", ""))[:100],
            "comments": int(item.get("comments", 0) or 0),
            "updated_at": item.get("updated_at"),
            "html_url": item.get("html_url"),
        }

    def _normalize_pull(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": item.get("number"),
            "title": redact_secrets(str(item.get("title", ""))[:500]),
            "body": redact_secrets(str(item.get("body") or "")[:12000]),
            "labels": [
                {"name": str(label.get("name", ""))[:100]}
                for label in item.get("labels", [])
                if isinstance(label, dict)
            ],
            "author": str((item.get("user") or {}).get("login", ""))[:100],
            "draft": bool(item.get("draft")),
            "head": str((item.get("head") or {}).get("ref", ""))[:240],
            "base": str((item.get("base") or {}).get("ref", ""))[:240],
            "updated_at": item.get("updated_at"),
            "html_url": item.get("html_url"),
        }

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        raw = self._request_bytes(
            path, method=method, body=body, maximum=8 * 1024 * 1024, expected=expected
        )
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubApiError("GitHub returned invalid JSON") from exc

    def _request_bytes(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        maximum: int,
        expected: tuple[int, ...] = (200,),
    ) -> bytes:
        if path and not path.startswith("/"):
            raise ValueError("GitHub API path must be empty or begin with /")
        url = f"{self.api_url}/repos/{self.repository}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "githubinference/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        opener = urllib.request.build_opener(_StripCrossHostAuthorization())
        last_error: BaseException | None = None
        for attempt in range(1, 4):
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    if response.status not in expected:
                        raise GitHubApiError(
                            f"unexpected GitHub status {response.status}"
                        )
                    raw = response.read(maximum + 1)
                    if len(raw) > maximum:
                        raise GitHubApiError("GitHub response exceeded configured size")
                    return raw
            except urllib.error.HTTPError as exc:
                detail = exc.read(8192).decode("utf-8", errors="replace")
                last_error = GitHubApiError(f"GitHub HTTP {exc.code}: {detail[:1000]}")
                if exc.code not in {403, 429, 500, 502, 503, 504} or attempt == 3:
                    raise last_error from exc
                retry_after = int(exc.headers.get("Retry-After", "0") or 0)
                time.sleep(min(20, max(retry_after, 2**attempt)))
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2**attempt)
        raise GitHubApiError(f"GitHub request failed: {last_error}")


def _read_subagent_archive(archive: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        if len(members) > 10:
            raise ValueError("subagent artifact has too many files")
        json_members = [
            item
            for item in members
            if item.filename.endswith(".json")
            and "/" not in item.filename.replace("\\", "/")
            and item.file_size <= 200000
        ]
        if len(json_members) != 1:
            raise ValueError("subagent artifact must contain one root JSON file")
        raw = bundle.read(json_members[0])
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("subagent result is not an object")
    return value
