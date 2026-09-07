#!/usr/bin/env bash
#
# install_on_server.sh — set up `import tofu` on a FRESH server that can reach
# github.com but has no Tofu checkout. Installs the package + all runtime
# deps from GitHub, then verifies.
#
# Copy this file to the new server (or just paste the pip line it runs).
#
# Usage:
#   scripts/install_on_server.sh
#   scripts/install_on_server.sh --help
#   # knobs:
#   REF=v0.5.1            scripts/install_on_server.sh   # pin a tag/commit (recommended)
#   WITH_PLAYWRIGHT=1     scripts/install_on_server.sh   # also fetch the Chromium binary
#   PY=/path/to/venv/bin/python  scripts/install_on_server.sh
#
set -euo pipefail

usage() {
    cat <<'EOF'
usage: scripts/install_on_server.sh

Install only Tofu's in-process Python facade (`import tofu`) and its runtime
dependencies from GitHub. This does not install or start the Tofu web app.
For the web app, use the repository-root install.sh documented in README.md.

Configuration is supplied through environment variables:
  REF=<tag-or-commit>       source revision (default: master; pin in production)
  PY=/path/to/python        target interpreter (default: python3)
  WITH_PLAYWRIGHT=0|1       also install headless Chromium (default: 0)
  GIT_REMOTE=<git-url>      source repository URL
  PIP_ARGS="..."            whitespace-separated extra pip options

  -h, --help                show this help without installing anything
EOF
}

case "$#:$*" in
    0:) ;;
    1:-h|1:--help) usage; exit 0 ;;
    *)
        echo "ERROR: this installer accepts no command-line options: $*" >&2
        usage >&2
        exit 2
        ;;
esac

GIT_REMOTE="${GIT_REMOTE:-https://github.com/rangehow/ToFu.git}"
REF="${REF:-master}"                 # tag/branch/commit; pin a tag for prod
PY="${PY:-python3}"
PIP_ARGS="${PIP_ARGS:-}"             # e.g. "-i https://your-mirror/simple"
WITH_PLAYWRIGHT="${WITH_PLAYWRIGHT-0}"

case "$WITH_PLAYWRIGHT" in
    0|1) ;;
    *) echo "ERROR: WITH_PLAYWRIGHT must be exactly 0 or 1." >&2; exit 2 ;;
esac
command -v "$PY" >/dev/null 2>&1 \
    || { echo "ERROR: target Python is not executable: $PY" >&2; exit 2; }

PIP_OPTIONS=()
if [[ -n "$PIP_ARGS" ]]; then
    read -r -a PIP_OPTIONS <<< "$PIP_ARGS"
fi

echo "==> Target python: $("$PY" -c 'import sys;print(sys.executable)')"
echo "==> Python version: $("$PY" -c 'import sys;print(\".\".join(map(str,sys.version_info[:3])))')"
"$PY" -c 'import sys; assert sys.version_info[:2] >= (3,12), "need Python >= 3.12"' \
    || { echo "ERROR: Python >= 3.12 required." >&2; exit 1; }

# ── 1. Install Tofu + all runtime deps straight from GitHub ──────────────
echo "==> Installing tofu from $GIT_REMOTE@$REF (this pulls ~25 deps)…"
"$PY" -m pip install --upgrade "${PIP_OPTIONS[@]}" \
    "git+${GIT_REMOTE}@${REF}"

# ── 2. Optional: Playwright browser binary (only if keyan fetches JS pages)
if [[ "$WITH_PLAYWRIGHT" == "1" ]]; then
    echo "==> Installing Chromium for Playwright…"
    # --only-shell: matches install.sh. A default install ALSO fetches the
    # 175 MB full Chromium build, which no consumer in this repo launches
    # (every call site is headless).
    "$PY" -m playwright install --only-shell chromium || \
        echo "    WARN: playwright install failed; JS-page fetch will degrade."
fi

# ── 3. Verify ─────────────────────────────────────────────────────────────
echo "==> Verifying import + façade surface…"
"$PY" - <<'PYEOF'
import tofu
print("  import tofu        OK   (api_version =", tofu.__api_version__, ")")
for fn in ("chat", "stream", "capabilities"):
    assert hasattr(tofu, fn), fn
print("  chat/stream/caps   OK")
caps = tofu.capabilities()
assert "config_schema" in caps and "presets" in caps
print("  capabilities()     OK   (presets =", caps["presets"], ")")
# Boundary: billing/BYO must NOT leak into the in-process surface.
leaked = [n for n in ("reserve","settle","debit","ephemeral") if n in dir(tofu)]
assert not leaked, leaked
print("  HTTP-only boundary OK")
print("READY: keyan can now `import tofu` on this server.")
PYEOF

echo
echo "==> SUCCESS. Next: point keyan at the façade and delete its vendored _chatui/."
echo "    Pin for reproducible redeploys with:  REF=<tag> $0"
