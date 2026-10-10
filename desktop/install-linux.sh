#!/usr/bin/env bash
# Per-user desktop integration with staged publication and rollback.
# Owns menu/icon/login entries only; application data and bundles are retained.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "ERROR: $*" >&2; exit 1; }
usage() {
  cat <<'HELP'
usage: ./install.sh [--autostart | --no-autostart | --uninstall]

Register this extracted Tofu bundle in the current user's Linux desktop.
Administrator privileges are not used. Data is written under XDG_DATA_HOME
(or ~/.local/share). Login startup uses XDG_CONFIG_HOME (or ~/.config).

  --autostart     also launch Tofu at the next desktop login
  --no-autostart  disable this bundle's login startup
  --uninstall    remove owned menu, icon and login entries; keep bundle/data
  -h, --help     show this help without changing files
HELP
}
MODE=install
STARTUP=preserve
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) [ "$#" -eq 1 ] || { usage >&2; exit 2; }; usage; exit 0 ;;
    --uninstall) [ "$MODE" = install ] && [ "$STARTUP" = preserve ] || { usage >&2; exit 2; }; MODE=uninstall ;;
    --autostart|--no-autostart)
      [ "$MODE" = install ] && [ "$STARTUP" = preserve ] || { usage >&2; exit 2; }
      STARTUP="$1" ;;
    *) echo "ERROR: unknown arguments: $*" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
DATA_HOME="${XDG_DATA_HOME:-${HOME:+$HOME/.local/share}}"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME:+$HOME/.config}}"
case "$DATA_HOME" in /*) ;; *) fail 'XDG_DATA_HOME or HOME must select an absolute directory.' ;; esac
case "$CONFIG_HOME" in /*) ;; *) fail 'XDG_CONFIG_HOME or HOME must select an absolute directory.' ;; esac
case "$APP_DIR" in *$'\n'*|*$'\r'*) fail 'bundle path contains a newline.' ;; esac
# Desktop-entry escaping and Exec argument escaping are two independent layers.
ESCAPED_APP_DIR="$(printf '%s' "$APP_DIR" | sed \
  -e 's/\\/\\\\\\\\/g' -e 's/"/\\\\"/g' -e 's/`/\\\\`/g' \
  -e 's/\$/\\\\$/g' -e 's/%/%%/g')"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/512x512/apps"
AUTO_DIR="$CONFIG_HOME/autostart"
ENTRY="$APPS_DIR/tofu.desktop"
ICON="$ICON_DIR/tofu.png"
AUTO="$AUTO_DIR/tofu.desktop"
ICON_SRC="$APP_DIR/_internal/static/icons/logo.png"
DESKTOP_SRC="$APP_DIR/tofu.desktop"
owned_entry() {
  [ ! -e "$1" ] || grep -Fxq "X-Tofu-Install-Dir=$APP_DIR" "$1" \
    || grep -Fxq "Exec=\"$ESCAPED_APP_DIR/Tofu\"" "$1"
}
for target in "$ENTRY" "$ICON" "$AUTO"; do
  [ ! -L "$target" ] || fail "integration target is a symlink: $target"
  [ ! -e "$target" ] || [ -f "$target" ] || fail "integration target is not a file: $target"
done
if [ "$MODE" = uninstall ] || [ "$STARTUP" = --no-autostart ]; then
  owned_entry "$ENTRY" || fail 'menu entry belongs to another bundle; uninstall from that bundle.'
  owned_entry "$AUTO" || fail 'login entry belongs to another bundle.'
fi
if [ "$MODE" = install ]; then
  [ -x "$APP_DIR/Tofu" ] || fail "executable missing: $APP_DIR/Tofu"
  [ -f "$ICON_SRC" ] || fail "icon missing: $ICON_SRC"
  [ -f "$DESKTOP_SRC" ] || fail "template missing: $DESKTOP_SRC"
  grep -q '^Exec=__INSTALL_DIR__/Tofu$' "$DESKTOP_SRC" || fail 'template has no recognized Exec placeholder.'
  if [ "$STARTUP" = preserve ] && [ -f "$AUTO" ] && grep -q '^X-Tofu-Install-Dir=' "$AUTO"; then
    STARTUP=--autostart  # Preserve enabled startup while refreshing its bundle path.
  fi
fi
mkdir -p "$APPS_DIR" "$ICON_DIR"
LOCK="$APPS_DIR/.tofu-install.lock"
mkdir "$LOCK" 2>/dev/null || fail "another integration writer owns $LOCK; verify it has exited before removing a stale lock."
ENTRY_TMP=''; ICON_TMP=''; AUTO_TMP=''
ENTRY_BACK=''; ICON_BACK=''; AUTO_BACK=''
COMMITTING=0; DONE=0; AUTO_CHANGE=0
rollback_target() {
  local target="$1" backup="$2"
  if [ -n "$backup" ]; then
    mv -f -- "$backup" "$target" || { echo "ERROR: recovery backup retained at $backup" >&2; return 1; }
  else
    rm -f -- "$target"
  fi
}
cleanup() {
  local code=$? recovery_failed=0
  trap - EXIT INT TERM
  if [ "$COMMITTING" -eq 1 ] && [ "$DONE" -eq 0 ]; then
    rollback_target "$ENTRY" "$ENTRY_BACK" || recovery_failed=1
    rollback_target "$ICON" "$ICON_BACK" || recovery_failed=1
    if [ "$AUTO_CHANGE" -eq 1 ]; then rollback_target "$AUTO" "$AUTO_BACK" || recovery_failed=1; fi
  fi
  rm -f -- "$ENTRY_TMP" "$ICON_TMP" "$AUTO_TMP"
  if [ "$recovery_failed" -eq 0 ]; then rm -f -- "$ENTRY_BACK" "$ICON_BACK" "$AUTO_BACK"; fi
  rmdir "$LOCK" || true
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
backup() {
  local target="$1" variable="$2" candidate
  if [ -f "$target" ]; then
    candidate="$(mktemp "${target}.backup.XXXXXX")"
    printf -v "$variable" '%s' "$candidate"
    cp -p -- "$target" "$candidate"
  fi
}
backup "$ENTRY" ENTRY_BACK
backup "$ICON" ICON_BACK
if [ "$MODE" = uninstall ] || [ "$STARTUP" != preserve ]; then
  AUTO_CHANGE=1
  mkdir -p "$AUTO_DIR"
  backup "$AUTO" AUTO_BACK
fi
if [ "$MODE" = install ]; then
  ENTRY_TMP="$(mktemp "$APPS_DIR/.tofu.desktop.XXXXXX")"
  ICON_TMP="$(mktemp "$ICON_DIR/.tofu.png.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = 'Exec=__INSTALL_DIR__/Tofu' ]; then
      printf 'Exec="%s/Tofu"\n' "$ESCAPED_APP_DIR"
    else
      printf '%s\n' "$line"
    fi
  done < "$DESKTOP_SRC" > "$ENTRY_TMP"
  printf 'X-Tofu-Install-Dir=%s\n' "$APP_DIR" >> "$ENTRY_TMP"
  chmod 755 "$ENTRY_TMP"
  cp -- "$ICON_SRC" "$ICON_TMP"
  chmod 644 "$ICON_TMP"
  if [ "$STARTUP" = --autostart ]; then
    AUTO_TMP="$(mktemp "$AUTO_DIR/.tofu.desktop.XXXXXX")"
    cp -- "$ENTRY_TMP" "$AUTO_TMP"
    chmod 644 "$AUTO_TMP"
  fi
fi
COMMITTING=1
if [ "$MODE" = uninstall ]; then
  rm -f -- "$ENTRY" "$ICON" "$AUTO"
else
  mv -f -- "$ICON_TMP" "$ICON"
  mv -f -- "$ENTRY_TMP" "$ENTRY"
  case "$STARTUP" in
    --autostart) mv -f -- "$AUTO_TMP" "$AUTO" ;;
    --no-autostart) rm -f -- "$AUTO" ;;
  esac
fi
DONE=1
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" || true
fi
if [ "$MODE" = uninstall ]; then
  echo "Tofu desktop integration removed. The bundle at $APP_DIR was kept."
else
  echo "Tofu installed. App menu entry: $ENTRY"
  echo "Launch from your application menu, or directly with: $APP_DIR/Tofu"
fi
