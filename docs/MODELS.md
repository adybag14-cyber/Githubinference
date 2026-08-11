# Model registry and promotion

## Pinned initial registry

| ID | Artifact size | Eligibility | Role |
|---|---:|---|---|
| `lfm2_5_2_6b_q4_k_m` | 1,674,454,848 bytes | automatic | Primary LiquidAI LFM2.5 2.6B caretaker |
| `lfm2_5_1_2b_q4_k_m` | 730,895,168 bytes | automatic | Low-memory LiquidAI fallback |
| `gemma4_e2b_it_qat_q4_0` | 3,349,516,256 bytes | manual | Google Gemma 4 E2B IT QAT target |
| `gemma4_e2b_it_qat_mtp_q8_0` | 97,835,456 bytes | manual draft | Matching Gemma MTP assistant conversion |

The full revisions and SHA256 values are in `config/models.json`. `download-model` always checks exact byte size and digest, including restored Actions cache entries. Invalid cache entries are quarantined rather than executed.

## Why LFM first

The 2.6B Q4 model is small enough to fit comfortably inside the current standard public Linux runner while still being materially more capable than sub-billion parameter caretakers. The 1.2B entry provides a known, official fallback if load time or memory pressure changes.

## Gemma 4 E2B and MTP

Gemma 4 E2B IT is included exactly as requested, but as a manual benchmark candidate. Its 3.35 GB quantized target plus runtime/context memory is meaningfully heavier on a runner with finite memory and disk. The optional multi-token-prediction assistant is passed to llama.cpp using `draft-mtp` speculative decoding.

The target is an official Google GGUF. The MTP assistant entry is a pinned community conversion of Google's matching assistant, so it cannot be selected by scheduled automation. Compare Gemma with and without MTP using `model-benchmark.yml`, record both latency and output quality, and promote it only through review.

## Promotion checklist

- Official or otherwise reviewed publisher and provenance.
- License compatible with intended use.
- Full immutable source revision.
- Exact filename, byte count, and independently verified SHA256.
- CPU load/inference succeeds on `ubuntu-latest` within the timeout.
- Peak disk and memory fit the runner with cleanup reserve.
- Repository-maintenance JSON quality is at least as good as the current model.
- Prompt-injection and malformed-output regression cases pass.
- A low-memory rollback remains pinned.
- Maintainer-reviewed PR; never a model-authored direct registry edit.

The scout is intentionally unable to download, execute, or register candidates.
