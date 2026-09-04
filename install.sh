#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — One-Command Installer (Linux / macOS)
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
#
#  With options:
#    curl -fsSL ... | bash -s -- --port 8080 --api-key-file /secure/key-file
#
#  Options:
#    --dir <path>          Install directory (default: ~/tofu)
#    --env <name>          Conda env name (default: tofu)
#    --port <n>            Server port (default: 15000)
#    --api-key-file <path> Read one LLM API key from a local secret file
#    --api-key <key>       Legacy compatibility; exposes the key in argv/history
#    --no-launch           Install only, don't start
#    --skip-playwright     Skip Playwright browser install
#    --skip-node           Legacy no-op. Release installs always consume the
#                          verified prebuilt frontend and never install Node.
#    --no-update-conda     Skip conda self-update (only relevant when we
#                          install our OWN sibling Miniforge — we never
#                          touch a pre-existing conda the user owns)
#    --reset-env           Recreate the selected environment from scratch
#                          (⚠️  DESTRUCTIVE: uv requires installer ownership
#                           proof; conda removes the named env.)
#    --use-conda           Force the legacy conda install path, skipping the
#                          default uv fast path. Use on very old systems
#                          (glibc < 2.28) if auto-detection misfires, or when
#                          you specifically want the conda-forge toolchain.
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
#  Default backend (uv): `uv venv` + `uv pip install -r requirements.txt`
#  from prebuilt wheels — `uv pip` covers every requirement, including the few
#  packages the legacy path installs via pip (PIP_ONLY_PKGS). The conda-forge
#  path below is the FALLBACK / legacy path, forced by --use-conda, very old
#  glibc, or a failed uv install. It:
#    1. Locates an acceptable conda OR installs a sibling Miniforge
#    2. (Sibling installs only) updates conda itself for solver fixes
#    3. Clones the repo if needed (conda can supply git)
#    4. Creates a fresh conda env with Python 3.12
#    5. Installs Python dependencies from conda-forge (a few via pip)
#    6. Installs ripgrep, fd-find, and Chromium shared libs from conda-forge
#    7. Installs the Playwright Chromium browser binary
#    8. Writes .tofu_env.json marker so server.py/bootstrap.py auto-activate
#    9. Joins the personal-mode configure/verify/launch tail
#
#  BOTH paths (shared tail, Step 8.45) also install the modern text tools
#  sd / goawk / miller into the env's bin/ — a standard part of every
#  install, since the model-facing run_command guidance relies on them
#  being on PATH (server.py/bootstrap.py prepend env bin on every boot).
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

# Keep terminals readable while producing plain CI/model logs. NO_COLOR is the
# cross-tool opt-out; non-interactive captures should never need ANSI stripping.
if [[ ! -t 1 || -n "${NO_COLOR+x}" ]]; then
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    BOLD=''
    NC=''
fi

info()  { echo -e "  ${CYAN}ℹ${NC}  $*"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
fail()  { echo -e "  ${RED}✗${NC}  $*"; exit 1; }
step()  { echo ""; echo -e "  ${BOLD}${CYAN}▸${NC}  ${BOLD}$*${NC}"; }

# ── Defaults ────────────────────────────────────────────────
INSTALL_DIR=""
if [[ -n "${HOME:-}" ]]; then
    INSTALL_DIR="${HOME}/tofu"
fi
DIR_EXPLICIT=0
ENV_NAME="tofu"
ENV_EXPLICIT=0
PY_VER="3.12"
PYTHON_EXPLICIT=0
PORT_FROM_ENV=0
if [[ -n "${PORT:-}" ]]; then
    PORT_FROM_ENV=1
else
    PORT="15000"
fi
PORT_EXPLICIT=0
API_KEY=""
API_KEY_FILE=""
API_KEY_SOURCE="not-configured"
NO_LAUNCH=0
SKIP_PLAYWRIGHT=0
SKIP_NODE=0
NO_UPDATE_CONDA=0
RESET_ENV=0
USE_CONDA=0        # 1 = force the legacy conda path, skip the uv fast path
MIN_CONDA_MAJOR=24          # minimum acceptable major version of an existing conda
FORCE_SIBLING_CONDA=0       # 1 = always install our own sibling Miniforge
WITH_DOCLING=0              # 1 = also install the optional `docling` package

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Install Tofu into a self-contained uv environment (with automatic conda
fallback on older systems), configure it, start it, and verify it is usable.

Common options:
  --dir PATH              Install directory (default: ~/tofu)
  --port N                Server port, 1-65535 (default: PORT or 15000)
  --api-key-file PATH     Read one LLM API key from a local secret file
  --no-launch             Install only; do not start or probe the server
  --use-conda             Force the conda install path
  -h, --help              Show this help without downloading or changing files

Advanced options:
  --env NAME              Conda environment name; selects conda (default: tofu)
  --python VERSION        Python version (default: 3.12)
  --skip-playwright       Skip the optional browser-engine download
  --skip-node             Legacy no-op (frontend bundles are prebuilt)
  --no-update-conda       Select conda; do not update installer-owned conda
  --reset-env             Recreate the selected env (destructive; ownership-gated)
  --min-conda N           Minimum existing conda major (default: 24)
  --force-sibling-conda   Select conda and use a private sibling Miniforge
  --with-docling          Select conda and install PDF parsing (~2 GB)
  --api-key KEY           Legacy compatibility only; leaks into argv/history

Examples:
  curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
  curl -fsSL .../install.sh | bash -s -- --port 8080 --no-launch

Troubleshooting: docs/INSTALL.md
EOF
}

usage_error() {
    echo "install.sh: $*" >&2
    echo "Try 'bash install.sh --help' for supported options." >&2
    exit 2
}

require_option_value() {
    local option="$1"
    local remaining="$2"
    local candidate="${3-}"
    if [[ "$remaining" -lt 2 || -z "$candidate" || "$candidate" == --* ]]; then
        usage_error "${option} requires a value"
    fi
}

# ── Parse arguments ─────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)          usage; exit 0 ;;
        --dir)              require_option_value "$1" "$#" "${2-}"; INSTALL_DIR="$2"; DIR_EXPLICIT=1; shift 2 ;;
        --dir=*)            INSTALL_DIR="${1#*=}"; [[ -n "$INSTALL_DIR" ]] || usage_error "--dir requires a value"; DIR_EXPLICIT=1; shift ;;
        --env)              require_option_value "$1" "$#" "${2-}"; ENV_NAME="$2"; ENV_EXPLICIT=1; USE_CONDA=1; shift 2 ;;
        --env=*)            ENV_NAME="${1#*=}"; [[ -n "$ENV_NAME" ]] || usage_error "--env requires a value"; ENV_EXPLICIT=1; USE_CONDA=1; shift ;;
        --python)           require_option_value "$1" "$#" "${2-}"; PY_VER="$2"; PYTHON_EXPLICIT=1; shift 2 ;;
        --python=*)         PY_VER="${1#*=}"; [[ -n "$PY_VER" ]] || usage_error "--python requires a value"; PYTHON_EXPLICIT=1; shift ;;
        --port)             require_option_value "$1" "$#" "${2-}"; PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
        --port=*)           PORT="${1#*=}"; [[ -n "$PORT" ]] || usage_error "--port requires a value"; PORT_EXPLICIT=1; shift ;;
        --api-key)          require_option_value "$1" "$#" "${2-}"; API_KEY="$2"; API_KEY_SOURCE="command-line"; shift 2 ;;
        --api-key=*)        API_KEY="${1#*=}"; [[ -n "$API_KEY" ]] || usage_error "--api-key requires a value"; API_KEY_SOURCE="command-line"; shift ;;
        --api-key-file)     require_option_value "$1" "$#" "${2-}"; API_KEY_FILE="$2"; shift 2 ;;
        --api-key-file=*)   API_KEY_FILE="${1#*=}"; [[ -n "$API_KEY_FILE" ]] || usage_error "--api-key-file requires a value"; shift ;;
        --no-launch)        NO_LAUNCH=1; shift ;;
        --skip-playwright)  SKIP_PLAYWRIGHT=1; shift ;;
        --skip-node)        SKIP_NODE=1; shift ;;
        --no-update-conda)  NO_UPDATE_CONDA=1; USE_CONDA=1; shift ;;
        --reset-env)        RESET_ENV=1; shift ;;
        --force-sqlite|--with-postgres|--pg-major|--reinit-pgdata)
            usage_error "$1 was removed; install.sh supports personal SQLite only; deploy distributed PostgreSQL with Kubernetes" ;;
        --pg-major=*)
            usage_error "--pg-major was removed; PostgreSQL is externally managed" ;;
        --use-conda)        USE_CONDA=1; shift ;;
        --min-conda)        require_option_value "$1" "$#" "${2-}"; MIN_CONDA_MAJOR="$2"; USE_CONDA=1; shift 2 ;;
        --min-conda=*)      MIN_CONDA_MAJOR="${1#*=}"; [[ -n "$MIN_CONDA_MAJOR" ]] || usage_error "--min-conda requires a value"; USE_CONDA=1; shift ;;
        --force-sibling-conda) FORCE_SIBLING_CONDA=1; USE_CONDA=1; shift ;;
        --with-docling)     WITH_DOCLING=1; USE_CONDA=1; shift ;;
        *)                  usage_error "unknown option: $1" ;;
    esac
done

if [[ -z "${HOME:-}" ]]; then
    usage_error "HOME is not set; set HOME to a writable directory before installing"
fi
if [[ -n "$API_KEY" && -n "$API_KEY_FILE" ]]; then
    usage_error "--api-key and --api-key-file cannot be combined"
fi
if [[ -n "$API_KEY_FILE" ]]; then
    [[ -f "$API_KEY_FILE" && -r "$API_KEY_FILE" ]] \
        || usage_error "--api-key-file must name a readable regular file (got: ${API_KEY_FILE})"
    _API_KEY_FILE_BYTES="$(wc -c < "$API_KEY_FILE" 2>/dev/null)" \
        || usage_error "could not read --api-key-file: ${API_KEY_FILE}"
    [[ "$_API_KEY_FILE_BYTES" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] \
        || usage_error "could not measure --api-key-file: ${API_KEY_FILE}"
    (( _API_KEY_FILE_BYTES <= 8192 )) \
        || usage_error "--api-key-file must be 8192 bytes or smaller"
    API_KEY="$(< "$API_KEY_FILE")"
    # Command substitution removes LF but retains the CR from a normal Windows
    # CRLF line ending. Accept that one terminator without accepting embedded
    # newlines or carriage returns in the credential itself.
    API_KEY="${API_KEY%$'\r'}"
    [[ -n "$API_KEY" ]] || usage_error "--api-key-file is empty: ${API_KEY_FILE}"
    API_KEY_SOURCE="file"
fi
if [[ "$API_KEY" == *$'\n'* || "$API_KEY" == *$'\r'* || ${#API_KEY} -gt 8192 ]]; then
    usage_error "LLM API key must be one non-empty line no longer than 8192 characters"
fi
if [[ -n "$API_KEY" ]]; then
    case "$API_KEY" in
        [[:space:]]*|*[[:space:]]|\'*|*\'|\"*|*\")
            usage_error "LLM API key cannot start/end with whitespace or quotes (it would change when .env is parsed)"
            ;;
    esac
    [[ "$API_KEY" != *,* ]] \
        || usage_error "--api-key-file must contain exactly one key; commas delimit multiple LLM_API_KEYS"
fi

if [[ ! "$PORT" =~ ^[0-9]+$ || ${#PORT} -gt 5 ]] \
        || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
    usage_error "--port must be an integer from 1 to 65535 (got: ${PORT})"
fi
if [[ ! "$MIN_CONDA_MAJOR" =~ ^[0-9]+$ || ${#MIN_CONDA_MAJOR} -gt 3 ]] \
        || (( 10#$MIN_CONDA_MAJOR < 1 )); then
    usage_error "--min-conda must be a positive integer (got: ${MIN_CONDA_MAJOR})"
fi
if [[ ! "$ENV_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    usage_error "--env must use 1-128 letters, numbers, dots, underscores, or hyphens"
fi
if [[ ! "$PY_VER" =~ ^[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]{1,3})?$ ]]; then
    usage_error "--python must be a concrete version such as 3.12"
fi
_PYTHON_MAJOR="${PY_VER%%.*}"
_PYTHON_REMAINDER="${PY_VER#*.}"
_PYTHON_MINOR="${_PYTHON_REMAINDER%%.*}"
if (( 10#$_PYTHON_MAJOR != 3 || 10#$_PYTHON_MINOR < 12 )); then
    usage_error "--python must select Python 3.12 or newer within the 3.x series"
fi
# ── Banner ──────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}🧈 Tofu (豆腐) — Self-Hosted AI Assistant${NC}"
echo -e "  ─────────────────────────────────────────"
echo -e "  uv installer with automatic conda fallback"
echo ""

# ── Tee ALL output (stdout + stderr) into a log file ──
# Everything printed from this point onward ends up in
# <INSTALL_DIR>/logs/install-YYYYMMDD_HHMMSS-PID.log — makes it easy to
# attach the full transcript when reporting an issue.
#
# Never create logs inside an empty --dir target before `git clone`: doing so
# makes that directory non-empty and causes Git to reject an otherwise valid
# install destination. A new checkout stages its log beside the target, then
# hard-links the still-open file into <INSTALL_DIR>/logs after source exists.
_TOFU_INSTALL_LOG_BASENAME="install-$(date +%Y%m%d_%H%M%S)-${BASHPID}.log"
if [[ -f "${INSTALL_DIR}/server.py" ]]; then
    _TOFU_LOG_DIR="${INSTALL_DIR}/logs"
    mkdir -p "$_TOFU_LOG_DIR" 2>/dev/null || _TOFU_LOG_DIR="/tmp"
    TOFU_INSTALL_LOG="${_TOFU_LOG_DIR}/${_TOFU_INSTALL_LOG_BASENAME}"
elif [[ -f "server.py" ]]; then
    _TOFU_LOG_DIR="$(pwd)/logs"
    mkdir -p "$_TOFU_LOG_DIR" 2>/dev/null || _TOFU_LOG_DIR="/tmp"
    TOFU_INSTALL_LOG="${_TOFU_LOG_DIR}/${_TOFU_INSTALL_LOG_BASENAME}"
else
    _TOFU_LOG_PARENT="$(dirname "$INSTALL_DIR")"
    if mkdir -p "$_TOFU_LOG_PARENT" 2>/dev/null && [[ -w "$_TOFU_LOG_PARENT" ]]; then
        TOFU_INSTALL_LOG="${_TOFU_LOG_PARENT}/.${_TOFU_INSTALL_LOG_BASENAME}.pending"
    else
        TOFU_INSTALL_LOG="/tmp/${_TOFU_INSTALL_LOG_BASENAME}"
    fi
fi
# Installer output can contain host paths and third-party tool diagnostics.
# Pre-create it privately before process-substitution writers open it.
(umask 077; set -o noclobber; : > "$TOFU_INSTALL_LOG") \
    || { echo "install.sh: cannot create private log ${TOFU_INSTALL_LOG}" >&2; exit 1; }
chmod 600 "$TOFU_INSTALL_LOG" \
    || { echo "install.sh: cannot protect log ${TOFU_INSTALL_LOG}" >&2; exit 1; }
# Use `tee` via process substitution so the terminal keeps receiving output.
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
    echo "options: dir=${INSTALL_DIR} env=${ENV_NAME} python=${PY_VER} port=${PORT}"
    echo "modes:   no_launch=${NO_LAUNCH} use_conda=${USE_CONDA} deployment=personal"
    echo "api key: ${API_KEY_SOURCE} (value redacted)"
    echo "pwd:     $(pwd)"
    echo "bash:    ${BASH_VERSION:-unknown}"
    echo "which conda (pre-locate): $(command -v conda 2>/dev/null || echo none)"
    echo "──────────────────────────────────────────────"
} >&2
info "Install log: $TOFU_INSTALL_LOG"
if [[ "$API_KEY_SOURCE" == "command-line" ]]; then
    warn "--api-key is visible in shell history/process lists; use --api-key-file or Settings → Providers next time"
fi

# On any non-zero exit (error, Ctrl-C, set -e trigger), remind the user
# where the log is so they can grab it for bug reports.
_tofu_exit_reminder() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "" >&2
        echo -e "  ${YELLOW}!${NC}  install.sh exited with code ${rc}" >&2
        echo -e "  ${YELLOW}!${NC}  Full transcript saved to: ${TOFU_INSTALL_LOG}" >&2
        echo -e "  ${YELLOW}!${NC}  Review the transcript for host paths or secrets before sharing it." >&2
    fi
}
trap _tofu_exit_reminder EXIT

_finalize_install_log_location() {
    local _final_dir="${INSTALL_DIR}/logs"
    local _final_log="${_final_dir}/${_TOFU_INSTALL_LOG_BASENAME}"
    [[ "$TOFU_INSTALL_LOG" == "$_final_log" ]] && return 0
    if ! mkdir -p "$_final_dir" 2>/dev/null; then
        warn "Could not create ${_final_dir}; keeping install log at ${TOFU_INSTALL_LOG}"
        return 0
    fi
    if [[ -e "$_final_log" || -L "$_final_log" ]]; then
        warn "Refusing to replace existing ${_final_log}; keeping install log at ${TOFU_INSTALL_LOG}"
        return 0
    fi
    # A hard link keeps tee/sed's already-open descriptor attached to the final
    # filename. It also fails safely rather than copying a partial live log
    # across filesystems or replacing an existing file.
    if ln "$TOFU_INSTALL_LOG" "$_final_log" 2>/dev/null; then
        if ! rm -f "$TOFU_INSTALL_LOG"; then
            warn "Install log also remains at ${TOFU_INSTALL_LOG}"
        fi
        TOFU_INSTALL_LOG="$_final_log"
        info "Install log moved to: $TOFU_INSTALL_LOG"
    else
        warn "Could not move the live install log safely; keeping it at ${TOFU_INSTALL_LOG}"
    fi
}

# ── Platform check ──────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)   PLATFORM="Linux" ;;
    Darwin)  PLATFORM="MacOSX" ;;
    *)       fail "Unsupported OS: $OS (Windows: download Tofu-Setup-*.exe from the release page)" ;;
esac
# Normalize arch spellings to the two names the Miniforge download paths use
# (Miniforge3-${PLATFORM}-${ARCH}.sh). Anything else must fail fast instead of
# surfacing late as a bogus "All Miniforge mirrors failed" during the download.
case "$ARCH" in
    amd64|x86_64)  ARCH="x86_64" ;;
    arm64|aarch64) ARCH="aarch64" ;;
    *)  fail "Unsupported architecture: ${ARCH} — install.sh supports x86_64/amd64 and aarch64/arm64 only. Use the Docker image instead (see docs/INSTALL.md)." ;;
esac
info "Platform: $OS $ARCH"

# ── Disk preflight ──────────────────────────────────────────
# ENOSPC used to surface only deep inside `uv pip install` / a conda solve.
# Check the install-dir parent's free space up front so a nearly-full disk
# fails here with a clear message. df is best-effort: if it is missing or
# unparsable we keep going (the package managers still report ENOSPC honestly).
_disk_preflight() {
    local _df_parent
    _df_parent="$(cd "$(dirname "${INSTALL_DIR}")" 2>/dev/null && pwd)"
    [[ -n "$_df_parent" ]] || _df_parent="$(dirname "${INSTALL_DIR}")"
    local _free_kb
    if ! _free_kb="$(df -Pk "$_df_parent" 2>/dev/null | awk 'NR==2 {print $4}')" \
            || [[ -z "$_free_kb" ]]; then
        warn "Could not measure free disk space on ${_df_parent} (df unavailable) — skipping disk preflight"
        return 0
    fi
    [[ "$_free_kb" =~ ^[0-9]+$ ]] || { warn "Could not parse free disk space for ${_df_parent} — skipping disk preflight"; return 0; }
    local _fail_kb=$((2 * 1024 * 1024))
    local _warn_gb=4
    local _extra=""
    [[ "$WITH_DOCLING" -eq 1 ]] && { _warn_gb=6; _extra=" with --with-docling"; }
    local _warn_kb=$((_warn_gb * 1024 * 1024))
    if (( _free_kb < _fail_kb )); then
        fail "Not enough free disk space: $((_free_kb / 1024)) MB free on ${_df_parent} (need at least 2 GB). Free space or choose another --dir."
    elif (( _free_kb < _warn_kb )); then
        warn "Low disk space: $((_free_kb / 1024)) MB free on ${_df_parent} (recommend at least ${_warn_gb} GB${_extra}). Continuing."
    else
        info "Disk space: $((_free_kb / 1024 / 1024)) GB free on ${_df_parent}"
    fi
}
_disk_preflight

# ═══════════════════════════════════════════════════════════════
#  Step 0.5: Ensure source is present (backend-agnostic)
#
#  Both the uv fast path and the conda path need requirements.txt in hand
#  BEFORE choosing a backend, so resolve INSTALL_DIR / clone here using the
#  system git. If a clone is required but git is missing, we force the conda
#  path (which can install git from conda-forge).
# ═══════════════════════════════════════════════════════════════
_prepare_source_checkout() {
    if [[ -f "${INSTALL_DIR}/server.py" ]]; then
        ok "Existing installation found at ${INSTALL_DIR}"
        if [[ -d "${INSTALL_DIR}/.git" ]] && command -v git &>/dev/null; then
            info "Updating via git pull..."
            if ! (cd "$INSTALL_DIR" && git pull --ff-only); then
                _SOURCE_UPDATE_FAILED=1
                warn "git pull failed — continuing with the existing checkout"
                printf '  Retry update: git -C %q pull --ff-only\n' "$INSTALL_DIR"
            fi
        fi
    elif [[ "$DIR_EXPLICIT" -eq 0 && -f "server.py" ]]; then
        INSTALL_DIR="$(pwd)"
        ok "Running from project directory: $INSTALL_DIR"
    elif [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
        fail "Install target exists but is not a directory: ${INSTALL_DIR}"
    elif [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        fail "Install target is non-empty but is not a Tofu checkout: ${INSTALL_DIR}. Choose an empty --dir or the existing Tofu directory."
    elif command -v git &>/dev/null; then
        info "Cloning https://github.com/rangehow/ToFu.git → ${INSTALL_DIR}"
        git clone https://github.com/rangehow/ToFu.git "$INSTALL_DIR" \
            || fail "git clone failed; see the install log at ${TOFU_INSTALL_LOG}"
        ok "Repository cloned"
    else
        return 1
    fi

    INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)" \
        || fail "Could not resolve install directory: ${INSTALL_DIR}"
    REQ_FILE="${INSTALL_DIR}/requirements.txt"
    [[ -f "$REQ_FILE" ]] || fail "requirements.txt not found at $REQ_FILE"
    _finalize_install_log_location
}

step "Getting Tofu source code"
_SOURCE_CHECKOUT_READY=0
_SOURCE_UPDATE_FAILED=0
if _prepare_source_checkout; then
    _SOURCE_CHECKOUT_READY=1
else
    warn "git not found and a clone is required — forcing the conda path (it installs git)"
    USE_CONDA=1
fi

# A destructive reset applies to the environment already bound to this
# checkout unless the user explicitly selects another backend/name.  This
# prevents a historical conda install from silently becoming a new uv install
# while leaving the environment the user asked to reset untouched.
if [[ "$RESET_ENV" -eq 1 && -f "${INSTALL_DIR}/.tofu_env.json" ]]; then
    _EXISTING_ENV_MARKER="${INSTALL_DIR}/.tofu_env.json"
    if grep -qE '"backend"[[:space:]]*:[[:space:]]*"uv"' \
            "$_EXISTING_ENV_MARKER"; then
        info "--reset-env: existing checkout uses uv"
    elif grep -qE '"backend"[[:space:]]*:[[:space:]]*"conda"|"conda_base"' \
            "$_EXISTING_ENV_MARKER"; then
        USE_CONDA=1
        if [[ "$ENV_EXPLICIT" -eq 0 ]]; then
            _MARKER_ENV_NAME="$(sed -nE \
                's/^[[:space:]]*"env_name"[[:space:]]*:[[:space:]]*"([A-Za-z0-9._-]+)"[,]?[[:space:]]*$/\1/p' \
                "$_EXISTING_ENV_MARKER" | head -n 1)"
            if [[ "$_MARKER_ENV_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
                ENV_NAME="$_MARKER_ENV_NAME"
            fi
        fi
        info "--reset-env: preserving existing conda backend (env ${ENV_NAME})"
    fi
fi

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
_UV_RESET_REFUSED=0
_UV_CONFIG_CONFLICT=0

# ═══════════════════════════════════════════════════════════════
#  Step 0.55: Download accelerants — MUST precede the backend fork
#
#  These were previously configured at ~L784, INSIDE the conda-only block
#  ($_FAST_PATH_DONE != 1). But _try_uv_install runs BEFORE that block and
#  returns on success, so on the DEFAULT (uv) path the mirror was never read:
#  a corp/China user's `uv pip install` went straight to pypi.org and hung to
#  the 900s timeout. The faster route was the one with zero acceleration.
#  Everything that redirects or caches a DOWNLOAD therefore lives here, above
#  the fork, so both backends inherit one source of truth.
# ═══════════════════════════════════════════════════════════════

# ── PyPI index (baked by export.py for corp hosts) ──
# pip and uv read DIFFERENT variables: exporting PIP_INDEX_URL alone leaves
# `uv pip install` pointed at the public PyPI, which is the whole bug. Set
# both. UV_INDEX_URL is uv's documented override (UV_DEFAULT_INDEX on newer
# builds) — export both names so the redirect survives a uv upgrade.
if [[ -n "${TOFU_PYPI_INDEX:-}" ]]; then
    info "PyPI index override: ${TOFU_PYPI_INDEX}"
    export PIP_INDEX_URL="${TOFU_PYPI_INDEX}"
    export UV_INDEX_URL="${TOFU_PYPI_INDEX}"
    export UV_DEFAULT_INDEX="${TOFU_PYPI_INDEX}"
    _PYPI_HOST="$(printf '%s' "$TOFU_PYPI_INDEX" | sed -E 's|^https?://([^/:]+).*|\1|')"
    export PIP_TRUSTED_HOST="${_PYPI_HOST}"
    export UV_INSECURE_HOST="${_PYPI_HOST}"
fi

# ── Playwright browser CDN mirror (opt-in) ──
# cdn.playwright.dev is slow-to-unreachable from mainland China. Honour a
# mirror when the operator sets one; empty = upstream, so public installs are
# unaffected.
if [[ -n "${TOFU_PLAYWRIGHT_MIRROR:-}" ]]; then
    info "Playwright download host: ${TOFU_PLAYWRIGHT_MIRROR}"
    export PLAYWRIGHT_DOWNLOAD_HOST="${TOFU_PLAYWRIGHT_MIRROR}"
fi

# ── Persistent, backend-shared download caches ──
# Both default to a per-env location, so a venv rebuild (`uv venv --clear`),
# a second env, or a plain re-run re-downloads ~115 MB of browser and the
# entire wheel set. Pin them to the user cache dir instead — deliberately
# OUTSIDE ${INSTALL_DIR}/.venv, which gets wiped on rebuild.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${HOME}/.cache/ms-playwright}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${HOME}/.cache/uv}"

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
    # Offline / air-gapped escape hatch: a pre-placed uv binary. Use it as-is
    # (symlinked onto PATH under the name `uv` if necessary) and skip the
    # astral.sh download entirely.
    if [[ -n "${TOFU_UV_LOCAL:-}" && -x "${TOFU_UV_LOCAL}" ]]; then
        info "Using local uv from TOFU_UV_LOCAL: ${TOFU_UV_LOCAL}"
        local _uv_dir
        _uv_dir="$(cd "$(dirname "${TOFU_UV_LOCAL}")" 2>/dev/null && pwd)"
        if [[ -n "$_uv_dir" && "$(basename "${TOFU_UV_LOCAL}")" == "uv" ]]; then
            export PATH="${_uv_dir}:${PATH}"
        else
            local _uv_abs="${_uv_dir:-$(dirname "${TOFU_UV_LOCAL}")}/$(basename "${TOFU_UV_LOCAL}")"
            local _uv_link_dir
            _uv_link_dir="$(mktemp -d "${TMPDIR:-/tmp}/tofu-uv.XXXXXX")"
            ln -s "$_uv_abs" "${_uv_link_dir}/uv"
            export PATH="${_uv_link_dir}:${PATH}"
        fi
        command -v uv &>/dev/null && return 0
    elif [[ -n "${TOFU_UV_LOCAL:-}" ]]; then
        warn "TOFU_UV_LOCAL set but not executable: ${TOFU_UV_LOCAL} — falling back to download"
    fi
    info "uv not found — installing it (astral.sh, bounded)..."
    local _t=""
    command -v timeout &>/dev/null && _t="timeout -k 5 120"
    if command -v curl &>/dev/null; then
        # Capture curl stderr into the install log so proxy/network failures are
        # visible for diagnosis (previously hidden behind /dev/null + `|| true`).
        # A failure still falls back to conda — the return below stays non-zero.
        if $_t sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >>"$TOFU_INSTALL_LOG" 2>&1; then
            :
        else
            warn "uv installer failed (see ${TOFU_INSTALL_LOG}); astral.sh may be unreachable behind a proxy"
        fi
    fi
    # uv installs to ~/.local/bin or ~/.cargo/bin — put both on PATH for this run.
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
    command -v uv &>/dev/null
}


_python_matches_request() {
    local executable="$1"
    local requested="$2"
    [[ -x "$executable" ]] || return 1
    "$executable" - "$requested" <<'PYEOF' >/dev/null 2>&1
import sys

requested = tuple(int(part) for part in sys.argv[1].split('.'))
actual = sys.version_info[:len(requested)]
raise SystemExit(0 if actual == requested else 1)
PYEOF
}


_uv_env_matches_install_marker() {
    local venv="$1"
    local marker="${INSTALL_DIR}/.tofu_env.json"
    [[ -x "${venv}/bin/python" && -f "$marker" ]] || return 1
    "${venv}/bin/python" - "$marker" "$venv" <<'PYEOF' >/dev/null 2>&1
import json
import os
import sys

try:
    with open(sys.argv[1], encoding='utf-8') as handle:
        marker = json.load(handle)
    matches = (
        marker.get('backend') == 'uv'
        and marker.get('owned_by_tofu_install') is True
        and os.path.realpath(str(marker.get('env_prefix') or ''))
            == os.path.realpath(sys.argv[2])
    )
except (OSError, TypeError, ValueError):
    matches = False
raise SystemExit(0 if matches else 1)
PYEOF
}


_uv_env_owned_by_installer() {
    local venv="$1"
    [[ -f "${venv}/.tofu-install-owned" ]] \
        && grep -qxF 'tofu-install-owned-v1' "${venv}/.tofu-install-owned" \
        && return 0
    _uv_env_matches_install_marker "$venv"
}


_reset_uv_env_if_requested() {
    local venv="$1"
    [[ "$RESET_ENV" -eq 1 ]] || return 0
    [[ -e "$venv" || -L "$venv" ]] || return 0

    # Recursive deletion is gated by explicit intent, an exact checkout-local
    # target, a no-symlink check, and installer ownership proof.
    if [[ -z "$INSTALL_DIR" || "$INSTALL_DIR" == "/" \
            || "$venv" != "${INSTALL_DIR%/}/.venv" || -L "$venv" \
            || ! -d "$venv" ]]; then
        warn "Refusing --reset-env for unverified uv environment: ${venv}"
        warn "Expected an installer ownership marker; move the directory aside manually if it is yours."
        _UV_RESET_REFUSED=1
        return 1
    fi
    if ! _uv_env_owned_by_installer "$venv"; then
        warn "Refusing --reset-env for unverified uv environment: ${venv}"
        warn "Expected an installer ownership marker; move the directory aside manually if it is yours."
        _UV_RESET_REFUSED=1
        return 1
    fi
    warn "--reset-env: removing installer-owned uv environment ${venv}"
    if ! rm -rf -- "$venv"; then
        warn "Could not remove ${venv}"
        _UV_RESET_REFUSED=1
        return 1
    fi
    ok "Installer-owned uv environment removed; rebuilding from scratch"
}


# The uv fast path. Sets ENV_PYTHON / ENV_PREFIX and writes .tofu_env.json on
# success and returns 0; returns non-zero on ANY failure so the caller falls
# back to conda. Never calls fail() — a failure here is recoverable.
_try_uv_install() {
    local _venv="${INSTALL_DIR}/.venv"
    _reset_uv_env_if_requested "$_venv" || return 1
    _ensure_uv || { warn "Could not obtain uv — falling back to conda"; return 1; }

    # Idempotent re-run: `uv venv` refuses to overwrite an existing venv (it
    # errors "use --clear"), which would spuriously drop a good install into the
    # conda fallback on every re-run. If a usable interpreter is already present,
    # reuse it — the `uv pip install` below is itself idempotent and fast.
    if [[ -x "${_venv}/bin/python" ]]; then
        if [[ "$PYTHON_EXPLICIT" -eq 1 ]] \
                && ! _python_matches_request "${_venv}/bin/python" "$PY_VER"; then
            warn "Existing uv environment does not satisfy --python ${PY_VER}: ${_venv}"
            warn "Re-run with --python ${PY_VER} --reset-env to rebuild it explicitly."
            _UV_CONFIG_CONFLICT=1
            return 1
        fi
        info "Reusing existing uv virtualenv at ${_venv}"
    else
        info "Creating uv virtualenv at ${_venv} (Python ${PY_VER})..."
        # --python-preference only-managed: seed the venv from uv's OWN standalone
        # CPython, never a system/conda interpreter. Two reasons: (1) hermetic +
        # reproducible (no dependence on whatever python the host ships); (2) it
        # guarantees .venv/bin/python resolves (realpath) to a DISTINCT base binary,
        # so server.py's re-exec guard is never short-circuited by a symlink
        # collision with the interpreter the user later launches from.
        uv venv "$_venv" --python "${PY_VER}" --python-preference only-managed 2>&1 || {
            warn "uv venv failed — falling back to conda"; return 1; }
        (umask 077; printf '%s\n' 'tofu-install-owned-v1' \
            > "${_venv}/.tofu-install-owned") || {
            warn "Could not record uv environment ownership — falling back to conda"
            return 1
        }
    fi

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
    if ! "$_uvpy" -c 'import lxml.etree, fitz, PIL, cryptography, quart, hypercorn, orjson, playwright' 2>&1; then
        warn "uv-installed wheels failed the import smoke-test (likely glibc too old) — falling back to conda"
        return 1
    fi
    # Presence is not enough for this exact-pin trio.  Exercise the classic
    # Markdown path so a split PyMuPDF/PyMuPDF4LLM environment is rejected at
    # install time rather than on the user's first paper.
    if ! PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" \
            "$_uvpy" "$INSTALL_DIR/scripts/verify_pdf_stack.py" 2>&1; then
        warn "uv-installed PyMuPDF stack failed its version/Markdown smoke-test — falling back to conda"
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
        # --only-shell: a default `install chromium` fetches BOTH the full
        # Chromium build (175.4 MB) and chrome-headless-shell (113.2 MB) plus
        # ffmpeg — measured 290.9 MB. Shell-only = 115.5 MB (-60%).
        #
        # The trade-off, stated honestly (an earlier version of this comment
        # claimed "no headless=False call site exists" — measured FALSE on
        # 2026-07-29): there is EXACTLY ONE headed call site in the product,
        # tofu_search/fetch/interactive_login.py (login-wall cookie capture).
        # chrome-headless-shell has NO headed mode — it is a separate, smaller
        # binary, not a flag — so shell-only means that ONE feature is
        # unavailable. Everything else (all fetch/render/screenshot paths) is
        # headless and fully served by the shell.
        #
        # We keep --only-shell: -60% download for every user, at the cost of a
        # rare, user-initiated feature that now degrades HONESTLY instead of
        # dying at launch — chromium_env.headed_chromium_executable() decides,
        # and the caller returns reason='headed_unavailable' naming the fix.
        # Users who need login-wall capture run:
        #   python -m playwright install chromium     (adds the full build)
        "$_uvpy" -m playwright install --only-shell chromium >/dev/null 2>&1 \
            && ok "Playwright Chromium installed" \
            || warn "Playwright Chromium install skipped/failed — JS-rendered fetch disabled until you run it manually"
        # Downloading the browser is not the same as being able to RUN it.
        # Unlike the conda path, a uv venv has no conda-forge to source
        # Chromium's GUI libs (libatk, libnss, fontconfig, fonts) from, so on a
        # bare host the binary lands but every launch dies on a missing .so.
        # Prove it launches now, while we can still say something useful —
        # otherwise the failure only surfaces much later as a dead browser tool.
        info "Verifying Chromium can actually launch..."
        if "$_uvpy" - <<'PYEOF' 2>/dev/null
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox'])
    pg = b.new_page()
    pg.set_content('<h1>x</h1>')
    assert pg.evaluate(
        "(()=>{const c=document.createElement('canvas').getContext('2d');"
        "c.font='60px sans-serif';return c.measureText('x').width;})()") > 0, 'no fonts'
    b.close()
PYEOF
        then
            ok "Chromium launches and renders text"
        else
            warn "Chromium is installed but cannot launch/render on this host (missing system libs or fonts)."
            warn "  Browser screenshots + JS-rendered fetch will be unavailable; plain HTTP fetching still works."
            warn "  Fix with root:    sudo $_uvpy -m playwright install-deps chromium"
            warn "  Fix rootless:     re-run ./install.sh --use-conda  (sources the libs + fonts from conda-forge)"
        fi
    fi

    # Publish the env for the shared downstream steps (.env and launch).
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
elif ! _glibc_ge_228; then
    info "Host glibc < 2.28 (or undetectable) — using the conda path for maximum"
    info "compatibility (PyMuPDF/Pillow ship no manylinux2014 wheel for old glibc)."
    USE_CONDA=1
else
    step "Installing via uv (fast path; falls back to conda on any failure)"
    if _try_uv_install; then
        _FAST_PATH_DONE=1
    elif [[ "$_UV_RESET_REFUSED" -eq 1 ]]; then
        fail "--reset-env was refused because .venv ownership could not be proven; no alternate environment was changed"
    elif [[ "$_UV_CONFIG_CONFLICT" -eq 1 ]]; then
        fail "--python conflicts with the existing uv environment; no alternate environment was changed"
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
if [[ "$FORCE_SIBLING_CONDA" -ne 1 && -f "$_TOFU_ENV_MARKER" ]] \
        && command -v python3 &>/dev/null; then
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

if [[ "$FORCE_SIBLING_CONDA" -eq 1 ]]; then
    # Explicit user intent outranks a prior marker that may point at a borrowed
    # conda. Step 2 below may still reuse the installer-owned sibling itself.
    CONDA_BIN=""
    info "--force-sibling-conda: ignoring marker and user-owned conda installations"
elif [[ -n "$CONDA_BIN" ]]; then
    : # already resolved from .tofu_env.json marker
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

# PyPI index override is configured ONCE at Step 0.55, ABOVE the uv-vs-conda
# fork, so both backends inherit it (PIP_INDEX_URL + UV_INDEX_URL + trusted
# host). It used to be duplicated here, inside the conda-only block, which is
# exactly why the uv fast path never saw the mirror. The env's pip.conf writer
# further down still reads $PIP_INDEX_URL — unchanged.

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
#  Step 3: Complete a source checkout deferred until conda supplied git
# ═══════════════════════════════════════════════════════════════
if [[ "$_SOURCE_CHECKOUT_READY" -ne 1 ]]; then
    step "Completing deferred Tofu source checkout"
    if ! command -v git &>/dev/null; then
        if [[ "$CONDA_OWNED_BY_US" -eq 1 ]]; then
            info "git not found — installing into the conda base we own..."
            conda install -n base -c conda-forge --override-channels -y git
        else
            fail "git not found, and the conda in use is user-owned (we never mutate it). Install git manually (e.g. apt-get install git / yum install git / brew install git), or create the Tofu env and run 'conda install -n ${ENV_NAME} -c conda-forge git', then re-run install.sh"
        fi
    fi
    _prepare_source_checkout \
        || fail "git is still unavailable after conda setup; cannot clone Tofu"
    _SOURCE_CHECKOUT_READY=1
fi

# ═══════════════════════════════════════════════════════════════
#  Step 4: Create / reuse conda env
# ═══════════════════════════════════════════════════════════════
step "Creating conda environment: ${ENV_NAME}"

CONDA_ENV_PREFIX="${CONDA_BASE}/envs/${ENV_NAME}"

_conda_env_matches_install_marker() {
    local marker="${INSTALL_DIR}/.tofu_env.json"
    [[ -x "${CONDA_BASE}/bin/python" && -f "$marker" ]] || return 1
    "${CONDA_BASE}/bin/python" - "$marker" "$CONDA_BASE" \
            "$ENV_NAME" "$CONDA_ENV_PREFIX" <<'PYEOF' >/dev/null 2>&1
import json
import os
import sys

try:
    with open(sys.argv[1], encoding='utf-8') as handle:
        marker = json.load(handle)
    backend = marker.get('backend')
    matches = (
        backend in (None, '', 'conda')
        and str(marker.get('env_name') or '') == sys.argv[3]
        and os.path.realpath(str(marker.get('conda_base') or ''))
            == os.path.realpath(sys.argv[2])
        and os.path.realpath(str(marker.get('env_prefix') or ''))
            == os.path.realpath(sys.argv[4])
    )
except (OSError, TypeError, ValueError):
    matches = False
raise SystemExit(0 if matches else 1)
PYEOF
}

ENV_EXISTS=0
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    ENV_EXISTS=1
fi

if [[ "$ENV_EXISTS" -eq 1 && "$RESET_ENV" -eq 1 ]]; then
    _conda_env_matches_install_marker || fail \
        "Refusing --reset-env for conda env '${ENV_NAME}': this checkout has no matching ownership marker. Choose another --env or move/remove the env manually."
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
if [[ "$PYTHON_EXPLICIT" -eq 1 ]] \
        && ! _python_matches_request "$PY" "$PY_VER"; then
    fail "Existing conda env '${ENV_NAME}' does not satisfy --python ${PY_VER}. Re-run with --python ${PY_VER} --reset-env to rebuild the marker-owned env."
fi
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
ENV_PREFIX="$CONDA_ENV_PREFIX"
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
    'backend':     'conda',
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
    # Quart + Hypercorn (ASGI server) — the core server runtime.
    # cryptography is needed for Hypercorn's auto-TLS (HTTP/2).
    "quart>=0.19"
    "hypercorn>=0.17"
    "cryptography>=42"
    "requests>=2.31"
    # jinja2 / urllib3 / pyyaml — transitive deps (jinja2←quart,
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
    # Bounded on both sides: Tofu's client was migrated to the mcp v2 API
    # (>=2,<3). Vendored servers carry their own pins in isolated envs.
    # Enforced by tests/test_mcp_sdk_pin_bounded.py.
    "mcp>=2,<3"
    # httpx — lib/llm/_transport.py imports it UNCONDITIONALLY at module top
    # (the LLM async transport). NOTE the name: mcp 2.x pulls the httpx2
    # FORK, which does NOT provide the `httpx` module — they are different
    # distributions, and httpx2's presence must not satisfy this entry.
    # Declared in requirements.txt after the desktop_dist clean-venv build
    # proved the gap (boot smoke: ModuleNotFoundError httpx, 2026-08-01).
    "httpx>=0.28"
    # orjson — fast JSON encoder; imported by routes/chat.py for chat
    # snapshot serialisation. Hard dep: the server won't boot without it.
    "orjson>=3.9"
    # Psycopg 3 is the external PostgreSQL client. The standalone installer
    # never installs PostgreSQL server binaries; conda supplies libpq.
    "psycopg>=3.2"
    "psycopg-pool>=3.2"
    # markdown — server-side Markdown rendering. Hard dep at import time.
    "markdown>=3.4"
    # tiktoken — exact BPE tokenizer tier for lib/token_counter.
    "tiktoken>=0.5"
    # Bootstrap PyMuPDF core from conda for old-glibc hosts. The exact trio is
    # harmonized with pip immediately below; this floor is not the final
    # version contract.
    "pymupdf>=1.24"
    # uv / uvx — used by lib/mcp/client.py to launch MCP servers
    "uv>=0.4"
)

# Pip-installed deps.
#
# trafilatura + htmldate are pure-Python packages; installing them via pip
# lets us get htmldate 1.9.4+ (no "lxml<6" upper bound) while retaining the
# current conda lxml stack. This is not a downgrade: pip provides a newer
# htmldate than the affected conda snapshots.
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
# PyMuPDF4LLM exact-pins both companions. Keep this array byte-aligned with
# requirements.txt; tests/test_pdf_stack_install_contract.py enforces it.
# Unlike the pure-Python group below, this trio is installed WITH dependency
# resolution so pymupdf-layout's runtime requirements cannot be absent on a
# clean conda fallback.
PDF_STACK_PKGS=(
    "pymupdf==1.27.2.3"
    "pymupdf_layout==1.27.2.3"
    "pymupdf4llm==1.27.2.3"
)

PIP_ONLY_PKGS=(
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
    # zhconv — pure-Python (MediaWiki tables, MIT), zero deps. Fail-safe gate
    # that normalizes voice-transcription output to Simplified Chinese
    # (lib/transcription/_zh.py). Not on conda-forge, so pip-only; --no-deps
    # is safe since it imports nothing beyond the stdlib.
    "zhconv>=1.4"
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
PIP_NAMES=(quart hypercorn cryptography requests psutil
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
# Snapshot the env's package list BEFORE the purge, so we can tell whether the
# purge actually removed anything (see _PURGED_SOMETHING below). Cheap: one
# `conda list` against an env we are about to solve anyway.
_CONDA_PKGS_BEFORE_PURGE="$(conda list -n "$ENV_NAME" 2>/dev/null || true)"
conda remove -n "$ENV_NAME" -y --force "${CONDA_CONFLICT_PKGS[@]}" >/dev/null 2>&1 || true
ok "Conflict-prone packages cleared (will reinstall below)"

# Snapshot AGAIN after the purge: the gate below must diff BEFORE vs AFTER to
# tell "the purge removed it" from "it was there all along". Judging only
# the BEFORE list is backwards — presence-beforehand is the steady state of
# every healthy env (the packages are purged precisely because the solve
# installs them), so that gate fired --force-reinstall on EVERY run.
_CONDA_PKGS_AFTER_PURGE="$(conda list -n "$ENV_NAME" 2>/dev/null || true)"

# Did the purge ACTUALLY remove anything? `conda remove` above is best-effort
# and silently succeeds on a clean env where none of those packages are
# present — which is the common re-run case.
_PURGED_SOMETHING=0
for _p in "${CONDA_CONFLICT_PKGS[@]}"; do
    if grep -qE "^${_p}[[:space:]]" <<< "${_CONDA_PKGS_BEFORE_PURGE}" && \
       ! grep -qE "^${_p}[[:space:]]" <<< "${_CONDA_PKGS_AFTER_PURGE}"; then
        _PURGED_SOMETHING=1
        break
    fi
done

# Also purge any pip-installed trafilatura/htmldate from prior runs so
# pip's own install below is clean.
python -m pip uninstall -y trafilatura htmldate courlan >/dev/null 2>&1 || true

# --force-reinstall makes conda re-lay-down files even when its metadata still
# thinks the package is satisfied — genuinely needed right after the purge
# above, because a pip-uninstall leaves conda's view stale.
#
# But it is NOT free: applied unconditionally it re-downloads and re-links all
# ~30 CONDA_PKGS on EVERY run, including a re-run of an already-correct env.
# So gate it on the purge having actually removed something. When nothing was
# purged there is no stale metadata to repair, and a plain `conda install` is
# a fast no-op. The retry branch below still force-reinstalls unconditionally,
# so a genuinely broken env is still repaired — we only skip the sledgehammer
# on the happy path.
_FORCE_REINSTALL=""
if [[ "$_PURGED_SOMETHING" -eq 1 ]]; then
    _FORCE_REINSTALL="--force-reinstall"
    info "Purge removed packages — using --force-reinstall to repair conda metadata"
else
    info "Nothing was purged — skipping --force-reinstall (re-run stays fast)"
fi
_install_main_deps() {
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y ${_FORCE_REINSTALL} "${CONDA_PKGS[@]}"
}

if ! _install_main_deps; then
    warn "First solve failed — doing a deeper reset of the conflicting packages and retrying"
    # The gate above only applies to the happy path. This branch runs ONLY
    # after the first solve already FAILED — the env is genuinely broken, so
    # it keeps its unconditional --force-reinstall (narrowing it would trade
    # a rare slow path for a rare unrepairable one).
    _FORCE_REINSTALL="--force-reinstall"
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
    "psycopg:psycopg"
    "psycopg_pool:psycopg-pool"
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

# ── Harmonize the exact PDF trio in the conda env ──
# Installing only pymupdf4llm with --no-deps used to leave users with conda's
# arbitrary pymupdf floor and no pymupdf-layout at all. The package was present,
# but its first real import/parse failed. Resolve the trio together and make a
# failure fatal: rich PDF parsing is a shipped default, not an optional surprise.
info "Installing pinned PyMuPDF stack: ${PDF_STACK_PKGS[*]}"
if ! _safe_pip_install --upgrade "${PDF_STACK_PKGS[@]}"; then
    fail "Pinned PyMuPDF stack install failed. Check that your package index carries all three exact versions, then rerun install.sh."
fi
ok "Pinned PyMuPDF stack installed"

# ── Install remaining pip-only deps into the conda env ──
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

# Exact versions plus one real Markdown page. This catches half-installed
# wheels, wrong-interpreter installs and ABI/version split-brain immediately.
if (cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        python scripts/verify_pdf_stack.py); then
    ok "PyMuPDF stack verified (exact pins + Markdown smoke)"
else
    fail "PyMuPDF stack verification failed. Run: python scripts/verify_pdf_stack.py"
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
# can't fetch them — but they are NOT pip-installed into this env anymore.
# Each launches ISOLATED via `uv run --no-project --with-editable <source>`
# (lib/mcp/client/_vendor.vendored_launch_argv): the server's dependency tree
# — its own `mcp` included — must never share Tofu's interpreter, or one
# server's SDK requirement can break the Tofu client (measured 2026-07-31).
# All this step does is locate the sources and pre-warm the isolated envs so
# the first connect is a fast handshake instead of a cold resolve.
# Sources, in priority order:
#   1. vendor/<name>/   — personal/internal EXPORTS bundle the source here.
#   2. ../<name>/        — a DEV checkout: sibling repos next to this one.
# Skipped silently if neither source exists (opensource exports).
step "Warming bundled internal MCP servers (isolated envs)"
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
elif ! command -v uv >/dev/null 2>&1; then
    warn "uv not available — cannot pre-warm bundled MCP servers"
    warn "The MCP tab Install buttons will still work, but first connects will be slow."
else
    for _src in "${_BUNDLED_MCPS[@]}"; do
        _name="$(basename "$_src")"
        _pkg="${_name//-/_}"
        info "Warming: ${_name} (${_src})"
        if uv run --no-project --with-editable "$_src" python -c "import ${_pkg}"; then
            ok "Bundled MCP ${_name} ready (isolated env warm)"
        else
            warn "Bundled MCP ${_name} warm failed — its Install button may be slow"
            warn "Retry manually: uv run --no-project --with-editable ${_src} python -c 'import ${_pkg}'"
        fi
    done
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

# ── Storage deployment ──────────────────────────────────────
# The standalone installer intentionally provisions only personal SQLite.
# Distributed PostgreSQL is externally managed and delivered by Kubernetes;
# the Psycopg client dependency is already part of the frozen Python project.
info "Using personal deployment mode with project-local SQLite"
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
#  Step 8: Playwright — Chromium browser + shared libs (rootless)
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
    step "Installing Playwright Chromium"

    # Repo root — chromium_env.py lives there; the verification probes below
    # import it. Defined unconditionally (set -u): the launch check runs on
    # every OS, the shared-lib install is Linux-only.
    _repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # On Linux, install Chromium's shared libs from conda-forge so that no
    # sudo / system packages are required. server.py / bootstrap.py export
    # $env_prefix/lib on LD_LIBRARY_PATH at startup (before any re-exec early
    # return) so the Chromium child process can resolve them.
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
            # Text rendering. Without fontconfig + at least one real font
            # family, Chromium launches and paints CSS fine but draws every
            # glyph as nothing — screenshots come back blank-but-styled, which
            # reads as "the page didn't load" rather than as an error. These
            # were previously only present as transitive deps of other
            # packages; pin them explicitly so a solver change can't drop them.
            fontconfig
            font-ttf-dejavu-sans-mono
            font-ttf-ubuntu
        )
        if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y "${CHROMIUM_LIBS[@]}"; then
            # One unavailable package must not forfeit the whole set. Measured
            # 2026-08-03: this host had gbm/nss/fonts but NO libatk — an early
            # group failure (or a list expansion that never re-ran) left the
            # env permanently short, and every Chromium launch died on
            # "libatk-1.0.so.0: cannot open shared object file".
            warn "Group install failed — retrying per-package (best-effort)"
            _chromium_lib_failures=()
            for _pkg in "${CHROMIUM_LIBS[@]}"; do
                if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y "$_pkg"; then
                    _chromium_lib_failures+=("$_pkg")
                fi
            done
            if [[ ${#_chromium_lib_failures[@]} -gt 0 ]]; then
                warn "Chromium shared-lib deps unavailable on this channel: ${_chromium_lib_failures[*]}"
                info "You can retry manually: conda install -n ${ENV_NAME} -c conda-forge <packages>"
            fi
        fi
        # Evidence check, not exit-code trust: the install only counts when a
        # directory carrying the sentinel libs actually exists — the same
        # probe chromium_env.chromium_lib_dirs() runs at server start.
        if PYTHONPATH="${_repo_root}" python -c "import chromium_env, sys; sys.exit(0 if chromium_env.chromium_lib_dirs() else 1)" 2>/dev/null; then
            ok "Chromium shared libs present in the env"
        else
            warn "No directory carrying Chromium GUI libs (libatk/libnss/libgbm) found in the env — browser will not launch"
            warn "  Fix rootless: conda install -n ${ENV_NAME} -c conda-forge atk-1.0 at-spi2-atk at-spi2-core alsa-lib xorg-libxcomposite xorg-libxdamage xorg-libxfixes xorg-libxrandr libxkbcommon nspr nss mesa-libgbm-cos7-x86_64 fontconfig font-ttf-dejavu-sans-mono font-ttf-ubuntu"
            warn "  Fix with root: sudo python -m playwright install-deps chromium"
        fi

        # FUSE-mounted env? Installed is not enough — a FUSE bad window kills
        # Chromium's .so reads at LAUNCH time (measured 2026-08-03 on
        # beegfs-fuse: 'libatk cannot open' storms alternating with successful
        # launches under a CONSTANT process env; the libs were never missing).
        # The deterministic answer is a local-disk copy + the
        # CHROMIUM_EXTRA_LIB_DIRS override (honored FIRST and unfiltered by
        # chromium_env.chromium_lib_dirs(); tofu_search's standalone fallback
        # reads the same variable).
        _env_prefix="$(conda run -n "$ENV_NAME" python -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
        if [[ -n "$_env_prefix" ]] && df -T "$_env_prefix" 2>/dev/null | awk 'NR==2{print $2}' | grep -qi fuse; then
            _browser_libs="${TOFU_BROWSER_LIBS_DIR:-${HOME}/tofu-browser-libs}"
            info "Env prefix is on FUSE (${_env_prefix}) — installing a local-disk Chromium-libs copy at ${_browser_libs}"
            if [[ -d "${_browser_libs}/conda-meta" ]]; then
                _local_cmd=(conda install -p "${_browser_libs}" -c conda-forge --override-channels -y)
            else
                _local_cmd=(conda create -p "${_browser_libs}" -c conda-forge --override-channels -y)
            fi
            if ! "${_local_cmd[@]}" "${CHROMIUM_LIBS[@]}"; then
                warn "Local-disk group install failed — retrying per-package"
                for _pkg in "${CHROMIUM_LIBS[@]}"; do
                    "${_local_cmd[@]}" "$_pkg" || warn "  local chromium lib '$_pkg' unavailable on this channel"
                done
            fi
            if [[ -f "${_browser_libs}/lib/libatk-1.0.so.0" ]]; then
                ok "Local-disk Chromium libs ready at ${_browser_libs}/lib"
                # In effect for THIS script's launch verification below;
                # restart_15000.sh auto-discovers the same default path.
                export CHROMIUM_EXTRA_LIB_DIRS="${_browser_libs}/lib"
                info "restart_15000.sh auto-discovers this path (TOFU_BROWSER_LIBS_DIR overrides)."
                info "For any other launcher: export CHROMIUM_EXTRA_LIB_DIRS=${_browser_libs}/lib"
            else
                warn "Local-disk copy incomplete — Chromium will keep resolving libs from the FUSE env (flaky)"
            fi
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
        warn "Manual recovery: conda activate ${ENV_NAME} && pip install 'playwright>=1.40' && python -m playwright install --only-shell chromium"
    else
        info "Downloading Chromium headless shell via playwright..."
        # --only-shell: see the uv path above for the full trade-off. Skips the
        # 175 MB full Chromium build that no HEADLESS path needs — the single
        # headed feature (login-wall capture) degrades with an actionable
        # message and is recovered by `python -m playwright install chromium`.
        if python -m playwright install --only-shell chromium; then
            ok "Playwright Chromium installed"
        else
            warn "Playwright Chromium install failed (non-critical — fetching still works via requests)"
        fi
        # Downloading the browser is not the same as being able to RUN it —
        # prove it launches AND renders text now, while the message is still
        # actionable (same check the uv fast path runs).
        info "Verifying Chromium can actually launch..."
        if PYTHONPATH="${_repo_root}" python - <<'PYEOF' 2>/dev/null
import chromium_env
chromium_env.ensure_chromium_env()
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox'])
    pg = b.new_page()
    pg.set_content('<h1>x</h1>')
    assert pg.evaluate(
        "(()=>{const c=document.createElement('canvas').getContext('2d');"
        "c.font='60px sans-serif';return c.measureText('x').width;})()") > 0, 'no fonts'
    b.close()
PYEOF
        then
            ok "Chromium launches and renders text"
        else
            warn "Chromium is installed but cannot launch/render on this host (missing system libs or fonts)."
            warn "  Browser screenshots + JS-rendered fetch will be unavailable; plain HTTP fetching still works."
            warn "  Re-run ./install.sh to retry the rootless shared-lib install, or with root: sudo python -m playwright install-deps chromium"
        fi
    fi
else
    info "Skipping Playwright (--skip-playwright)"
fi

fi  # ── end legacy conda path ($_FAST_PATH_DONE != 1) ──

# ═══════════════════════════════════════════════════════════════
#  Step 8.4: Prebuilt frontend — release users never need Node/npm.
# ═══════════════════════════════════════════════════════════════
step "Installing verified prebuilt frontend"
_FRONTEND_VERSION="$(tr -d '[:space:]' < "${INSTALL_DIR}/VERSION")"
[[ -n "$_FRONTEND_VERSION" ]] || fail "VERSION is empty; cannot resolve frontend artifact"
_FRONTEND_NAME="frontend-dist-${_FRONTEND_VERSION}.tar.gz"
_FRONTEND_BASE="${TOFU_FRONTEND_DIST_BASE_URL:-https://github.com/rangehow/ToFu/releases/download/v${_FRONTEND_VERSION}}"
_FRONTEND_TMP="$(mktemp -d "${TMPDIR:-/tmp}/tofu-frontend.XXXXXX")"
_FRONTEND_ARCHIVE="${_FRONTEND_TMP}/${_FRONTEND_NAME}"
_FRONTEND_CHECKSUM="${_FRONTEND_ARCHIVE}.sha256"
_download_frontend_file() {
    local _url="$1" _dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 15 -o "$_dest" "$_url"
    elif command -v wget >/dev/null 2>&1; then
        wget --tries=3 --timeout=30 -O "$_dest" "$_url"
    else
        fail "Need curl or wget to download the prebuilt frontend"
    fi
}
_download_frontend_file "${_FRONTEND_BASE}/${_FRONTEND_NAME}" "$_FRONTEND_ARCHIVE"
_download_frontend_file "${_FRONTEND_BASE}/${_FRONTEND_NAME}.sha256" "$_FRONTEND_CHECKSUM"
if command -v sha256sum >/dev/null 2>&1; then
    (cd "$_FRONTEND_TMP" && sha256sum -c "${_FRONTEND_NAME}.sha256")
elif command -v shasum >/dev/null 2>&1; then
    (cd "$_FRONTEND_TMP" && shasum -a 256 -c "${_FRONTEND_NAME}.sha256")
else
    fail "Need sha256sum or shasum to verify the frontend artifact"
fi
rm -rf "${INSTALL_DIR}/static/vite"
tar xzf "$_FRONTEND_ARCHIVE" -C "$INSTALL_DIR"
rm -rf "$_FRONTEND_TMP"
(cd "$INSTALL_DIR" && "$ENV_PYTHON" scripts/verify_frontend_dist.py) || \
    fail "Downloaded frontend manifest or chunks are incomplete"
ok "Prebuilt frontend ${_FRONTEND_VERSION} installed (Node/npm not required)"

# ═══════════════════════════════════════════════════════════════
#  Step 8.45: Modern text tools — sd / goawk / miller (BOTH paths)
#
#  The run_command tool description (lib/tools/project.py) tells the model
#  to prefer `sd` over sed for substitutions, `mlr` over hand-rolled awk
#  for CSV/TSV/JSON column work, and `goawk -i csv` for awk-with-CSV — so
#  these binaries are a standard part of every install, not optional
#  extras. They land in the env's bin/, which server.py / bootstrap.py
#  prepend to PATH on every boot, so run_command subprocesses always
#  resolve them.
#
#  Two sources, one result: conda-forge when this env is conda-managed
#  (the legacy path — also the only route that works through corp conda
#  mirrors on air-gapped hosts), otherwise pinned static release archives
#  (single self-contained binaries — sd is musl-linked, goawk/miller are
#  Go — so they run on any glibc, CentOS-7-era hosts included). A tool
#  that cannot be installed from either source is warned about once;
#  GNU sed/awk remain the universal fallback the model can use.
# ═══════════════════════════════════════════════════════════════
step "Installing modern text tools (sd, goawk, miller)"

_SD_VER="1.0.0"        # chmln/sd
_GOAWK_VER="1.29.0"    # benhoyt/goawk
_MILLER_VER="6.13.0"   # johnkerl/miller
_TEXT_TOOL_DIR="${ENV_PREFIX}/bin"
# Prefix-style mirror override for ALL GitHub release fetches below
# (e.g. "https://ghproxy.net/https://github.com"). Empty = upstream.
_GH_REL="${TOFU_GH_RELEASE_BASE:-https://github.com}"

# conda-forge carries all three and integrates with the env's solver —
# use it whenever this env is conda-managed. A missing feedstock or a
# solver failure just drops through to the static-binary fallback per
# tool (the presence checks below decide).
if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then
    if conda install -n "$ENV_NAME" -c conda-forge --override-channels -y sd goawk miller; then
        ok "sd + goawk + miller installed from conda-forge"
    else
        warn "conda-forge text-tool install failed — falling back to static binaries"
    fi
fi

_tofu_dl() {
    local url="$1" dest="$2"
    if command -v curl &>/dev/null; then
        curl -4 -fsSL --connect-timeout 15 --max-time 300 -o "$dest" "$url"
    elif command -v wget &>/dev/null; then
        wget -4 -q --timeout=300 -O "$dest" "$url"
    else
        return 1
    fi
}

# Fetch+install ONE tool from a release tarball unless it already
# resolves. $1=display name, $2=binary name, rest=candidate URLs tried
# in order (archive layouts differ between projects — locate the binary
# after extraction rather than assuming a fixed path inside the tar).
_tofu_install_text_tool() {
    local name="$1" bin="$2"; shift 2
    if command -v "$bin" &>/dev/null; then
        ok "$name already present ($(command -v "$bin"))"
        return 0
    fi
    if [[ -x "${_TEXT_TOOL_DIR}/${bin}" ]]; then
        ok "$name already installed in the env"
        return 0
    fi
    local tmp url found
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/tofu-${name}.XXXXXX")"
    for url in "$@"; do
        info "Downloading ${name}: ${url}"
        if _tofu_dl "$url" "${tmp}/pkg.tgz" && tar xzf "${tmp}/pkg.tgz" -C "$tmp" 2>/dev/null; then
            found=""
            found="$(find "$tmp" -type f -name "$bin" 2>/dev/null | head -1 || true)"
            if [[ -n "$found" ]]; then
                mkdir -p "$_TEXT_TOOL_DIR"
                cp "$found" "${_TEXT_TOOL_DIR}/${bin}"
                chmod 0755 "${_TEXT_TOOL_DIR}/${bin}"
                if "${_TEXT_TOOL_DIR}/${bin}" --version >/dev/null 2>&1; then
                    ok "$name installed → ${_TEXT_TOOL_DIR}/${bin}"
                    rm -rf "$tmp"
                    return 0
                fi
                warn "$name binary failed its --version smoke test"
                rm -f "${_TEXT_TOOL_DIR}/${bin}"
            fi
        fi
        # Drop the failed/partial archive before the next candidate.
        rm -f "${tmp}/pkg.tgz"
    done
    rm -rf "$tmp"
    warn "$name could not be installed (no reachable source) — GNU sed/awk fallback remains"
    return 0   # an accelerant never aborts the install
}

# Release-asset naming differs per project and arch — encode the matrix
# once. Miller's macOS archives have shipped under both 'macos' and
# 'darwin' spellings across releases; both are listed as candidates.
case "${OS}/${ARCH}" in
    Linux/x86_64)   _SD_T="x86_64-unknown-linux-musl";  _GA_T="linux_amd64";  _ML_T=("linux-amd64") ;;
    Linux/aarch64)  _SD_T="aarch64-unknown-linux-musl"; _GA_T="linux_arm64";  _ML_T=("linux-arm64") ;;
    MacOSX/x86_64)  _SD_T="x86_64-apple-darwin";        _GA_T="darwin_amd64"; _ML_T=("macos-amd64" "darwin-amd64") ;;
    MacOSX/arm64)   _SD_T="aarch64-apple-darwin";       _GA_T="darwin_arm64"; _ML_T=("macos-arm64" "darwin-arm64") ;;
    *)              _SD_T=""; _GA_T=""; _ML_T=() ;;
esac

if [[ -n "$_SD_T" ]]; then
    _tofu_install_text_tool "sd" "sd" \
        "${_GH_REL}/chmln/sd/releases/download/v${_SD_VER}/sd-v${_SD_VER}-${_SD_T}.tar.gz"
    _tofu_install_text_tool "goawk" "goawk" \
        "${_GH_REL}/benhoyt/goawk/releases/download/v${_GOAWK_VER}/goawk_v${_GOAWK_VER}_${_GA_T}.tar.gz"
    _ml_urls=()
    for _t in "${_ML_T[@]}"; do
        _ml_urls+=("${_GH_REL}/johnkerl/miller/releases/download/v${_MILLER_VER}/miller-${_MILLER_VER}-${_t}.tar.gz")
    done
    _tofu_install_text_tool "miller" "mlr" "${_ml_urls[@]}"
else
    warn "No prebuilt text-tool archives for ${OS}/${ARCH} — GNU sed/awk remain"
fi


# ═══════════════════════════════════════════════════════════════
#  Step 8.5: Select the standalone personal storage topology
# ═══════════════════════════════════════════════════════════════
step "Selecting personal SQLite storage"
DB_BACKEND_CHOICE="sqlite"
if [[ -d "${INSTALL_DIR}/data/pgdata" ]]; then
    warn "A legacy project-local PostgreSQL directory was found and left untouched."
    warn "Verify a backup, then use the documented stopped-writer migration before removing it."
fi
ok "Personal deployment selected; no database server process is installed or started"

# ═══════════════════════════════════════════════════════════════
#  Step 9: Configure .env
# ═══════════════════════════════════════════════════════════════
step "Configuring .env"

ENV_FILE="${INSTALL_DIR}/.env"
ENV_EXAMPLE="${INSTALL_DIR}/.env.example"

_ENV_FILE_EXISTED=0
[[ -f "$ENV_FILE" ]] && _ENV_FILE_EXISTED=1
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
# Provider credentials may be written below or added through the UI later.
# Never leave this file readable by other users merely because the caller's
# umask was permissive.
chmod 600 "$ENV_FILE" \
    || fail "Could not restrict ${ENV_FILE} to owner-only access (chmod 600)"

# Update/insert a key in .env
_set_env_var() {
    local key="$1" value="$2" file="$3"
    # sed replacement strings interpret backslash, ampersand, and the chosen
    # delimiter. Escape all three so a credential can never corrupt adjacent
    # .env lines or turn into replacement syntax.
    local escaped_value="$value"
    escaped_value="${escaped_value//\\/\\\\}"
    escaped_value="${escaped_value//&/\\&}"
    escaped_value="${escaped_value//|/\\|}"
    if grep -qE "^[#[:space:]]*${key}=" "$file" 2>/dev/null; then
        # Portable sed -i (macOS requires a backup ext)
        if [[ "$OS" == "Darwin" ]]; then
            sed -i '' -E "s|^[#[:space:]]*${key}=.*|${key}=${escaped_value}|" "$file"
        else
            sed -i -E "s|^[#[:space:]]*${key}=.*|${key}=${escaped_value}|" "$file"
        fi
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

# Delete a retired key without interpreting its former value. The key names
# below are code-owned constants, so the sed expression never includes user
# input.
_unset_env_var() {
    local key="$1" file="$2"
    if [[ "$OS" == "Darwin" ]]; then
        sed -i '' -E "/^[#[:space:]]*${key}=.*/d" "$file"
    else
        sed -i -E "/^[#[:space:]]*${key}=.*/d" "$file"
    fi
}

if [[ "$_ENV_FILE_EXISTED" -eq 0 || "$PORT_EXPLICIT" -eq 1 ]]; then
    _set_env_var "PORT" "$PORT" "$ENV_FILE"
elif [[ "$PORT_FROM_ENV" -eq 1 ]]; then
    info "Using explicit environment PORT=${PORT}; leaving existing .env unchanged"
elif ! _EXISTING_ENV_PORT="$(
        PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$ENV_PYTHON" - "$ENV_FILE" <<'PYEOF'
import sys
from tofu_dotenv import read_dotenv_values

print(read_dotenv_values(sys.argv[1]).get('PORT', ''))
PYEOF
    )"; then
    fail "Could not read PORT from existing ${ENV_FILE}"
elif [[ -z "$_EXISTING_ENV_PORT" ]]; then
    _set_env_var "PORT" "$PORT" "$ENV_FILE"
    info "Existing .env had no PORT; added default PORT=${PORT}"
elif [[ ! "$_EXISTING_ENV_PORT" =~ ^[0-9]+$ \
        || ${#_EXISTING_ENV_PORT} -gt 5 ]] \
        || (( 10#$_EXISTING_ENV_PORT < 1 || 10#$_EXISTING_ENV_PORT > 65535 )); then
    fail "Existing ${ENV_FILE} has invalid PORT; set an integer from 1 to 65535 or pass --port"
else
    PORT="$_EXISTING_ENV_PORT"
    info "Preserving existing PORT=${PORT} (pass --port to change it)"
fi
if [[ -n "$API_KEY" ]]; then
    _set_env_var "LLM_API_KEYS" "$API_KEY" "$ENV_FILE"
    ok "API key configured"
fi

# Publish only the current standalone deployment contract. Retired selectors
# from an existing .env must be removed because the production boot gate rejects
# them rather than guessing the operator's intended authority.
for retired_key in \
        TOFU_REQUIRE_PG TOFU_REPLICA_RING TOFU_STORAGE_MODE \
        CHATUI_STORAGE_MODE \
        TOFU_PG_PORT TOFU_PG_HOST TOFU_PG_REQUIRE_FLOCK \
        TOFU_STOP_PG_ON_EXIT TOFU_STORAGE_PG_PORT; do
    _unset_env_var "$retired_key" "$ENV_FILE"
done
while IFS= read -r retired_key; do
    _unset_env_var "$retired_key" "$ENV_FILE"
done < <(sed -nE \
    's/^[#[:space:]]*((TOFU|CHATUI)_DB_[A-Za-z0-9_]+)=.*/\1/p' \
    "$ENV_FILE")
_set_env_var "TOFU_DEPLOYMENT_MODE" "personal" "$ENV_FILE"
_set_env_var "TOFU_PROCESS_ROLE" "all" "$ENV_FILE"
info "TOFU_DEPLOYMENT_MODE=personal and TOFU_PROCESS_ROLE=all pinned in .env"

ok ".env ready (PORT=${PORT})"
# sed -i implementations may replace the inode. Reassert the credential-file
# boundary after every update instead of assuming they preserve its mode.
chmod 600 "$ENV_FILE" \
    || fail "Could not restrict ${ENV_FILE} to owner-only access (chmod 600)"


# ═══════════════════════════════════════════════════════════════
#  Step 9.5: Post-install Sidecar smoke test (write → read → delete)
#
#  Prove the SELECTED backend actually works on THIS machine before we
#  declare success — a semantic write/read/delete round-trip through the
#  authenticated storage.v1 Sidecar that the server will use.  The installer
#  never imports a database driver or opens the selected database itself.
#  Runs for BOTH the uv and conda paths (this is the shared tail; the conda
#  guard closed back before Step 8.5). Failure ABORTS the install (fail) with
#  a backend-specific hint. The temp table is dropped in a finally so no
#  _tofu_install_smoke residue is left in the user's real DB.
# ═══════════════════════════════════════════════════════════════
step "Verifying the storage Sidecar works (write → read → delete)"

_SMOKE_BACKEND="sqlite"

_SMOKE_TIMEOUT=""
command -v timeout >/dev/null 2>&1 && _SMOKE_TIMEOUT="timeout -k 5 60"

if (cd "$INSTALL_DIR" \
        && TOFU_DEPLOYMENT_MODE=personal TOFU_PROCESS_ROLE=all \
        $_SMOKE_TIMEOUT "$ENV_PYTHON" - <<'PYEOF'
import sys
import uuid
try:
    from lib.storage import StorageSupervisor
    key = 'install-' + uuid.uuid4().hex
    with StorageSupervisor(backend=None, startup_timeout=45) as storage:
        backend = storage.client.health()['backend']
        storage.client.command(
            'record.put',
            {'namespace': 'system.install_smoke', 'key': key, 'value': True},
            'put-' + key,
        )
        stored = storage.client.query(
            'record.get',
            {'namespace': 'system.install_smoke', 'key': key},
        )
        if stored is None or stored.get('value') is not True:
            raise RuntimeError('Sidecar read-back did not match the committed value')
        storage.client.command(
            'record.delete',
            {'namespace': 'system.install_smoke', 'key': key},
            'delete-' + key,
        )
        if storage.client.query(
                'record.get',
                {'namespace': 'system.install_smoke', 'key': key}) is not None:
            raise RuntimeError('Sidecar delete was not visible')
    print('  Storage smoke OK (backend=%s): semantic write/read/delete passed' % backend)
except Exception as e:
    sys.stderr.write('  Storage Sidecar smoke FAILED: %s\n' % e)
    sys.exit(1)
PYEOF
); then
    ok "Storage Sidecar verified (${_SMOKE_BACKEND}): semantic write/read/delete passed"
else
    fail "SQLite backend failed its post-install smoke test — check project disk space/write permissions. Full log: ${TOFU_INSTALL_LOG}"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 10: Launch or print completion
# ═══════════════════════════════════════════════════════════════
echo ""
ok "Files and environment installed successfully."
info "Full install log: $TOFU_INSTALL_LOG"
if [[ "$_SOURCE_UPDATE_FAILED" -eq 1 ]]; then
    warn "Source update was not applied; this run repaired the existing checkout only."
    printf '  Retry update after resolving network/local changes: git -C %q pull --ff-only\n' \
        "$INSTALL_DIR"
fi
echo ""

if [[ "$NO_LAUNCH" -eq 1 ]]; then
    ok "Installation complete (install-only mode; server not started)."
    echo "  Start later (.tofu_env.json selects the installed interpreter):"
    printf '    cd %q && %q server.py\n' "$INSTALL_DIR" "$ENV_PYTHON"
    if [[ "$_FAST_PATH_DONE" -eq 1 ]]; then
        echo "  Optional explicit activation: source \"${ENV_PREFIX}/bin/activate\""
    elif [[ "$CONDA_OWNED_BY_US" -eq 1 ]]; then
        echo "  Optional explicit activation: source \"${CONDA_BASE}/etc/profile.d/conda.sh\" && conda activate ${ENV_NAME}"
    else
        echo "  Optional explicit activation: conda activate ${ENV_NAME}"
    fi
    printf '  After starting, verify with: %q healthcheck.py --runtime\n' \
        "$ENV_PYTHON"
    exit 0
fi

# ── Port preflight ──────────────────────────────────────────
# A busy port previously failed only at server launch with a generic message.
# Probe it now so a collision fails fast with a clear hint (and the owning
# PID when ss/lsof can reveal it — best-effort, no hard dependency).
_port_is_free() {
    "$ENV_PYTHON" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind(('0.0.0.0', port))
except OSError:
    sys.exit(1)
finally:
    probe.close()
PYEOF
}

if ! _port_is_free; then
    _PORT_OWNER=""
    if command -v ss &>/dev/null; then
        _PORT_OWNER="$(ss -ltnp 2>/dev/null | grep -E ":${PORT}[[:space:]]" | head -n1 | sed 's/^[[:space:]]*//' || true)"
    elif command -v lsof &>/dev/null; then
        _PORT_OWNER="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -n1 || true)"
    fi
    if [[ -n "$_PORT_OWNER" ]]; then
        fail "Port ${PORT} is already in use: ${_PORT_OWNER} — choose a free port with --port"
    fi
    fail "Port ${PORT} is already in use — choose a free port with --port"
fi
step "Starting Tofu server"
echo ""
echo -e "  ${BOLD}🧈 Tofu is starting on port ${PORT}...${NC}"
echo ""

cd "$INSTALL_DIR"

# Use the interpreter we just installed and verified. The uv path intentionally
# does not activate its venv, and some hosts have only `python3` (or no system
# Python at all); relying on a bare `python` here either fails or runs the
# healthcheck outside the environment that owns Playwright and the app.
# Managed startup returns after the one worker is healthy, so keep this sequence
# foreground and deterministic and let scripts receive the real verdict.
if ! "$ENV_PYTHON" server.py; then
    echo "" >&2
    echo -e "  ${RED}✗${NC}  Tofu was installed, but managed startup failed." >&2
    printf '  Diagnose: %q serverctl.py doctor\n' "$ENV_PYTHON" >&2
    printf '  Logs:     %q serverctl.py logs\n' "$ENV_PYTHON" >&2
    echo "  Install:  ${TOFU_INSTALL_LOG}" >&2
    exit 1
fi

step "Verifying the running installation"
_RUNTIME_BROWSER_ARGS=()
if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
    _RUNTIME_BROWSER_ARGS+=(--require-browser)
fi
if "$ENV_PYTHON" healthcheck.py --runtime --port "${PORT}" --wait 15 \
        "${_RUNTIME_BROWSER_ARGS[@]}"; then
    :
else
    echo "" >&2
    echo -e "  ${RED}✗${NC}  Tofu started, but required runtime validation failed." >&2
    printf '  Re-run:   %q healthcheck.py --runtime --port %q\n' \
        "$ENV_PYTHON" "$PORT" >&2
    printf '  Diagnose: %q serverctl.py doctor\n' "$ENV_PYTHON" >&2
    printf '  Logs:     %q serverctl.py logs\n' "$ENV_PYTHON" >&2
    exit 1
fi

_RUNTIME_URL="$(
    "$ENV_PYTHON" serverctl.py status --json 2>/dev/null \
        | "$ENV_PYTHON" -c \
            'import json,sys; print(json.load(sys.stdin).get("applicationUrl") or "")' \
            2>/dev/null
)" || _RUNTIME_URL=""
[[ -n "$_RUNTIME_URL" ]] || _RUNTIME_URL="http://localhost:${PORT}"

echo ""
ok "Installation complete — Tofu is ready: ${_RUNTIME_URL}"
echo "  Open:     ${_RUNTIME_URL}"
printf '  Status:   %q serverctl.py status\n' "$ENV_PYTHON"
printf '  Stop:     %q serverctl.py stop\n' "$ENV_PYTHON"
printf '  Restart:  %q serverctl.py restart\n' "$ENV_PYTHON"
