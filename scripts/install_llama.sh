#!/usr/bin/env bash
set -euo pipefail

# The published b11935 binaries predate upstream LFM3 support. Build the
# reviewed source revision instead; never reuse an unverified cached executable.
readonly LLAMA_REVISION="19ff817fb25e9a1b838b1d7f9f4325f7bf9779c4"
readonly LLAMA_SHA256="313e00aa459e0a7e2250afd29e52ae6a5774bbb504602fb1ef6ed4f6c7df0e30"
readonly LLAMA_URL="https://github.com/ggml-org/llama.cpp/archive/${LLAMA_REVISION}.tar.gz"
readonly INSTALL_DIR="${1:-.runtime/llama}"
readonly CACHED_ARCHIVE="${INSTALL_DIR}/llama-${LLAMA_REVISION}-${LLAMA_SHA256}.tar.gz"
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
  echo "Downloading pinned llama.cpp source ${LLAMA_REVISION}" >&2
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
mkdir -p "${staging}/source"
tar -xzf "${CACHED_ARCHIVE}" --strip-components=1 -C "${staging}/source"

# Compile only the CPU server. Disable optional downloads and use static project
# libraries so the completed runtime can be moved out of the staging directory.
cmake -S "${staging}/source" -B "${staging}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_COMMIT="${LLAMA_REVISION}" \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DLLAMA_CONNECT=OFF \
  -DLLAMA_OPENSSL=OFF \
  -DGGML_CUDA=OFF >&2
cmake --build "${staging}/build" --target llama-server --parallel 4 >&2

server="$(find_server "${staging}/build")"
if [[ -z "${server}" ]]; then
  echo "The verified llama.cpp source did not produce llama-server" >&2
  exit 1
fi
chmod 0755 "${server}"
server_relative="${server#"${staging}/"}"

# Build afresh from the verified archive on every invocation, then replace the
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
