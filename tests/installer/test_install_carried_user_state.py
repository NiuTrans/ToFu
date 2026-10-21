"""A carried internal export must report its skills and MCP registrations.

An internal export ships the operator's installed skill packages and a
host-portable ``mcp_servers.json`` (see ``export_pkg/_internal``). Nothing
re-installs them at the destination: the runtime scans the same relative paths
and personal-mode boot migrates the legacy MCP file into the owner store. That
made the whole handoff silent — a package whose launcher cannot resolve only
failed at first use, long after ``install.sh`` printed success.

These tests pin the installer-side contract that closes the gap: the export
side must keep writing the paths the installer verifies, and the verifier must
stay a warning rather than an install-killing failure, because both halves are
optional capability state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from export_pkg._internal import _SKILL_STATE_ROOTS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _installer() -> str:
    return (ROOT / 'install.sh').read_text(encoding='utf-8')


def _carried_block() -> str:
    source = _installer()
    start = source.index('Carried user state: installed skills')
    end = source.index('step "Committing verified installation"', start)
    return source[start:end]


def test_installer_verifies_every_carried_skill_root():
    """Each root the export restores must be counted by the installer."""
    block = _carried_block()
    for src_rel, dst_rel in _SKILL_STATE_ROOTS:
        assert src_rel == dst_rel, (
            f'{src_rel} is restored to {dst_rel}; the verifier reads the '
            f'destination path, so the two must agree')
        assert f'${{INSTALL_DIR}}/{dst_rel}' in block, (
            f'the installer never inspects {dst_rel}, so a destination with '
            f'missing skill packages would still print a clean install')


def test_installer_reports_carried_mcp_launchers_that_cannot_run():
    """An unusable launcher is named instead of failing at first connect."""
    block = _carried_block()
    assert 'mcp_servers.json' in block
    assert 'not launchable here' in block
    # Mirrors lib/mcp/vendored.VENDORED_LAUNCHERS source order.
    for base in ("'vendor'", "'packages'", "'tools'"):
        assert base in block, (
            f'the launcher probe no longer covers {base}/, so a vendored '
            f'server would be reported as missing on a healthy destination')


def test_carried_state_failures_never_abort_the_install():
    """Optional state must warn; aborting here would block a good install."""
    block = _carried_block()
    failing = [line for line in block.splitlines()
               if re.match(r'\s*fail\s', line)]
    assert not failing, (
        'carried skill/MCP state is optional capability state — a missing '
        f'launcher or unreadable config must not fail the installation: {failing}')
    assert 'warn "Some carried MCP registrations cannot launch' in block


def test_verification_runs_before_the_install_is_committed():
    """A report after the commit cannot describe what was committed."""
    source = _installer()
    assert (source.index('Carried user state: installed skills')
            < source.index('step "Committing verified installation"'))


def test_ssh_skill_dependency_is_declared_for_the_runtime_interpreter():
    """``codelab-ssh-connect`` imports paramiko from the runtime interpreter.

    Its bundled scripts are carried into internal exports, so the destination
    interpreter must import it; the skill document tells the reader to run
    them with the project interpreter.
    """
    requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    assert 'paramiko>=' in requirements, (
        'a carried skill imports paramiko; without a declared floor the '
        'destination fails with ModuleNotFoundError at first use')
