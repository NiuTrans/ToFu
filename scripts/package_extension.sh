#!/usr/bin/env bash
#
# package_extension.sh — build a Chrome Web Store upload zip from
# browser_extension/.
#
# Usage:
#   scripts/package_extension.sh            # zip the dev extension as-is
#   scripts/package_extension.sh --store    # zip with the trimmed store manifest
#
# Output: dist/tofu-browser-bridge-<version>[-store].zip
#
# The store zip uses docs/chrome-web-store/manifest.store.json, which drops the
# permissions the code cannot justify (see PERMISSIONS_JUSTIFICATION.md). The
# actual code files (background.js, popup.*, icons) are copied unchanged.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/browser_extension"
STORE_MANIFEST="$REPO_ROOT/docs/chrome-web-store/manifest.store.json"
OUT_DIR="$REPO_ROOT/dist"

STORE_BUILD=0
if [[ "${1:-}" == "--store" ]]; then
  STORE_BUILD=1
fi

if [[ ! -d "$SRC_DIR" ]]; then
  echo "ERROR: $SRC_DIR not found" >&2
  exit 1
fi

# Resolve the version from whichever manifest we will ship.
if [[ "$STORE_BUILD" == "1" ]]; then
  MANIFEST_FOR_VERSION="$STORE_MANIFEST"
else
  MANIFEST_FOR_VERSION="$SRC_DIR/manifest.json"
fi
VERSION="$(grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]+"' "$MANIFEST_FOR_VERSION" \
            | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?')"
if [[ -z "$VERSION" ]]; then
  echo "ERROR: could not parse version from $MANIFEST_FOR_VERSION" >&2
  exit 1
fi

SUFFIX=""
[[ "$STORE_BUILD" == "1" ]] && SUFFIX="-store"
ZIP_NAME="tofu-browser-bridge-${VERSION}${SUFFIX}.zip"

mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Copy only the files the extension actually needs — never the whole repo.
cp "$SRC_DIR/background.js" "$STAGE/"
cp "$SRC_DIR/origin_marker.js" "$STAGE/"
cp "$SRC_DIR/popup.html"    "$STAGE/"
cp "$SRC_DIR/popup.js"      "$STAGE/"
cp "$SRC_DIR/icon16.png"    "$STAGE/"
cp "$SRC_DIR/icon48.png"    "$STAGE/"
cp "$SRC_DIR/icon128.png"   "$STAGE/"

if [[ "$STORE_BUILD" == "1" ]]; then
  cp "$STORE_MANIFEST" "$STAGE/manifest.json"
  echo "[package] using TRIMMED store manifest ($(python3 -c "
import json,sys
d=set(json.load(open('$SRC_DIR/manifest.json'))['permissions'])
s=set(json.load(open('$STORE_MANIFEST'))['permissions'])
print('%d permissions removed, %d declared' % (len(d-s), len(s)))
" 2>/dev/null || echo 'permission-trimmed'))"
else
  cp "$SRC_DIR/manifest.json" "$STAGE/manifest.json"
  echo "[package] using dev manifest as-is"
fi

OUT_ZIP="$OUT_DIR/$ZIP_NAME"
rm -f "$OUT_ZIP"
( cd "$STAGE" && zip -r -q "$OUT_ZIP" . -x '*.DS_Store' )

echo "[package] wrote $OUT_ZIP"
echo "[package] contents:"
( cd "$STAGE" && find . -type f | sed 's|^\./|  |' | sort )
