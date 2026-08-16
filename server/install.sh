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

write_java_env() {
  local resolved java_dir java_home
  resolved=$(readlink -f "$(command -v "$JAVA_BIN")")
  java_dir=$(dirname "$resolved")
  java_home=$(dirname "$java_dir")
  cat > "$runtime/java.env" <<EOF
# Generated from the Java executable selected during installation.
JAVA_HOME=$java_home
PATH=$java_dir:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
EOF
}

render_template() {
  local input=$1
  local output=$2
  local line

  [[ -f "$input" ]] || fail "Missing template: $input"
  : > "$output"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line//@SERVER_DISPLAY_NAME@/$SERVER_DISPLAY_NAME}
    line=${line//@SERVER_PORT@/$SERVER_PORT}
    line=${line//@SERVER_MIN_MEMORY@/$SERVER_MIN_MEMORY}
    line=${line//@SERVER_MAX_MEMORY@/$SERVER_MAX_MEMORY}
    printf '%s\n' "$line" >> "$output"
  done < "$input"
}

: "${PACK_URL:?pack.env must define PACK_URL}"
: "${PACKWIZ_BOOTSTRAP_URL:?pack.env must define PACKWIZ_BOOTSTRAP_URL}"
: "${PACKWIZ_BOOTSTRAP_SHA256:?pack.env must define PACKWIZ_BOOTSTRAP_SHA256}"
: "${FORGE_VERSION:?pack.env must define FORGE_VERSION}"
: "${FORGE_INSTALLER_URL:?pack.env must define FORGE_INSTALLER_URL}"
: "${FORGE_INSTALLER_SHA256:?pack.env must define FORGE_INSTALLER_SHA256}"
: "${SERVER_DISPLAY_NAME:?pack.env must define SERVER_DISPLAY_NAME}"
: "${SERVER_PORT:?pack.env must define SERVER_PORT}"
: "${SERVER_MIN_MEMORY:?pack.env must define SERVER_MIN_MEMORY}"
: "${SERVER_MAX_MEMORY:?pack.env must define SERVER_MAX_MEMORY}"
[[ "$PACK_URL" == https://* || "$PACK_URL" == http://* ]] || fail 'PACK_URL must start with http:// or https://'
[[ "$PACK_URL" != *YOUR_GITHUB_OWNER* && "$PACK_URL" != *YOUR_REPOSITORY* ]] || fail 'Configure PACK_URL in pack.env before installing.'

runtime="$ROOT/server/runtime"
if [[ -r "$runtime/java.env" ]]; then
  # shellcheck disable=SC1090
  source "$runtime/java.env"
fi

require_command install
require_command readlink
require_command sha256sum
require_command systemctl
JAVA_BIN=${JAVA_BIN:-java}
require_command "$JAVA_BIN"
java_version=$({ "$JAVA_BIN" -version 2>&1 || true; } | awk -F '"' '/version/ {print $2; exit}')
[[ "$java_version" == 17.* ]] || fail "Java 17 is required. Found: ${java_version:-unknown}. Set JAVA_BIN to a Java 17 executable."

mkdir -p "$runtime"
write_java_env

bootstrap="$runtime/packwiz-installer-bootstrap.jar"
if [[ ! -f "$bootstrap" ]]; then
  require_command curl
  partial="$bootstrap.part"
  rm -f "$partial"
  curl --fail --location --retry 3 --retry-all-errors --output "$partial" "$PACKWIZ_BOOTSTRAP_URL"
  verify_sha256 "$PACKWIZ_BOOTSTRAP_SHA256" "$partial"
  install -m 0644 "$partial" "$bootstrap"
  rm -f "$partial"
else
  verify_sha256 "$PACKWIZ_BOOTSTRAP_SHA256" "$bootstrap"
fi

if [[ ! -s "$runtime/server.properties" ]]; then
  render_template "$ROOT/server/server.properties.example" "$runtime/server.properties"
fi

if [[ ! -s "$runtime/user_jvm_args.txt" ]]; then
  render_template "$ROOT/server/user_jvm_args.txt.example" "$runtime/user_jvm_args.txt"
fi

printf '%s\n' 'Synchronizing the server Packwiz subset...'
(cd "$runtime" && "$JAVA_BIN" -jar "$bootstrap" -g -s server "$PACK_URL")

if [[ ! -f "$runtime/run.sh" ]]; then
  require_command curl
  installer="$runtime/.forge-${FORGE_VERSION}-installer.jar"
  printf 'Installing Forge %s...\n' "$FORGE_VERSION"
  curl --fail --location --retry 3 --retry-all-errors --output "$installer" "$FORGE_INSTALLER_URL"
  verify_sha256 "$FORGE_INSTALLER_SHA256" "$installer"
  (
    cd "$runtime"
    "$JAVA_BIN" -jar "$installer" --installServer
  )
  rm -f "$installer"
fi

unit="$HOME/.config/systemd/user/minecraft-atfc.service"
install -D -m 0644 "$ROOT/server/minecraft-atfc.service" "$unit"
systemctl --user daemon-reload
systemctl --user enable minecraft-atfc.service

cat <<EOF
Installed Auto-TFC at: $runtime

Before the first start:
  1. Create $runtime/eula.txt with eula=true after reading Mojang's EULA.
  2. Review $runtime/server.properties and $runtime/user_jvm_args.txt.
  3. Start it with: systemctl --user start minecraft-atfc.service

If the user service must survive logout or reboot, run this manually if needed:
  sudo loginctl enable-linger "\$USER"
EOF
