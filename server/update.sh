#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/pack.env"

fail() {
  printf 'ATFC: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

verify_sha256() {
  local expected=$1
  local file=$2
  local actual
  actual=$(sha256sum "$file" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || fail "Checksum mismatch for $file"
}

: "${PACK_URL:?pack.env must define PACK_URL}"
: "${PACKWIZ_BOOTSTRAP_SHA256:?pack.env must define PACKWIZ_BOOTSTRAP_SHA256}"
[[ "$PACK_URL" == https://* || "$PACK_URL" == http://* ]] || fail 'PACK_URL must start with http:// or https://'
[[ "$PACK_URL" != *YOUR_GITHUB_OWNER* && "$PACK_URL" != *YOUR_REPOSITORY* ]] || fail 'Configure PACK_URL in pack.env before updating.'

runtime="$ROOT/server/runtime"
if [[ -r "$runtime/java.env" ]]; then
  # shellcheck disable=SC1090
  source "$runtime/java.env"
fi

require_command readlink
require_command systemctl
require_command sha256sum
JAVA_BIN=${JAVA_BIN:-java}
require_command "$JAVA_BIN"
java_version=$({ "$JAVA_BIN" -version 2>&1 || true; } | awk -F '"' '/version/ {print $2; exit}')
[[ "$java_version" == 17.* ]] || fail "Java 17 is required. Found: ${java_version:-unknown}. Set JAVA_BIN to a Java 17 executable."

[[ -f "$runtime/run.sh" ]] || fail "Forge run.sh is missing. Run ./server/install.sh first."
bootstrap="$runtime/packwiz-installer-bootstrap.jar"
[[ -f "$bootstrap" ]] || fail "Missing $bootstrap. Run ./server/install.sh first."
verify_sha256 "$PACKWIZ_BOOTSTRAP_SHA256" "$bootstrap"

unit="$HOME/.config/systemd/user/minecraft-atfc.service"
mkdir -p "$(dirname "$unit")"
ln -sfn "$ROOT/server/minecraft-atfc.service" "$unit"
systemctl --user daemon-reload
systemctl --user reset-failed minecraft-atfc.service

if systemctl --user is-active --quiet minecraft-atfc.service; then
  printf '%s\n' 'Stopping minecraft-atfc.service...'
  systemctl --user stop minecraft-atfc.service
fi

printf '%s\n' 'Synchronizing the server Packwiz subset...'
if ! (cd "$runtime" && "$JAVA_BIN" -jar "$bootstrap" -g -s server "$PACK_URL"); then
  printf '%s\n' 'Pack synchronization failed. The server remains stopped.' >&2
  exit 1
fi

printf '%s\n' 'Starting minecraft-atfc.service...'
systemctl --user start minecraft-atfc.service
