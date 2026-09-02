#!/usr/bin/env bash
# Tofu — Linux desktop integration installer.
#
# Ships INSIDE the portable tarball (Tofu/install.sh after extraction) and
# registers the app with the desktop environment: an application-menu entry
# plus a themed icon. Without this, "install on Linux" meant extract + run a
# binary from a terminal — no menu presence, no icon, the only platform with
# zero install UX.
#
# Everything here is per-user (XDG_DATA_HOME, or ~/.local/share) — no sudo,
# no root, matching the Windows per-user install contract. Safe to re-run
# (idempotent).
#
# Usage:
#   ./install.sh              install or refresh the desktop integration
#   ./install.sh --uninstall  remove the menu entry and icon (keep the bundle)
#   ./install.sh --help       show the contract without changing anything

set -euo pipefail

# The extracted bundle directory (this script lives at its root).
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_SRC="$APP_DIR/_internal/static/icons/logo.png"
DESKTOP_SRC="$APP_DIR/tofu.desktop"

fail() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
usage: ./install.sh [--uninstall]

Register this extracted Tofu bundle in the current user's Linux desktop.
Administrator privileges are not used. Data is written under XDG_DATA_HOME
(or ~/.local/share).

  --uninstall  remove the menu entry and icon; keep the extracted bundle
  -h, --help   show this help without changing files
EOF
}

MODE=install
case "$#:$*" in
  0:) ;;
  1:-h|1:--help) usage; exit 0 ;;
  1:--uninstall) MODE=uninstall ;;
  *)
    echo "ERROR: unknown arguments: $*" >&2
    usage >&2
    exit 2
    ;;
esac

if [ -n "${XDG_DATA_HOME:-}" ]; then
  DATA_HOME="$XDG_DATA_HOME"
elif [ -n "${HOME:-}" ]; then
  DATA_HOME="$HOME/.local/share"
else
  fail "neither XDG_DATA_HOME nor HOME is set; cannot choose a per-user install directory."
fi
case "$DATA_HOME" in
  /*) ;;
  *) fail "XDG_DATA_HOME must be an absolute path (got: $DATA_HOME)." ;;
esac

ICON_DST_DIR="$DATA_HOME/icons/hicolor/512x512/apps"
APPS_DIR="$DATA_HOME/applications"

if [ "$MODE" = uninstall ]; then
  rm -f -- "$APPS_DIR/tofu.desktop" "$ICON_DST_DIR/tofu.png"
  if command -v update-desktop-database >/dev/null 2>&1 \
      && [ -d "$APPS_DIR" ]; then
    update-desktop-database "$APPS_DIR" || true
  fi
  echo "Tofu desktop integration removed. The bundle at $APP_DIR was kept."
  exit 0
fi

[ -f "$APP_DIR/Tofu" ] || fail "Tofu binary not found at $APP_DIR/Tofu — run this script from the extracted bundle directory."
[ -f "$ICON_SRC" ] || fail "icon not found at $ICON_SRC — the bundle looks incomplete."
[ -f "$DESKTOP_SRC" ] || fail "tofu.desktop template not found at $DESKTOP_SRC — the bundle looks incomplete."

# A desktop Exec value has its own quoting rules. Render the whole Exec line
# instead of interpolating APP_DIR into a sed replacement: '&' and '\\' are
# meaningful to sed, spaces split an unquoted Exec value, and '%' starts a
# desktop-entry field code. Newlines cannot be represented safely here.
case "$APP_DIR" in
  *$'\n'*|*$'\r'*) fail "the extracted bundle path contains a newline; move it to a normal directory and retry." ;;
esac
ESCAPED_APP_DIR="$(printf '%s' "$APP_DIR" | sed \
  -e 's/\\/\\\\/g' \
  -e 's/"/\\"/g' \
  -e 's/`/\\`/g' \
  -e 's/\$/\\$/g' \
  -e 's/%/%%/g')"

# ── Icon ──
# hicolor/512x512 is the largest standard slot; desktop environments scale
# down from it. (The source logo is 1024px — a larger image in the slot is
# fine in practice and keeps this script dependency-free.)
mkdir -p "$ICON_DST_DIR"
cp "$ICON_SRC" "$ICON_DST_DIR/tofu.png"

# ── Application-menu entry ──
mkdir -p "$APPS_DIR"
# Render the absolute install path into a temporary file, then atomically
# replace the menu entry so interruption cannot leave a truncated launcher.
grep -q '^Exec=__INSTALL_DIR__/Tofu$' "$DESKTOP_SRC" \
  || fail "tofu.desktop has no recognized Exec placeholder — the bundle is inconsistent."
ENTRY_TMP="$(mktemp "$APPS_DIR/.tofu.desktop.XXXXXX")" \
  || fail "could not create a temporary desktop entry under $APPS_DIR."
trap 'rm -f "$ENTRY_TMP"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$line" = 'Exec=__INSTALL_DIR__/Tofu' ]; then
    printf 'Exec="%s/Tofu"\n' "$ESCAPED_APP_DIR"
  else
    printf '%s\n' "$line"
  fi
done < "$DESKTOP_SRC" > "$ENTRY_TMP"
chmod 755 "$ENTRY_TMP"
mv -f "$ENTRY_TMP" "$APPS_DIR/tofu.desktop"
trap - EXIT

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
