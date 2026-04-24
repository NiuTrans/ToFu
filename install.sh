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
#    --dir <path>       Install directory (default: ~/tofu)
#    --env <name>       Conda env name (default: tofu)
#    --port <n>         Server port (default: 15000)
#    --api-key <key>    Pre-configure LLM API key
#    --no-launch        Install only, don't start
#    --skip-playwright  Skip Playwright browser install
#    --no-update-conda  Skip conda self-update
#    --reset-env        Delete the existing conda env and recreate from scratch
#                       (⚠️  DESTRUCTIVE: removes ANY extra packages the user
#                        installed into this env. Only use for your own env.)
#
#  This script relies ENTIRELY on conda (conda-forge). It:
#    1. Installs Miniforge if no conda is found
#    2. Updates conda itself (outdated conda causes many solver issues)
#    3. Clones the repo if needed
#    4. Creates a fresh conda env with Python 3.10+
#    5. Installs ALL Python dependencies from conda-forge (no pip)
#    6. Installs ripgrep, fd-find, and Chromium shared libs from conda-forge
#    7. Installs the Playwright Chromium browser binary
#    8. Launches the server
#
#  For Windows, use install.ps1 instead.
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
NO_UPDATE_CONDA=0
RESET_ENV=0

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
        --no-update-conda)  NO_UPDATE_CONDA=1; shift ;;
        --reset-env)        RESET_ENV=1; shift ;;
        *)  FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# ── Banner ──────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}🧈 Tofu (豆腐) — Self-Hosted AI Assistant${NC}"
echo -e "  ─────────────────────────────────────────"
echo -e "  Conda-based installer"
echo ""

# ── Platform check ──────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)   PLATFORM="Linux" ;;
    Darwin)  PLATFORM="MacOSX" ;;
    *)       fail "Unsupported OS: $OS (use install.ps1 on Windows)" ;;
esac
info "Platform: $OS $ARCH"

# ═══════════════════════════════════════════════════════════════
#  Step 1: Locate or install conda (Miniforge)
# ═══════════════════════════════════════════════════════════════
step "Locating conda"

CONDA_BIN=""
if command -v conda &>/dev/null; then
    CONDA_BIN="$(command -v conda)"
    ok "Found conda at $CONDA_BIN"
elif command -v mamba &>/dev/null; then
    # If mamba is on PATH without conda, find the base conda it shipped with
    CONDA_BIN="$(command -v mamba)"
    ok "Found mamba at $CONDA_BIN (will use with conda fallback)"
elif [[ -x "${HOME}/miniforge3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/miniforge3/bin/conda"
    ok "Found existing Miniforge at ${HOME}/miniforge3"
elif [[ -x "${HOME}/miniconda3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/miniconda3/bin/conda"
    ok "Found existing Miniconda at ${HOME}/miniconda3"
elif [[ -x "${HOME}/anaconda3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/anaconda3/bin/conda"
    ok "Found existing Anaconda at ${HOME}/anaconda3"
else
    info "No conda found — installing Miniforge (conda-forge by default)..."
    MINIFORGE_DIR="${HOME}/miniforge3"
    MF_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-${PLATFORM}-${ARCH}.sh"
    TMP_INSTALLER="$(mktemp -t miniforge.XXXXXX.sh)"
    trap 'rm -f "$TMP_INSTALLER"' EXIT

    info "Downloading $MF_URL"
    if command -v curl &>/dev/null; then
        curl -fsSL "$MF_URL" -o "$TMP_INSTALLER"
    elif command -v wget &>/dev/null; then
        wget -q "$MF_URL" -O "$TMP_INSTALLER"
    else
        fail "Need curl or wget to download Miniforge"
    fi

    bash "$TMP_INSTALLER" -b -p "$MINIFORGE_DIR"
    CONDA_BIN="${MINIFORGE_DIR}/bin/conda"
    [[ -x "$CONDA_BIN" ]] || fail "Miniforge install did not produce $CONDA_BIN"
    ok "Miniforge installed at $MINIFORGE_DIR"
fi

# Activate conda for this shell (needed for `conda activate`)
CONDA_BASE="$("$CONDA_BIN" info --base 2>/dev/null)"
[[ -n "$CONDA_BASE" ]] || fail "Could not determine conda base directory"
# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

# ═══════════════════════════════════════════════════════════════
#  Step 2: Update conda FIRST (outdated conda = solver hangs)
#
#  This MUST run before any other conda command touches an env.
#  Classic symptoms of an outdated conda:
#    - "Solving environment: \\ " spinning forever
#    - "PackagesNotFoundError" for packages that clearly exist
#    - libmamba plugin errors
# ═══════════════════════════════════════════════════════════════
if [[ "$NO_UPDATE_CONDA" -eq 0 ]]; then
    step "Updating conda (MUST happen before anything else)"
    OLD_VER="$(conda --version 2>/dev/null || echo unknown)"
    info "Current version: ${OLD_VER}"

    # Always update from conda-forge to get latest solver (libmamba) fixes.
    if conda update -n base -c conda-forge --override-channels -y conda; then
        NEW_VER="$(conda --version 2>/dev/null || echo unknown)"
        if [[ "$OLD_VER" == "$NEW_VER" ]]; then
            ok "conda already up to date (${NEW_VER})"
        else
            ok "conda updated: ${OLD_VER} → ${NEW_VER}"
        fi
    else
        warn "conda self-update failed — this is NOT fatal but may cause solver issues later"
        warn "If the next steps hang on 'Solving environment', re-run with updated conda:"
        warn "  conda update -n base -c conda-forge --override-channels -y conda"
    fi

    # Ensure libmamba solver is installed and set as default — it's 10x faster
    # and avoids many classic-solver hangs/failures. This is CRITICAL on
    # large conda-forge envs (hundreds of packages with interlocking deps).
    info "Ensuring libmamba solver is installed..."
    if conda install -n base -c conda-forge --override-channels -y conda-libmamba-solver >/dev/null 2>&1; then
        conda config --set solver libmamba || true
        ok "libmamba solver active (10x faster than classic)"
    else
        warn "Could not install libmamba solver — using classic (slower)"
    fi
else
    warn "Skipping conda self-update (--no-update-conda)"
    warn "If you hit solver hangs or 'PackagesNotFoundError', remove --no-update-conda and retry."
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

# Activate it for subsequent installs
conda activate "$ENV_NAME"
PY="$(command -v python)"
ok "Using Python: $PY ($(python --version 2>&1))"

# ═══════════════════════════════════════════════════════════════
#  Step 5: Install Python dependencies via conda-forge
# ═══════════════════════════════════════════════════════════════
step "Installing Python dependencies from conda-forge"

# Map requirements.txt → conda-forge package names. Most match 1:1.
# Notable: flask-compress → flask-compress; python-pptx → python-pptx;
# Pillow → pillow (conda is case-insensitive on install).
CONDA_PKGS=(
    "flask>=3.0"
    "flask-compress>=1.14"
    "requests>=2.31"
    "psutil>=5.9"
    "trafilatura>=1.6"
    "playwright>=1.40"
    "pillow>=10.0"
    "python-pptx>=0.6.21"
    "lxml>=5.3"
    # BS4 — HTML fallback parser in lib/fetch/html_extract.py
    "beautifulsoup4>=4.12"
    # python-dateutil — eagerly imported by lib/fetch/html_extract.py
    "python-dateutil>=2.8"
    # Office document parsers for lib/doc_parser.py (upload pipeline)
    "python-docx>=1.0"
    "openpyxl>=3.1"
    "xlrd>=2.0"
    "olefile>=0.46"
    "mcp>=1.0"
    # PDF parsing (fitz) — used in lib/pdf_parser and routes/paper
    "pymupdf>=1.24"
    # uv / uvx — used by lib/mcp/client.py to launch MCP servers
    "uv>=0.4"
)

# Pip-only deps — not available on conda-forge, installed via pip INTO the env.
PIP_ONLY_PKGS=(
    "pymupdf4llm>=0.0.17"
)

# ── Heal broken envs: remove any pip-installed versions of these deps ──
# A common failure mode on older hosts (CentOS 7 / glibc 2.17) is that an
# earlier run left pip's manylinux wheel of lxml in the env. That wheel
# links to GLIBC_2.25+ and crashes at import. We uninstall any pip copies
# first so conda-forge's (sysroot-linked) version is the one used.
info "Purging any pip-installed copies that would shadow conda-forge..."
PIP_NAMES=(flask flask-compress Flask-Compress requests psutil trafilatura
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
CONDA_CONFLICT_PKGS=(
    postgresql psycopg2
    trafilatura htmldate
    lxml libxml2 libxml2-16 libxslt
    icu
)
conda remove -n "$ENV_NAME" -y --force "${CONDA_CONFLICT_PKGS[@]}" >/dev/null 2>&1 || true
ok "Conflict-prone packages cleared (will reinstall below)"

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
        conda deactivate >/dev/null 2>&1 || true
        conda env remove -n "$ENV_NAME" -y
        conda create -n "$ENV_NAME" -c conda-forge --override-channels -y "python=${PY_VER}"
        conda activate "$ENV_NAME"
        PY="$(command -v python)"
        ok "Env '${ENV_NAME}' rebuilt with fresh Python ${PY_VER}"
        _install_main_deps
    fi
fi
ok "Python dependencies installed"

# ── Install pip-only deps (e.g. pymupdf4llm) into the conda env ──
# pymupdf4llm is not shipped on conda-forge; it's a thin LLM-oriented Markdown
# extractor built on top of pymupdf (which we just installed via conda).
if [[ ${#PIP_ONLY_PKGS[@]} -gt 0 ]]; then
    info "Installing pip-only deps (not on conda-forge): ${PIP_ONLY_PKGS[*]}"
    if python -m pip install --no-deps --upgrade "${PIP_ONLY_PKGS[@]}"; then
        ok "Pip-only deps installed"
    else
        warn "pip install --no-deps failed — retrying with dependency resolution"
        if python -m pip install --upgrade "${PIP_ONLY_PKGS[@]}"; then
            ok "Pip-only deps installed (with dependency resolution)"
        else
            warn "Pip-only deps install failed — some PDF features may be degraded"
        fi
    fi
fi

# ── Install PostgreSQL + psycopg2 from conda-forge (optional but recommended) ──
# tofu uses PG for better concurrency (100+ concurrent users), auto-falls back
# to SQLite if PG is missing. Installing from conda-forge gives a rootless,
# userspace PG that auto-bootstraps at first run.
info "Installing PostgreSQL + psycopg2 from conda-forge (for multi-user concurrency)..."
if conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
        'postgresql>=16' 'psycopg2>=2.9' >/dev/null 2>&1; then
    ok "PostgreSQL + psycopg2 installed (will auto-bootstrap on first run)"
else
    warn "Could not install PostgreSQL — tofu will fall back to SQLite (fine for <100 users)"
fi

# ── Verify lxml imports (catches glibc mismatches immediately) ──
info "Verifying lxml + trafilatura import correctly..."
if python -c "import lxml.etree, trafilatura; print('lxml', lxml.__version__, 'trafilatura', trafilatura.__version__)"; then
    ok "Import check passed"
else
    warn "lxml/trafilatura failed to import."
    warn "If you see 'GLIBC_2.xx not found', a pip wheel is still shadowing conda's copy."
    warn "Try: conda activate ${ENV_NAME} && pip uninstall -y lxml && conda install -c conda-forge --force-reinstall lxml"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 6: Verify SQLite (built into Python)
# ═══════════════════════════════════════════════════════════════
step "Checking SQLite"
SQLITE_VER="$(python -c 'import sqlite3; print(sqlite3.sqlite_version)')"
ok "SQLite $SQLITE_VER (built into Python)"

# ═══════════════════════════════════════════════════════════════
#  Step 7: Install ripgrep & fd-find from conda-forge (fast search)
# ═══════════════════════════════════════════════════════════════
step "Installing ripgrep + fd-find (fast code/file search)"
if conda install -n "$ENV_NAME" -c conda-forge --override-channels -y ripgrep fd-find; then
    ok "ripgrep + fd-find installed"
else
    warn "ripgrep/fd-find install failed — code search will fall back to grep / os.walk"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8: Playwright — Chromium browser + shared libs (rootless)
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
    step "Installing Playwright Chromium"

    # On Linux, install Chromium's shared libs from conda-forge so that no
    # sudo / system packages are required. lib/fetch/playwright_pool.py
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

    info "Downloading Chromium browser binary via playwright..."
    if python -m playwright install chromium; then
        ok "Playwright Chromium installed"
    else
        warn "Playwright Chromium install failed (non-critical — fetching still works via requests)"
    fi
else
    info "Skipping Playwright (--skip-playwright)"
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
ok ".env ready (PORT=${PORT})"

# ═══════════════════════════════════════════════════════════════
#  Step 10: Launch or print completion
# ═══════════════════════════════════════════════════════════════
echo ""
ok "Installation complete!"
echo ""
echo "  To activate this env later:"
echo "    conda activate ${ENV_NAME}"
echo "    cd ${INSTALL_DIR}"
echo "    python server.py"
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
