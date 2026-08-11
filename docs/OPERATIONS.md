# Operations

## Initial repository activation

1. Merge the bootstrap draft PR into the default branch after CI and the real model smoke pass.
2. Confirm Actions are enabled for the repository.
3. Keep `CARETAKER_WRITE_ENABLED=false` for the first manual caretaker run.
4. Inspect both artifacts: `caretaker-analysis-*` contains the raw bounded decision; `caretaker-apply-*` proves the write gate remained closed.
5. Add `caretaker:review` only to issues or pull requests where automated feedback is wanted.
6. Set `CARETAKER_WRITE_ENABLED=true` only after reviewing the first outputs.

Recommended commands:

```bash
gh variable set CARETAKER_WRITE_ENABLED --repo adybag14-cyber/Githubinference --body false
gh label create caretaker:review --repo adybag14-cyber/Githubinference --color 1d76db --description "Allow bounded caretaker comments" --force
gh label create caretaker:state --repo adybag14-cyber/Githubinference --color 5319e7 --description "Caretaker state and follow-up" --force
gh workflow run model-smoke.yml --repo adybag14-cyber/Githubinference
gh workflow run caretaker.yml --repo adybag14-cyber/Githubinference
```

## Write rollback

The fastest rollback is immediate and does not require a code deployment:

```bash
gh variable set CARETAKER_WRITE_ENABLED --repo adybag14-cyber/Githubinference --body false
gh run list --repo adybag14-cyber/Githubinference --workflow caretaker.yml
```

Cancel an active run from the Actions UI or with `gh run cancel RUN_ID`. Existing report branches/PRs are ordinary GitHub objects and can be closed manually. Idempotency markers prevent a retried run from repeating the same comment or issue.

## Configure the temporary endpoint

This requires a Cloudflare account and a named, remotely managed tunnel. Do not use a random Quick Tunnel as a production address.

1. In Cloudflare Zero Trust, create a named tunnel.
2. Add a public hostname such as `inference.example.com` whose service is `http://localhost:8787`.
3. Copy the tunnel token.
4. Generate a client key locally, for example `openssl rand -base64 48`. Keep a copy in a password manager.
5. In GitHub, create an environment named `inference`. Environment protection/reviewer rules are recommended.
6. Add environment secrets `CLOUDFLARE_TUNNEL_TOKEN` and `INFERENCE_API_KEY`.
7. Add environment variable `INFERENCE_PUBLIC_URL` with the HTTPS hostname.
8. Dispatch `endpoint.yml` for 5-330 minutes (up to 5 hours 30 minutes).
9. The workflow verifies local gateway authentication, waits for a registered tunnel connection, and performs an authenticated public `/v1/models` probe before announcing readiness.

The model and gateway stay on loopback. `cloudflared` makes the outbound connection; no inbound runner port is opened. The gateway provides application authentication even if Cloudflare Access is not configured. For stronger protection, add a Cloudflare Access service-token policy in front of the same hostname as a second factor.

The workflow reserves a 355-minute job window. A maximum-length 330-minute contributor session therefore retains 25 minutes for pinned-model startup or fallback, tunnel and authenticated public readiness checks, and deterministic process cleanup.

GitHub never reveals an Actions secret after it is stored. Maintainers/contributors who need client access must receive `INFERENCE_API_KEY` out of band. Repository contributors should not be granted permission merely to read a key, because there is no such read-back mechanism.

## Endpoint rollback and incident response

1. Cancel the endpoint workflow. Its exact model, gateway, and tunnel PIDs are terminated by the job trap.
2. Rotate/delete `INFERENCE_API_KEY` in the `inference` environment.
3. Rotate the Cloudflare tunnel token or disable/delete the public hostname.
4. Review the live job status. Endpoint logs and the temporary tunnel-token file are deliberately not uploaded and disappear with the runner.

Never place either secret in a workflow input, issue, pull request, artifact, cache, command output, or repository file.

## Model fallback

If the primary cannot load:

The scheduled caretaker and temporary endpoint automatically try `lfm2_5_1_2b_q4_k_m` only when the primary process exits before health. The artifact/job summary records the active model. A manually selected fallback does not cascade further.

1. Manually dispatch `model-smoke.yml` with `lfm2_5_1_2b_q4_k_m` to diagnose it independently.
2. If it passes, manually run `caretaker.yml` with that model.
3. Change the scheduled default only through a reviewed PR if the primary failure persists.

Gemma and MTP are benchmark-only until promotion criteria in `docs/MODELS.md` pass.

## Schedule behavior

GitHub schedules can run late or be dropped at high load. This repository uses minute 17 instead of the top of the hour to reduce peak congestion. Scheduled workflows execute only from the default branch and may be disabled after 60 days of no repository activity. A missed run is not recovered with a recursive loop; the next ordinary cron or a maintainer dispatch is the recovery path.

## Artifact and cache hygiene

- Analysis artifacts: 14 days.
- Apply audits and benchmarks: 30 days.
- Subagent artifacts: 7 days.
- Endpoint logs and tunnel-token files: never uploaded; deleted with the ephemeral runner.
- Model cache entries are content-addressed in the filename and rehashed before use.
- Workflow actions and downloaded binaries are pinned by immutable digest/commit.
