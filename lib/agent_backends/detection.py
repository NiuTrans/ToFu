"""lib/agent_backends/detection.py — Detect installed CLI coding agents.

Checks for the presence and authentication status of external CLI agents
(Claude Code, Codex) by probing the filesystem and running version commands.

Used by the backend registry to populate availability status in the
``/api/v1/agent-backends/status`` API response.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess

from lib.log import get_logger

logger = get_logger(__name__)


def _find_in_common_locations(binary_name: str) -> str | None:
    """Search for a CLI binary in common locations beyond PATH.

    Many server environments have separate conda/miniforge envs, so the
    binary might be installed in one env but not on the current PATH.
    We search common locations to find it.

    Args:
        binary_name: The binary name (e.g. 'claude', 'codex').

    Returns:
        Absolute path to the binary, or None.
    """
    home = os.path.expanduser('~')
    os.path.dirname(os.path.dirname(home))  # e.g. /mnt/.../user

    # Candidate directories to search
    search_patterns = [
        # Current user's conda/miniforge envs
        os.path.join(home, 'miniconda3/envs/*/bin', binary_name),
        os.path.join(home, 'miniforge3/envs/*/bin', binary_name),
        os.path.join(home, 'anaconda3/envs/*/bin', binary_name),
        os.path.join(home, 'conda/envs/*/bin', binary_name),
        os.path.join(home, '.conda/envs/*/bin', binary_name),
        # Conda base
        os.path.join(home, 'miniconda3/bin', binary_name),
        os.path.join(home, 'miniforge3/bin', binary_name),
        os.path.join(home, 'anaconda3/bin', binary_name),
        # npm global
        os.path.join(home, '.npm-global/bin', binary_name),
        os.path.join(home, 'node_modules/.bin', binary_name),
        # Common system locations
        '/usr/local/bin/' + binary_name,
        '/usr/bin/' + binary_name,
    ]

    # Also search subdirectories of user's INS/workspace dirs
    # (for shared server environments like /mnt/.../user/hadoop-xxx/INS/xxx/)
    for parent_dir in [home, os.path.dirname(home)]:
        if os.path.isdir(parent_dir):
            search_patterns.extend([
                os.path.join(parent_dir, '*/miniforge3/envs/*/bin', binary_name),
                os.path.join(parent_dir, '*/miniconda3/envs/*/bin', binary_name),
                os.path.join(parent_dir, 'INS/*/miniforge3/envs/*/bin', binary_name),
                os.path.join(parent_dir, 'INS/*/miniconda3/envs/*/bin', binary_name),
            ])

    for pattern in search_patterns:
        matches = glob.glob(pattern)
        for match in matches:
            if os.path.isfile(match) and os.access(match, os.X_OK):
                logger.info('[Detection] Found %s at %s (outside PATH)', binary_name, match)
                return match

    return None


def detect_cli(binary_name: str, *, timeout: int = 10) -> dict:
    """Detect a CLI binary: availability, path, and version.

    First checks PATH, then searches common conda/miniforge/npm locations.

    Args:
        binary_name: The binary name to look up (e.g. 'claude', 'codex').
        timeout: Maximum seconds to wait for ``--version``.

    Returns:
        Dict with keys: available (bool), path (str|None), version (str|None).
    """
    path = shutil.which(binary_name)
    if not path:
        # Search common locations outside PATH
        path = _find_in_common_locations(binary_name)
    if not path:
        logger.debug('[Detection] %s not found in PATH or common locations', binary_name)
        return {'available': False, 'path': None, 'version': None}

    result = {'available': True, 'path': path, 'version': None}

    # Get version
    try:
        proc = subprocess.run(
            [path, '--version'],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'NO_COLOR': '1'},
        )
        if proc.returncode == 0:
            version_str = proc.stdout.strip()
            # Take first line only (some CLIs print extra info)
            if version_str:
                result['version'] = version_str.split('\n')[0].strip()
                logger.debug('[Detection] %s version: %s', binary_name, result['version'])
    except subprocess.TimeoutExpired:
        logger.warning('[Detection] %s --version timed out after %ds', binary_name, timeout)
    except Exception as e:
        logger.debug('[Detection] %s --version failed: %s', binary_name, e)

    return result


def detect_claude_code() -> dict:
    """Detect Claude Code CLI and check authentication.

    Returns:
        Dict with keys: available, path, version, authenticated.
    """
    info = detect_cli('claude')
    info['authenticated'] = False

    if not info['available']:
        return info

    # Check auth: claude doctor or a quick non-interactive probe
    # The simplest check: --version succeeds = installed OK.
    # For auth, check if ~/.claude/ exists with session data.
    try:
        claude_dir = os.path.expanduser('~/.claude')
        if os.path.isdir(claude_dir):
            # Look for credentials or auth markers
            info['authenticated'] = True
            logger.debug('[Detection] Claude Code auth: ~/.claude/ exists')
        else:
            # Try running a minimal command to check auth
            proc = subprocess.run(
                [info['path'], '-p', 'echo test', '--max-turns', '0', '--output-format', 'json'],
                capture_output=True, text=True, timeout=15,
                env={
                    **os.environ,
                    'CLAUDE_CODE_SKIP_UPDATE_CHECK': '1',
                    'NO_COLOR': '1',
                },
            )
            # If it doesn't error about auth, we're good
            if 'auth' not in proc.stderr.lower() and 'login' not in proc.stderr.lower():
                info['authenticated'] = True
    except subprocess.TimeoutExpired:
        logger.debug('[Detection] Claude Code auth check timed out')
    except Exception as e:
        logger.debug('[Detection] Claude Code auth check failed: %s', e)

    return info


def detect_codex() -> dict:
    """Detect Codex CLI and check authentication.

    Returns:
        Dict with keys: available, path, version, authenticated.
    """
    info = detect_cli('codex')
    info['authenticated'] = False

    if not info['available']:
        return info

    # Check for OpenAI auth — codex uses OPENAI_API_KEY or ~/.codex/
    try:
        # Check environment variable
        if os.environ.get('OPENAI_API_KEY'):
            info['authenticated'] = True
            logger.debug('[Detection] Codex auth: OPENAI_API_KEY set')
        else:
            # Check for config directory
            codex_dir = os.path.expanduser('~/.codex')
            if os.path.isdir(codex_dir):
                info['authenticated'] = True
                logger.debug('[Detection] Codex auth: ~/.codex/ exists')
            else:
                # Try running version check — if it errors about auth, not authed
                proc = subprocess.run(
                    [info['path'], '--version'],
                    capture_output=True, text=True, timeout=10,
                    env={**os.environ, 'NO_COLOR': '1'},
                )
                if proc.returncode == 0:
                    info['authenticated'] = True
    except subprocess.TimeoutExpired:
        logger.debug('[Detection] Codex auth check timed out')
    except Exception as e:
        logger.debug('[Detection] Codex auth check failed: %s', e)

    return info
