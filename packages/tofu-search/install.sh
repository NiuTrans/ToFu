#!/usr/bin/env bash
# install.sh — install tofu-search and its (optional) extras.
#
# Usage:
#   ./install.sh                core dependencies only
#   ./install.sh --playwright   core + Playwright, then install the chromium browser
#   ./install.sh --pdf          core + PDF extraction (pymupdf / pymupdf4llm)
#   ./install.sh --all          everything above
#
# Honors $PIP (default: "python3 -m pip"). Installs the package itself in
# editable mode so local changes are picked up.
set -euo pipefail

cd "$(dirname "$0")"

PIP="${PIP:-python3 -m pip}"
WANT_PLAYWRIGHT=0
WANT_PDF=0

for arg in "$@"; do
  case "$arg" in
    --all)        WANT_PLAYWRIGHT=1; WANT_PDF=1 ;;
    --playwright) WANT_PLAYWRIGHT=1 ;;
    --pdf)        WANT_PDF=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 2 ;;
  esac
done

EXTRAS=""
if [ "$WANT_PLAYWRIGHT" -eq 1 ] && [ "$WANT_PDF" -eq 1 ]; then
  EXTRAS="[all]"
elif [ "$WANT_PLAYWRIGHT" -eq 1 ]; then
  EXTRAS="[playwright]"
elif [ "$WANT_PDF" -eq 1 ]; then
  EXTRAS="[pdf]"
fi

echo "==> Installing tofu-search${EXTRAS} (editable)"
$PIP install -e ".${EXTRAS}"

if [ "$WANT_PLAYWRIGHT" -eq 1 ]; then
  echo "==> Installing Chromium for Playwright"
  python3 -m playwright install chromium || {
    echo "WARNING: 'playwright install chromium' failed. SPA/bot-protection" >&2
    echo "         fallback will be disabled until Chromium is installed." >&2
  }

  # Downloading the browser is not the same as being able to RUN it: on a
  # rootless Linux host every launch dies on a missing .so (measured
  # 2026-08-03: libatk-1.0.so.0). conda-forge carries the whole set, so when
  # the install python lives in a conda env, source the libs there — no
  # sudo, no system packages. One unavailable package must not forfeit the
  # set (an early atomic failure is exactly how the 2026-08-03 host ended up
  # with gbm/nss but no libatk), hence the per-package fallback.
  if [ "$(uname -s)" = "Linux" ] && command -v conda >/dev/null 2>&1; then
    _py_prefix="$(python3 -c 'import sys; print(sys.prefix)')"
    if [ -d "$_py_prefix/conda-meta" ]; then
      echo "==> Installing Chromium shared-lib deps from conda-forge (rootless)"
      _libs=(atk-1.0 at-spi2-atk at-spi2-core alsa-lib xorg-libxcomposite \
             xorg-libxdamage xorg-libxfixes xorg-libxrandr libxkbcommon \
             nspr nss libgbm fontconfig font-ttf-dejavu-sans-mono)
      if ! conda install -p "$_py_prefix" -c conda-forge --override-channels -y "${_libs[@]}"; then
        for _pkg in "${_libs[@]}"; do
          conda install -p "$_py_prefix" -c conda-forge --override-channels -y "$_pkg" || \
            echo "WARNING: chromium lib '$_pkg' unavailable on this channel" >&2
        done
      fi
      # Evidence check: the install only counts when a sentinel lib is on disk.
      if [ ! -f "$_py_prefix/lib/libatk-1.0.so.0" ]; then
        echo "WARNING: libatk still missing — Chromium will not launch." >&2
        echo "         Fix with root: sudo python3 -m playwright install-deps chromium" >&2
      fi

      # FUSE-mounted env? Installed is not enough — a FUSE bad window kills
      # Chromium's .so reads at LAUNCH time (measured 2026-08-03 on
      # beegfs-fuse: 'libatk cannot open' storms alternating with successful
      # launches under a CONSTANT process env). The deterministic answer is a
      # local-disk copy + CHROMIUM_EXTRA_LIB_DIRS (the pool's fallback reads
      # it first, unfiltered).
      if df -T "$_py_prefix" 2>/dev/null | awk 'NR==2{print $2}' | grep -qi fuse; then
        _browser_libs="${TOFU_BROWSER_LIBS_DIR:-$HOME/tofu-browser-libs}"
        echo "==> Env prefix is on FUSE — installing a local-disk Chromium-libs copy at $_browser_libs"
        if [ -d "$_browser_libs/conda-meta" ]; then
          _local_cmd=(conda install -p "$_browser_libs" -c conda-forge --override-channels -y)
        else
          _local_cmd=(conda create -p "$_browser_libs" -c conda-forge --override-channels -y)
        fi
        if ! "${_local_cmd[@]}" "${_libs[@]}"; then
          for _pkg in "${_libs[@]}"; do
            "${_local_cmd[@]}" "$_pkg" || echo "WARNING: local chromium lib '$_pkg' unavailable on this channel" >&2
          done
        fi
        if [ -f "$_browser_libs/lib/libatk-1.0.so.0" ]; then
          echo "==> Local-disk Chromium libs ready."
          echo "    export CHROMIUM_EXTRA_LIB_DIRS=$_browser_libs/lib"
        else
          echo "WARNING: local-disk copy incomplete — launches will stay FUSE-dependent" >&2
        fi
      fi
    else
      echo "==> Not a conda env ($_py_prefix) — skipping rootless shared-lib install"
      echo "    If Chromium fails to launch: sudo python3 -m playwright install-deps chromium"
    fi
  fi
fi

echo "==> Verifying import"
python3 -c "import tofu_search; print('tofu-search', tofu_search.__version__, 'OK')"

echo "Done."
