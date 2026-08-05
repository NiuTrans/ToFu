#!/usr/bin/env bash
# Tofu — Linux desktop integration installer.
#
# Ships INSIDE the portable tarball (Tofu/install.sh after extraction) and
# registers the app with the desktop environment: an application-menu entry
# plus a themed icon. Without this, "install on Linux" meant extract + run a
# binary from a terminal — no menu presence, no icon, the only platform with
# zero install UX.
#
# Everything here is per-user (~/.local) — no sudo, no root, matching the
# Windows per-user install contract. Safe to re-run (idempotent).
#
# Uninstall:
#   rm ~/.local/share/applications/tofu.desktop
#   rm ~/.local/share/icons/hicolor/512x512/apps/tofu.png

set -euo pipefail

# The extracted bundle directory (this script lives at its root).
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_SRC="$APP_DIR/_internal/static/icons/logo.png"
DESKTOP_SRC="$APP_DIR/tofu.desktop"

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$APP_DIR/Tofu" ] || fail "Tofu binary not found at $APP_DIR/Tofu — run this script from the extracted bundle directory."
[ -f "$ICON_SRC" ] || fail "icon not found at $ICON_SRC — the bundle looks incomplete."
[ -f "$DESKTOP_SRC" ] || fail "tofu.desktop template not found at $DESKTOP_SRC — the bundle looks incomplete."

# ── Icon ──
# hicolor/512x512 is the largest standard slot; desktop environments scale
# down from it. (The source logo is 1024px — a larger image in the slot is
# fine in practice and keeps this script dependency-free.)
ICON_DST_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
mkdir -p "$ICON_DST_DIR"
cp "$ICON_SRC" "$ICON_DST_DIR/tofu.png"

# ── Application-menu entry ──
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
# Render the absolute install path into the template.
sed "s|__INSTALL_DIR__|$APP_DIR|g" "$DESKTOP_SRC" > "$APPS_DIR/tofu.desktop"
chmod +x "$APPS_DIR/tofu.desktop"

# ── Refresh the desktop database (best-effort; not all distros ship it) ──
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" || true
fi

echo ""
echo "  ✓ Tofu installed."
echo "    App menu entry: $APPS_DIR/tofu.desktop"
echo "    You can now launch Tofu from your application menu,"
echo "    or directly with: $APP_DIR/Tofu"
echo ""
