# Architecture

## Trust boundaries

Githubinference has four distinct trust domains:

1. **Untrusted evidence** — repository files, issue/PR text, model-discovery metadata, old artifacts, subagent tasks, and every model response.
2. **Read-only analysis** — snapshot collection, llama.cpp, caretaker turns, model scout, and subagents. This job receives read permissions only.
3. **Deterministic policy** — strict schemas, budgets, path checks, label checks, idempotency markers, and write gating. There is no model-selected shell command or API endpoint.
4. **Write-capable apply** — a separate job that downloads a bounded analysis artifact and invokes fixed GitHub API methods. It receives no inference service and no arbitrary command from the model.

The split is important: prompt instructions are defense in depth, but GitHub permissions and deterministic code are the actual authority boundary.

## Caretaker lifecycle

1. Cron or `workflow_dispatch` starts `caretaker.yml`.
2. The read-only job checks out a fixed commit.
3. Snapshot collection reads bounded UTF-8 source/document files, recent GitHub state, old subagent results, and optional model-scout metadata. Obvious credential patterns are redacted.
4. A SHA256-verified GGUF is loaded by a pinned llama.cpp release bound to loopback.
5. The model produces a strict JSON decision. One correction attempt is allowed for malformed JSON.
6. Each turn is policy-validated and the cumulative action budget is enforced.
7. `decision.json`, `analysis.json`, and `snapshot.json` become an immutable workflow artifact.
8. A separate apply job re-parses and re-validates the decision. If `CARETAKER_WRITE_ENABLED` is false, it writes only an audit artifact.
9. If enabled, fixed API methods may comment, open one issue, dispatch bounded subagents, or create a draft report PR containing inert proposal files.
10. A continuation request is revisited at the next ordinary schedule. No recursive caretaker dispatch exists.

## Action contract

The model can request only these action types:

| Action | Effect after policy | Hard boundary |
|---|---|---|
| `review_issue` | Idempotent comment | Target must carry `caretaker:review`; max 3/run |
| `open_issue` | Idempotent issue | Max 1/run |
| `propose_change` | `.patch` plus explanation in draft report PR | Never applied; protected paths rejected |
| `propose_model` | Candidate report | Never edits model registry |
| `request_subagent` | Dispatch read-only workflow | Max 2/run; bounded task/scope |
| `checkpoint` | Markdown state record | Accepted only with `continuation: continue`; next normal schedule only |

The total across all model turns is at most eight actions.

## Model promotion

Discovery and promotion are separate:

1. The read-only Hugging Face scout collects bounded metadata from allowlisted publishers.
2. The caretaker may write a candidate report.
3. A maintainer identifies an exact GGUF, immutable revision, expected size, SHA256, and license.
4. `model-benchmark.yml` runs real CPU inference; quality-specific tests can be added to the PR.
5. A reviewed PR changes `config/models.json`.
6. `model-smoke.yml` verifies automatic models before merge.

No output from steps 1-2 is executable configuration.

## Subagents

A subagent is not a child process with shared memory or credentials. It is a separately metered, read-only `workflow_dispatch` run with a bounded task and scope. Its single JSON artifact is treated as untrusted evidence on a later caretaker snapshot. The apply policy permits at most two dispatches per parent run.

## Runtime boundary

The scheduled default is 45 minutes. Manual caretaker experiments may request up to 340 minutes. The analysis loop checks a reserve before every turn, then stores a checkpoint and exits. The workflow timeout is 355 minutes to preserve cleanup and artifact time below GitHub's six-hour job maximum.

The endpoint is separate: it accepts only manual dispatch, has a 65-minute job timeout, and enforces an internal 5-60 minute lifetime.
