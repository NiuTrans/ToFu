#!/usr/bin/env python3
"""install.py — Cross-platform one-command installer for Tofu (豆腐).

Works on Linux, macOS, and Windows. Requires only Python 3.10+.
No conda, no system packages, no root/admin required.

Usage:
    python install.py                     # Install and start
    python install.py --no-launch         # Install only
    python install.py --port 8080         # Custom port
    python install.py --api-key sk-xxx    # Pre-configure API key
    python install.py --docker            # Use Docker instead

What it does:
    1. Verifies Python 3.10+
    2. Creates a virtual environment (.venv)
    3. Installs Python dependencies via pip (or uv if available)
    4. Locates or installs PostgreSQL per platform
    5. Optionally installs Playwright (browser automation)
    6. Creates .env from template
    7. Launches the server

The script is idempotent — safe to run multiple times.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import urllib.request
import urllib.error
import json
import time

# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

MIN_PYTHON = (3, 10)
REPO_URL = "https://github.com/rangehow/ToFu.git"
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ═══════════════════════════════════════════════════════════════
#  Terminal output helpers
# ═══════════════════════════════════════════════════════════════

# Windows cmd.exe doesn't support ANSI by default before Win10 1511.
# We enable it via SetConsoleMode if possible, otherwise degrade gracefully.
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
        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
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


def info(msg: str):
    print(f"  {_c('34', 'ℹ')}  {msg}")

def ok(msg: str):
    print(f"  {_c('32', '✓')}  {msg}")

def warn(msg: str):
    print(f"  {_c('33', '!')}  {msg}")

def fail(msg: str):
    print(f"  {_c('31', '✗')}  {msg}")
    sys.exit(1)

def step(msg: str):
    print(f"\n  {_c('1;36', '▸')} {_c('1', msg)}")


# ═══════════════════════════════════════════════════════════════
#  Utility functions
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
    """Cross-platform shutil.which."""
    return shutil.which(name)


def python_version() -> tuple[int, int]:
    return sys.version_info[:2]


def venv_python(venv_dir: str) -> str:
    """Return the path to the Python executable inside a venv."""
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def venv_bin(venv_dir: str, name: str) -> str:
    """Return the path to a binary inside a venv."""
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", f"{name}.exe")
    return os.path.join(venv_dir, "bin", name)


# ═══════════════════════════════════════════════════════════════
#  Step 1: Verify Python version
# ═══════════════════════════════════════════════════════════════

def check_python():
    step("Checking Python")
    major, minor = python_version()
    if (major, minor) < MIN_PYTHON:
        fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, "
            f"but you have {major}.{minor}.\n"
            f"   Download from: https://www.python.org/downloads/"
        )
    ok(f"Python {major}.{minor} ({sys.executable})")


# ═══════════════════════════════════════════════════════════════
#  Step 2: Get source code
# ═══════════════════════════════════════════════════════════════

def get_source(install_dir: str):
    step("Getting Tofu source code")

    # Already in the project directory (server.py exists)?
    if os.path.isfile(os.path.join(install_dir, "server.py")):
        # Update if it's a git repo
        if os.path.isdir(os.path.join(install_dir, ".git")):
            info("Updating existing installation...")
            result = run(["git", "pull", "--ff-only"], check=False,
                         capture=True, cwd=install_dir)
            if result.returncode != 0:
                warn("Could not auto-update (you may have local changes)")
        ok(f"Source ready at {install_dir}")
        return

    # Clone
    if not which("git"):
        fail(
            "Git is required to clone the repository.\n"
            "   Install from: https://git-scm.com/downloads"
        )
    info(f"Cloning to {install_dir}...")
    run(["git", "clone", REPO_URL, install_dir])
    ok(f"Source ready at {install_dir}")


# ═══════════════════════════════════════════════════════════════
#  Step 3: Create virtual environment
# ═══════════════════════════════════════════════════════════════

def setup_venv(install_dir: str) -> str:
    """Create a .venv and return the path to its Python executable.

    Strategy:
      1. If conda env is already active (CONDA_PREFIX set), use it as-is.
      2. If uv is available, use `uv venv` (faster).
      3. Otherwise, use `python -m venv`.
    """
    step("Setting up Python environment")

    venv_dir = os.path.join(install_dir, ".venv")

    # If running inside an active conda env, respect it
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix and os.path.isfile(os.path.join(conda_prefix, "bin", "python")):
        py = os.path.join(conda_prefix, "bin", "python")
        ok(f"Using active conda environment: {conda_prefix}")
        return py
    if conda_prefix and IS_WINDOWS:
        py = os.path.join(conda_prefix, "python.exe")
        if os.path.isfile(py):
            ok(f"Using active conda environment: {conda_prefix}")
            return py

    # Check if .venv already exists and is valid
    venv_py = venv_python(venv_dir)
    if os.path.isfile(venv_py):
        ok(f"Virtual environment already exists: .venv")
        return venv_py

    # Create venv
    # Try uv first (much faster)
    uv = which("uv")
    if uv:
        info("Creating virtual environment with uv...")
        run([uv, "venv", venv_dir, "--python", sys.executable], cwd=install_dir)
    else:
        info("Creating virtual environment with venv...")
        run([sys.executable, "-m", "venv", venv_dir], cwd=install_dir)

    if not os.path.isfile(venv_py):
        fail(f"Failed to create virtual environment at {venv_dir}")

    ok(f"Virtual environment created: .venv")
    return venv_py


# ═══════════════════════════════════════════════════════════════
#  Step 4: Install Python dependencies
# ═══════════════════════════════════════════════════════════════

def install_deps(py: str, install_dir: str):
    """Install Python dependencies using pip or uv."""
    step("Installing Python dependencies")

    req_file = os.path.join(install_dir, "requirements.txt")
    if not os.path.isfile(req_file):
        warn("No requirements.txt found — skipping pip install")
        return

    # Prefer uv for speed, fall back to pip
    uv = which("uv")
    if uv:
        info("Using uv for fast dependency installation...")
        run([uv, "pip", "install", "-r", req_file, "--python", py],
            cwd=install_dir)
    else:
        # Upgrade pip first
        run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"],
            cwd=install_dir)
        run([py, "-m", "pip", "install", "-r", req_file],
            cwd=install_dir)

    ok("Python dependencies installed")


# ═══════════════════════════════════════════════════════════════
#  Step 5: Ensure PostgreSQL is available
# ═══════════════════════════════════════════════════════════════

def _find_pg_binary(name: str) -> str | None:
    """Locate a PostgreSQL binary across common install locations."""
    # 1. Check PATH
    found = which(name)
    if found:
        return found

    # 2. Platform-specific common locations
    candidates = []
    if IS_MACOS:
        # Homebrew (Intel + Apple Silicon)
        candidates += [
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
        ]
        # Homebrew versioned
        for ver in range(20, 14, -1):
            candidates.append(f"/opt/homebrew/opt/postgresql@{ver}/bin/{name}")
            candidates.append(f"/usr/local/opt/postgresql@{ver}/bin/{name}")
        # Postgres.app
        candidates.append(f"/Applications/Postgres.app/Contents/Versions/latest/bin/{name}")

    elif IS_LINUX:
        # System packages (Debian/Ubuntu, RHEL/Fedora)
        for ver in range(20, 14, -1):
            candidates.append(f"/usr/lib/postgresql/{ver}/bin/{name}")
        candidates += [f"/usr/bin/{name}", f"/usr/local/bin/{name}"]

    elif IS_WINDOWS:
        # Standard Windows install dirs
        pg_dirs = [
            os.path.expandvars(r"%ProgramFiles%\PostgreSQL"),
            os.path.expandvars(r"%ProgramFiles(x86)%\PostgreSQL"),
        ]
        for pg_dir in pg_dirs:
            if os.path.isdir(pg_dir):
                for ver_dir in sorted(os.listdir(pg_dir), reverse=True):
                    candidates.append(os.path.join(pg_dir, ver_dir, "bin", f"{name}.exe"))

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None


def ensure_postgresql(py: str, install_dir: str):
    """Check for PostgreSQL and suggest installation if missing."""
    step("Checking PostgreSQL")

    initdb = _find_pg_binary("initdb")
    pg_ctl = _find_pg_binary("pg_ctl")

    if initdb and pg_ctl:
        # Get version
        try:
            result = subprocess.run(
                [pg_ctl, "--version"], capture_output=True, text=True, timeout=5
            )
            ver = result.stdout.strip().split()[-1] if result.returncode == 0 else "?"
        except Exception:
            ver = "?"
        ok(f"PostgreSQL {ver} found")
        return

    # PostgreSQL not found — try to install
    info("PostgreSQL not found in PATH. Attempting auto-install...")

    installed = False

    # Strategy 1: conda (if available)
    conda = which("conda") or which("mamba")
    if conda:
        info(f"Installing PostgreSQL via {os.path.basename(conda)}...")
        result = run(
            [conda, "install", "-c", "conda-forge", "-y", "postgresql>=18"],
            check=False, cwd=install_dir,
        )
        if result.returncode == 0 and _find_pg_binary("initdb"):
            ok("PostgreSQL installed via conda")
            installed = True

    # Strategy 2: Homebrew (macOS)
    if not installed and IS_MACOS and which("brew"):
        info("Installing PostgreSQL via Homebrew...")
        result = run(["brew", "install", "postgresql@18"], check=False)
        if result.returncode == 0 and _find_pg_binary("initdb"):
            ok("PostgreSQL installed via Homebrew")
            installed = True

    # Strategy 3: Suggest manual install
    if not installed:
        warn("Could not auto-install PostgreSQL.")
        print()
        if IS_MACOS:
            info("Install manually with one of:")
            print("     brew install postgresql@18")
            print("     conda install -c conda-forge postgresql")
            print("     # Or download Postgres.app: https://postgresapp.com")
        elif IS_LINUX:
            info("Install manually with one of:")
            print("     sudo apt install postgresql         # Debian/Ubuntu")
            print("     sudo dnf install postgresql-server  # Fedora/RHEL")
            print("     conda install -c conda-forge postgresql")
        elif IS_WINDOWS:
            info("Install manually:")
            print("     1. Download from: https://www.postgresql.org/download/windows/")
            print("     2. Run the installer (use default settings)")
            print("     3. Ensure the bin/ directory is added to PATH")
            print("     4. Re-run this installer")
            print()
            print("     Or with conda: conda install -c conda-forge postgresql")
            print("     Or with Chocolatey: choco install postgresql")
        print()
        warn("The server will attempt to auto-bootstrap PostgreSQL on first start.")
        warn("If that fails, install PostgreSQL manually using the commands above.")


# ═══════════════════════════════════════════════════════════════
#  Step 6: Install ripgrep (optional — fast code search)
# ═══════════════════════════════════════════════════════════════

def install_ripgrep(install_dir: str):
    """Install ripgrep for fast code search (optional, non-fatal).

    ripgrep is ~5x faster than GNU grep on our codebase. The grep_search
    tool falls back to GNU grep → pure Python if rg is unavailable.
    """
    step("Checking ripgrep (fast code search)")

    if which("rg"):
        # Get version
        try:
            result = subprocess.run(
                ["rg", "--version"], capture_output=True, text=True, timeout=5
            )
            ver = result.stdout.strip().split("\n")[0] if result.returncode == 0 else "?"
        except Exception:
            ver = "?"
        ok(f"ripgrep already installed ({ver})")
        return

    info("ripgrep not found. Attempting auto-install...")

    installed = False

    # Strategy 1: conda (if available — works cross-platform)
    conda = which("conda") or which("mamba")
    if conda:
        info(f"Installing ripgrep via {os.path.basename(conda)}...")
        result = run(
            [conda, "install", "-c", "conda-forge", "-y", "ripgrep"],
            check=False, capture=True, cwd=install_dir,
        )
        if result.returncode == 0 and which("rg"):
            ok("ripgrep installed via conda")
            installed = True

    # Strategy 2: Homebrew (macOS)
    if not installed and IS_MACOS and which("brew"):
        info("Installing ripgrep via Homebrew...")
        result = run(["brew", "install", "ripgrep"], check=False, capture=True)
        if result.returncode == 0 and which("rg"):
            ok("ripgrep installed via Homebrew")
            installed = True

    # Strategy 3: apt (Linux, if sudo available)
    if not installed and IS_LINUX and which("sudo") and which("apt-get"):
        info("Installing ripgrep via apt...")
        result = run(
            ["sudo", "apt-get", "install", "-y", "ripgrep"],
            check=False, capture=True,
        )
        if result.returncode == 0 and which("rg"):
            ok("ripgrep installed via apt")
            installed = True

    # Strategy 4: cargo (Rust — works everywhere)
    if not installed and which("cargo"):
        info("Installing ripgrep via cargo (this may take a minute)...")
        result = run(["cargo", "install", "ripgrep"], check=False, capture=True)
        if result.returncode == 0 and which("rg"):
            ok("ripgrep installed via cargo")
            installed = True

    if not installed:
        warn("Could not auto-install ripgrep (non-critical).")
        info("Code search will use GNU grep instead (~5x slower).")
        print()
        if IS_MACOS:
            info("Install manually: brew install ripgrep")
        elif IS_LINUX:
            info("Install manually: sudo apt install ripgrep")
            info("  or: conda install -c conda-forge ripgrep")
        elif IS_WINDOWS:
            info("Install manually: winget install BurntSushi.ripgrep.MSVC")
            info("  or: choco install ripgrep")
            info("  or: conda install -c conda-forge ripgrep")
        print()


# ═══════════════════════════════════════════════════════════════
#  Step 7: Install fd-find (optional — fast file search)
# ═══════════════════════════════════════════════════════════════

def install_fd(install_dir: str):
    """Install fd-find for fast file search (optional, non-fatal).

    fd is ~3-4x faster than GNU find / Python os.walk on large dirs.
    The find_files tool falls back to Python os.walk if fd is unavailable.
    """
    step("Checking fd-find (fast file search)")

    if which("fd") or which("fdfind"):
        bin_name = which("fd") or which("fdfind")
        try:
            result = subprocess.run(
                [bin_name, "--version"], capture_output=True, text=True, timeout=5
            )
            ver = result.stdout.strip().split("\n")[0] if result.returncode == 0 else "?"
        except Exception:
            ver = "?"
        ok(f"fd-find already installed ({ver})")
        return

    info("fd-find not found. Attempting auto-install...")

    installed = False

    # Strategy 1: conda (if available — works cross-platform)
    conda = which("conda") or which("mamba")
    if conda:
        info(f"Installing fd-find via {os.path.basename(conda)}...")
        result = run(
            [conda, "install", "-c", "conda-forge", "-y", "fd-find"],
            check=False, capture=True, cwd=install_dir,
        )
        if result.returncode == 0 and (which("fd") or which("fdfind")):
            ok("fd-find installed via conda")
            installed = True

    # Strategy 2: Homebrew (macOS)
    if not installed and IS_MACOS and which("brew"):
        info("Installing fd-find via Homebrew...")
        result = run(["brew", "install", "fd"], check=False, capture=True)
        if result.returncode == 0 and which("fd"):
            ok("fd-find installed via Homebrew")
            installed = True

    # Strategy 3: apt (Linux, if sudo available)
    if not installed and IS_LINUX and which("sudo") and which("apt-get"):
        info("Installing fd-find via apt...")
        result = run(
            ["sudo", "apt-get", "install", "-y", "fd-find"],
            check=False, capture=True,
        )
        if result.returncode == 0 and (which("fd") or which("fdfind")):
            ok("fd-find installed via apt")
            installed = True

    # Strategy 4: cargo (Rust — works everywhere)
    if not installed and which("cargo"):
        info("Installing fd-find via cargo (this may take a minute)...")
        result = run(["cargo", "install", "fd-find"], check=False, capture=True)
        if result.returncode == 0 and which("fd"):
            ok("fd-find installed via cargo")
            installed = True

    if not installed:
        warn("Could not auto-install fd-find (non-critical).")
        info("File search will use Python os.walk instead (~3x slower).")
        print()
        if IS_MACOS:
            info("Install manually: brew install fd")
        elif IS_LINUX:
            info("Install manually: sudo apt install fd-find")
            info("  or: conda install -c conda-forge fd-find")
        elif IS_WINDOWS:
            info("Install manually: winget install sharkdp.fd")
            info("  or: choco install fd")
            info("  or: conda install -c conda-forge fd-find")
        print()


# ═══════════════════════════════════════════════════════════════
#  Step 8: Install Playwright (optional — browser automation)
# ═══════════════════════════════════════════════════════════════

def install_playwright(py: str, install_dir: str):
    """Install Playwright for advanced web fetching (optional, non-fatal)."""
    step("Installing Playwright browser (optional)")

    result = run(
        [py, "-m", "pip", "install", "playwright", "-q"],
        check=False, capture=True, cwd=install_dir,
    )
    if result.returncode != 0:
        warn("Playwright pip install failed (non-critical — basic fetching still works)")
        return

    result = run(
        [py, "-m", "playwright", "install", "chromium"],
        check=False, capture=True, cwd=install_dir,
    )
    if result.returncode == 0:
        ok("Playwright chromium installed")
    else:
        warn("Playwright chromium install failed (non-critical)")


# ═══════════════════════════════════════════════════════════════
#  Step 9: Configure .env
# ═══════════════════════════════════════════════════════════════

def configure_env(install_dir: str, port: int, api_key: str | None):
    step("Configuration")

    env_file = os.path.join(install_dir, ".env")
    env_example = os.path.join(install_dir, ".env.example")

    if not os.path.isfile(env_file):
        if os.path.isfile(env_example):
            shutil.copy2(env_example, env_file)
            info("Created .env from template")
        else:
            # Create minimal .env
            with open(env_file, "w") as f:
                f.write(f"PORT={port}\n")
                f.write("BIND_HOST=0.0.0.0\n")
            info("Created minimal .env")

    # Update port
    _update_env_var(env_file, "PORT", str(port))

    # Set API key if provided
    if api_key:
        _update_env_var(env_file, "LLM_API_KEYS", api_key)
        ok("API key configured")


def _update_env_var(env_file: str, key: str, value: str):
    """Update or add an environment variable in a .env file."""
    lines = []
    found = False

    if os.path.isfile(env_file):
        with open(env_file) as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Match both "KEY=value" and "# KEY=value" (commented out)
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
#  Step 10: Docker path
# ═══════════════════════════════════════════════════════════════

def docker_install(install_dir: str, port: int, api_key: str | None):
    """Install and run via Docker Compose."""
    step("Docker installation")

    docker = which("docker")
    if not docker:
        fail(
            "Docker is not installed.\n"
            "   Get it from: https://docs.docker.com/get-docker/"
        )

    get_source(install_dir)
    os.chdir(install_dir)

    # Set env vars for docker-compose
    os.environ["PORT"] = str(port)
    if api_key:
        os.environ["LLM_API_KEYS"] = api_key

    run(["docker", "compose", "up", "-d"], cwd=install_dir)

    print()
    ok("Tofu is running via Docker!")
    print(f"   Open {_c('1', f'http://localhost:{port}')} in your browser")
    print(f"   Configure API keys in Settings → Providers")
    print()
    print(f"   Logs:    docker compose logs -f tofu")
    print(f"   Stop:    docker compose down")
    print(f"   Update:  docker compose pull && docker compose up -d")
    print()


# ═══════════════════════════════════════════════════════════════
#  Step 11: Launch server
# ═══════════════════════════════════════════════════════════════

def launch(py: str, install_dir: str, port: int):
    step("Starting Tofu server")
    print()
    print(f"  {_c('1', '🧈 Tofu is starting on port')} {_c('1', str(port))}...")
    print(f"  Open {_c('1', f'http://localhost:{port}')} in your browser")
    print()
    print(f"  {_c('36', 'First launch:')}  PostgreSQL will auto-initialize (~10s)")
    print(f"  {_c('36', 'Configure:')}     Click ⚙️ Settings → Providers to add your LLM API keys")
    print()
    print(f"  Press Ctrl+C to stop the server")
    print()

    os.chdir(install_dir)
    os.execv(py, [py, "server.py"])


def print_install_complete(py: str, install_dir: str, port: int):
    print()
    ok("Installation complete!")
    print()
    if IS_WINDOWS:
        # Show Windows-friendly activation commands
        venv_activate = os.path.join(install_dir, ".venv", "Scripts", "activate.bat")
        if os.path.isfile(venv_activate):
            print(f"  To start Tofu:")
            print(f"    cd {install_dir}")
            print(f"    .venv\\Scripts\\activate")
            print(f"    python server.py")
        else:
            print(f"  To start Tofu:")
            print(f"    cd {install_dir}")
            print(f"    {py} server.py")
    else:
        venv_activate = os.path.join(install_dir, ".venv", "bin", "activate")
        if os.path.isfile(venv_activate):
            print(f"  To start Tofu:")
            print(f"    cd {install_dir}")
            print(f"    source .venv/bin/activate")
            print(f"    python server.py")
        else:
            print(f"  To start Tofu:")
            print(f"    cd {install_dir}")
            print(f"    {py} server.py")
    print()
    print(f"  Then open {_c('1', f'http://localhost:{port}')} in your browser")
    print()


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Tofu (豆腐) — Cross-platform installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python install.py                          # Install and start
              python install.py --port 8080              # Custom port
              python install.py --api-key sk-xxx         # Pre-configure API key
              python install.py --no-launch              # Install only
              python install.py --docker                 # Use Docker
              python install.py --skip-playwright        # Skip Playwright install
        """),
    )
    parser.add_argument("--dir", default=None,
                        help="Install directory (default: current dir or ~/tofu)")
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
    args = parser.parse_args()

    # ── Banner ──
    print()
    print(f"  {_c('1', '🧈 Tofu (豆腐) — Self-Hosted AI Assistant')}")
    print(f"  {'─' * 43}")
    plat = f"{platform.system()} {platform.machine()}"
    print(f"  Platform: {plat} | Python: {sys.version.split()[0]}")
    print()

    # ── Determine install directory ──
    if args.dir:
        install_dir = os.path.abspath(args.dir)
    elif os.path.isfile("server.py"):
        # We're already in the project directory
        install_dir = os.getcwd()
    else:
        install_dir = os.path.join(os.path.expanduser("~"), "tofu")

    # ── Docker path ──
    if args.docker:
        docker_install(install_dir, args.port, args.api_key)
        return

    # ── Native install path ──
    check_python()
    get_source(install_dir)
    py = setup_venv(install_dir)
    install_deps(py, install_dir)
    ensure_postgresql(py, install_dir)

    install_ripgrep(install_dir)
    install_fd(install_dir)

    if not args.skip_playwright:
        install_playwright(py, install_dir)

    configure_env(install_dir, args.port, args.api_key)

    if args.no_launch:
        print_install_complete(py, install_dir, args.port)
    else:
        launch(py, install_dir, args.port)


if __name__ == "__main__":
    main()
