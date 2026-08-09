#!/usr/bin/env bash
set -euo pipefail

readonly LLAMA_TAG="b10333"
readonly LLAMA_SHA256="936ce04d98abe2a977e9dd2ff92659bb96947e136acee8f2bc3e21d8eaebbf23"
readonly LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-ubuntu-x64.tar.gz"
readonly INSTALL_DIR="${1:-.runtime/llama}"
readonly STAMP="${INSTALL_DIR}/.asset.sha256"

find_server() {
  find "${INSTALL_DIR}" -type f -name llama-server -print -quit 2>/dev/null || true
}

existing="$(find_server)"
if [[ -n "${existing}" && -x "${existing}" && -f "${STAMP}" ]] && grep -qx "${LLAMA_SHA256}" "${STAMP}"; then
  printf '%s\n' "${existing}"
  exit 0
fi

mkdir -p "${INSTALL_DIR}"
temporary="$(mktemp -d)"
trap 'rm -rf -- "${temporary}"' EXIT
archive="${temporary}/llama.tar.gz"

echo "Downloading pinned llama.cpp ${LLAMA_TAG}" >&2
curl --proto '=https' --tlsv1.2 --fail --location --retry 4 --retry-all-errors \
  --output "${archive}" "${LLAMA_URL}"
printf '%s  %s\n' "${LLAMA_SHA256}" "${archive}" | sha256sum --check --status

if tar -tzf "${archive}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo "Refusing unsafe paths in the pinned llama.cpp archive" >&2
  exit 1
fi
tar -xzf "${archive}" -C "${INSTALL_DIR}"

server="$(find_server)"
if [[ -z "${server}" ]]; then
  echo "The verified llama.cpp archive did not contain llama-server" >&2
  exit 1
fi
chmod 0755 "${server}"
printf '%s\n' "${LLAMA_SHA256}" > "${STAMP}"
printf '%s\n' "${server}"
