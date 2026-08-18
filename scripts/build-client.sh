#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/pack.env"

fail() {
  printf 'ATFC: %s\n' "$*" >&2
  exit 1
}

: "${PACK_URL:?pack.env must define PACK_URL}"
: "${PACKWIZ_BOOTSTRAP_URL:?pack.env must define PACKWIZ_BOOTSTRAP_URL}"
: "${PACKWIZ_BOOTSTRAP_SHA256:?pack.env must define PACKWIZ_BOOTSTRAP_SHA256}"
: "${PACK_NAME:?pack.env must define PACK_NAME}"
: "${CLIENT_MIN_MEMORY_MB:?pack.env must define CLIENT_MIN_MEMORY_MB}"
: "${CLIENT_MAX_MEMORY_MB:?pack.env must define CLIENT_MAX_MEMORY_MB}"

PACKWIZ_COMMAND=${PACKWIZ:-packwiz}
command -v "$PACKWIZ_COMMAND" >/dev/null 2>&1 || fail "Packwiz command not found: $PACKWIZ_COMMAND"
command -v curl >/dev/null 2>&1 || fail 'curl is required to fetch the Packwiz bootstrap'
command -v envsubst >/dev/null 2>&1 || fail 'envsubst is required to render client/instance.cfg'
command -v sha256sum >/dev/null 2>&1 || fail 'sha256sum is required to verify the Packwiz bootstrap'
command -v zip >/dev/null 2>&1 || fail 'zip is required to build the Prism archive'

[[ -f "$ROOT/pack/pack.toml" ]] || fail 'pack/pack.toml is missing'
if find "$ROOT/pack" -type f -name 'distanthorizons*.pw.toml' -print -quit | grep -q .; then
  grep -Fxq 'distantGeneratorMode = "FEATURES"' "$ROOT/pack/config/DistantHorizons.toml" || fail 'DH must use normal FEATURES generation'
fi
(cd "$ROOT/pack" && "$PACKWIZ_COMMAND" list >/dev/null) || fail 'Packwiz could not parse pack/'

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/.minecraft"

export PACK_NAME PACK_URL CLIENT_MIN_MEMORY_MB CLIENT_MAX_MEMORY_MB
envsubst '${PACK_NAME} ${PACK_URL} ${CLIENT_MIN_MEMORY_MB} ${CLIENT_MAX_MEMORY_MB}' \
  < "$ROOT/client/instance.cfg" > "$stage/instance.cfg"
cp "$ROOT/client/mmc-pack.json" "$stage/mmc-pack.json"

bootstrap="$stage/.minecraft/packwiz-installer-bootstrap.jar"
curl --fail --location --retry 3 --retry-all-errors \
  --output "$bootstrap" "$PACKWIZ_BOOTSTRAP_URL"
actual_sha256=$(sha256sum "$bootstrap" | awk '{print $1}')
[[ "$actual_sha256" == "$PACKWIZ_BOOTSTRAP_SHA256" ]] || fail 'Packwiz bootstrap checksum mismatch'

if [[ -f "$ROOT/client/servers.dat" ]]; then
  cp "$ROOT/client/servers.dat" "$stage/.minecraft/servers.dat"
  touch -t 198001010000 "$stage/.minecraft/servers.dat"
fi

touch -t 198001010000 "$stage/instance.cfg" "$stage/mmc-pack.json" "$bootstrap"
output="$ROOT/dist/atfc-prism.zip"
mkdir -p "$ROOT/dist"
rm -f "$output"
(
  cd "$stage"
  files=(instance.cfg mmc-pack.json .minecraft/packwiz-installer-bootstrap.jar)
  [[ -f .minecraft/servers.dat ]] && files+=(.minecraft/servers.dat)
  zip -X -9 -q "$output" "${files[@]}"
)
zip -Tqq "$output"

if [[ "$PACK_URL" == *YOUR_GITHUB_OWNER* || "$PACK_URL" == *YOUR_REPOSITORY* ]]; then
  printf '%s\n' 'warning: PACK_URL is still a placeholder; configure pack.env before sharing.' >&2
fi
printf '%s\n' "$output"
