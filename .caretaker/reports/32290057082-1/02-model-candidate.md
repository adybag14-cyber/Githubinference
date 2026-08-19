# Model candidate (benchmark required)

- Repository: `LiquidAI/LFM2.5-2.6B-GGUF`
- Revision: `not supplied`

## Rationale

The primary caretaker model (LFM2.5 2.6B) is the largest and most resource-intensive. While it's the primary model, there's no evidence of memory or performance issues in the current snapshot. No immediate change is needed, but benchmarking against the fallback model (LFM2.5 1.2B) could validate memory efficiency.

## Evidence URLs

- https://github.com/adybag14-cyber/Githubinference/actions/

This candidate cannot enter the automatic registry without an immutable revision, artifact digest, compatible license, CPU smoke/quality benchmarks, and a rollback plan.
