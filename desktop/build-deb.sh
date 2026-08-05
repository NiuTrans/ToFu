#!/usr/bin/env bash
# Build Tofu-<ver>-linux-x86_64.deb from the PyInstaller bundle (dist/Tofu).
#
# Usage: desktop/build-deb.sh <bundle_dir> <version> [out_dir]
#
# Why .deb is the primary Linux installer (evaluated against AppImage,
# pt_a64216b959694605):
#   * dpkg-deb ships in the ubuntu-latest CI base image — ZERO downloaded
#     tooling, so the supply chain grows by nothing (appimagetool would be a
#     third-party binary fetched every build).
#   * No FUSE anywhere: not at build time (mksquashfs is absent on some
#     runners/hosts — measured 2026-08-01 on the dev box) and, decisively,
#     not at RUN time: type-2 AppImages need libfuse2, which Ubuntu 22.04+
#     no longer installs by default — "double-click to run" would become
#     "sudo apt install libfuse2 first" for exactly the users this format is
#     meant to serve. A .deb opens in Ubuntu Software / `apt install ./…`
#     with no such trap.
#   * Native lifecycle: apt owns install/upgrade/remove, and the menu entry
#     + icon registration happen through the standard postinst hooks instead
#     of a script the user must know to run.
# The tarball (with its per-user install.sh) stays as the no-sudo /
# non-Debian fallback — the two formats are complements, not competitors.
#
# Layout installed by the package:
#   /opt/Tofu/                                 — the application bundle
#   /usr/share/applications/tofu.desktop       — menu entry (Exec=/opt/Tofu/Tofu)
#   /usr/share/icons/hicolor/512x512/apps/tofu.png — themed icon
#
# /opt is intentionally read-only to the app: lib/runtime_paths detects the
# unwritable exe dir and falls back to the per-user data dir
# (~/.local/share/Tofu, XDG_DATA_HOME honoured) — the same contract the
# Windows Program Files fallback uses.

set -euo pipefail

BUNDLE="${1:?usage: build-deb.sh <bundle_dir> <version> [out_dir]}"
VERSION="${2:?version required, e.g. 0.16.0}"
OUT_DIR="${3:-.}"

fail() { echo "ERROR: $*" >&2; exit 1; }

command -v dpkg-deb >/dev/null 2>&1 || fail "dpkg-deb not found (Debian/Ubuntu toolchain required)"
[ -f "$BUNDLE/Tofu" ] || fail "$BUNDLE/Tofu missing — pass the PyInstaller bundle dir"
[ -f "$BUNDLE/_internal/static/icons/logo.png" ] || fail "$BUNDLE looks incomplete (no bundled logo)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
ROOTFS="$WORKDIR/rootfs"

# ── Payload ──
mkdir -p "$ROOTFS/opt/Tofu"
cp -a "$BUNDLE/." "$ROOTFS/opt/Tofu/"
chmod 755 "$ROOTFS/opt/Tofu/Tofu"

# ── Desktop integration (system-wide; the tarball's install.sh is the
#    per-user counterpart for the no-sudo case) ──
mkdir -p "$ROOTFS/usr/share/applications"
sed "s|__INSTALL_DIR__|/opt/Tofu|g" "$SCRIPT_DIR/tofu.desktop" \
  > "$ROOTFS/usr/share/applications/tofu.desktop"
mkdir -p "$ROOTFS/usr/share/icons/hicolor/512x512/apps"
cp "$BUNDLE/_internal/static/icons/logo.png" \
  "$ROOTFS/usr/share/icons/hicolor/512x512/apps/tofu.png"

# ── DEBIAN metadata ──
SIZE_KB="$(du -sk "$ROOTFS" | cut -f1)"
mkdir -p "$ROOTFS/DEBIAN"
cat > "$ROOTFS/DEBIAN/control" <<EOF
Package: tofu
Version: $VERSION
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: $SIZE_KB
Maintainer: Tofu Project <https://github.com/rangehow/ToFu>
Description: Self-hosted AI assistant (desktop)
 Tofu is a self-hosted AI assistant with a system-tray launcher and an
 auto-opening local web UI. This package installs the application to
 /opt/Tofu and registers a desktop menu entry.
 .
 User data (config, databases, logs) lives per-user in
 ~/.local/share/Tofu — the application never writes into /opt.
EOF

cat > "$ROOTFS/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$ROOTFS/DEBIAN/postinst"

OUT="$OUT_DIR/Tofu-${VERSION}-linux-x86_64.deb"
# --root-owner-group: CI runs as a non-root user, but package payloads must
# be root-owned or apt warns/behaves oddly on some distros. The flag needs
# dpkg >= 1.19 (ubuntu-latest has 1.21+; older hosts like the CentOS 7 dev
# box carry 1.18) — feature-detect instead of assuming, so the script still
# builds everywhere and only the ownership hardening degrades.
OWNER_FLAG=()
if dpkg-deb --help 2>&1 | grep -q -- '--root-owner-group'; then
  OWNER_FLAG=(--root-owner-group)
fi
# ${arr[@]+"${arr[@]}"}: bash < 4.4 (CentOS 7 ships 4.2) treats expanding an
# EMPTY array under `set -u` as "unbound variable" — guard the empty case.
dpkg-deb --build ${OWNER_FLAG[@]+"${OWNER_FLAG[@]}"} "$ROOTFS" "$OUT" >/dev/null
echo "Built: $OUT ($(du -h "$OUT" | cut -f1))"
