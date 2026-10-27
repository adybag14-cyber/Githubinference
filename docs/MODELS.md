# Model registry and promotion

## Pinned registry

| ID | Artifact size | Eligibility | Role |
|---|---:|---|---|
| `lfm3_1_7b_q4_k_m` | 1,099,627,808 bytes | automatic | Primary LiquidAI LFM3 1.7B caretaker |
| `lfm2_5_2_6b_q4_k_m` | 1,674,454,848 bytes | automatic | Previous primary and explicit rollback |
| `lfm2_5_1_2b_q4_k_m` | 730,895,168 bytes | automatic | Low-memory LiquidAI fallback |
| `gemma4_e2b_it_qat_q4_0` | 3,349,516,256 bytes | manual | Google Gemma 4 E2B IT QAT target |
| `gemma4_e2b_it_qat_mtp_q8_0` | 97,835,456 bytes | manual draft | Matching Gemma MTP assistant conversion |

The full revisions and SHA256 values are in `config/models.json`. `download-model` always checks exact byte size and digest, including restored Actions cache entries. Invalid cache entries are quarantined rather than executed.

## LFM3 migration and runtime

The default is the official [LiquidAI LFM3-1.7B Q4_K_M GGUF](https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF), pinned to revision `b3f2ae5d73cd3c91bf40e8ba752c648aa05dffdf`. Its 1.10 GB artifact is smaller than the previous 1.67 GB LFM2.5 2.6B artifact. The 1.2B entry remains the automatic low-memory fallback; the previous primary remains available in manual workflow choices for comparison and rollback.

The registry follows LiquidAI's general-purpose sampling guidance: temperature 0.1, top-k 50, and repetition penalty 1.1. The control plane disables thinking and constrains caretaker responses with a JSON schema. The configured context remains bounded to 16,384 tokens.

The published llama.cpp `b11935` binary predates [upstream LFM3 support](https://github.com/ggml-org/llama.cpp/pull/31252). `scripts/install_llama.sh` therefore builds the CPU server from source revision `19ff817fb25e9a1b838b1d7f9f4325f7bf9779c4`, whose archive SHA256 is `313e00aa459e0a7e2250afd29e52ae6a5774bbb504602fb1ef6ed4f6c7df0e30`. Every installation verifies the source archive and compiles afresh with four build workers; optional UI downloads, accelerator backends, and the unified app are disabled. Linux development hosts need CMake and a C/C++ build toolchain. GitHub's Ubuntu runner supplies these.

Before promotion, run `model-smoke.yml` on the candidate ref for LFM3, the previous primary, and the 1.2B fallback. Each run verifies downloaded bytes, exercises actual CPU inference and the caretaker contract, and uploads latency, process memory, disk usage, and generation-attempt evidence. A deterministic fallback after rejected model responses fails the contract smoke. Compare the recorded behavior rather than inferring task quality from parameter count or release date.

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
