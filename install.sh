#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Conda-based One-Command Installer (Linux / macOS)
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
#
#  With options:
#    curl -fsSL ... | bash -s -- --port 8080 --api-key sk-xxx
#
#  Options:
#    --dir <path>          Install directory (default: ~/tofu)
#    --env <name>          Conda env name (default: tofu)
#    --port <n>            Server port (default: 15000)
#    --api-key <key>       Pre-configure LLM API key
#    --no-launch           Install only, don't start
#    --skip-playwright     Skip Playwright browser install
#    --skip-node           Skip the OPTIONAL Node.js + esbuild step entirely
#                          (conda-nodejs solve + npm). The JS bundle then uses
#                          the dependency-free Python minifier — byte-identical
#                          output, just a slightly larger gzip. Fastest install.
#    --no-update-conda     Skip conda self-update (only relevant when we
#                          install our OWN sibling Miniforge — we never
#                          touch a pre-existing conda the user owns)
#    --reset-env           Delete the existing conda env and recreate from scratch
#                          (⚠️  DESTRUCTIVE: removes ANY extra packages the user
#                           installed into this env. Only use for your own env.)
#    --use-conda           Force the legacy conda install path, skipping the
#                          default uv fast path. Use on very old systems
#                          (glibc < 2.28) if auto-detection misfires, or when
#                          you specifically want the conda-forge toolchain.
#    --with-postgres       Install + bootstrap PostgreSQL (opt-in). WITHOUT this
#                          flag the installer uses SQLite by default — zero
#                          config, no dependencies, fine for single-user /
#                          <100 concurrent. Pass --with-postgres only when you
#                          need PG's higher concurrency (100+ users). PG install
#                          is the slowest, most failure-prone install step
#                          (icu/libxml2/PG-major solve + initdb), so it is no
#                          longer done by default.
#    --force-sqlite        Force SQLite even if --with-postgres was also passed
#                          (SQLite wins). Also leaves any existing pgdata in
#                          place, unused. Historically used when the host's
#                          conda-forge snapshot couldn't satisfy PG deps.
#    --pg-major <N>        Force a specific PG major version (e.g. 17). Default
#                          tries 18 → 17 → 16 in order, picking the first one
#                          whose solve succeeds on this host.
#    --reinit-pgdata       If data/pgdata exists but was created by a different
#                          PG major than the one we install, back it up and
#                          re-initdb. WITHOUT this flag we auto-detect the
#                          mismatch and fall back to SQLite (data preserved).
#    --min-conda <N>       Minimum acceptable conda MAJOR version (default 24).
#                          If the user's conda is older we install a private
#                          sibling Miniforge instead of touching theirs.
#    --force-sibling-conda Always install our own sibling Miniforge, even
#                          when an existing conda is new enough.
#    --with-docling        ALSO install the optional `docling` package for
#                          layout-aware PDF parsing (better tables + math
#                          formulas on academic PDFs). Adds ~2 GB (pulls
#                          torch + model weights). Opt-in because the base
#                          install works fine with pymupdf4llm alone.
#                          After install, set PDF_TEXT_MODE=structured in
#                          your .env (or per-request textMode=structured)
#                          to route /api/pdf/parse through docling.
#
#  Conda discovery & "don't break the user's setup" policy
#  ────────────────────────────────────────────────────────
#  1. We look for an existing conda. If one is found AND its major version
#     is >= --min-conda, we USE IT AS-IS — no `conda update`, no `conda init`,
#     no `conda config` writes (those would mutate the user's ~/.condarc and
#     ~/.bashrc). All env operations are scoped to the Tofu env we create.
#  2. Otherwise we install Miniforge as a SIBLING of the project directory:
#        <parent of INSTALL_DIR>/tofu-miniforge3/
#     Sibling (not nested) so `git clean -fdx` inside the project doesn't
#     wipe it. We use the parent of INSTALL_DIR (NOT $HOME) because users
#     on shared filesystems / codelab containers often lack write access to
#     their own $HOME, but DO own the project parent. This way the Miniforge
#     install lives at the same permission level as the project.
#  3. After env creation we write <INSTALL_DIR>/.tofu_env.json — a marker
#     read by server.py / bootstrap.py to re-exec into the right interpreter
#     when the user runs `python server.py` from a shell where the Tofu env
#     wasn't `conda activate`d. This avoids any need to mutate ~/.bashrc.
#
#  This script relies ENTIRELY on conda (conda-forge). It:
#    1. Locates an acceptable conda OR installs a sibling Miniforge
#    2. (Sibling installs only) updates conda itself for solver fixes
#    3. Clones the repo if needed
#    4. Creates a fresh conda env with Python 3.10+
#    5. Installs ALL Python dependencies from conda-forge (no pip)
#    6. Installs ripgrep, fd-find, and Chromium shared libs from conda-forge
#    7. Installs PostgreSQL with layered fallback (18 → 17 → 16 → SQLite)
#    8. Validates data/pgdata/ matches installed PG major (auto-heals)
#    9. Installs the Playwright Chromium browser binary
#   10. Writes .tofu_env.json marker so server.py/bootstrap.py auto-activate
#   11. Launches the server
#
#  For Windows, download the .exe installer from the GitHub release page.
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Color helpers ───────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "  ${CYAN}ℹ${NC}  $*"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
fail()  { echo -e "  ${RED}✗${NC}  $*"; exit 1; }
step()  { echo ""; echo -e "  ${BOLD}${CYAN}▸${NC}  ${BOLD}$*${NC}"; }

# ── Defaults ────────────────────────────────────────────────
INSTALL_DIR="${HOME}/tofu"
ENV_NAME="tofu"
PY_VER="3.12"
PORT="15000"
API_KEY=""
NO_LAUNCH=0
SKIP_PLAYWRIGHT=0
SKIP_NODE=0
NO_UPDATE_CONDA=0
RESET_ENV=0
FORCE_SQLITE=0
WITH_POSTGRES=0     # 0 = SQLite default (PG opt-in); 1 = install+bootstrap PG
USE_CONDA=0        # 1 = force the legacy conda path, skip the uv fast path
PG_MAJOR=""         # empty = auto-pick from PG_MAJOR_CANDIDATES
REINIT_PGDATA=0
PG_MAJOR_CANDIDATES=(18 17 16)
MIN_CONDA_MAJOR=24          # minimum acceptable major version of an existing conda
FORCE_SIBLING_CONDA=0       # 1 = always install our own sibling Miniforge
WITH_DOCLING=0              # 1 = also install the optional `docling` package

# ── Parse arguments ─────────────────────────────────────────
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)              INSTALL_DIR="$2"; shift 2 ;;
        --env)               ENV_NAME="$2"; shift 2 ;;
        --python)           PY_VER="$2"; shift 2 ;;
        --port)             PORT="$2"; FORWARD_ARGS+=("--port" "$2"); shift 2 ;;
        --api-key)          API_KEY="$2"; FORWARD_ARGS+=("--api-key" "$2"); shift 2 ;;
        --no-launch)        NO_LAUNCH=1; shift ;;
        --skip-playwright)  SKIP_PLAYWRIGHT=1; shift ;;
        --skip-node)        SKIP_NODE=1; shift ;;
        --no-update-conda)  NO_UPDATE_CONDA=1; shift ;;
        --reset-env)        RESET_ENV=1; shift ;;
        --force-sqlite)     FORCE_SQLITE=1; shift ;;
        --with-postgres)    WITH_POSTGRES=1; shift ;;
        --use-conda)        USE_CONDA=1; shift ;;
        --pg-major)         PG_MAJOR="$2"; shift 2 ;;
        --reinit-pgdata)    REINIT_PGDATA=1; shift ;;
        --min-conda)        MIN_CONDA_MAJOR="$2"; shift 2 ;;
        --force-sibling-conda) FORCE_SIBLING_CONDA=1; shift ;;
        --with-docling)     WITH_DOCLING=1; shift ;;
        *)  FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# ── Banner ──────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}🧈 Tofu (豆腐) — Self-Hosted AI Assistant${NC}"
echo -e "  ─────────────────────────────────────────"
echo -e "  Conda-based installer"
echo ""

# ── Tee ALL output (stdout + stderr) into a log file ──
# Everything printed from this point onward ends up in
# <INSTALL_DIR>/logs/install-YYYYMMDD_HHMMSS.log — makes it easy to
# attach the full transcript when reporting an issue.
#
# We respect --dir here but fall back to the CWD if the dir doesn't exist
# yet (e.g. first-ever clone). The log is re-linked to the final path
# once the install directory is known for sure.
_TOFU_LOG_ROOT="${INSTALL_DIR}"
[[ -d "$_TOFU_LOG_ROOT" ]] || _TOFU_LOG_ROOT="$(pwd)"
_TOFU_LOG_DIR="${_TOFU_LOG_ROOT}/logs"
mkdir -p "$_TOFU_LOG_DIR" 2>/dev/null || _TOFU_LOG_DIR="/tmp"
TOFU_INSTALL_LOG="${_TOFU_LOG_DIR}/install-$(date +%Y%m%d_%H%M%S).log"
# Use `tee` via process substitution so the log captures the raw
# (ANSI-coloured) output that the user sees. Colours are fine in the
# log — most tools that read it (pagers, chat UI) handle them, and you
# can strip them later with `sed -r 's/\x1b\[[0-9;]*m//g'` if you want.
# stdbuf -oL keeps stdout line-buffered so progress shows up immediately
# even when piped to tee (solves the "nothing prints for 30s" issue
# during long conda solves).
# Strip ANSI colour escapes BEFORE tee'ing into the file so the log is
# readable as plain text (terminals still see the coloured stream).
# Uses process substitution: terminal gets raw, log gets sed-stripped.
#
# PORTABILITY: `stdbuf` ships with GNU coreutils and is ABSENT on stock
# macOS/BSD; `sed -u` (unbuffered) is a GNU extension BSD sed rejects.
# Using either unconditionally makes this `exec` redirect fail on macOS,
# which aborts the whole install before a single package is fetched
# (symptom: empty tofu dir). So probe for both and degrade gracefully.
_TOFU_STDBUF=""
if command -v stdbuf &>/dev/null; then
    _TOFU_STDBUF="stdbuf -oL"
fi
_TOFU_SED_U=""
if command -v sed &>/dev/null && echo x | sed -u '' &>/dev/null; then
    _TOFU_SED_U="-u"
fi
if command -v sed &>/dev/null; then
    exec > >($_TOFU_STDBUF tee >($_TOFU_STDBUF sed $_TOFU_SED_U $'s/\x1b\\[[0-9;]*[a-zA-Z]//g' >> "$TOFU_INSTALL_LOG")) 2>&1
else
    exec > >($_TOFU_STDBUF tee -a "$TOFU_INSTALL_LOG") 2>&1
fi
# Record key metadata at the top of the log for future debugging.
{
    echo "──────────────────────────────────────────────"
    echo "tofu install.sh — $(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host:    $(hostname 2>/dev/null || echo unknown)"
    echo "user:    $(whoami 2>/dev/null || echo unknown)"
    echo "args:    $0 $*"
    echo "pwd:     $(pwd)"
    echo "bash:    ${BASH_VERSION:-unknown}"
    echo "which conda (pre-locate): $(command -v conda 2>/dev/null || echo none)"
    echo "──────────────────────────────────────────────"
} >&2
info "Install log: $TOFU_INSTALL_LOG"

# On any non-zero exit (error, Ctrl-C, set -e trigger), remind the user
# where the log is so they can grab it for bug reports.
_tofu_exit_reminder() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "" >&2
        echo -e "  ${YELLOW}!${NC}  install.sh exited with code ${rc}" >&2
        echo -e "  ${YELLOW}!${NC}  Full transcript saved to: ${TOFU_INSTALL_LOG}" >&2
        echo -e "  ${YELLOW}!${NC}  Copy it when filing a bug:  cat \"${TOFU_INSTALL_LOG}\"" >&2
    fi
}
trap _tofu_exit_reminder EXIT

# ── Platform check ──────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)   PLATFORM="Linux" ;;
    Darwin)  PLATFORM="MacOSX" ;;
    *)       fail "Unsupported OS: $OS (Windows: download Tofu-Setup-*.exe from the release page)" ;;
esac
info "Platform: $OS $ARCH"

# ═══════════════════════════════════════════════════════════════
#  Step 0.5: Ensure source is present (backend-agnostic)
#
#  Both the uv fast path and the conda path need requirements.txt in hand
#  BEFORE choosing a backend, so resolve INSTALL_DIR / clone here using the
#  system git. If a clone is required but git is missing, we force the conda
#  path (which can install git from conda-forge).
# ═══════════════════════════════════════════════════════════════
step "Getting Tofu source code"
if [[ -f "${INSTALL_DIR}/server.py" ]]; then
    ok "Existing installation found at ${INSTALL_DIR}"
    if [[ -d "${INSTALL_DIR}/.git" ]] && command -v git &>/dev/null; then
        info "Updating via git pull..."
        (cd "$INSTALL_DIR" && git pull --ff-only) || warn "git pull failed — continuing with existing code"
    fi
elif [[ -f "server.py" ]]; then
    INSTALL_DIR="$(pwd)"
    ok "Running from project directory: $INSTALL_DIR"
elif command -v git &>/dev/null; then
    info "Cloning https://github.com/rangehow/ToFu.git → ${INSTALL_DIR}"
    git clone https://github.com/rangehow/ToFu.git "$INSTALL_DIR"
    ok "Repository cloned"
else
    warn "git not found and a clone is required — forcing the conda path (it installs git)"
    USE_CONDA=1
fi
REQ_FILE="${INSTALL_DIR}/requirements.txt"

# ═══════════════════════════════════════════════════════════════
#  Step 0.6: Choose install backend — uv fast path vs legacy conda
#
#  Default is the uv fast path: `uv venv` + `uv pip install -r requirements`,
#  which resolves+installs prebuilt manylinux wheels in ~1-2 min with zero
#  from-source builds — an order of magnitude faster than the conda-forge
#  solve. We fall back to conda (unchanged) when any of these hold:
#    • --use-conda was passed (explicit opt-out)
#    • --with-postgres was passed (PG binaries live in conda; SQLite runs
#      anywhere, so we don't make the user also remember --use-conda)
#    • the host glibc is < 2.28 (PyMuPDF/Pillow ship no manylinux2014 wheel,
#      so uv would fail resolution / hit GLIBC_x-not-found on CentOS7-era hosts)
#    • the uv install or its import smoke-test fails (belt-and-braces: even if
#      the glibc probe passes, a missing/broken wheel triggers the fallback)
#  A clean fallback to conda is the compatibility floor and must never break.
# ═══════════════════════════════════════════════════════════════
_FAST_PATH_DONE=0

# Return 0 iff this host's glibc is >= 2.28 (or non-Linux, e.g. macOS where
# wheels are arch-tagged and the old GLIBC trap doesn't apply). Conservative:
# if the version can't be determined, return non-zero (→ prefer conda).
_glibc_ge_228() {
    [[ "$OS" != "Linux" ]] && return 0
    local v
    v="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)"
    [[ -z "$v" ]] && v="$(getconf GNU_LIBC_VERSION 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)"
    [[ -z "$v" ]] && return 1
    awk -v x="$v" 'BEGIN{n=split(x,a,".");exit !(a[1]>2||(a[1]==2&&a[2]>=28))}'
}

# Best-effort: ensure a `uv` binary is available. Returns 0 if usable.
_ensure_uv() {
    command -v uv &>/dev/null && return 0
    info "uv not found — installing it (astral.sh, bounded)..."
    local _t=""
    command -v timeout &>/dev/null && _t="timeout -k 5 120"
    if command -v curl &>/dev/null; then
        $_t sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null 2>&1 || true
    fi
    # uv installs to ~/.local/bin or ~/.cargo/bin — put both on PATH for this run.
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
    command -v uv &>/dev/null
}

# The uv fast path. Sets ENV_PYTHON / ENV_PREFIX and writes .tofu_env.json on
# success and returns 0; returns non-zero on ANY failure so the caller falls
# back to conda. Never calls fail() — a failure here is recoverable.
_try_uv_install() {
    _ensure_uv || { warn "Could not obtain uv — falling back to conda"; return 1; }

    local _venv="${INSTALL_DIR}/.venv"
    info "Creating uv virtualenv at ${_venv} (Python ${PY_VER})..."
    # --python-preference only-managed: seed the venv from uv's OWN standalone
    # CPython, never a system/conda interpreter. Two reasons: (1) hermetic +
    # reproducible (no dependence on whatever python the host ships); (2) it
    # guarantees .venv/bin/python resolves (realpath) to a DISTINCT base binary,
    # so server.py's re-exec guard is never short-circuited by a symlink
    # collision with the interpreter the user later launches from.
    uv venv "$_venv" --python "${PY_VER}" --python-preference only-managed 2>&1 || {
        warn "uv venv failed — falling back to conda"; return 1; }

    local _uvpy="${_venv}/bin/python"
    [[ -x "$_uvpy" ]] || { warn "uv venv produced no python — falling back to conda"; return 1; }

    local _t=""
    command -v timeout &>/dev/null && _t="timeout -k 15 900"
    info "Installing Python dependencies with uv (prebuilt wheels)..."
    $_t uv pip install --python "$_uvpy" -r "$REQ_FILE" 2>&1 || {
        warn "uv pip install failed — falling back to conda"; return 1; }

    # ── Import smoke-test: THE compatibility gate ──
    # PyMuPDF (fitz) + Pillow (PIL) are the packages with the highest manylinux
    # glibc floor, so an old-glibc host that slipped past _glibc_ge_228 (or a
    # broken wheel) surfaces HERE as an ImportError / GLIBC_x-not-found, and we
    # fall back to conda cleanly. This is the belt-and-braces the owner required.
    info "Verifying the wheel stack imports (fitz/PIL are the glibc-floor canaries)..."
    if ! "$_uvpy" -c 'import lxml.etree, fitz, PIL, cryptography, quart, hypercorn, orjson, sqlalchemy, playwright' 2>&1; then
        warn "uv-installed wheels failed the import smoke-test (likely glibc too old) — falling back to conda"
        return 1
    fi

    # rg / fd are performance optimizations, NOT hard deps (grep_search degrades
    # rg → grep → pure-Python). Detect system copies; never build from source.
    if ! command -v rg &>/dev/null; then
        warn "ripgrep (rg) not found — search falls back to grep/Python (slower, still works)."
        warn "  For best speed install it from your OS: apt install ripgrep  /  yum install ripgrep"
    fi
    if ! command -v fd &>/dev/null && ! command -v fdfind &>/dev/null; then
        warn "fd not found — file search falls back to a Python walker (slower, still works)."
        warn "  Optional: apt install fd-find  /  yum install fd-find"
    fi

    # Playwright Chromium — best-effort, never blocks (browser tools degrade).
    if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
        info "Installing Playwright Chromium (best-effort)..."
        "$_uvpy" -m playwright install chromium >/dev/null 2>&1 \
            && ok "Playwright Chromium installed" \
            || warn "Playwright Chromium install skipped/failed — JS-rendered fetch disabled until you run it manually"
    fi

    # Publish the env for the shared downstream steps (.env, launch, pgdata probe).
    ENV_PREFIX="$_venv"
    ENV_PYTHON="$_uvpy"
    # Write the .tofu_env.json marker with backend='uv'. server.py keys off this
    # to skip the conda-only CONDA_PREFIX shim (a venv is not a conda env).
    "$_uvpy" - "$INSTALL_DIR" "$_venv" "$_uvpy" <<'PYEOF'
import json, os, sys, time
install_dir, env_prefix, env_python = sys.argv[1:4]
marker = {
    'schema': 1,
    'created_at': int(time.time()),
    'backend': 'uv',
    'env_prefix': env_prefix,
    'python': env_python,
    'owned_by_tofu_install': True,
    'note': ('Written by install.sh (uv fast path). Read by server.py / '
             'bootstrap.py to re-exec into the venv interpreter. Safe to '
             'delete to disable auto-activation. NOT exported (gitignored).'),
}
with open(os.path.join(install_dir, '.tofu_env.json'), 'w', encoding='utf-8') as f:
    json.dump(marker, f, indent=2)
print(f"  ✓ Wrote {os.path.join(install_dir, '.tofu_env.json')}")
PYEOF
    ok "uv fast path complete (venv at ${_venv})"
    return 0
}

if [[ "$USE_CONDA" -eq 1 ]]; then
    info "Using the conda install path (--use-conda)."
elif [[ "$WITH_POSTGRES" -eq 1 ]]; then
    info "PostgreSQL requested (--with-postgres) — PG binaries live in the conda"
    info "environment, so switching to the conda install path automatically."
    USE_CONDA=1
elif ! _glibc_ge_228; then
    info "Host glibc < 2.28 (or undetectable) — using the conda path for maximum"
    info "compatibility (PyMuPDF/Pillow ship no manylinux2014 wheel for old glibc)."
    USE_CONDA=1
else
    step "Installing via uv (fast path; falls back to conda on any failure)"
    if _try_uv_install; then
        _FAST_PATH_DONE=1
    else
        warn "uv fast path did not complete — continuing with the conda install path"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Steps 1–8 below are the LEGACY CONDA PATH. They run only when the uv
#  fast path did not complete ($_FAST_PATH_DONE != 1). The whole block is
#  guarded by a single `if` so the conda logic stays byte-for-byte intact
#  (no reindent) — we just skip it wholesale on the fast path. Both paths
#  converge below at Step 8.5 with ENV_PYTHON / ENV_PREFIX already set.
#
#  Pre-seed the conda-only globals that the SHARED launch tail references so
#  `set -u` never trips on the uv path (where the conda block is skipped).
#  On the uv path there is no conda base and the env is Tofu-owned.
# ═══════════════════════════════════════════════════════════════
CONDA_BASE="${CONDA_BASE:-}"
CONDA_OWNED_BY_US="${CONDA_OWNED_BY_US:-0}"
if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then

# ═══════════════════════════════════════════════════════════════
#  Step 1: Locate, version-check, or install conda (Miniforge)
#
#  POLICY: never mutate a conda the user already owns. We only "manage"
#  conda when WE installed it (sibling Miniforge under the project parent).
# ═══════════════════════════════════════════════════════════════
step "Locating conda"

# Resolve project parent so we can compute the sibling Miniforge path.
# At this point INSTALL_DIR may not exist yet (first-time clone) — that's
# fine, we just need its parent directory string.
_INSTALL_PARENT="$(cd "$(dirname "${INSTALL_DIR}")" 2>/dev/null && pwd)"
if [[ -z "$_INSTALL_PARENT" ]]; then
    # Parent doesn't exist either — fall back to dirname of the literal path
    _INSTALL_PARENT="$(dirname "${INSTALL_DIR}")"
fi
SIBLING_CONDA_DIR="${_INSTALL_PARENT}/tofu-miniforge3"

# Returns 0 if "$1" >= MIN_CONDA_MAJOR, else 1. "$1" is conda --version output
# like "conda 24.7.1" or just "24.7.1". Accepts unknown/blank as a fail.
_conda_version_ok() {
    local raw="${1:-}"
    [[ -n "$raw" ]] || return 1
    # Extract first dotted version-looking token
    local ver
    ver="$(echo "$raw" | grep -oE '[0-9]+(\.[0-9]+)+' | head -n1)"
    [[ -n "$ver" ]] || return 1
    local major="${ver%%.*}"
    [[ "$major" =~ ^[0-9]+$ ]] || return 1
    [[ "$major" -ge "$MIN_CONDA_MAJOR" ]]
}

# Probe an arbitrary conda binary for its version. Echoes raw output.
_probe_conda_version() {
    local bin="$1"
    [[ -x "$bin" ]] || { echo ""; return; }
    "$bin" --version 2>/dev/null || echo ""
}

CONDA_BIN=""
CONDA_OWNED_BY_US=0   # 1 = we installed this conda (sibling); we may update it.
                      # 0 = pre-existing user conda; HANDS OFF (no update / init / config).

# 0. Highest priority: a previous successful install wrote .tofu_env.json
#    pointing at a specific conda_base. Reuse it so we never silently
#    install a SECOND miniforge to a different location (which would leave
#    the existing env's packages unused and cause pip to fall back to
#    --user when its newly-created site-packages isn't ready yet).
_TOFU_ENV_MARKER="${INSTALL_DIR}/.tofu_env.json"
if [[ -f "$_TOFU_ENV_MARKER" ]] && command -v python3 &>/dev/null; then
    _MARKER_BASE="$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('conda_base',''))
except Exception:
    pass" "$_TOFU_ENV_MARKER" 2>/dev/null || true)"
    if [[ -n "${_MARKER_BASE:-}" && -x "${_MARKER_BASE}/bin/conda" ]]; then
        _ver_raw="$(_probe_conda_version "${_MARKER_BASE}/bin/conda")"
        if _conda_version_ok "$_ver_raw"; then
            CONDA_BIN="${_MARKER_BASE}/bin/conda"
            # If this conda lives at our sibling path, we own it; otherwise
            # treat it as user-owned (don't auto-update it).
            if [[ "${_MARKER_BASE}" == "${SIBLING_CONDA_DIR}" ]]; then
                CONDA_OWNED_BY_US=1
            fi
            ok "Reusing conda from .tofu_env.json: $CONDA_BIN (${_ver_raw})"
        else
            warn ".tofu_env.json points at conda ${_MARKER_BASE} but version is too old (${_ver_raw:-unknown}) — will search elsewhere"
        fi
    fi
fi

# 1. Existing user conda — accept only if version >= MIN_CONDA_MAJOR.
_existing_conda_candidates=()
if command -v conda &>/dev/null; then
    _existing_conda_candidates+=("$(command -v conda)")
fi
for _cand in \
    "${HOME}/miniforge3/bin/conda" \
    "${HOME}/miniconda3/bin/conda" \
    "${HOME}/anaconda3/bin/conda" \
    "/opt/conda/bin/conda" \
    "/opt/miniforge3/bin/conda"; do
    [[ -x "$_cand" ]] && _existing_conda_candidates+=("$_cand")
done

if [[ -n "$CONDA_BIN" ]]; then
    : # already resolved from .tofu_env.json marker
elif [[ "$FORCE_SIBLING_CONDA" -eq 1 ]]; then
    info "--force-sibling-conda: ignoring any pre-existing conda"
else
    for _cand in "${_existing_conda_candidates[@]}"; do
        _ver_raw="$(_probe_conda_version "$_cand")"
        if _conda_version_ok "$_ver_raw"; then
            CONDA_BIN="$_cand"
            ok "Using existing conda: $CONDA_BIN (${_ver_raw})"
            info "(version satisfies --min-conda=${MIN_CONDA_MAJOR} — leaving it untouched)"
            break
        else
            warn "Existing conda at $_cand is too old: ${_ver_raw:-unknown} (need major >= ${MIN_CONDA_MAJOR})"
        fi
    done
fi

# 2. If a sibling Miniforge from a previous Tofu install exists and passes
#    the version check, prefer it (we own it, so we can manage it).
if [[ -z "$CONDA_BIN" && -x "${SIBLING_CONDA_DIR}/bin/conda" ]]; then
    _ver_raw="$(_probe_conda_version "${SIBLING_CONDA_DIR}/bin/conda")"
    if _conda_version_ok "$_ver_raw"; then
        CONDA_BIN="${SIBLING_CONDA_DIR}/bin/conda"
        CONDA_OWNED_BY_US=1
        ok "Reusing prior sibling Miniforge: ${SIBLING_CONDA_DIR} (${_ver_raw})"
    else
        warn "Sibling Miniforge at ${SIBLING_CONDA_DIR} is too old (${_ver_raw:-unknown}) — will refresh"
    fi
fi

# 3. Install a fresh sibling Miniforge if needed.
if [[ -z "$CONDA_BIN" ]]; then
    info "Installing private Miniforge as project sibling: ${SIBLING_CONDA_DIR}"
    info "(rationale: we need conda >= ${MIN_CONDA_MAJOR}; not touching any existing conda you may have)"

    # Pick the first writable install location:
    #   1. <parent of INSTALL_DIR>/tofu-miniforge3   (preferred — same level as project)
    #   2. <INSTALL_DIR>/.miniforge3                  (nested — last resort)
    #   3. $HOME/.tofu-miniforge3                     (only if both above fail)
    _CHOSEN=""
    for _try in \
        "${SIBLING_CONDA_DIR}" \
        "${INSTALL_DIR}/.miniforge3" \
        "${HOME}/.tofu-miniforge3"; do
        _try_parent="$(dirname "$_try")"
        # Make sure parent exists and is writable
        if [[ ! -d "$_try_parent" ]]; then
            mkdir -p "$_try_parent" 2>/dev/null || continue
        fi
        if [[ -w "$_try_parent" ]]; then
            _CHOSEN="$_try"
            break
        fi
    done
    [[ -n "$_CHOSEN" ]] || fail "No writable parent dir for Miniforge install (tried sibling, nested, \$HOME)"
    SIBLING_CONDA_DIR="$_CHOSEN"

    # Pre-downloaded installer escape hatch: if the user set
    # TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-...-.sh, skip the network
    # dance entirely.  Useful for offline / air-gapped corp hosts where
    # neither github.com nor any mirror is reachable.
    if [[ -n "${TOFU_MINIFORGE_LOCAL:-}" && -f "${TOFU_MINIFORGE_LOCAL}" ]]; then
        info "Using pre-downloaded Miniforge installer: ${TOFU_MINIFORGE_LOCAL}"
        bash "${TOFU_MINIFORGE_LOCAL}" -b -p "$SIBLING_CONDA_DIR"
        CONDA_BIN="${SIBLING_CONDA_DIR}/bin/conda"
        [[ -x "$CONDA_BIN" ]] || fail "Miniforge install did not produce $CONDA_BIN"
        CONDA_OWNED_BY_US=1
        ok "Miniforge installed at $SIBLING_CONDA_DIR (from local installer)"
        _ver_raw="$(_probe_conda_version "$CONDA_BIN")"
        if _conda_version_ok "$_ver_raw"; then
            ok "Conda version OK: ${_ver_raw}"
        else
            warn "Freshly installed Miniforge reports version ${_ver_raw:-unknown}"
        fi
        # Skip the download+mirror path below.
        _SKIP_MINIFORGE_DOWNLOAD=1
    fi

    # Mirror fallback chain — corp proxies often block github.com release
    # asset downloads (returning 403 from objects.githubusercontent.com),
    # so try the official URL first, then well-known China mirrors, and
    # finally the Sankuai-internal Miniconda mirror as last-resort fallback
    # (same conda binary; we use --override-channels later so the default
    # channel set doesn't matter).
    # Override / extend with TOFU_MINIFORGE_MIRRORS="url1 url2 ..." env var.
    MF_FILE="Miniforge3-${PLATFORM}-${ARCH}.sh"
    # Sankuai mirror uses Anaconda's Miniconda filename pattern instead of
    # Miniforge's. PLATFORM is "Linux"/"MacOSX" and ARCH matches both.
    MC_FILE="Miniconda3-latest-${PLATFORM}-${ARCH}.sh"
    MF_URLS=(
        "https://github.com/conda-forge/miniforge/releases/latest/download/${MF_FILE}"
        "https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/${MF_FILE}"
        "https://mirrors.bfsu.edu.cn/github-release/conda-forge/miniforge/LatestRelease/${MF_FILE}"
        "https://mirror.nju.edu.cn/github-release/conda-forge/miniforge/LatestRelease/${MF_FILE}"
        "https://mirrors.internal.example.com/conda/miniconda/${MC_FILE}"
    )
    if [[ -n "${TOFU_MINIFORGE_MIRRORS:-}" ]]; then
        # User-supplied mirrors take priority.
        read -r -a _USER_MIRRORS <<< "${TOFU_MINIFORGE_MIRRORS}"
        MF_URLS=("${_USER_MIRRORS[@]}" "${MF_URLS[@]}")
    fi
    if [[ "${_SKIP_MINIFORGE_DOWNLOAD:-0}" -ne 1 ]]; then
    TMP_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/miniforge.XXXXXX")"
    # Don't override the global EXIT trap (which is the install-log reminder);
    # use a RETURN-style cleanup at the end of this branch.
    # Force IPv4 — many corp networks return AAAA records but have no v6
    # routing, so the default dual-stack connect hangs/fails with
    # "Network is unreachable" on the v6 address.
    _DOWNLOADED=0
    for _MF_URL in "${MF_URLS[@]}"; do
        info "Downloading $_MF_URL"
        if command -v curl &>/dev/null; then
            if curl -4 -fsSL --connect-timeout 15 --max-time 600 "$_MF_URL" -o "$TMP_INSTALLER"; then
                _DOWNLOADED=1
                break
            fi
            warn "curl failed for $_MF_URL — trying next mirror"
        elif command -v wget &>/dev/null; then
            if wget -4 -q --timeout=600 "$_MF_URL" -O "$TMP_INSTALLER"; then
                _DOWNLOADED=1
                break
            fi
            warn "wget failed for $_MF_URL — trying next mirror"
        else
            rm -f "$TMP_INSTALLER"
            fail "Need curl or wget to download Miniforge"
        fi
        # Clean up any partial file before retrying the next mirror.
        : > "$TMP_INSTALLER"
    done
    if [[ "$_DOWNLOADED" -ne 1 ]]; then
        rm -f "$TMP_INSTALLER"
        warn "All Miniforge mirrors failed (tried ${#MF_URLS[@]})."
        warn "Workaround: manually download Miniforge3-${PLATFORM}-${ARCH}.sh on a machine"
        warn "  with network access, copy it to this host, then re-run:"
        warn "    TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-${PLATFORM}-${ARCH}.sh bash install.sh"
        warn "Or override the mirror list:"
        warn "    TOFU_MINIFORGE_MIRRORS=\"<url1> <url2>\" bash install.sh"
        fail "All Miniforge mirrors failed — see workarounds above."
    fi

    # `-b` batch (no prompts), `-p` install prefix. Note: NO `conda init`.
    # Running `conda init` would mutate the caller's ~/.bashrc — we never
    # want that, especially not in shared-codelab containers where bashrc
    # belongs to whoever's session this is. Activation is handled by the
    # .tofu_env.json marker (read by server.py / bootstrap.py).
    bash "$TMP_INSTALLER" -b -p "$SIBLING_CONDA_DIR"
    rm -f "$TMP_INSTALLER"

    CONDA_BIN="${SIBLING_CONDA_DIR}/bin/conda"
    [[ -x "$CONDA_BIN" ]] || fail "Miniforge install did not produce $CONDA_BIN"
    CONDA_OWNED_BY_US=1
    ok "Miniforge installed at $SIBLING_CONDA_DIR (we own this — safe to manage)"

    # Verify it actually meets the version bar.
    _ver_raw="$(_probe_conda_version "$CONDA_BIN")"
    if ! _conda_version_ok "$_ver_raw"; then
        warn "Freshly installed Miniforge reports version ${_ver_raw:-unknown}"
        warn "(expected major >= ${MIN_CONDA_MAJOR}; will try to update below)"
    else
        ok "Conda version OK: ${_ver_raw}"
    fi
    fi  # _SKIP_MINIFORGE_DOWNLOAD guard
fi

# Activate conda for this shell only (needed for `conda activate <env>`).
# This sources profile.d/conda.sh into the CURRENT shell ONLY — does not
# mutate ~/.bashrc, ~/.zshrc, or any persistent shell state.
CONDA_BASE="$("$CONDA_BIN" info --base 2>/dev/null)"
[[ -n "$CONDA_BASE" ]] || fail "Could not determine conda base directory"
# shellcheck disable=SC1091
set +u
source "${CONDA_BASE}/etc/profile.d/conda.sh"
set -u
info "Conda base: $CONDA_BASE  (owned-by-us=${CONDA_OWNED_BY_US})"

# ═══════════════════════════════════════════════════════════════
#  Step 1.5: If TOFU_CONDA_MIRROR is set, redirect conda-forge to it
#
#  Many corp networks (e.g. YourProvider) use an HTTP proxy that 403s
#  `conda.anaconda.org` even though it allows the rest of the internet.
#  When that's the case, set TOFU_CONDA_MIRROR to a base URL whose
#  `<base>/conda-forge/<arch>/repodata.json` is reachable.
#
#  For YourProvider hosts, the export's bake-proxy step also writes
#  `TOFU_CONDA_MIRROR=https://mirrors.internal.example.com/conda/cloud` so this
#  block kicks in automatically.  Vanilla / public installs are
#  unaffected — the variable is empty and we never touch .condarc.
#
#  We write to the SIBLING-conda's .condarc only (CONDA_BASE/.condarc),
#  never the user's global ~/.condarc.  Skipped entirely when
#  CONDA_OWNED_BY_US=0 (we don't touch a pre-existing user conda).
# ═══════════════════════════════════════════════════════════════
if [[ "$CONDA_OWNED_BY_US" -eq 1 && -n "${TOFU_CONDA_MIRROR:-}" ]]; then
    info "Configuring conda-forge mirror: ${TOFU_CONDA_MIRROR}"
    cat > "${CONDA_BASE}/.condarc" <<EOF
channels:
  - conda-forge
custom_channels:
  conda-forge: ${TOFU_CONDA_MIRROR}
default_channels:
  - ${TOFU_CONDA_MIRROR}/conda-forge
ssl_verify: true
remote_connect_timeout_secs: 30
remote_read_timeout_secs: 60
remote_max_retries: 3
# Empty proxy_servers tells conda to ignore HTTP(S)_PROXY env vars,
# which on this host 403 conda.anaconda.org.  The mirror host is
# already in no_proxy via .internal.example.com (or whatever bypass list the
# export injected), so requests go DIRECT.
proxy_servers: {}
EOF
    ok "Wrote ${CONDA_BASE}/.condarc (conda-forge → ${TOFU_CONDA_MIRROR}/conda-forge)"
fi

# Similarly for pip: if TOFU_PYPI_INDEX is set (by export, for corp hosts),
# configure the sibling env's pip to use it.  pip.conf is scoped to this
# user/home; put it in $HOME/.config/pip/pip.conf IF not already present,
# but prefer the per-conda-env pip.conf if we can determine the env path.
# (We actually write it after the env is created — see later.)
if [[ -n "${TOFU_PYPI_INDEX:-}" ]]; then
    info "PyPI index override: ${TOFU_PYPI_INDEX}"
    # Export PIP_INDEX_URL for any pip invocation during install.sh.
    # The pip-stanza writer lower in the script picks up this variable
    # too, so the env's pip.conf is permanently pinned.
    export PIP_INDEX_URL="${TOFU_PYPI_INDEX}"
    # Trust the mirror host (common for corp http mirrors without TLS).
    _PYPI_HOST="$(printf '%s' "$TOFU_PYPI_INDEX" | sed -E 's|^https?://([^/:]+).*|\1|')"
    export PIP_TRUSTED_HOST="${_PYPI_HOST}"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 2: Update conda — ONLY if it's the sibling we own
#
#  Outdated conda causes solver hangs and "PackagesNotFoundError" for
#  packages that clearly exist. But updating someone ELSE's conda would
#  be invasive — we never do that. The user-owned path was already
#  version-checked above and rejected if too old.
# ═══════════════════════════════════════════════════════════════
if [[ "$CONDA_OWNED_BY_US" -eq 1 && "$NO_UPDATE_CONDA" -eq 0 ]]; then
    step "Updating sibling conda (we own it)"
    OLD_VER="$(conda --version 2>/dev/null || echo unknown)"
    info "Current version: ${OLD_VER}"

    if conda update -n base -c conda-forge --override-channels -y conda; then
        NEW_VER="$(conda --version 2>/dev/null || echo unknown)"
        if [[ "$OLD_VER" == "$NEW_VER" ]]; then
            ok "conda already up to date (${NEW_VER})"
        else
            ok "conda updated: ${OLD_VER} → ${NEW_VER}"
        fi
    else
        warn "conda self-update failed — this is NOT fatal but may cause solver issues later"
    fi

    # libmamba solver — 10x faster, avoids classic solver hangs.
    # Set as default ONLY for the sibling conda we own (.condarc lives in
    # CONDA_BASE since we never ran `conda init`). This does NOT touch the
    # user's global ~/.condarc.
    info "Ensuring libmamba solver is installed (sibling conda only)..."
    if conda install -n base -c conda-forge --override-channels -y conda-libmamba-solver >/dev/null 2>&1; then
        # Write to the sibling's .condarc (CONDA_BASE/.condarc), not ~/.condarc.
        CONDA_ROOT_PREFIX="$CONDA_BASE" conda config --file "${CONDA_BASE}/.condarc" --set solver libmamba || true
        ok "libmamba solver active for sibling conda (10x faster than classic)"
    else
        warn "Could not install libmamba solver — using classic (slower)"
    fi
elif [[ "$CONDA_OWNED_BY_US" -eq 0 ]]; then
    info "Skipping conda self-update (using your existing conda — leaving it alone)"
    info "If you ever hit solver hangs, you can manually run:"
    info "  conda update -n base -c conda-forge --override-channels -y conda"
elif [[ "$NO_UPDATE_CONDA" -eq 1 ]]; then
    warn "Skipping conda self-update (--no-update-conda)"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 3: Check git and clone repo if needed
# ═══════════════════════════════════════════════════════════════
step "Getting Tofu source code"

if ! command -v git &>/dev/null; then
    info "git not found — installing via conda-forge..."
    conda install -n base -c conda-forge --override-channels -y git
fi

if [[ -f "${INSTALL_DIR}/server.py" ]]; then
    ok "Existing installation found at ${INSTALL_DIR}"
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        info "Updating via git pull..."
        (cd "$INSTALL_DIR" && git pull --ff-only) || warn "git pull failed — continuing with existing code"
    fi
elif [[ -f "server.py" ]]; then
    INSTALL_DIR="$(pwd)"
    ok "Running from project directory: $INSTALL_DIR"
else
    info "Cloning https://github.com/rangehow/ToFu.git → ${INSTALL_DIR}"
    git clone https://github.com/rangehow/ToFu.git "$INSTALL_DIR"
    ok "Repository cloned"
fi

REQ_FILE="${INSTALL_DIR}/requirements.txt"
[[ -f "$REQ_FILE" ]] || fail "requirements.txt not found at $REQ_FILE"

# ═══════════════════════════════════════════════════════════════
#  Step 4: Create / reuse conda env
# ═══════════════════════════════════════════════════════════════
step "Creating conda environment: ${ENV_NAME}"

ENV_EXISTS=0
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    ENV_EXISTS=1
fi

if [[ "$ENV_EXISTS" -eq 1 && "$RESET_ENV" -eq 1 ]]; then
    warn "--reset-env: removing existing env '${ENV_NAME}' (this deletes ALL packages in it)"
    conda env remove -n "$ENV_NAME" -y
    ENV_EXISTS=0
fi

if [[ "$ENV_EXISTS" -eq 1 ]]; then
    ok "Env '${ENV_NAME}' already exists — will update in place"
    info "(tip: re-run with --reset-env to wipe and rebuild it from scratch)"
else
    info "Creating env '${ENV_NAME}' with Python ${PY_VER}..."
    conda create -n "$ENV_NAME" -c conda-forge --override-channels -y "python=${PY_VER}"
    ok "Env '${ENV_NAME}' created"
fi

# Activate it for subsequent installs.
# Conda's own activate/deactivate scripts (e.g. gxx_linux-64) reference
# CONDA_BACKUP_* variables that are unset on first run, which trips
# `set -u`. Relax it just for the conda call.
set +u
conda activate "$ENV_NAME"
set -u
PY="$(command -v python)"
ok "Using Python: $PY ($(python --version 2>&1))"

# ─────────────────────────────────────────────────────────────
#  Write .tofu_env.json marker
#
#  This is the bridge between install.sh and server.py / bootstrap.py.
#  When the user later runs `python server.py` from a shell that does NOT
#  have this conda env activated (very common — they may have just opened
#  a new terminal, or a system /usr/bin/python is on PATH first), the
#  re-exec guard at the top of server.py reads this file and re-execs
#  into the right interpreter via os.execv. No `conda init` required, no
#  shell rc-file mutation, no PATH games — just a single JSON file inside
#  the project that tells server.py "use THIS python".
#
#  Robust > dynamic-write-into-server.py because:
#    • git pull never conflicts with us
#    • export.py just gitignores one file (already added to .gitignore)
#    • multiple Tofu checkouts on the same machine each get their own
#      independent marker pointing at their own env
# ─────────────────────────────────────────────────────────────
ENV_PREFIX="${CONDA_BASE}/envs/${ENV_NAME}"
ENV_PYTHON="${ENV_PREFIX}/bin/python"
[[ -x "$ENV_PYTHON" ]] || fail "Env python not found at $ENV_PYTHON after conda activate"

# Use Python to write JSON safely (no shell quoting traps with paths
# containing spaces / unicode).
"$ENV_PYTHON" - "$INSTALL_DIR" "$CONDA_BASE" "$ENV_NAME" "$ENV_PREFIX" "$ENV_PYTHON" "$CONDA_OWNED_BY_US" <<'PYEOF'
import json, os, sys, time
install_dir, conda_base, env_name, env_prefix, env_python, owned = sys.argv[1:7]
marker = {
    'schema': 1,
    'created_at': int(time.time()),
    'conda_base':   conda_base,
    'env_name':     env_name,
    'env_prefix':   env_prefix,
    'python':       env_python,
    'owned_by_tofu_install': owned == '1',
    'note': ('Written by install.sh. Read by server.py / bootstrap.py to '
             're-exec into the correct interpreter. Safe to delete to disable '
             'auto-activation. NOT exported (gitignored).'),
}
out = os.path.join(install_dir, '.tofu_env.json')
with open(out, 'w', encoding='utf-8') as f:
    json.dump(marker, f, indent=2)
print(f'  ✓ Wrote {out}')
PYEOF
ok ".tofu_env.json marker written (server.py will auto-activate this env)"

# ═══════════════════════════════════════════════════════════════
#  Step 5: Install Python dependencies via conda-forge
# ═══════════════════════════════════════════════════════════════
step "Installing Python dependencies from conda-forge"

# Map requirements.txt → conda-forge package names.
#
# IMPORTANT: trafilatura and htmldate are INTENTIONALLY NOT in this list.
# The conda-forge htmldate package (≤1.9.3) pins "lxml<6,>=5.3", which
# forces libxml2<2.14, which forces icu<76. That transitively blocks
# PostgreSQL 18.1+ (needs icu 78) AND blocks lxml 6.x from being installed.
# The upstream htmldate 1.9.4 (released 2025-11-04) already removed the
# "<6" upper bound on lxml, but conda-forge's feedstock hasn't caught up.
# We install both via pip below — they're pure Python and pip is happy to
# install the unpinned latest version, sidestepping the entire icu deadlock.
CONDA_PKGS=(
    # pip itself — conda 'python' packages OMIT pip by default in recent
    # conda-forge builds. Without this, `python -m pip install ...` below
    # fails with "No module named pip" and trafilatura/htmldate never get
    # installed. Install pip explicitly every time.
    "pip>=23"
    # Quart (async Flask) + Hypercorn (ASGI server) — the core server runtime.
    # cryptography is needed for Hypercorn's auto-TLS (HTTP/2).
    "quart>=0.19"
    "hypercorn>=0.17"
    "cryptography>=42"
    "flask>=3.0"
    "flask-compress>=1.14"
    "requests>=2.31"
    # jinja2 / urllib3 / pyyaml — transitive deps (jinja2←flask/quart,
    # urllib3←requests, pyyaml used directly by routes/api_docs.py for the
    # YAML OpenAPI spec). Pinned in requirements.txt to CVE-clearing floors;
    # listed here so the drift guard passes and clean envs get the fixed
    # versions instead of whatever the resolver happens to pull transitively.
    "jinja2>=3.1.6"
    "urllib3>=1.26.19"
    "pyyaml>=6.0"
    "psutil>=5.9"
    "playwright>=1.40"
    "pillow>=10.0"
    # numpy + scipy — used by scripts/png_to_svg.py for background removal
    # (flood-fill connected-components) in generate_image(svg=true). Without
    # them the SVG bg-removal step silently degrades to a worse trace.
    "numpy>=1.24"
    "scipy>=1.10"
    "python-pptx>=0.6.21"
    # lxml ≥6 works with libxml2 2.14+ and icu 75 OR 78 — gives the solver
    # maximum freedom. It's ABI-compatible with lxml 5.x at the Python level.
    "lxml>=6"
    # BS4 — HTML fallback parser in tofu_search/fetch/html_extract.py
    "beautifulsoup4>=4.12"
    # python-dateutil — eagerly imported by tofu_search/fetch/html_extract.py
    "python-dateutil>=2.8"
    # Office document parsers for lib/doc_parser.py (upload pipeline)
    "python-docx>=1.0"
    "openpyxl>=3.1"
    "xlrd>=2.0"
    "olefile>=0.46"
    "mcp>=1.0"
    # orjson — fast JSON encoder; imported by routes/chat.py for chat
    # snapshot serialisation. Hard dep: the server won't boot without it.
    "orjson>=3.9"
    # sqlalchemy Core — lib/database/_core_schema.py builds the chat
    # persistence schema with it. Hard dep: imported at server boot.
    "sqlalchemy>=2.0"
    # markdown — server-side Markdown rendering. Hard dep at import time.
    "markdown>=3.4"
    # tiktoken — exact BPE tokenizer tier for lib/token_counter.
    "tiktoken>=0.5"
    # PDF parsing (fitz) — used in lib/pdf_parser and routes/paper
    "pymupdf>=1.24"
    # uv / uvx — used by lib/mcp/client.py to launch MCP servers
    "uv>=0.4"
)

# Pip-installed deps.
#
# trafilatura + htmldate are pure-Python packages; installing them via pip
# lets us get htmldate 1.9.4+ (no "lxml<6" upper bound), which in turn lets
# the conda env install PG 18 + icu 78 + lxml 6 cleanly. This is NOT a
# downgrade — it's the opposite: pip gives us NEWER htmldate than conda has.
#
# We ALSO list trafilatura's other pure-Python deps explicitly here
# (justext, courlan, dateparser, charset-normalizer) because we install
# with --no-deps below (to prevent pip from pulling an old lxml that
# shadows our conda lxml 6). Without these, importing trafilatura fails
# with "ModuleNotFoundError: No module named 'justext'" etc.
# NOTE: `docling` (optional, layout-aware PDF parsing — better tables/math on
# academic PDFs) is NOT in this list. It's installed separately later when
# --with-docling is passed, because it pulls ~2 GB of torch + model weights
# and most users don't need it (pymupdf4llm covers the common case).
PIP_ONLY_PKGS=(
    "pymupdf4llm>=0.0.17"
    # vtracer — Rust-backed raster→vector tracer for generate_image(svg=true)
    # (scripts/png_to_svg.py). Self-contained wheel: no Python deps, does not
    # touch lxml/icu, so --no-deps is safe. Hard dep — the svg parameter on
    # the generate_image tool is always advertised, so it must always work.
    "vtracer>=0.6.11"
    "trafilatura>=1.6"
    "htmldate>=1.9.4"
    # trafilatura's pure-Python deps (from its pyproject.toml).
    # certifi/urllib3 are already pulled in by requests via conda.
    "justext>=3.0.1"
    # justext still imports lxml.html.clean, which was extracted into a
    # separate package in lxml 5.2+. Pinning it explicitly keeps imports
    # working regardless of which lxml major version conda installs.
    "lxml_html_clean>=0.4"
    "courlan>=1.3.2"
    "charset-normalizer>=3.4.0"
    # htmldate's pure-Python deps.
    "dateparser>=1.1.2"
    # Transitive runtime deps that are NOT auto-pulled because we install
    # the pip stack with --no-deps (to keep conda's lxml 6 from being
    # shadowed). All pure-Python wheels — they touch neither lxml nor icu,
    # so listing them explicitly is safe. Skipping any of these breaks
    # `from babel import Locale` (in courlan.filters) at server boot.
    "babel>=2.12"          # required by courlan>=1.3 (Locale, UnknownLocaleError)
    "tld>=0.13"            # required by courlan
    "pytz>=2024.1"         # required by dateparser
    "regex>=2024.0"        # required by dateparser
    "tzlocal>=5.0"         # required by dateparser
)

# ── Drift guard: every dep declared in requirements.txt must be covered by
#    CONDA_PKGS or PIP_ONLY_PKGS (or installed by a dedicated step below).
#    install.sh deliberately splits installs across conda/pip to dodge the
#    lxml6/icu78/PG18 deadlock, so we can't just `pip install -r`. Instead we
#    fail FAST here if the hand-maintained lists fall out of sync with
#    requirements.txt — far better than a ModuleNotFoundError at server boot.
_REQ_FILE="${INSTALL_DIR:-$PWD}/requirements.txt"
if [[ -f "$_REQ_FILE" ]]; then
    _norm() { tr 'A-Z' 'a-z' | sed -E 's/[<>=!~; ].*//; s/_/-/g; s/[[:space:]]//g'; }
    # Packages installed by dedicated steps, not the two arrays:
    #   tofu-search (own step), docling (--with-docling only).
    _EXEMPT=$'tofu-search\ndocling'
    _covered="$(printf '%s\n' "${CONDA_PKGS[@]}" "${PIP_ONLY_PKGS[@]}" | _norm; printf '%s\n' "$_EXEMPT")"
    _declared="$(grep -vE '^\s*#' "$_REQ_FILE" | grep -vE '^\s*$' | _norm | sort -u)"
    _missing="$(comm -23 <(printf '%s\n' "$_declared" | sort -u) <(printf '%s\n' "$_covered" | sort -u))"
    if [[ -n "$_missing" ]]; then
        warn "requirements.txt declares packages NOT covered by install.sh:"
        printf '%s\n' "$_missing" | sed 's/^/    - /' >&2
        warn "Add each to CONDA_PKGS (conda-forge) or PIP_ONLY_PKGS (pip) above."
        fail "install.sh package lists are out of sync with requirements.txt."
    fi
    ok "Dependency lists cover all of requirements.txt"
fi

# ── Heal broken envs: remove any pip-installed versions of these deps ──
# A common failure mode on older hosts (CentOS 7 / glibc 2.17) is that an
# earlier run left pip's manylinux wheel of lxml in the env. That wheel
# links to GLIBC_2.25+ and crashes at import. We uninstall any pip copies
# first so conda-forge's (sysroot-linked) version is the one used.
info "Purging any pip-installed copies that would shadow conda-forge..."
# Note: trafilatura + htmldate are INTENTIONALLY kept in pip (we WANT
# pip versions of those — conda-forge's htmldate ≤1.9.3 has the
# lxml<6 pin that locks us out of modern icu/PG). So we DON'T include
# them in this purge list.
PIP_NAMES=(quart hypercorn cryptography flask flask-compress Flask-Compress requests psutil
           playwright pillow Pillow python-pptx lxml beautifulsoup4 bs4
           python-dateutil dateutil python-docx docx openpyxl xlrd olefile
           mcp pymupdf PyMuPDF uv)
PIP_LIST="$(python -m pip list --format=freeze 2>/dev/null || true)"
TO_UNINSTALL=()
for name in "${PIP_NAMES[@]}"; do
    if echo "$PIP_LIST" | grep -iq "^${name}=="; then
        TO_UNINSTALL+=("$name")
    fi
done
if [[ ${#TO_UNINSTALL[@]} -gt 0 ]]; then
    info "Removing pip copies: ${TO_UNINSTALL[*]}"
    python -m pip uninstall -y "${TO_UNINSTALL[@]}" || warn "pip uninstall had issues"
else
    ok "No pip-installed deps to purge"
fi

info "Solving and installing: ${CONDA_PKGS[*]}"
# ── Pre-emptive conflict heal ──
# Some packages from previous install runs (e.g. an older postgresql pulled
# in a pinned icu/libxml2 that blocks newer trafilatura/lxml). Before the
# main solve, purge known conflict sources so the solver has a clean slate.
# All removes are best-effort — missing packages are fine.
info "Purging potentially conflicting conda packages (best-effort)..."
# trafilatura + htmldate removed from conda (we install via pip — see
# PIP_ONLY_PKGS above for rationale). If a previous run installed them
# via conda, nuke them here so their stale 'lxml<6' pin doesn't fight us.
CONDA_CONFLICT_PKGS=(
    postgresql psycopg2
    trafilatura htmldate courlan
    lxml libxml2 libxml2-16 libxslt
    icu
)
conda remove -n "$ENV_NAME" -y --force "${CONDA_CONFLICT_PKGS[@]}" >/dev/null 2>&1 || true
ok "Conflict-prone packages cleared (will reinstall below)"

# Also purge any pip-installed trafilatura/htmldate from prior runs so
# pip's own install below is clean.
python -m pip uninstall -y trafilatura htmldate courlan >/dev/null 2>&1 || true

# --force-reinstall: make sure conda actually re-lays-down the files even if
# its metadata still thinks the package is satisfied (common right after a
# pip-uninstall — conda's view of the env can be stale).
_install_main_deps() {
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y --force-reinstall "${CONDA_PKGS[@]}"
}

if ! _install_main_deps; then
    warn "First solve failed — doing a deeper reset of the conflicting packages and retrying"
    # Deeper reset: also strip libs that often pin icu/libxml2, then retry.
    conda remove -n "$ENV_NAME" -y --force \
        postgresql psycopg2 libpq \
        trafilatura htmldate courlan \
        lxml libxml2 libxml2-16 libxslt \
        icu \
        >/dev/null 2>&1 || true
    if ! _install_main_deps; then
        # ── Last-resort: nuke the env and rebuild from scratch ──
        # The env's conda-meta/history still pins old specs (e.g. postgresql>=18)
        # that --force removes don't clear. Only `env remove` truly resets it.
        warn "Deep reset still failed — conda env history has stale pins."
        warn "Auto-rebuilding env '${ENV_NAME}' from scratch (one-time, ~2 min)..."
        set +u
        conda deactivate >/dev/null 2>&1 || true
        set -u
        conda env remove -n "$ENV_NAME" -y
        conda create -n "$ENV_NAME" -c conda-forge --override-channels -y "python=${PY_VER}"
        set +u
        conda activate "$ENV_NAME"
        set -u
        PY="$(command -v python)"
        ok "Env '${ENV_NAME}' rebuilt with fresh Python ${PY_VER}"
        _install_main_deps
    fi
fi
ok "Python dependencies installed"

# ── Post-install import check: conda's metadata occasionally says a
#    package is installed when the actual files are missing (happens when
#    a prior run did `conda remove --force` and cache got confused).
#    Verify each critical package imports; if any fail, force a
#    --force-reinstall targeted at just those.
info "Verifying critical conda packages import correctly..."
_IMPORT_CHECK_PKGS=(
    "quart:quart"
    "hypercorn:hypercorn"
    "cryptography:cryptography"
    "flask:flask"
    "flask_compress:flask-compress"
    "requests:requests"
    "psutil:psutil"
    "playwright:playwright"
    "PIL:pillow"
    "numpy:numpy"
    "scipy:scipy"
    "pptx:python-pptx"
    "lxml:lxml"
    "bs4:beautifulsoup4"
    "dateutil:python-dateutil"
    "docx:python-docx"
    "openpyxl:openpyxl"
    "mcp:mcp"
    "fitz:pymupdf"
)
_MISSING_PKGS=()
for _spec in "${_IMPORT_CHECK_PKGS[@]}"; do
    _mod="${_spec%%:*}"
    _conda_name="${_spec##*:}"
    if ! python -c "import ${_mod}" 2>/dev/null; then
        warn "  ${_mod} (conda pkg '${_conda_name}') imports missing"
        _MISSING_PKGS+=("$_conda_name")
    fi
done
if [[ ${#_MISSING_PKGS[@]} -gt 0 ]]; then
    warn "Conda metadata inconsistent — force-reinstalling: ${_MISSING_PKGS[*]}"
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
        --force-reinstall "${_MISSING_PKGS[@]}" || \
        warn "Force-reinstall failed — env may need a full rebuild (re-run with --reset-env)"
fi

# ── pip-install helper: forces install into the conda env, never ~/.local ──
#
# Why this exists: pip silently falls back to `--user` (writes to
# ~/.local/lib/pythonX.Y/site-packages) when it thinks the target
# site-packages isn't writable. On cross-DC FUSE mounts the writability
# probe can flake, and even when it succeeds, having any pip wheel
# under ~/.local shadows the conda env's copy at runtime → mysterious
# "wrong version" / "GLIBC not found" failures. We hard-disable that
# fallback for every pip call in this script:
#   - PIP_USER=0 + unset PYTHONUSERBASE: blocks --user mode
#   - --prefix "$ENV_PREFIX": pin install location to the conda env
#   - explicit Permission-denied detection: if pip *still* manages to
#     write somewhere it can't, fail loudly instead of warn-and-continue
#
# Usage: _safe_pip_install <pip args...>
#   Returns 0 on success.
#   Returns 1 on ordinary failure (caller decides whether to retry).
#   Calls fail() (exits) on Permission-denied — that is never recoverable
#   without user intervention.
_safe_pip_install() {
    local _log
    _log="$(mktemp "${TMPDIR:-/tmp}/tofu_pip.XXXXXX")"
    local _rc=0
    (
        # Some hosts force --user globally via the PIP_USER env var OR a
        # pip.conf 'user=true' (~/.pip/pip.conf, ~/.config/pip/pip.conf,
        # /etc/pip.conf). With --user active, pip refuses --prefix:
        # "Can not combine '--user' and '--prefix'". Setting PIP_USER=0 is
        # NOT enough — pip still treats the var as set, and a pip.conf
        # default is untouched. Neutralise BOTH sources:
        #   - env: unset PIP_USER / PYTHONUSERBASE
        #   - config files: PIP_CONFIG_FILE=/dev/null makes pip ignore every
        #     pip.conf (the index URL comes from PIP_INDEX_URL, set elsewhere,
        #     so the corp mirror still applies).
        #   - CLI: --no-user as a final belt-and-braces override.
        unset PIP_USER
        unset PYTHONUSERBASE
        export PIP_CONFIG_FILE=/dev/null
        # Tee so the user still sees pip's output live; capture to log
        # for the post-mortem permission check.
        python -m pip install --no-user --prefix "$ENV_PREFIX" "$@" 2>&1 | tee "$_log"
        exit "${PIPESTATUS[0]}"
    )
    _rc=$?
    if [[ $_rc -ne 0 ]] && grep -qE 'Permission denied|\[Errno 13\]' "$_log"; then
        warn "pip hit Permission denied — refusing to fall back to --user."
        warn "  Offending output:"
        grep -E 'Permission denied|\[Errno 13\]' "$_log" | head -5 | sed 's/^/    /' >&2
        warn "  Likely cause: ~/.local has stale entries from a previous failed install,"
        warn "  or the conda env site-packages is not writable for the current user."
        warn "  Recovery: rm -rf ~/.local/lib/python*/site-packages/{courlan,trafilatura,htmldate}"
        warn "           ls -ld ${ENV_PREFIX}/lib/python*/site-packages   # must be writable"
        rm -f "$_log"
        fail "pip install aborted on permission error — see messages above."
    fi
    rm -f "$_log"
    return "$_rc"
}

# ── Install pip-only deps (e.g. pymupdf4llm) into the conda env ──
# pymupdf4llm is not shipped on conda-forge; it's a thin LLM-oriented Markdown
# extractor built on top of pymupdf (which we just installed via conda).
if [[ ${#PIP_ONLY_PKGS[@]} -gt 0 ]]; then
    info "Installing pip-only deps (not on conda-forge): ${PIP_ONLY_PKGS[*]}"

    # Defensive: ensure pip is actually importable in this env. Recent
    # conda-forge 'python' no longer bundles pip automatically; if the
    # main deps install above didn't pull it in, install it now so the
    # pip commands below don't fail with "No module named pip".
    if ! python -c "import pip" 2>/dev/null; then
        warn "pip not found in env — installing it from conda-forge now"
        if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y 'pip>=23'; then
            warn "Could not install pip via conda — trying ensurepip as fallback"
            python -m ensurepip --upgrade 2>/dev/null || true
        fi
    fi

    if ! python -c "import pip" 2>/dev/null; then
        warn "pip STILL not available — skipping pip installs (trafilatura/htmldate/pymupdf4llm)"
        warn "Manual recovery: conda install -n ${ENV_NAME} -c conda-forge pip && \\"
        warn "                 pip install ${PIP_ONLY_PKGS[*]}"
    elif _safe_pip_install --no-deps --upgrade "${PIP_ONLY_PKGS[@]}"; then
        ok "Pip-only deps installed"
    else
        warn "pip install --no-deps failed — retrying with dependency resolution"
        if _safe_pip_install --upgrade "${PIP_ONLY_PKGS[@]}"; then
            ok "Pip-only deps installed (with dependency resolution)"
        else
            warn "Pip-only deps install failed — some PDF features may be degraded"
        fi
    fi
fi

# ── tofu-search — the standalone search + content-fetch pipeline ──
# server.py lists tofu_search.fetch / tofu_search.search as CRITICAL imports,
# so the server refuses to boot without it. Two install sources:
#   1. A bundled wheel under vendor/ (personal/internal exports) — used when
#      present, because corp networks point pip at an internal mirror that
#      does NOT carry tofu-search (only public PyPI does).
#   2. Public PyPI (opensource installs / fresh git clone on a vanilla host).
# --no-deps is safe: its deps (requests / trafilatura / bs4 / lxml /
# python-dateutil) are installed above, and --no-deps keeps pip from
# shadowing conda's lxml 6.
# A bare "import tofu_search" is NOT a safe skip condition: an OLDER copy that
# predates a server symbol (e.g. a colleague's pre-existing env stuck on an
# earlier release) imports fine yet is missing the names server.py / handlers
# import, so the server still dies at boot with
#   "ImportError: cannot import name '<symbol>' from 'tofu_search'".
# Skip ONLY when the installed build (a) meets the requirements.txt floor AND
# (b) exposes the exact symbols the server imports. Floor is read from
# requirements.txt so it stays in sync with the drift guard above.
_TS_FLOOR="$(grep -iE '^[[:space:]]*tofu-search[[:space:]]*>=' "${INSTALL_DIR:-$PWD}/requirements.txt" 2>/dev/null | sed -E 's/.*>=[[:space:]]*//; s/[^0-9.].*//' | head -1)"
[[ -z "$_TS_FLOOR" ]] && _TS_FLOOR="0.4.0"
_TS_SKIP_PROBE="$(cat <<PYEOF
import sys
try:
    import tofu_search as ts
    from tofu_search import fetch_page_content, looks_like_text_asset, perform_web_search  # noqa: F401
except Exception:
    sys.exit(1)
def _v(s):
    out = []
    for p in (str(s).split('+')[0].split('.') + ['0', '0', '0'])[:3]:
        d = ''.join(ch for ch in p if ch.isdigit())
        out.append(int(d) if d else 0)
    return tuple(out)
sys.exit(0 if _v(getattr(ts, '__version__', '0')) >= _v('${_TS_FLOOR}') else 2)
PYEOF
)"
if python -c "$_TS_SKIP_PROBE" 2>/dev/null; then
    ok "tofu-search satisfies floor ${_TS_FLOOR} with required symbols — skipping"
elif ! python -c "import pip" 2>/dev/null; then
    warn "pip not available — cannot install tofu-search (server will fail to boot)"
else
    step "Installing/upgrading tofu-search (required search/fetch pipeline; need >= ${_TS_FLOOR} with server symbols)"
    _TOFU_SEARCH_WHL=""
    if [[ -d "${INSTALL_DIR}/vendor" ]]; then
        _TOFU_SEARCH_WHL="$(ls -1 "${INSTALL_DIR}"/vendor/tofu_search-*.whl 2>/dev/null | { sort -V 2>/dev/null || sort; } | tail -1)"
    fi
    if [[ -n "$_TOFU_SEARCH_WHL" ]]; then
        info "Installing bundled wheel: ${_TOFU_SEARCH_WHL##*/}"
        if _safe_pip_install --no-deps --upgrade "$_TOFU_SEARCH_WHL"; then
            ok "tofu-search installed from bundled wheel"
        else
            warn "Bundled tofu-search wheel install failed — falling back to PyPI"
            _safe_pip_install --no-deps --upgrade "tofu-search>=${_TS_FLOOR}" \
                && ok "tofu-search installed from PyPI" \
                || fail "tofu-search install failed — the server will not boot. Retry: pip install tofu-search"
        fi
    else
        info "No bundled wheel — installing from PyPI"
        if _safe_pip_install --no-deps --upgrade "tofu-search>=${_TS_FLOOR}"; then
            ok "tofu-search installed from PyPI"
        else
            warn "tofu-search install from PyPI failed."
            warn "  If you are behind a corp mirror that lacks tofu-search, retry with public PyPI:"
            warn "    pip install --index-url https://pypi.org/simple/ 'tofu-search>=${_TS_FLOOR}'"
            fail "tofu-search install failed — the server will not boot without it."
        fi
    fi
fi

# ── Optional: bundled internal MCP servers (hope-mcp, xuecheng-mcp, llm-mcp) ──
# These private servers aren't on PyPI, so the MCP tab's "Install" button
# (which spawns `<name>-mcp` on PATH) can't fetch them — we must pip-install
# the source here. Sources, in priority order:
#   1. vendor/<name>/   — personal/internal EXPORTS bundle the source here.
#   2. ../<name>/        — a DEV checkout: sibling repos next to this one.
# Covering the sibling case means a developer clone (no vendor/) also gets the
# launchers on PATH, so click-to-install is a fast handshake, not a cold pip.
# Skipped silently if neither source exists (opensource exports).
step "Installing bundled internal MCP servers"
_BUNDLED_MCPS=()
for _mcp in hope-mcp xuecheng-mcp llm-mcp; do
    _vendor_path="${INSTALL_DIR}/vendor/${_mcp}"
    _sibling_path="$(cd "${INSTALL_DIR}/.." 2>/dev/null && pwd)/${_mcp}"
    if [[ -f "${_vendor_path}/pyproject.toml" ]]; then
        _BUNDLED_MCPS+=("$_vendor_path")
    elif [[ -f "${_sibling_path}/pyproject.toml" ]]; then
        _BUNDLED_MCPS+=("$_sibling_path")
    fi
done
if [[ ${#_BUNDLED_MCPS[@]} -eq 0 ]]; then
    info "No bundled MCP repos (vendor/ or sibling checkout) — skipping"
elif ! python -c "import pip" 2>/dev/null; then
    warn "pip not available — cannot install bundled MCP servers"
    warn "Manual recovery later: pip install ${_BUNDLED_MCPS[*]}"
else
    info "Installing: ${_BUNDLED_MCPS[*]}"
    if _safe_pip_install --upgrade "${_BUNDLED_MCPS[@]}"; then
        ok "Bundled MCP servers installed (hope-mcp / xuecheng-mcp / llm-mcp now on PATH)"
    else
        warn "Bundled MCP install failed — Settings → MCP install buttons may fail"
        warn "Retry manually: pip install ${_BUNDLED_MCPS[*]}"
    fi
fi

# ── Optional: Docling (layout-aware PDF parsing) ──
# Opt-in via --with-docling. Adds ~2 GB to the env (pulls torch + downloads
# model weights on first use). Not installed by default because the base
# pymupdf4llm path already gives a good Markdown render for most PDFs —
# docling shines on academic papers with borderless tables and math.
if [[ "$WITH_DOCLING" -eq 1 ]]; then
    step "Installing optional Docling (layout-aware PDF parsing)..."
    if python -c "import pip" 2>/dev/null; then
        # Use the CPU-only torch wheel index by default so we don't pull
        # the multi-GB CUDA wheels on machines that won't use them. Users
        # on GPU boxes can just `pip install docling` themselves afterwards
        # to replace torch with the GPU variant.
        _DOCLING_INDEX="https://download.pytorch.org/whl/cpu"
        info "  pip install docling (--extra-index-url ${_DOCLING_INDEX})"
        if _safe_pip_install --upgrade \
             --extra-index-url "${_DOCLING_INDEX}" \
             "docling>=2.0"; then
            ok "Docling installed — set PDF_TEXT_MODE=structured in .env to enable"
        else
            warn "Docling install failed — the server will still run (fallback: pymupdf4llm)"
            warn "You can retry manually: pip install docling --extra-index-url ${_DOCLING_INDEX}"
        fi
    else
        warn "pip not available — cannot install docling. Skipping."
    fi
fi

# ── Install PostgreSQL + psycopg2 from conda-forge (optional but recommended) ──
# tofu uses PG for better concurrency (100+ concurrent users), auto-falls back
# to SQLite if PG is missing.
#
# Layered fallback: try PG 18 → 17 → 16 → SQLite. Different conda-forge
# snapshots pin icu/libxml2 in ways that conflict with trafilatura/lxml
# (we saw this on hosts where PG 18 requires icu>=78 but trafilatura needs
# icu<76). Trying older majors often succeeds because their icu pins are
# looser. The first major whose solve succeeds wins.
PG_INSTALLED_MAJOR=""   # set to the major we successfully installed, empty if we gave up
if [[ "$WITH_POSTGRES" -ne 1 ]]; then
    # ── SQLite is the default (2026-07). PostgreSQL is opt-in via
    #    --with-postgres because its install (icu/libxml2/PG-major solve +
    #    initdb + smoke-test) is the slowest, most failure-prone step and
    #    single-user setups don't need it. Leaving PG_INSTALLED_MAJOR empty
    #    makes the pgdata-validation + smoke-test steps below no-op cleanly
    #    and pins TOFU_DB_BACKEND=sqlite in .env.
    if [[ "$FORCE_SQLITE" -eq 1 ]]; then
        info "--force-sqlite: using SQLite (PostgreSQL not installed)"
    else
        info "Using SQLite (default, zero-config). Pass --with-postgres to install"
        info "PostgreSQL instead (recommended only for 100+ concurrent users)."
    fi
elif [[ "$FORCE_SQLITE" -eq 1 ]]; then
    info "--force-sqlite overrides --with-postgres: skipping PostgreSQL install entirely"
else
    # If user pinned a specific major, only try that one.
    if [[ -n "$PG_MAJOR" ]]; then
        _PG_TRY=("$PG_MAJOR")
    else
        _PG_TRY=("${PG_MAJOR_CANDIDATES[@]}")
    fi

    info "Installing PostgreSQL + psycopg2 from conda-forge (trying majors: ${_PG_TRY[*]})"
    # ── Pre-clean prior PG remnants from the env ──
    # A previous run may have left a different PG major installed. Its
    # history pin will fight any attempt to install a different major.
    # --force remove clears the package files; the history pin is cleared
    # later by --prune-deps if needed.
    conda remove -n "$ENV_NAME" -y --force postgresql libpq psycopg2 >/dev/null 2>&1 || true

    _PG_BIN_DIR="${CONDA_BASE}/envs/${ENV_NAME}/bin"
    _PG_LAST_LOG=""

    # Install strategy: try the requested major with a plain spec first.
    # Since trafilatura/htmldate are now pip-installed (see Step 5 above),
    # nothing in the env forces a libxml2 version, so the solver is free to
    # pick whichever icu/libxml2 combination matches the PG major chosen.
    #
    # If the first attempt still fails (e.g. conda-forge snapshot is mid-
    # migration and PG's icu-78 libpq build isn't fully propagated to this
    # arch yet), we fall back to the next major in the list.
    for _try_major in "${_PG_TRY[@]}"; do
        info "  Trying PostgreSQL ${_try_major}.x ..."
        _PG_LAST_LOG="/tmp/tofu_pg_install_${_try_major}.log"

        set +e
        conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
            "postgresql=${_try_major}" 'psycopg2>=2.9' 2>&1 | tee "$_PG_LAST_LOG"
        _rc="${PIPESTATUS[0]}"
        set -e

        if [[ "$_rc" -eq 0 && -x "${_PG_BIN_DIR}/postgres" ]]; then
            _got_major="$("${_PG_BIN_DIR}/postgres" --version 2>/dev/null \
                | awk '{print $3}' | cut -d. -f1)"
            if [[ "$_got_major" == "$_try_major" ]]; then
                PG_INSTALLED_MAJOR="$_got_major"
                ok "PostgreSQL ${PG_INSTALLED_MAJOR}.x installed + psycopg2"
                break
            fi
            warn "  Installed postgres reports major=${_got_major}, expected ${_try_major}"
        elif [[ "$_rc" -ne 0 ]]; then
            warn "  PG ${_try_major}.x solve failed (rc=${_rc}) — see ${_PG_LAST_LOG}"
        else
            warn "  conda returned 0 but ${_PG_BIN_DIR}/postgres missing"
        fi

        # Ensure next attempt starts clean (important: leftover libpq/history
        # pins can make the next major fail for unrelated reasons).
        conda remove -n "$ENV_NAME" -y --force postgresql libpq psycopg2 >/dev/null 2>&1 || true
    done

    if [[ -z "$PG_INSTALLED_MAJOR" ]]; then
        warn "All PG majors failed to install on this host"
        [[ -n "$_PG_LAST_LOG" ]] && warn "Last conda log: ${_PG_LAST_LOG}"
        warn ""
        warn "Diagnosis checklist (from the conda solver output above):"
        warn "  1. Is the conda-forge snapshot mid-migration for your arch?"
        warn "     → Run: conda search -c conda-forge --override-channels 'postgresql=18' --info | head -40"
        warn "       and check whether libpq-18.x builds exist for your platform."
        warn "  2. Does something in the env still pin icu/libxml2 to an old side?"
        warn "     → Run: conda list -n ${ENV_NAME} | grep -E '(icu|libxml2|lxml)'"
        warn "     → If you see 'icu 75' but PG needs 78 (or vice-versa), inspect the"
        warn "       'history' file: \$CONDA_PREFIX/conda-meta/history"
        warn "  3. Is conda itself outdated?"
        warn "     → Re-run WITHOUT --no-update-conda"
        warn ""
        warn "Last-resort: re-run with --force-sqlite if you just want to get running (SQLite"
        warn "                 is fine for single-user / <100 concurrent and is bit-for-bit"
        warn "                 compatible with the same app code)."
    fi
fi

# ── Verify the full HTML-fetch stack imports (no hidden missing deps) ──
# This runs the same chain that server.py will run at startup, so any
# ModuleNotFoundError here surfaces BEFORE the user hits it.
#
# We also include the transitive runtime deps (babel/tld/pytz/regex/tzlocal)
# in the import probe — those are the ones most likely to be missing because
# we install with --no-deps. If any leaf import fails, self-heal by re-running
# pip WITH dependency resolution (constrained so it can't downgrade lxml),
# then re-verify. Only fail-stop if the second attempt still doesn't import,
# so install.sh never prints "Installation complete!" on a broken env again.
info "Verifying lxml + trafilatura + htmldate + justext + transitive deps import correctly..."

_TOFU_IMPORT_PROBE='import lxml.etree, lxml_html_clean, trafilatura, htmldate, justext, courlan, dateparser, babel, tld, pytz, regex, tzlocal, tofu_search.search, tofu_search.fetch; from tofu_search import fetch_page_content, looks_like_text_asset, perform_web_search; import tofu_search as _ts; print("lxml", lxml.__version__, "trafilatura", trafilatura.__version__, "htmldate", htmldate.__version__, "justext", justext.__version__, "tofu_search", getattr(_ts, "__version__", "?"))'
_TOFU_IMPORT_ERR="$(mktemp "${TMPDIR:-/tmp}/tofu_import_err.XXXXXX")"

if python -c "$_TOFU_IMPORT_PROBE" 2>"$_TOFU_IMPORT_ERR"; then
    ok "Import check passed"
    rm -f "$_TOFU_IMPORT_ERR"
else
    warn "Import check FAILED — auto-healing missing transitive deps"
    sed 's/^/    /' "$_TOFU_IMPORT_ERR" >&2 || true

    # Self-heal: re-run pip WITH dep resolution, but constrain lxml so the
    # resolver can't downgrade conda's lxml 6 (the original reason we used
    # --no-deps). Constraint files apply to ALL packages pip considers,
    # not just direct asks, so any lxml downgrade attempt is blocked.
    _TOFU_PIP_CONSTRAINT="$(mktemp "${TMPDIR:-/tmp}/tofu_pip_constraint.XXXXXX")"
    {
        echo "lxml>=6"
        echo "libxml2>=2.14"   # ignored if not on PyPI; harmless
    } > "$_TOFU_PIP_CONSTRAINT"

    info "Re-running pip install (with deps, constrained lxml>=6)..."
    if _safe_pip_install --upgrade --constraint "$_TOFU_PIP_CONSTRAINT" "${PIP_ONLY_PKGS[@]}"; then
        info "Re-installed pip stack with dependency resolution"
    else
        warn "Auto-heal pip install failed — falling back to explicit transitive set"
        _safe_pip_install --upgrade babel tld pytz regex tzlocal || \
            warn "  Could not install babel/tld/pytz/regex/tzlocal directly either"
    fi
    rm -f "$_TOFU_PIP_CONSTRAINT"

    # Re-verify; this time, if it STILL doesn't import, abort the install
    # so the user gets a real error instead of a silent broken state.
    if python -c "$_TOFU_IMPORT_PROBE" 2>"$_TOFU_IMPORT_ERR"; then
        ok "Import check passed after auto-heal"
        rm -f "$_TOFU_IMPORT_ERR"
    else
        warn "Imports still broken after auto-heal. Last error:"
        sed 's/^/    /' "$_TOFU_IMPORT_ERR" >&2 || true
        warn "If you see 'GLIBC_2.xx not found', a pip wheel is still shadowing conda's copy."
        warn "Try: conda activate ${ENV_NAME} && pip uninstall -y lxml && \\"
        warn "     conda install -c conda-forge --force-reinstall lxml"
        warn "If you see 'No module named X', run: pip install X"
        fail "Critical fetch-stack imports broken — see ${_TOFU_IMPORT_ERR}"
    fi
fi

# ── Verify the PNG→SVG stack (generate_image svg=true) ──
# vtracer (pip, Rust wheel) + numpy/scipy (conda) power scripts/png_to_svg.py.
# The generate_image tool ALWAYS advertises the `svg` parameter, so these must
# import. vtracer ships no Python deps, so a plain pip retry (no lxml
# constraint needed) is the right self-heal.
info "Verifying PNG→SVG stack (vtracer + numpy + scipy) imports correctly..."
_SVG_IMPORT_PROBE='import vtracer, numpy, scipy; print("vtracer ok, numpy", numpy.__version__, "scipy", scipy.__version__)'
if python -c "$_SVG_IMPORT_PROBE" 2>/dev/null; then
    ok "PNG→SVG stack import check passed"
else
    warn "PNG→SVG stack import failed — retrying vtracer via pip"
    if _safe_pip_install --upgrade vtracer && python -c "$_SVG_IMPORT_PROBE" 2>/dev/null; then
        ok "PNG→SVG stack import check passed after retry"
    else
        warn "vtracer still not importable — generate_image(svg=true) will fail."
        warn "Manual recovery: conda activate ${ENV_NAME} && pip install vtracer"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 6: Verify SQLite (built into Python)
# ═══════════════════════════════════════════════════════════════
step "Checking SQLite"
SQLITE_VER="$(python -c 'import sqlite3; print(sqlite3.sqlite_version)')"
ok "SQLite $SQLITE_VER (built into Python)"

# ═══════════════════════════════════════════════════════════════
#  Step 7: Install ripgrep, fd-find & tmux from conda-forge
# ═══════════════════════════════════════════════════════════════
step "Installing ripgrep + fd-find + tmux (fast search + terminal multiplexer)"
if conda install -n "$ENV_NAME" -c conda-forge --override-channels -y ripgrep fd-find tmux; then
    ok "ripgrep + fd-find + tmux installed"
else
    warn "ripgrep/fd-find/tmux install failed — code search will fall back to grep / os.walk"
fi


# ═══════════════════════════════════════════════════════════════
#  Node.js (OPTIONAL) — powers two best-effort, fail-open features:
#    1. `node --check` syntax gate on the built JS bundle (lib/js_bundler.py)
#    2. the optional esbuild stronger-minify pass (~12% smaller gzip bundle)
#  Neither is required: without node the bundler uses its dependency-free
#  Python minifier and the app is byte-identical. So this step NEVER fails
#  the install — it only enhances.
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_NODE" -eq 1 ]]; then
    step "Skipping Node.js + esbuild (--skip-node)"
    info "JS bundle will use the dependency-free Python minifier (byte-identical output)."
elif conda install -n "$ENV_NAME" -c conda-forge --override-channels -y nodejs; then
    step "Installing Node.js + esbuild (optional — stronger JS bundle minify)"
    ok "Node.js installed"
    # ── npm must FAIL-FAST, never hang ────────────────────────────────
    # npm has no corp-mirror redirect and its defaults are fetch-timeout
    # 300s × fetch-retries 2, so on a network that blocks
    # registry.npmjs.org it STALLS for many minutes per package instead
    # of erroring — the `|| warn` fallback below then never fires and the
    # whole install appears frozen.  Since this step is OPTIONAL and
    # fail-open (the Python minifier is byte-identical), we bound npm hard:
    #   1. a ~5s PREFLIGHT reachability probe against the effective
    #      registry — if it's unreachable we SKIP npm outright (turns the
    #      worst case from 5 min into ~5s, generically, on ANY blocked net).
    #   2. npm_config_* env vars cap per-request timeout + retries so a
    #      registry that resolves-but-stalls errors in ~1 min, not ~15.
    #   3. an outer `timeout` wrapper is an absolute ceiling regardless.
    #   4. TOFU_NPM_REGISTRY (baked by export for corp hosts) redirects
    #      the registry to a reachable mirror, same story as conda/pip.
    export npm_config_fetch_timeout=60000
    export npm_config_fetch_retries=1
    export npm_config_fetch_retry_maxtimeout=20000
    export npm_config_fetch_retry_mintimeout=5000
    if [[ -n "${TOFU_NPM_REGISTRY:-}" ]]; then
        info "npm registry override: ${TOFU_NPM_REGISTRY}"
        export npm_config_registry="${TOFU_NPM_REGISTRY}"
    fi
    # Portable hard-timeout wrapper: GNU `timeout`, macOS `gtimeout`, else none.
    _NPM_TIMEOUT=""
    if command -v timeout >/dev/null 2>&1; then
        _NPM_TIMEOUT="timeout 300"
    elif command -v gtimeout >/dev/null 2>&1; then
        _NPM_TIMEOUT="gtimeout 300"
    fi
    # ── Preflight: is the effective registry reachable in ~5s? ──────────
    # This is the real speedup: on a network that can't reach the registry
    # (the corp-proxy case), skip npm in ~5s instead of burning the full
    # 5-min timeout cap. The probe MUST itself be hard-bounded so it can
    # never become the new hang. `curl --max-time 5` is the ceiling; a
    # `timeout 6` wrapper is a belt-and-braces backstop for a curl that
    # ignores its own timeout (e.g. stuck in DNS).
    #
    # CRITICAL — probe a real PACKAGE endpoint, not the registry ROOT.
    # A transparent corp proxy can 200/redirect the registry root while
    # still 403-ing actual package traffic; a HEAD to the root would then
    # FALSE-POSITIVE "reachable" and npm would stall on the first package
    # fetch anyway. So we GET the metadata of a package we actually need
    # (`esbuild`) — exactly the traffic npm will do — and rely on curl's
    # `-f` (fail on HTTP >= 400) so a 403/404 on package traffic is
    # correctly classified UNREACHABLE. Same for wget's `--server-response`
    # gate via exit code on 4xx/5xx.
    _NPM_REGISTRY_URL="${npm_config_registry:-https://registry.npmjs.org/}"
    _NPM_PROBE_URL="${_NPM_REGISTRY_URL%/}/esbuild"
    _NPM_REACHABLE=1
    info "Checking npm registry reachability (5s preflight): ${_NPM_PROBE_URL}"
    if command -v curl >/dev/null 2>&1; then
        # -f → non-zero exit on 4xx/5xx (a proxy 403 on package traffic).
        ${_NPM_TIMEOUT:+timeout 6} curl -fsS --max-time 5 -o /dev/null "$_NPM_PROBE_URL" \
            2>/dev/null || _NPM_REACHABLE=0
    elif command -v wget >/dev/null 2>&1; then
        # wget exits non-zero on 4xx/5xx unless --content-on-error; a GET to
        # /dev/null exercises real package traffic (not a HEAD --spider).
        ${_NPM_TIMEOUT:+timeout 6} wget -q --timeout=5 --tries=1 -O /dev/null "$_NPM_PROBE_URL" \
            2>/dev/null || _NPM_REACHABLE=0
    else
        # No probe tool — fall through to the timeout-bounded npm run.
        _NPM_REACHABLE=1
    fi
    if [[ "$_NPM_REACHABLE" -eq 0 ]]; then
        warn "npm registry unreachable (${_NPM_REGISTRY_URL}) — skipping npm; bundler falls back to the Python minifier (no impact on the app)"
        warn "To enable esbuild later: set a reachable registry (export TOFU_NPM_REGISTRY=<mirror>) and re-run, or 'cd ${INSTALL_DIR} && npm ci'"
    # One-time `npm ci` populates node_modules/ (esbuild + the typecheck
    # harness). Persists across restarts — server.py never re-runs it.
    elif [[ -f "${INSTALL_DIR}/package-lock.json" ]]; then
        info "Installing JS devDependencies (npm ci — one-time, 5-min cap)..."
        (cd "$INSTALL_DIR" && $_NPM_TIMEOUT npm ci --no-audit --no-fund) \
            && ok "JS devDependencies installed (esbuild available to the bundler)" \
            || warn "npm ci failed/timed out — bundler falls back to the Python minifier (no impact on the app)"
    else
        info "Installing esbuild (npm install — one-time, 5-min cap)..."
        (cd "$INSTALL_DIR" && $_NPM_TIMEOUT npm install --no-audit --no-fund) \
            && ok "esbuild installed" \
            || warn "npm install failed/timed out — bundler falls back to the Python minifier (no impact on the app)"
    fi
else
    step "Installing Node.js + esbuild (optional — stronger JS bundle minify)"
    warn "Node.js install skipped/failed — JS bundle uses the dependency-free Python minifier (fine; the app is unaffected)"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8: Playwright — Chromium browser + shared libs (rootless)
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
    step "Installing Playwright Chromium"

    # On Linux, install Chromium's shared libs from conda-forge so that no
    # sudo / system packages are required. tofu_search/fetch/playwright_pool.py
    # auto-prepends $CONDA_PREFIX/lib to LD_LIBRARY_PATH at runtime.
    if [[ "$OS" == "Linux" ]]; then
        info "Installing Chromium shared-lib deps from conda-forge (rootless)..."
        CHROMIUM_LIBS=(
            atk-1.0
            at-spi2-atk
            at-spi2-core
            alsa-lib
            xorg-libxcomposite
            xorg-libxdamage
            xorg-libxfixes
            xorg-libxrandr
            libxkbcommon
            nspr
            nss
            mesa-libgbm-cos7-x86_64
        )
        if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y "${CHROMIUM_LIBS[@]}"; then
            warn "Some Chromium shared-lib deps failed to install — browser may not launch"
            info "You can retry manually: conda install -n ${ENV_NAME} -c conda-forge <packages>"
        else
            ok "Chromium shared libs installed into conda env"
        fi
    fi

    # Self-heal: the Chromium download below runs `python -m playwright`, which
    # needs the `playwright` pip package importable. If the earlier pip step
    # failed/was skipped, this would die with "No module named 'playwright'"
    # and leave JS-rendered fetching silently disabled. Reinstall it first.
    if ! python -c "import playwright" 2>/dev/null; then
        warn "playwright module not importable — reinstalling it before Chromium download"
        if _safe_pip_install --upgrade "playwright>=1.40"; then
            ok "playwright pip package installed"
        else
            warn "Could not install the playwright pip package — Chromium download will be skipped"
        fi
    fi

    if ! python -c "import playwright" 2>/dev/null; then
        warn "playwright still not importable — skipping Chromium download (fetching still works via requests)"
        warn "Manual recovery: conda activate ${ENV_NAME} && pip install 'playwright>=1.40' && python -m playwright install chromium"
    else
        info "Downloading Chromium browser binary via playwright..."
        if python -m playwright install chromium; then
            ok "Playwright Chromium installed"
        else
            warn "Playwright Chromium install failed (non-critical — fetching still works via requests)"
        fi
    fi
else
    info "Skipping Playwright (--skip-playwright)"
fi

fi  # ── end legacy conda path ($_FAST_PATH_DONE != 1) ──

# ═══════════════════════════════════════════════════════════════
#  Step 8.5: Validate data/pgdata/ matches installed PG major
#
#  Catches: "unrecognized configuration parameter 'autovacuum_worker_slots'"
#  (PG 18 data dir running under PG 17 binary) and similar version skews
#  that make the scheduler spin forever on "connection refused".
#
#  Policy:
#    - No pgdata/ yet           → nothing to check, PG bootstrap will initdb later.
#    - pgdata major == installed major → OK, reuse.
#    - mismatch + --reinit-pgdata     → back up pgdata, let PG bootstrap re-initdb.
#    - mismatch without --reinit-pgdata → pin TOFU_DB_BACKEND=sqlite (data preserved).
#    - pgdata exists but no PG installed locally → pin TOFU_DB_BACKEND=sqlite.
# ═══════════════════════════════════════════════════════════════
step "Validating PostgreSQL data directory (version compatibility)"

# Resolve the pgdata path the SAME way the runtime does (lib/database/db_paths.py):
# on a network/FUSE mount the LIVE cluster is redirected to local disk
# ($TOFU_DB_LOCAL_ROOT/pgdata, default /tmp/tofu/pgdata); on a vanilla local box
# it stays at <data>/pgdata (byte-identical). Querying the resolver — instead of
# hardcoding data/pgdata — is what keeps install.sh and server.py from EVER
# disagreeing on where the cluster lives (the exact bug that made PG silently
# fall back to SQLite). Run under TOFU_DB_BACKEND=sqlite so merely importing the
# DB layer to ASK the question can never auto-start PG as a side-effect.
PGDATA_SPLIT="0"
PGDATA_LEGACY="${INSTALL_DIR}/data/pgdata"
_PGDATA_INFO="$(cd "$INSTALL_DIR" && TOFU_DB_BACKEND=sqlite "$ENV_PYTHON" - <<'PYEOF' 2>/dev/null
import sys
try:
    from lib.runtime_paths import data_root
    from lib.database.db_paths import (
        resolve_pgdata_dir, legacy_pgdata_dir, local_data_split_enabled)
    dr = data_root()
    print(resolve_pgdata_dir(dr))
    print(legacy_pgdata_dir(dr))
    print('1' if local_data_split_enabled(dr) else '0')
except Exception:
    sys.exit(1)
PYEOF
)"
if [[ -n "$_PGDATA_INFO" ]]; then
    PGDATA_DIR="$(sed -n '1p' <<<"$_PGDATA_INFO")"
    PGDATA_LEGACY="$(sed -n '2p' <<<"$_PGDATA_INFO")"
    PGDATA_SPLIT="$(sed -n '3p' <<<"$_PGDATA_INFO")"
else
    # Resolver query failed (unexpected post-install) — degrade to the historical
    # in-tree path so the rest of the step still runs.
    warn "Could not query the runtime pgdata resolver \u2014 falling back to data/pgdata"
    PGDATA_DIR="${INSTALL_DIR}/data/pgdata"
fi

if [[ "$PGDATA_DIR" != "$PGDATA_LEGACY" ]]; then
    info "Runtime pgdata resolves to ${PGDATA_DIR}"
    info "(local-disk split engaged; the legacy in-tree path would be ${PGDATA_LEGACY})"
fi
# /tmp is a common but VOLATILE default for the local-disk split — warn so the
# user doesn't assume the cluster lives on persistent storage.
if [[ "$PGDATA_DIR" == /tmp/* ]]; then
    warn "Resolved pgdata is under /tmp (${PGDATA_DIR})."
    warn "If this /tmp is cleared on reboot, the live PostgreSQL cluster will NOT persist."
    warn "Set TOFU_DB_LOCAL_ROOT to a persistent local volume to keep DB data across reboots."
fi

PGDATA_MAJOR=""
if [[ -f "${PGDATA_DIR}/PG_VERSION" ]]; then
    PGDATA_MAJOR="$(tr -d '[:space:]' < "${PGDATA_DIR}/PG_VERSION" | cut -d. -f1)"
    info "Found existing pgdata (PG ${PGDATA_MAJOR})"
fi

# Default: whatever we installed wins.
DB_BACKEND_CHOICE=""   # empty = auto (let server.py decide), 'sqlite' = force

if [[ "$FORCE_SQLITE" -eq 1 ]]; then
    DB_BACKEND_CHOICE="sqlite"
    if [[ -n "$PGDATA_MAJOR" ]]; then
        info "--force-sqlite: leaving pgdata in place but using SQLite"
    fi
elif [[ -z "$PG_INSTALLED_MAJOR" ]]; then
    # PG never got installed
    if [[ -n "$PGDATA_MAJOR" ]]; then
        warn "pgdata exists (PG ${PGDATA_MAJOR}) but no PG binaries installed in env"
        warn "Would cause scheduler/db retry storms \u2014 pinning TOFU_DB_BACKEND=sqlite"
        warn "Your existing PostgreSQL data is NOT lost, just unused. To re-enable it,"
        warn "re-run the installer with --with-postgres (installs PG ${PGDATA_MAJOR} and reuses this pgdata)."
    else
        info "No PG installed \u2014 tofu will use SQLite"
    fi
    DB_BACKEND_CHOICE="sqlite"
elif [[ -n "$PGDATA_MAJOR" && "$PGDATA_MAJOR" != "$PG_INSTALLED_MAJOR" ]]; then
    warn "pgdata major (${PGDATA_MAJOR}) differs from installed PG (${PG_INSTALLED_MAJOR})"
    warn "Running pgdata under a mismatched major will cause FATAL config-param errors"
    if [[ "$REINIT_PGDATA" -eq 1 ]]; then
        _BAK="${PGDATA_DIR}.bak.$(date +%Y%m%d_%H%M%S)"
        info "--reinit-pgdata: backing up existing pgdata \u2192 ${_BAK}"
        mv "$PGDATA_DIR" "$_BAK"
        ok "pgdata moved aside; PG bootstrap will initdb fresh under PG ${PG_INSTALLED_MAJOR}"
        # Also nuke the SQLite db if we want a totally clean slate? No \u2014
        # SQLite is independent, leave it alone.
    else
        warn "Re-run with --reinit-pgdata to auto-initdb (existing PG data will be backed up)"
        warn "For now, pinning TOFU_DB_BACKEND=sqlite so scheduler doesn't spin"
        DB_BACKEND_CHOICE="sqlite"
    fi
elif [[ -n "$PGDATA_MAJOR" ]]; then
    ok "pgdata (PG ${PGDATA_MAJOR}) matches installed PG (${PG_INSTALLED_MAJOR}) \u2014 reusing"
else
    ok "PG ${PG_INSTALLED_MAJOR} ready; bootstrap will initdb on first server.py run"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8.6: Smoke-test PG startup (best-effort, don't block install)
#
#  If we chose to use PG, try `pg_ctl start` once under a timeout so
#  config-file errors surface NOW instead of during first /api call.
# ═══════════════════════════════════════════════════════════════
# ── PG recovery helpers (borrowed/user-owned env with a broken PostgreSQL) ──
# Step 7 already (re)installs postgresql into the env, but a corrupt shared dep
# (icu / libpq) can survive a plain remove+install because conda deems it
# "satisfied" and never re-fetches it — so initdb/pg_ctl can still hang or fail
# on a borrowed env (owned_by_tofu_install=false). When the bootstrap/smoke-test
# below fails on such an env we try ONE --force-reinstall of the PG stack (which
# re-fetches even satisfied builds) and retry; if it STILL fails the reliable
# remedy is a clean Tofu-owned env, which _pg_broken_env_advice prints. We only
# touch PG packages here (never the base conda), consistent with Step 7 which
# already installs postgresql into the same env.
_pg_force_reinstall() {
    warn "Recovery: force-reinstalling PostgreSQL stack in env '${ENV_NAME}' (postgresql=${PG_INSTALLED_MAJOR} + libpq + icu)"
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
        --force-reinstall "postgresql=${PG_INSTALLED_MAJOR}" libpq icu 'psycopg2>=2.9'
}

_pg_broken_env_advice() {
    warn ""
    if [[ "$CONDA_OWNED_BY_US" -eq 0 ]]; then
        warn "The conda env '${ENV_NAME}' is a pre-existing / borrowed conda"
        warn "(owned_by_tofu_install=false) and its PostgreSQL build appears broken"
        warn "(initdb/pg_ctl failed even after a force-reinstall)."
        warn "For a reliable PG cluster, re-run with a clean Tofu-owned env:"
        warn "    bash install.sh --force-sibling-conda --reset-env"
        warn "or repair PG in the current env yourself:"
        warn "    conda install -n ${ENV_NAME} -c conda-forge --force-reinstall postgresql=${PG_INSTALLED_MAJOR} libpq icu"
    fi
    warn ""
}

# Delegated runtime bootstrap (initdb + start-verify) — factored into a function
# so the borrowed-env recovery path can retry it after a force-reinstall. Reads
# the global $_PG_BOOTSTRAP_TIMEOUT wrapper set by the caller. Returns the
# delegate's rc (124/137 = outer hard-timeout fired).
_run_pg_bootstrap_delegate() {
    local _rc=0
    (cd "$INSTALL_DIR" && $_PG_BOOTSTRAP_TIMEOUT "$ENV_PYTHON" - <<'PYEOF'
import sys
from lib.runtime_paths import data_root
from lib.database.db_paths import resolve_pgdata_dir
from lib.database._core import (
    BASE_DIR, PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME)
from lib.database._bootstrap import _ensure_pg_running, _stop_local_pg_quietly
pgdata = resolve_pgdata_dir(data_root())
res = _ensure_pg_running(pgdata, BASE_DIR, PG_HOST, PG_PORT,
                         PG_USER, PG_PASSWORD, PG_DBNAME)
if not res:
    print('ensure_pg_running returned no result', file=sys.stderr)
    sys.exit(1)
# Leave the cluster the way the server expects to find it on first boot.
try:
    _stop_local_pg_quietly(pgdata)
except Exception as e:
    print(f'(non-fatal) stop after bootstrap failed: {e}', file=sys.stderr)
print(f"OK pgdata={pgdata} port={res.get('PG_PORT')}")
PYEOF
    ) || _rc=$?
    return $_rc
}

# pg_ctl smoke-test of an EXISTING cluster — factored so the recovery path can
# retry it after a force-reinstall. Returns 0 if PG started (then stopped) OK.
_run_pg_ctl_smoke() {
    local _pgctl="${CONDA_BASE}/envs/${ENV_NAME}/bin/pg_ctl"
    local _logdir="${INSTALL_DIR}/logs"
    mkdir -p "$_logdir"
    "$_pgctl" -D "$PGDATA_DIR" stop -m fast >/dev/null 2>&1 || true
    rm -f "${PGDATA_DIR}/postmaster.pid" 2>/dev/null || true
    if "$_pgctl" -D "$PGDATA_DIR" -l "${_logdir}/postgresql.log" -w -t 15 start >/dev/null 2>&1; then
        "$_pgctl" -D "$PGDATA_DIR" stop -m fast >/dev/null 2>&1 || true
        return 0
    fi
    return 1
}

if [[ -z "$DB_BACKEND_CHOICE" && -n "$PG_INSTALLED_MAJOR" && "$PGDATA_SPLIT" == "1" && ! -d "$PGDATA_DIR" ]]; then
    # ── Split engaged + resolved cluster not yet created ──
    # The bash pg_ctl smoke-test below can only START an EXISTING cluster; it
    # cannot fulfil the "bootstrap will initdb on first server.py run" promise
    # for the local-disk path. So DELEGATE the real initdb + start-verify to the
    # runtime's own bootstrap (_ensure_pg_running) against the RESOLVED path —
    # install.sh and server.py then run the identical code, so a config-file
    # error surfaces NOW instead of at the first API call. This is exactly what
    # the runtime does on first boot; doing it here just moves it earlier.
    step "Bootstrapping PostgreSQL at ${PGDATA_DIR} (initdb via runtime)"
    # Portable hard-timeout wrapper for the delegated bootstrap: GNU `timeout`,
    # macOS `gtimeout`, else none. Mirrors the npm-install wrapper above (~L1428).
    # The runtime bootstrap it delegates to is ALREADY internally bounded
    # (initdb 60s / pg_ctl 30s / createdb 15s ≈ 2min worst case), so this outer
    # ceiling is pure defense-in-depth: subprocess's SIGKILL cannot reap a
    # D-state (uninterruptible-sleep) process wedged on a hung FUSE mount, and
    # without an outer wrapper such a process would hang the whole installer.
    # `-k` escalates TERM→KILL; 300s is well above the internal budget so a
    # slow-but-progressing initdb is never killed prematurely.
    _PG_BOOTSTRAP_TIMEOUT=""
    if command -v timeout >/dev/null 2>&1; then
        _PG_BOOTSTRAP_TIMEOUT="timeout -k 10 300"
    elif command -v gtimeout >/dev/null 2>&1; then
        _PG_BOOTSTRAP_TIMEOUT="gtimeout -k 10 300"
    fi
    _pg_boot_rc=0
    _run_pg_bootstrap_delegate || _pg_boot_rc=$?
    # If a borrowed env's corrupt PG stack made the delegate fail (but NOT a
    # FUSE-wedge hard-timeout — force-reinstall can't fix a hung mount), try one
    # force-reinstall of the PG stack and retry the delegate once.
    if [[ "$_pg_boot_rc" -ne 0 && "$_pg_boot_rc" -ne 124 && "$_pg_boot_rc" -ne 137 ]]; then
        warn "Runtime PG bootstrap failed (rc=${_pg_boot_rc}) \u2014 attempting PG stack recovery"
        if _pg_force_reinstall; then
            _pg_boot_rc=0
            _run_pg_bootstrap_delegate || _pg_boot_rc=$?
        else
            warn "PG force-reinstall itself failed"
        fi
    fi
    if [[ "$_pg_boot_rc" -eq 0 ]]; then
        ok "PostgreSQL cluster initialized + start-verified at ${PGDATA_DIR}"
    else
        if [[ "$_pg_boot_rc" -eq 124 || "$_pg_boot_rc" -eq 137 ]]; then
            warn "Runtime PG bootstrap exceeded the 300s hard timeout (possible wedged FUSE mount) \u2014 aborted"
        else
            warn "Runtime PG bootstrap failed even after force-reinstall \u2014 see ${INSTALL_DIR}/logs/postgresql.log"
            _pg_broken_env_advice
        fi
        warn "Pinning TOFU_DB_BACKEND=sqlite to avoid scheduler retry storms"
        DB_BACKEND_CHOICE="sqlite"
    fi
elif [[ -z "$DB_BACKEND_CHOICE" && -n "$PG_INSTALLED_MAJOR" && -d "$PGDATA_DIR" ]]; then
    step "Smoke-testing PostgreSQL startup"
    if _run_pg_ctl_smoke; then
        ok "PostgreSQL started successfully (smoke test)"
    else
        # A borrowed env's corrupt PG binary can fail to start an otherwise-valid
        # cluster. Try one force-reinstall of the PG stack and re-smoke-test.
        warn "PG failed to start during smoke test \u2014 attempting PG stack recovery"
        if _pg_force_reinstall && _run_pg_ctl_smoke; then
            ok "PostgreSQL started successfully after force-reinstall"
        else
            warn "PG still fails to start after force-reinstall \u2014 see ${INSTALL_DIR}/logs/postgresql.log"
            _pg_broken_env_advice
            warn "Pinning TOFU_DB_BACKEND=sqlite to avoid scheduler retry storms"
            warn "Re-run with --reinit-pgdata after moving ${PGDATA_DIR} aside if you want fresh PG"
            DB_BACKEND_CHOICE="sqlite"
        fi
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 9: Configure .env
# ═══════════════════════════════════════════════════════════════
step "Configuring .env"

ENV_FILE="${INSTALL_DIR}/.env"
ENV_EXAMPLE="${INSTALL_DIR}/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        info "Created .env from template"
    else
        cat > "$ENV_FILE" <<EOF
PORT=${PORT}
BIND_HOST=0.0.0.0
EOF
        info "Created minimal .env"
    fi
fi

# Update/insert a key in .env
_set_env_var() {
    local key="$1" value="$2" file="$3"
    if grep -qE "^[#[:space:]]*${key}=" "$file" 2>/dev/null; then
        # Portable sed -i (macOS requires a backup ext)
        if [[ "$OS" == "Darwin" ]]; then
            sed -i '' -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$file"
        else
            sed -i -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$file"
        fi
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

_set_env_var "PORT" "$PORT" "$ENV_FILE"
if [[ -n "$API_KEY" ]]; then
    _set_env_var "LLM_API_KEYS" "$API_KEY" "$ENV_FILE"
    ok "API key configured"
fi

# Write DB backend decision into .env so server.py knows exactly which
# backend to use (no silent PG-then-fallback retry storms at startup).
if [[ "$DB_BACKEND_CHOICE" == "sqlite" ]]; then
    _set_env_var "TOFU_DB_BACKEND" "sqlite" "$ENV_FILE"
    info "TOFU_DB_BACKEND=sqlite pinned in .env"
elif [[ -n "$PG_INSTALLED_MAJOR" ]]; then
    _set_env_var "TOFU_DB_BACKEND" "postgres" "$ENV_FILE"
    info "TOFU_DB_BACKEND=postgres pinned in .env (PG ${PG_INSTALLED_MAJOR})"
fi

ok ".env ready (PORT=${PORT})"


# ═══════════════════════════════════════════════════════════════
#  Step 9.5: Post-install DB smoke test (create → insert → read → delete)
#
#  Prove the SELECTED backend actually works on THIS machine before we
#  declare success — a create/insert/read-back/delete/drop round-trip via
#  the same interpreter and the same resolved DB target the server will use.
#  Runs for BOTH the uv and conda paths (this is the shared tail; the conda
#  guard closed back before Step 8.5). Failure ABORTS the install (fail) with
#  a backend-specific hint. The temp table is dropped in a finally so no
#  _tofu_install_smoke residue is left in the user's real DB.
# ═══════════════════════════════════════════════════════════════
step "Verifying the database backend works (create → insert → read → delete)"

# Mirror the .env backend decision: sqlite unless a PG major was installed
# AND we didn't pin sqlite.
_SMOKE_BACKEND="sqlite"
[[ -z "$DB_BACKEND_CHOICE" && -n "$PG_INSTALLED_MAJOR" ]] && _SMOKE_BACKEND="postgres"

_SMOKE_TIMEOUT=""
command -v timeout >/dev/null 2>&1 && _SMOKE_TIMEOUT="timeout -k 5 60"

if (cd "$INSTALL_DIR" && TOFU_DB_BACKEND="$_SMOKE_BACKEND" $_SMOKE_TIMEOUT "$ENV_PYTHON" - <<'PYEOF'
import os, sys
backend = os.environ.get('TOFU_DB_BACKEND', 'sqlite').lower()
conn = None
created = False
try:
    if backend == 'postgres':
        import psycopg2
        from lib.database import PG_DSN
        conn = psycopg2.connect(PG_DSN)
        ph = '%s'
    else:
        import sqlite3
        from lib.database import DB_PATH
        # The server makedirs the data dir at boot; do the same here so a
        # first-ever install has somewhere to put the file. An UNWRITABLE
        # path still raises (→ caught below → exit 1), so this does not mask
        # the permission-failure case.
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or '.', exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        ph = '?'
    cur = conn.cursor()
    # Single TEXT column: `INTEGER PRIMARY KEY` autofills on SQLite but is a
    # NULL-PK error on PG, so keep it backend-neutral.
    cur.execute('CREATE TABLE IF NOT EXISTS _tofu_install_smoke (v TEXT)')
    created = True
    cur.execute('INSERT INTO _tofu_install_smoke (v) VALUES (' + ph + ')', ('ok',))
    conn.commit()
    cur.execute('SELECT v FROM _tofu_install_smoke LIMIT 1')
    row = cur.fetchone()
    assert row is not None and row[0] == 'ok', 'read-back mismatch'
    cur.execute('DELETE FROM _tofu_install_smoke')
    conn.commit()
    print('  DB smoke OK (backend=%s): create/insert/read/delete round-trip passed' % backend)
except Exception as e:
    sys.stderr.write('  DB smoke FAILED (backend=%s): %s\n' % (backend, e))
    sys.exit(1)
finally:
    # Always drop the temp table so no _tofu_install_smoke residue survives,
    # even if the round-trip raised midway.
    if conn is not None:
        try:
            if created:
                cur2 = conn.cursor()
                cur2.execute('DROP TABLE IF EXISTS _tofu_install_smoke')
                conn.commit()
        except Exception as _drop_err:
            sys.stderr.write('  (warning) could not drop smoke table: %s\n' % _drop_err)
        try:
            conn.close()
        except Exception:
            pass
PYEOF
); then
    ok "Database backend verified (${_SMOKE_BACKEND}): create/insert/read/delete round-trip passed"
else
    if [[ "$_SMOKE_BACKEND" == "sqlite" ]]; then
        fail "SQLite backend failed its post-install smoke test — check disk space / write permissions on ${INSTALL_DIR}/data, or re-run with --with-postgres to use PostgreSQL. Full log: ${TOFU_INSTALL_LOG}"
    else
        fail "PostgreSQL backend failed its post-install smoke test — see ${INSTALL_DIR}/logs/postgresql.log, or re-run with --force-sqlite to use SQLite. Full log: ${TOFU_INSTALL_LOG}"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 10: Launch or print completion
# ═══════════════════════════════════════════════════════════════
echo ""
ok "Installation complete!"
echo ""
echo "  To start Tofu later, any of these work (.tofu_env.json auto-activates):"
echo "    cd ${INSTALL_DIR} && python server.py"
if [[ "$_FAST_PATH_DONE" -eq 1 ]]; then
    echo ""
    echo "  (Optional, to explicitly activate the uv venv — not required thanks to .tofu_env.json:)"
    echo "    source \"${ENV_PREFIX}/bin/activate\""
elif [[ "$CONDA_OWNED_BY_US" -eq 1 ]]; then
    echo ""
    echo "  (Optional, if you want the env on your PATH for other tools too:)"
    echo "    source \"${CONDA_BASE}/etc/profile.d/conda.sh\" && conda activate ${ENV_NAME}"
else
    echo ""
    echo "  (Optional, to explicitly activate — not required thanks to .tofu_env.json:)"
    echo "    conda activate ${ENV_NAME}"
fi
echo ""
info "Full install log: $TOFU_INSTALL_LOG"
echo ""

if [[ "$NO_LAUNCH" -eq 1 ]]; then
    info "Install-only mode — not launching server."
    exit 0
fi

step "Starting Tofu server"
echo ""
echo -e "  ${BOLD}🧈 Tofu is starting on port ${PORT}...${NC}"
echo -e "  Open ${BOLD}http://localhost:${PORT}${NC} in your browser"
echo ""
echo "  Press Ctrl+C to stop the server"
echo ""

cd "$INSTALL_DIR"
exec python server.py
