"""Pytest execution policy, shared by the selector and verification engine.

Entry point: build_pytest_command. This standalone owner uses only stdlib;
selection rules live in test_select.py. Its code is a global execution input.
Contract: docs/modules/project_integration.md, Merge verification policy.
"""
from __future__ import annotations

import sys


def _parallel_workers(existing: list[str] | None,
                      jobs: str | None) -> str | None:
    """Return the requested/adaptive xdist worker count, or ``None``.

    A pytest controller and every worker import the full test bootstrap. For a
    one-file inner loop that startup costs more than the tests, so the implicit
    default stays serial. Larger selections request ``auto``; the repository's
    xdist hook then applies the shared resource probe and selected-file cap. An
    explicit ``--jobs`` remains authoritative.
    """
    if jobs is not None:
        return None if jobs == '0' else jobs
    if existing is not None and len(existing) <= 1:
        return None
    return 'auto'


def build_pytest_command(existing: list[str] | None,
                         jobs: str | None) -> list[str]:
    """Build the hermetic selected-test command.

    ``main`` disables pytest entry-point autoload to keep host-only plugins out
    of the inner loop. pyproject.toml explicitly loads xdist and owns the
    ``worksteal`` scheduler, so this command only selects the worker count.
    """
    if existing == []:
        raise ValueError('empty selection must not launch unscoped pytest')
    workers = _parallel_workers(existing, jobs)
    cmd = [sys.executable, '-m', 'pytest', '-p', 'no:napari']
    if existing is not None:
        cmd += existing
    cmd += ['-m', '(unit or api) and not (visual or slow or live_llm)']
    if workers is not None:
        cmd += ['-n', workers]
    cmd += ['--timeout=300', '--tb=short', '-q']
    return cmd
