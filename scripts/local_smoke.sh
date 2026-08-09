#!/usr/bin/env bash
set -euo pipefail

readonly MODEL_ID="${1:-lfm2_5_2_6b_q4_k_m}"
readonly PORT="${2:-18080}"
readonly CACHE_DIRECTORY="${MODEL_CACHE_DIRECTORY:-.cache/models}"
readonly LOG_DIRECTORY="${LOCAL_SMOKE_LOG_DIRECTORY:-.runtime/local-smoke}"

if ! [[ "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "Port must be an integer between 1024 and 65535" >&2
  exit 2
fi

export PYTHONPATH="${PYTHONPATH:-src}"
mkdir -p "${LOG_DIRECTORY}"
llama_server="$(bash scripts/install_llama.sh .runtime/llama)"
manual=()
if [[ "${MODEL_ID}" == gemma4_* ]]; then
  manual=(--allow-manual-model)
fi

python3 -m githubinference serve \
  --model-id "${MODEL_ID}" \
  --cache-directory "${CACHE_DIRECTORY}" \
  --llama-server "${llama_server}" \
  --port "${PORT}" \
  "${manual[@]}" >"${LOG_DIRECTORY}/llama-server.log" 2>&1 &
server_pid=$!

cleanup() {
  if kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}"
    wait "${server_pid}" || true
  fi
}
trap cleanup EXIT INT TERM

ready=false
for (( attempt=1; attempt<=300; attempt++ )); do
  if curl --fail --silent "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    wait "${server_pid}"
    exit 1
  fi
  sleep 2
done
if [[ "${ready}" != true ]]; then
  echo "Model did not become ready; inspect ${LOG_DIRECTORY}/llama-server.log" >&2
  exit 1
fi

python3 -m githubinference smoke \
  --base-url "http://127.0.0.1:${PORT}" \
  --model-id "${MODEL_ID}"

python3 -m githubinference snapshot \
  --root . \
  --repository local/Githubinference \
  --ref local-smoke \
  --output "${LOG_DIRECTORY}/snapshot.json"
python3 -m githubinference caretaker-smoke \
  --snapshot "${LOG_DIRECTORY}/snapshot.json" \
  --base-url "http://127.0.0.1:${PORT}" \
  --model-id "${MODEL_ID}" \
  --output "${LOG_DIRECTORY}/caretaker-contract.json"
