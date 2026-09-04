"""Executable contract for request-scoped model-routing activation."""

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
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.unit
def test_server_boot_loads_v2_contract_but_not_legacy_or_dispatch_adapter():
    proc = _run_isolated(
        'import sys; import server; '
        'print("ROUTING", "lib.model_routing" in sys.modules, '
        '"lib.byo_resolve" in sys.modules, '
        '"lib.model_routing.dispatch_adapter" in sys.modules, '
        '"lib.llm_dispatch.ephemeral" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'ROUTING True False False False' in proc.stdout


@pytest.mark.unit
def test_plain_global_model_resolution_stays_dispatch_free():
    proc = _run_isolated(
        'import sys; from types import SimpleNamespace; '
        'import lib.byo_resolve as resolver; '
        'resolver.resolve_model_string = lambda *_args, **_kwargs: '
        'SimpleNamespace(model_id="global-model", provider=None); '
        'result = resolver.resolve_model_and_provider("global-model", None, 7); '
        'print("PLAIN", result, "EPHEMERAL", '
        '"lib.llm_dispatch.ephemeral" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert "PLAIN ('global-model', None, None, None, None) EPHEMERAL False" \
        in proc.stdout
