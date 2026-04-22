#!/usr/bin/env python3
"""install.py — Cross-platform conda-based installer for Tofu (豆腐).

Works on Linux, macOS, and Windows. Relies ENTIRELY on conda (conda-forge)
for both the Python environment and all dependencies — no pip, no venv,
no system package managers, no root/admin.

Usage:
    python install.py                     # Install and start
    python install.py --no-launch         # Install only
    python install.py --port 8080         # Custom port
    python install.py --api-key sk-xxx    # Pre-configure API key
    python install.py --env tofu          # Conda env name (default: tofu)
    python install.py --python 3.12       # Python version (default: 3.12)
    python install.py --no-update-conda   # Skip conda self-update
    python install.py --docker            # Use Docker instead

What it does:
    1. Locates conda (installs Miniforge if missing)
    2. Updates conda itself (outdated conda causes solver issues)
    3. Installs libmamba solver and sets it as default
    4. Clones the repository if needed
    5. Creates a conda env with the requested Python version
    6. Installs all Python dependencies from conda-forge (no pip)
    7. Installs ripgrep + fd-find from conda-forge
    8. Installs Playwright Chromium + shared-lib deps from conda-forge
    9. Creates .env from template
   10. Launches the server

The script is idempotent — safe to run multiple times.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request

# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

REPO_URL = "https://github.com/rangehow/ToFu.git"
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

DEFAULT_ENV_NAME = "tofu"
DEFAULT_PY_VER = "3.12"

# Python dependencies — mirrors requirements.txt, resolved from conda-forge.
CONDA_PYTHON_DEPS = [
    "flask>=3.0",
    "flask-compress>=1.14",
    "requests>=2.31",
    "psutil>=5.9",
    "trafilatura>=1.6",
    "playwright>=1.40",
    "pillow>=10.0",
    "python-pptx>=0.6.21",
    "lxml>=4.9",
    "mcp>=1.0",
]

# Rootless Chromium shared-lib dependencies (Linux only). Matches the packages
# lib/fetch/playwright_pool.py expects on $CONDA_PREFIX/lib.
CHROMIUM_CONDA_DEPS = [
    "atk-1.0",
    "at-spi2-atk",
    "at-spi2-core",
    "alsa-lib",
    "xorg-libxcomposite",
    "xorg-libxdamage",
    "xorg-libxfixes",
    "xorg-libxrandr",
    "libxkbcommon",
    "nspr",
    "nss",
    "mesa-libgbm-cos7-x86_64",
]


# ═══════════════════════════════════════════════════════════════
#  Terminal output helpers
# ═══════════════════════════════════════════════════════════════

_COLORS_ENABLED = False


def _enable_ansi_windows():
    """Enable ANSI escape codes on Windows 10+."""
    global _COLORS_ENABLED
    if not IS_WINDOWS:
        _COLORS_ENABLED = True
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        _COLORS_ENABLED = True
    except Exception:
        _COLORS_ENABLED = False


_enable_ansi_windows()


def _c(code: str, text: str) -> str:
    if not _COLORS_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def info(msg: str): print(f"  {_c('34', 'ℹ')}  {msg}")
def ok(msg: str):   print(f"  {_c('32', '✓')}  {msg}")
def warn(msg: str): print(f"  {_c('33', '!')}  {msg}")
def fail(msg: str):
    print(f"  {_c('31', '✗')}  {msg}")
    sys.exit(1)

def step(msg: str):
    print(f"\n  {_c('1;36', '▸')} {_c('1', msg)}")


# ═══════════════════════════════════════════════════════════════
#  Utility
# ═══════════════════════════════════════════════════════════════

def run(cmd: list[str], check: bool = True, capture: bool = False,
        env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it first."""
    display = " ".join(cmd)
    info(f"$ {display}")
    merged_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run(
            cmd, check=check, capture_output=capture,
            text=True, env=merged_env, **kwargs
        )
    except FileNotFoundError:
        fail(f"Command not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        if check:
            fail(f"Command failed (exit {e.returncode}): {display}")
        return e  # type: ignore[return-value]


def which(name: str) -> str | None:
    return shutil.which(name)


# ═══════════════════════════════════════════════════════════════
#  Step 1: Locate or install conda (Miniforge)
# ═══════════════════════════════════════════════════════════════

def _candidate_conda_paths() -> list[str]:
    """Standard install locations for conda, ordered by preference."""
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        exe = "Scripts\\conda.exe"
        return [
            os.path.join(home, "miniforge3", exe),
            os.path.join(home, "Miniforge3", exe),
            os.path.join(home, "miniconda3", exe),
            os.path.join(home, "Miniconda3", exe),
            os.path.join(home, "anaconda3", exe),
            os.path.join(home, "Anaconda3", exe),
        ]
    return [
        os.path.join(home, "miniforge3", "bin", "conda"),
        os.path.join(home, "miniconda3", "bin", "conda"),
        os.path.join(home, "anaconda3", "bin", "conda"),
        "/opt/conda/bin/conda",
    ]


def _miniforge_url() -> str:
    """Build the Miniforge installer URL for this platform."""
    arch = platform.machine()
    if IS_WINDOWS:
        # Miniforge only ships x86_64 installers for Windows
        fname = "Miniforge3-Windows-x86_64.exe"
    elif IS_MACOS:
        fname = f"Miniforge3-MacOSX-{arch}.sh"
    elif IS_LINUX:
        fname = f"Miniforge3-Linux-{arch}.sh"
    else:
        fail(f"Unsupported platform: {sys.platform} / {arch}")
    return f"https://github.com/conda-forge/miniforge/releases/latest/download/{fname}"


def _install_miniforge() -> str:
    """Download and run the Miniforge installer. Returns path to conda exe."""
    home = os.path.expanduser("~")
    install_prefix = os.path.join(home, "miniforge3")
    if os.path.exists(install_prefix):
        fail(
            f"{install_prefix} already exists but conda was not found inside it.\n"
            f"   Remove the directory or pass --env/--dir to use an existing install."
        )

    url = _miniforge_url()
    info(f"Downloading Miniforge installer from {url}")

    suffix = ".exe" if IS_WINDOWS else ".sh"
    fd, tmp_path = tempfile.mkstemp(prefix="miniforge-", suffix=suffix)
    os.close(fd)

    try:
        urllib.request.urlretrieve(url, tmp_path)
        if IS_WINDOWS:
            # Silent install to ~/miniforge3
            info("Running Miniforge silent installer...")
            run([
                tmp_path, "/InstallationType=JustMe", "/RegisterPython=0",
                "/S", f"/D={install_prefix}",
            ])
            conda_exe = os.path.join(install_prefix, "Scripts", "conda.exe")
        else:
            os.chmod(tmp_path, 0o755)
            info("Running Miniforge installer (batch mode)...")
            run(["bash", tmp_path, "-b", "-p", install_prefix])
            conda_exe = os.path.join(install_prefix, "bin", "conda")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not os.path.isfile(conda_exe):
        fail(f"Miniforge install did not produce {conda_exe}")
    ok(f"Miniforge installed at {install_prefix}")
    return conda_exe


def locate_conda() -> str:
    """Find an existing conda, or install Miniforge. Returns path to conda exe."""
    step("Locating conda")

    # PATH lookup
    conda = which("conda")
    if conda:
        ok(f"Found conda on PATH: {conda}")
        return conda

    # Active CONDA_EXE env var (set when a conda env is activated)
    conda_env = os.environ.get("CONDA_EXE")
    if conda_env and os.path.isfile(conda_env):
        ok(f"Found conda via $CONDA_EXE: {conda_env}")
        return conda_env

    # Known install locations
    for cand in _candidate_conda_paths():
        if os.path.isfile(cand):
            ok(f"Found conda at {cand}")
            return cand

    # Need to install it
    info("No conda installation found — installing Miniforge (conda-forge)...")
    return _install_miniforge()


# ═══════════════════════════════════════════════════════════════
#  Step 2: Update conda itself
# ═══════════════════════════════════════════════════════════════

def update_conda(conda: str):
    """Update conda from conda-forge and enable libmamba solver.

    This MUST run before any other conda command touches an env.
    Outdated versions of conda commonly:
      - spin forever on 'Solving environment: \\ '
      - raise PackagesNotFoundError for packages that clearly exist
      - fail libmamba plugin initialization

    So we always refresh first.
    """
    step("Updating conda (MUST happen before anything else)")

    # Show current version
    old_ver = run([conda, "--version"], check=False, capture=True)
    old_str = old_ver.stdout.strip() if old_ver.returncode == 0 else "unknown"
    info(f"Current: {old_str}")

    res = run(
        [conda, "update", "-n", "base", "-c", "conda-forge", "-y", "conda"],
        check=False, capture=True,
    )
    if res.returncode != 0:
        warn("conda self-update failed — this is NOT fatal but may cause solver issues later")
        warn("If the next steps hang on 'Solving environment', re-run after:")
        warn(f"  {conda} update -n base -c conda-forge -y conda")
    else:
        new_ver = run([conda, "--version"], check=False, capture=True)
        new_str = new_ver.stdout.strip() if new_ver.returncode == 0 else "latest"
        if old_str == new_str:
            ok(f"conda already up to date ({new_str})")
        else:
            ok(f"conda updated: {old_str} → {new_str}")

    # Install and activate libmamba solver (10x faster, fixes many classic
    # solver hangs on big conda-forge envs). This is CRITICAL on large
    # conda-forge envs (hundreds of packages with interlocking deps).
    info("Ensuring libmamba solver is installed and active...")
    res = run(
        [conda, "install", "-n", "base", "-c", "conda-forge", "-y",
         "conda-libmamba-solver"],
        check=False, capture=True,
    )
    if res.returncode == 0:
        run([conda, "config", "--set", "solver", "libmamba"], check=False, capture=True)
        ok("libmamba solver active (10x faster than classic)")
    else:
        warn("Could not install libmamba solver — using classic (slower)")


# ═══════════════════════════════════════════════════════════════
#  Step 3: Get source code
# ═══════════════════════════════════════════════════════════════

def get_source(conda: str, install_dir: str):
    step("Getting Tofu source code")

    if os.path.isfile(os.path.join(install_dir, "server.py")):
        if os.path.isdir(os.path.join(install_dir, ".git")):
            info("Updating existing installation...")
            git = which("git") or _install_git_via_conda(conda)
            if git:
                result = run([git, "pull", "--ff-only"], check=False,
                             capture=True, cwd=install_dir)
                if result.returncode != 0:
                    warn("Could not auto-update (you may have local changes)")
        ok(f"Source ready at {install_dir}")
        return

    git = which("git") or _install_git_via_conda(conda)
    if not git:
        fail("Git is required to clone the repository.")

    info(f"Cloning to {install_dir}...")
    run([git, "clone", REPO_URL, install_dir])
    ok(f"Source ready at {install_dir}")


def _install_git_via_conda(conda: str) -> str | None:
    """Install git into base env via conda-forge when missing on PATH."""
    info("git not found — installing via conda-forge into base env...")
    res = run(
        [conda, "install", "-n", "base", "-c", "conda-forge", "-y", "git"],
        check=False, capture=True,
    )
    if res.returncode != 0:
        warn("Failed to install git via conda")
        return None
    return which("git")


# ═══════════════════════════════════════════════════════════════
#  Step 4: Create / reuse conda env
# ═══════════════════════════════════════════════════════════════

def _env_exists(conda: str, name: str) -> bool:
    res = run([conda, "env", "list"], check=False, capture=True)
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts and parts[0] == name:
            return True
    return False


def _env_python(conda: str, env_name: str) -> str:
    """Return path to the python executable inside the named conda env."""
    res = run(
        [conda, "run", "-n", env_name, "python", "-c",
         "import sys; print(sys.executable)"],
        check=True, capture=True,
    )
    return res.stdout.strip()


def setup_env(conda: str, env_name: str, py_ver: str,
              reset: bool = False) -> str:
    """Create (if needed) the conda env and return the python path inside it.

    If ``reset`` is True and the env already exists, remove it first. This is
    destructive — any user-installed extras in that env are wiped.
    """
    step(f"Creating conda environment: {env_name}")

    exists = _env_exists(conda, env_name)
    if exists and reset:
        warn(f"--reset-env: removing existing env '{env_name}' "
             "(this deletes ALL packages in it)")
        run([conda, "env", "remove", "-n", env_name, "-y"], check=False)
        exists = False

    if exists:
        ok(f"Env '{env_name}' already exists — will update in place")
        info("(tip: re-run with --reset-env to wipe and rebuild it from scratch)")
    else:
        info(f"Creating env '{env_name}' with Python {py_ver}...")
        run([conda, "create", "-n", env_name, "-c", "conda-forge", "-y",
             f"python={py_ver}"])
        ok(f"Env '{env_name}' created")

    py = _env_python(conda, env_name)
    ok(f"Python: {py}")
    return py


# ═══════════════════════════════════════════════════════════════
#  Step 5: Install Python dependencies from conda-forge
# ═══════════════════════════════════════════════════════════════

# Pip distribution names of every dep we manage via conda. When an env has
# leftover pip-installed wheels for any of these (common culprit: lxml,
# which ships manylinux wheels linked against a newer glibc), we must
# uninstall them first — otherwise conda happily no-ops and the env keeps
# the broken pip copy. See the GLIBC_2.25 crash on CentOS-7 hosts.
_PIP_DIST_NAMES = [
    "flask",
    "flask-compress", "Flask-Compress",
    "requests",
    "psutil",
    "trafilatura",
    "playwright",
    "pillow", "Pillow",
    "python-pptx",
    "lxml",
    "mcp",
]


def _purge_pip_installed_deps(conda: str, env_name: str, py: str):
    """Remove any pip-installed copies of our target deps from the env.

    Without this, a previously-broken install (e.g. pip's lxml manylinux
    wheel that requires a newer glibc) will shadow conda-forge's version
    and the env stays broken.
    """
    info("Checking for pip-installed deps that would shadow conda-forge…")
    # `pip list --format=freeze` emits `name==version` lines — fast and parse-safe.
    res = run([py, "-m", "pip", "list", "--format=freeze"],
              check=False, capture=True)
    if res.returncode != 0:
        info("pip list failed — skipping purge step")
        return

    installed = set()
    for line in res.stdout.splitlines():
        name = line.split("==", 1)[0].strip().lower()
        if name:
            installed.add(name)

    to_remove = sorted({n for n in _PIP_DIST_NAMES if n.lower() in installed})
    if not to_remove:
        ok("No pip-installed deps to purge")
        return

    info(f"Uninstalling pip copies (will be reinstalled from conda-forge): {to_remove}")
    run([py, "-m", "pip", "uninstall", "-y", *to_remove],
        check=False, capture=True)
    ok(f"Purged {len(to_remove)} pip-installed dep(s)")


def install_deps(conda: str, env_name: str, install_dir: str, py: str):
    step("Installing Python dependencies from conda-forge")
    req_file = os.path.join(install_dir, "requirements.txt")
    if not os.path.isfile(req_file):
        warn("No requirements.txt found — skipping")
        return

    # Heal broken envs: remove any pip-installed versions of our deps so
    # conda-forge's versions are the ones that actually get loaded.
    _purge_pip_installed_deps(conda, env_name, py)

    info(f"Packages: {', '.join(CONDA_PYTHON_DEPS)}")
    # --force-reinstall: make sure conda actually re-lays-down the files even
    # if its metadata still thinks the package is satisfied (common right
    # after a pip-uninstall — conda's view of the env can be stale).
    run([conda, "install", "-n", env_name, "-c", "conda-forge", "-y",
         "--force-reinstall", *CONDA_PYTHON_DEPS])
    ok("Python dependencies installed from conda-forge")

    # PostgreSQL + psycopg2 (optional but recommended for concurrency).
    # tofu auto-falls back to SQLite if PG is missing — but installing it
    # here means users with 100+ concurrent sessions get better performance
    # out of the box. The PG instance is rootless and auto-bootstraps.
    info("Installing PostgreSQL + psycopg2 from conda-forge (for multi-user concurrency)...")
    res = run([conda, "install", "-n", env_name, "-c", "conda-forge", "-y",
               "postgresql>=16", "psycopg2>=2.9"], check=False, capture=True)
    if res.returncode == 0:
        ok("PostgreSQL + psycopg2 installed (will auto-bootstrap on first run)")
    else:
        warn("Could not install PostgreSQL — tofu will use SQLite "
             "(fine for <100 users)")

    # Verify lxml imports — catches glibc mismatch immediately instead of
    # at server startup. If this fails, the env is still broken and the
    # user knows why.
    info("Verifying lxml + trafilatura import correctly…")
    verify_cmd = [py, "-c",
        "import lxml.etree, trafilatura; "
        "print('lxml', lxml.__version__, 'trafilatura', trafilatura.__version__)"]
    res = run(verify_cmd, check=False, capture=True)
    if res.returncode == 0:
        ok(f"Import check: {res.stdout.strip()}")
    else:
        warn("lxml/trafilatura import check failed:")
        print(res.stdout)
        print(res.stderr)
        warn("If you see a GLIBC version error, the env still has a bad pip-installed")
        warn("wheel. Try: conda activate " + env_name + " && pip uninstall -y lxml && "
             "conda install -c conda-forge --force-reinstall lxml")


# ═══════════════════════════════════════════════════════════════
#  Step 6: Verify SQLite
# ═══════════════════════════════════════════════════════════════

def check_sqlite(py: str):
    step("Checking SQLite")
    res = run([py, "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
              check=True, capture=True)
    ok(f"SQLite {res.stdout.strip()} (built into Python)")


# ═══════════════════════════════════════════════════════════════
#  Step 7: Install ripgrep + fd-find
# ═══════════════════════════════════════════════════════════════

def install_search_tools(conda: str, env_name: str):
    step("Installing ripgrep + fd-find (fast code/file search)")
    res = run([conda, "install", "-n", env_name, "-c", "conda-forge", "-y",
               "ripgrep", "fd-find"], check=False, capture=True)
    if res.returncode == 0:
        ok("ripgrep + fd-find installed via conda-forge")
    else:
        warn("ripgrep/fd-find install failed — tools will fall back to grep / os.walk")


# ═══════════════════════════════════════════════════════════════
#  Step 8: Playwright (browser + shared libs)
# ═══════════════════════════════════════════════════════════════

def install_playwright(conda: str, env_name: str, py: str):
    step("Installing Playwright Chromium (optional)")

    # On Linux, install Chromium's shared libs from conda-forge (rootless).
    # lib/fetch/playwright_pool.py auto-prepends $CONDA_PREFIX/lib to
    # LD_LIBRARY_PATH at runtime.
    if IS_LINUX:
        info("Installing Chromium shared-lib deps from conda-forge (rootless)...")
        res = run(
            [conda, "install", "-n", env_name, "-c", "conda-forge", "-y",
             *CHROMIUM_CONDA_DEPS],
            check=False, capture=True,
        )
        if res.returncode == 0:
            ok("Chromium shared libs installed into conda env")
        else:
            warn("Some Chromium shared-lib deps failed — browser may not launch")

    info("Downloading Chromium browser binary via playwright...")
    res = run([py, "-m", "playwright", "install", "chromium"],
              check=False, capture=True)
    if res.returncode == 0:
        ok("Playwright Chromium installed")
    else:
        warn("Playwright Chromium install failed (non-critical)")
        info("Web fetching will use requests + trafilatura instead.")


# ═══════════════════════════════════════════════════════════════
#  Step 9: Configure .env
# ═══════════════════════════════════════════════════════════════

def configure_env_file(install_dir: str, port: int, api_key: str | None):
    step("Configuring .env")

    env_file = os.path.join(install_dir, ".env")
    env_example = os.path.join(install_dir, ".env.example")

    if not os.path.isfile(env_file):
        if os.path.isfile(env_example):
            shutil.copy2(env_example, env_file)
            info("Created .env from template")
        else:
            with open(env_file, "w") as f:
                f.write(f"PORT={port}\n")
                f.write("BIND_HOST=0.0.0.0\n")
            info("Created minimal .env")

    _update_env_var(env_file, "PORT", str(port))
    if api_key:
        _update_env_var(env_file, "LLM_API_KEYS", api_key)
        ok("API key configured")
    ok(f".env ready (PORT={port})")


def _update_env_var(env_file: str, key: str, value: str):
    lines = []
    found = False

    if os.path.isfile(env_file):
        with open(env_file) as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(env_file, "w") as f:
        f.writelines(new_lines)


# ═══════════════════════════════════════════════════════════════
#  Docker path
# ═══════════════════════════════════════════════════════════════

def docker_install(conda: str, install_dir: str, port: int, api_key: str | None):
    step("Docker installation")

    docker = which("docker")
    if not docker:
        fail(
            "Docker is not installed.\n"
            "   Get it from: https://docs.docker.com/get-docker/"
        )

    get_source(conda, install_dir)
    os.chdir(install_dir)

    os.environ["PORT"] = str(port)
    if api_key:
        os.environ["LLM_API_KEYS"] = api_key

    run(["docker", "compose", "up", "-d"], cwd=install_dir)

    print()
    ok("Tofu is running via Docker!")
    print(f"   Open {_c('1', f'http://localhost:{port}')} in your browser")
    print("   Configure API keys in Settings → Providers")
    print()
    print("   Logs:    docker compose logs -f tofu")
    print("   Stop:    docker compose down")
    print("   Update:  docker compose pull && docker compose up -d")
    print()


# ═══════════════════════════════════════════════════════════════
#  Launch / completion
# ═══════════════════════════════════════════════════════════════

def launch(conda: str, env_name: str, py: str, install_dir: str, port: int):
    step("Starting Tofu server")
    print()
    print(f"  {_c('1', '🧈 Tofu is starting on port')} {_c('1', str(port))}...")
    print(f"  Open {_c('1', f'http://localhost:{port}')} in your browser")
    print()
    print(f"  {_c('36', 'First launch:')}  Database auto-initializes instantly")
    print(f"  {_c('36', 'Configure:')}     Click ⚙️ Settings → Providers to add your LLM API keys")
    print(f"  {_c('36', 'Env:')}           conda activate {env_name}")
    print()
    print("  Press Ctrl+C to stop the server")
    print()

    os.chdir(install_dir)
    # Exec the env's python directly — no activation needed.
    if IS_WINDOWS:
        # os.execv on Windows has quoting quirks; use subprocess + exit
        try:
            proc = subprocess.run([py, "server.py"], cwd=install_dir)
            sys.exit(proc.returncode)
        except KeyboardInterrupt:
            sys.exit(130)
    else:
        os.execv(py, [py, "server.py"])


def print_install_complete(env_name: str, install_dir: str, port: int):
    print()
    ok("Installation complete!")
    print()
    print("  To start Tofu:")
    print(f"    conda activate {env_name}")
    print(f"    cd {install_dir}")
    print("    python server.py")
    print()
    print(f"  Then open {_c('1', f'http://localhost:{port}')} in your browser")
    print()


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Tofu (豆腐) — Conda-based cross-platform installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python install.py                          # Install and start
              python install.py --port 8080              # Custom port
              python install.py --api-key sk-xxx         # Pre-configure API key
              python install.py --env tofu --python 3.12 # Custom env / Python
              python install.py --no-launch              # Install only
              python install.py --no-update-conda        # Skip conda self-update
              python install.py --docker                 # Use Docker
              python install.py --skip-playwright        # Skip Playwright
        """),
    )
    parser.add_argument("--dir", default=None,
                        help="Install directory (default: current dir or ~/tofu)")
    parser.add_argument("--env", default=DEFAULT_ENV_NAME,
                        help=f"Conda env name (default: {DEFAULT_ENV_NAME})")
    parser.add_argument("--python", default=DEFAULT_PY_VER, dest="py_ver",
                        help=f"Python version (default: {DEFAULT_PY_VER})")
    parser.add_argument("--port", type=int, default=15000,
                        help="Server port (default: 15000)")
    parser.add_argument("--api-key", default=None,
                        help="Pre-configure LLM API key")
    parser.add_argument("--docker", action="store_true",
                        help="Use Docker instead of native install")
    parser.add_argument("--no-launch", action="store_true",
                        help="Install only, don't start the server")
    parser.add_argument("--skip-playwright", action="store_true",
                        help="Skip Playwright browser installation")
    parser.add_argument("--no-update-conda", action="store_true",
                        help="Skip the conda self-update step")
    parser.add_argument("--reset-env", action="store_true",
                        help="Delete the existing conda env before re-creating it. "
                             "DESTRUCTIVE: wipes any extra packages the user installed.")
    args = parser.parse_args()

    # ── Banner ──
    print()
    print(f"  {_c('1', '🧈 Tofu (豆腐) — Self-Hosted AI Assistant')}")
    print(f"  {'─' * 43}")
    plat = f"{platform.system()} {platform.machine()}"
    print(f"  Platform: {plat} | Python bootstrap: {sys.version.split()[0]}")
    print(f"  Mode: conda-only (conda-forge)")
    print()

    # ── Determine install directory ──
    if args.dir:
        install_dir = os.path.abspath(args.dir)
    elif os.path.isfile("server.py"):
        install_dir = os.getcwd()
    else:
        install_dir = os.path.join(os.path.expanduser("~"), "tofu")

    # ── Locate conda (always needed, even for Docker path for `git`) ──
    conda = locate_conda()

    # ── Docker path ──
    if args.docker:
        docker_install(conda, install_dir, args.port, args.api_key)
        return

    # ── Conda-only native install ──
    if not args.no_update_conda:
        update_conda(conda)
    else:
        info("Skipping conda self-update (--no-update-conda)")

    get_source(conda, install_dir)
    py = setup_env(conda, args.env, args.py_ver, reset=args.reset_env)
    install_deps(conda, args.env, install_dir, py)
    check_sqlite(py)
    install_search_tools(conda, args.env)

    if not args.skip_playwright:
        install_playwright(conda, args.env, py)

    configure_env_file(install_dir, args.port, args.api_key)

    if args.no_launch:
        print_install_complete(args.env, install_dir, args.port)
    else:
        launch(conda, args.env, py, install_dir, args.port)


if __name__ == "__main__":
    main()
