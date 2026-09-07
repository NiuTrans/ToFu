#!/usr/bin/env python3
"""bootstrap.py — Smart server launcher with LLM-guided dependency repair.

Usage:  python bootstrap.py          (drop-in replacement for python server.py)

Behaviour:
  1. Try to start server.py normally.
  2. If it crashes (usually a missing package), spin up a tiny status page
     on the same port so the user can watch progress in the browser.
  3. Send the traceback to the project's LLM API for analysis.
  4. Install whatever packages the LLM recommends (pip install).
  5. Retry — loop until success or the error is deemed unresolvable.

If server.py starts cleanly, this script is 100 % transparent — the user
sees exactly the same output as running ``python server.py`` directly.

IMPORTANT: This facade and the whole ``bootstrap_pkg`` package use ONLY the
Python standard library.  They must work even when *every* pip package is
missing (that's the whole point).

Layout (facade-retained split, 2026-08-21 — the monolith was 2.7k lines):
  bootstrap_pkg/env_reexec.py   — .tofu_env.json detection + os.execv re-exec
  bootstrap_pkg/runtime.py      — .env/config, SSE EventBus, LLM diagnosis call
  bootstrap_pkg/providers.py    — provider templates + model catalogue I/O
  bootstrap_pkg/install.py      — conda/pip/requirements.txt install machinery
  bootstrap_pkg/status_page.py  — mini HTTP status server (SSE + config form)
  bootstrap_pkg/launcher.py     — server.py supervision + main repair loop
This module re-exports the full historical API surface (tests monkeypatch
``bootstrap.socket`` / ``bootstrap.urllib`` and import the ``_bootstrap_*``
helpers from here) and preserves the import-time ordering contract:
env re-exec FIRST, ``.env`` load SECOND. Every import below is a deliberate
re-export — they are listed in ``__all__`` so linters keep them.
"""

from __future__ import annotations

# The monolith's full stdlib import block, retained as attributes: tests
# monkeypatch through them (bootstrap.socket / bootstrap.urllib / boot.time …).
import http.server  # noqa: F401
import ipaddress    # noqa: F401
import json         # noqa: F401
import os           # noqa: F401
import queue        # noqa: F401
import re           # noqa: F401
import signal       # noqa: F401
import socket       # noqa: F401
import subprocess   # noqa: F401
import sys          # noqa: F401
import tempfile     # noqa: F401
import textwrap     # noqa: F401
import threading    # noqa: F401
import time         # noqa: F401
import urllib.error    # noqa: F401
import urllib.parse    # noqa: F401
import urllib.request  # noqa: F401


def _print_cli_help() -> None:
    """Print the repair launcher's contract without booting or re-executing."""
    print(
        'usage: python bootstrap.py\n\n'
        'Start Tofu with automatic dependency diagnosis and bounded repair.\n'
        'Server configuration is read from the environment and project .env.\n'
        'This repair launcher does not accept server command-line options.\n\n'
        'For normal managed startup, prefer: python server.py\n'
        'For lifecycle diagnostics, run:  python serverctl.py doctor\n'
        'For this help, run:              python bootstrap.py --help'
    )


# Help must be safe even when .tofu_env.json points at another interpreter.
# The normal import/re-export contract below remains unchanged for every real
# launch and for modules importing bootstrap in tests.
if __name__ == '__main__':
    if any(arg in ('-h', '--help') for arg in sys.argv[1:]):
        _print_cli_help()
        raise SystemExit(0)
    if '--version' in sys.argv[1:]:
        try:
            with open(os.path.join(os.path.dirname(__file__), 'VERSION'),
                      encoding='utf-8') as _version_file:
                _version = _version_file.read().strip() or 'unknown'
        except OSError:
            _version = 'unknown'
        print(f'Tofu {_version}')
        raise SystemExit(0)
    if sys.argv[1:]:
        print(
            'bootstrap.py: server command-line options are not supported; '
            'use `python server.py [SERVER_OPTIONS]`, or put persistent '
            'settings such as PORT in the project .env.',
            file=sys.stderr,
        )
        raise SystemExit(2)

# ── 1) env re-exec BEFORE anything else (historical import-time ordering) ──
from bootstrap_pkg.env_reexec import (
    BASE_DIR,
    _tofu_export_env_native_paths,
    _tofu_maybe_reexec_into_env,
)

_tofu_maybe_reexec_into_env()

# ── 2) .env load SECOND ──
from bootstrap_pkg.runtime import (
    EventBus,
    _call_llm,
    _get_config,
    _load_dotenv,
    consume_restart_request,
    request_restart,
)

try:
    _load_dotenv()
except OSError as _dotenv_error:
    if __name__ != '__main__':
        raise
    print(
        f'[bootstrap] Invalid project .env: {_dotenv_error}',
        file=sys.stderr,
    )
    print(
        '[bootstrap] Fix or replace the project .env, then run '
        '`python serverctl.py doctor`.',
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# ── 3) the rest of the historical API surface ──
from bootstrap_pkg.providers import (
    _BUILTIN_PROVIDER_TEMPLATES,
    _bootstrap_choose_model,
    _bootstrap_data_root,
    _bootstrap_discover_models,
    _bootstrap_infer_capabilities,
    _bootstrap_persist_provider,
    _bootstrap_template_models,
    _load_provider_templates,
)
from bootstrap_pkg.install import (
    _CONDA_PYTHON_DEPS,
    _CRITICAL_BOOT_PACKAGES,
    _INSTALL_BLOCKLIST,
    PIP_TIMEOUT,
    _detect_mypyc_broken_packages,
    _find_conda_exe,
    _is_import_or_package_error,
    _is_mypyc_error,
    _pip_install,
    _running_in_conda_env,
    _try_conda_install_deps,
    _try_fix_mypyc,
    _try_requirements_txt,
)
from bootstrap_pkg.status_page import (
    _STATUS_HTML,
    _find_free_port,
    _start_status_server,
    _stop_status_server,
)
from bootstrap_pkg.launcher import (
    MAX_REPAIR_ROUNDS,
    _is_external_kill,
    _keep_alive_until_interrupt,
    _log_external_kill,
    _restart_after_external_kill,
    _try_start_server,
    main,
)

__all__ = [
    # stdlib handles tests monkeypatch through
    'socket', 'urllib',
    # env_reexec
    'BASE_DIR', '_tofu_export_env_native_paths',
    '_tofu_maybe_reexec_into_env',
    # runtime
    'EventBus', '_call_llm', '_get_config', '_load_dotenv',
    'consume_restart_request', 'request_restart',
    # providers
    '_BUILTIN_PROVIDER_TEMPLATES', '_bootstrap_choose_model',
    '_bootstrap_data_root', '_bootstrap_discover_models',
    '_bootstrap_infer_capabilities', '_bootstrap_persist_provider',
    '_bootstrap_template_models', '_load_provider_templates',
    # install
    '_CONDA_PYTHON_DEPS', '_CRITICAL_BOOT_PACKAGES', '_INSTALL_BLOCKLIST',
    'PIP_TIMEOUT', '_detect_mypyc_broken_packages', '_find_conda_exe',
    '_is_import_or_package_error', '_is_mypyc_error', '_pip_install',
    '_running_in_conda_env', '_try_conda_install_deps', '_try_fix_mypyc',
    '_try_requirements_txt',
    # status_page
    '_STATUS_HTML', '_find_free_port', '_start_status_server',
    '_stop_status_server',
    # launcher
    'MAX_REPAIR_ROUNDS', '_is_external_kill', '_keep_alive_until_interrupt',
    '_log_external_kill', '_restart_after_external_kill', '_try_start_server',
    'main',
]

if __name__ == '__main__':
    main()
