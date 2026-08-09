#!/usr/bin/env bash
set -euo pipefail

readonly CLOUDFLARED_TAG="2026.7.3"
readonly CLOUDFLARED_SHA256="9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17"
readonly CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_TAG}/cloudflared-linux-amd64"
readonly INSTALL_DIR="${1:-.runtime/cloudflared}"
readonly BINARY="${INSTALL_DIR}/cloudflared"
readonly STAMP="${INSTALL_DIR}/.asset.sha256"

if [[ -x "${BINARY}" && -f "${STAMP}" ]] && grep -qx "${CLOUDFLARED_SHA256}" "${STAMP}"; then
  printf '%s\n' "${BINARY}"
  exit 0
fi

mkdir -p "${INSTALL_DIR}"
temporary="$(mktemp -d)"
trap 'rm -rf -- "${temporary}"' EXIT
download="${temporary}/cloudflared"

echo "Downloading pinned cloudflared ${CLOUDFLARED_TAG}" >&2
curl --proto '=https' --tlsv1.2 --fail --location --retry 4 --retry-all-errors \
  --output "${download}" "${CLOUDFLARED_URL}"
printf '%s  %s\n' "${CLOUDFLARED_SHA256}" "${download}" | sha256sum --check --status
install -m 0755 "${download}" "${BINARY}"
printf '%s\n' "${CLOUDFLARED_SHA256}" > "${STAMP}"
printf '%s\n' "${BINARY}"
