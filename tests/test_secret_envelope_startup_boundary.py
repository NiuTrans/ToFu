"""Executable contracts for use-loaded authenticated secret encryption."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_secret_module_import_keeps_cipher_implementation_dormant():
    proc = _run_isolated(
        'import sys; import lib.secret_envelope as secrets; '
        'print("SECRET-PACKAGE", secrets.secret_hint("abcdefgh12345678"), '
        '"cryptography" in sys.modules, '
        '"cryptography.fernet" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SECRET-PACKAGE abcd…5678 False False' in proc.stdout


@pytest.mark.unit
def test_server_boot_keeps_cipher_implementation_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-SECRET", '
        '"lib.secret_envelope" in sys.modules, '
        '"lib.byo_providers" in sys.modules, '
        '"cryptography" in sys.modules, '
        '"cryptography.fernet" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    # Route registration needs the repository envelope seam, but the retired
    # BYO aggregate and both cipher implementation modules stay dormant.
    assert 'SERVER-SECRET True False False False' in proc.stdout
