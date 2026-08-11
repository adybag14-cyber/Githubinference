#!/usr/bin/env bash
set -euo pipefail

readonly LLAMA_TAG="b10333"
readonly LLAMA_SHA256="936ce04d98abe2a977e9dd2ff92659bb96947e136acee8f2bc3e21d8eaebbf23"
readonly LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-ubuntu-x64.tar.gz"
readonly INSTALL_DIR="${1:-.runtime/llama}"
readonly CACHED_ARCHIVE="${INSTALL_DIR}/llama-${LLAMA_TAG}-${LLAMA_SHA256}.tar.gz"
readonly VERIFIED_DIR="${INSTALL_DIR}/verified-${LLAMA_SHA256}"

find_server() {
  find "$1" -type f -name llama-server -print -quit 2>/dev/null || true
}

mkdir -p "${INSTALL_DIR}"
temporary="$(mktemp -d)"
staging=""
backup=""

cleanup() {
  rm -rf -- "${temporary}"
  if [[ -n "${staging}" ]]; then
    rm -rf -- "${staging}"
  fi
  if [[ -n "${backup}" ]]; then
    rm -rf -- "${backup}"
  fi
}

trap cleanup EXIT
download="${temporary}/llama.tar.gz"

if [[ ! -f "${CACHED_ARCHIVE}" ]] ||
  ! printf '%s  %s\n' "${LLAMA_SHA256}" "${CACHED_ARCHIVE}" | sha256sum --check --status; then
  echo "Downloading pinned llama.cpp ${LLAMA_TAG}" >&2
  curl --proto '=https' --tlsv1.2 --fail --location --retry 4 --retry-all-errors \
    --output "${download}" "${LLAMA_URL}"
  printf '%s  %s\n' "${LLAMA_SHA256}" "${download}" | sha256sum --check --status
  install -m 0644 "${download}" "${CACHED_ARCHIVE}"
fi
printf '%s  %s\n' "${LLAMA_SHA256}" "${CACHED_ARCHIVE}" | sha256sum --check --status

if tar -tzf "${CACHED_ARCHIVE}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo "Refusing unsafe paths in the pinned llama.cpp archive" >&2
  exit 1
fi
staging="$(mktemp -d "${INSTALL_DIR}/.verified-${LLAMA_SHA256}.XXXXXX")"
tar -xzf "${CACHED_ARCHIVE}" -C "${staging}"

server="$(find_server "${staging}")"
if [[ -z "${server}" ]]; then
  echo "The verified llama.cpp archive did not contain llama-server" >&2
  exit 1
fi
chmod 0755 "${server}"
server_relative="${server#"${staging}/"}"

# Extract afresh from the verified archive on every invocation, then replace the
# one hash-addressed runtime directory. This avoids trusting cached executables
# while preventing complete releases from accumulating across runs.
if [[ -e "${VERIFIED_DIR}" ]]; then
  backup="$(mktemp -d "${INSTALL_DIR}/.previous-${LLAMA_SHA256}.XXXXXX")"
  rmdir "${backup}"
  mv -- "${VERIFIED_DIR}" "${backup}"
fi
if ! mv -- "${staging}" "${VERIFIED_DIR}"; then
  if [[ -n "${backup}" && -e "${backup}" ]]; then
    mv -- "${backup}" "${VERIFIED_DIR}"
    backup=""
  fi
  exit 1
fi
staging=""
if [[ -n "${backup}" ]]; then
  rm -rf -- "${backup}"
  backup=""
fi

# Clean up directories produced by older installer revisions and superseded
# pins only after the new verified runtime is in place.
find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'verified-*' \
  ! -path "${VERIFIED_DIR}" -exec rm -rf -- {} +
printf '%s\n' "${VERIFIED_DIR}/${server_relative}"
