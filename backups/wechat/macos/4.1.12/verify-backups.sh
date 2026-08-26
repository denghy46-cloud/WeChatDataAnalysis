#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

shasum -a 256 -c SHA256SUMS

for image in ./*.dmg; do
  hdiutil verify "$image" >/dev/null
  echo "DMG checksum valid: $(basename "$image")"
done
