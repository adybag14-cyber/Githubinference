# Security policy

Githubinference runs untrusted repository and user-authored text through an LLM, so prompt injection is an expected condition, not an exceptional one. Security depends on capability separation and deterministic validation rather than model obedience.

## Supported version

Only the latest `main` branch is supported during this experiment.

## Report a vulnerability

Use GitHub's private vulnerability reporting feature if it is enabled for the repository. Do not open a public issue containing a credential, exploit payload, tunnel token, API key, or private endpoint details. Revoke exposed credentials before waiting for a response.

## Hard boundaries

- The inference job has read-only GitHub permissions.
- All file, issue, PR, scout, task, artifact, and model text is tagged and handled as untrusted data.
- The LLM has no shell/tool calling and chooses no URL, command, permission, secret, branch, or GitHub API route.
- The apply job reparses the artifact with a fixed schema and action budgets.
- Writes are off unless `CARETAKER_WRITE_ENABLED` parses as true.
- Comments require `caretaker:review` and use idempotency markers.
- Proposed diffs are stored as inert files; binary patches, renames, traversal, protected paths, and more than 20 paths are rejected.
- Model promotion is maintainer-reviewed and digest-pinned.
- The endpoint is manual, environment-scoped, loopback-only behind a bearer gateway, and capped at 60 minutes.
- The caretaker cannot merge, approve, push to `main`, change settings, create releases, or recursively dispatch itself.

## Residual risks

- Model output may be inaccurate, offensive, copyrighted, or subtly malicious.
- A maintainer can still make a bad decision when reviewing a model proposal.
- GitHub Actions, Hugging Face, Cloudflare, model publishers, and pinned release accounts are supply-chain dependencies.
- SHA pinning protects immutability after review; it does not prove that the reviewed artifact was benign.
- Public repositories expose workflow logs and non-secret artifacts. Snapshot/artifact contents must be treated as public.
- Regex redaction is defense in depth and cannot recognize every credential format. Never place secrets in issues or tracked files.
- A bearer key can be shared or stolen. Use a long random key, short endpoint windows, environment reviewers, and rotation.
- The endpoint has no availability guarantee and only one concurrent inference slot.

## Maintainer checklist for sensitive changes

Require careful human review for changes to `.github/workflows/`, `scripts/install_*`, `config/caretaker.json`, `config/models.json`, `src/githubinference/policy.py`, `src/githubinference/github_api.py`, `src/githubinference/gateway.py`, and this file. Do not run secret-bearing workflows from untrusted pull-request code.
