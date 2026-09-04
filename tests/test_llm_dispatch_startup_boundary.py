"""Executable contracts for request-loaded LLM dispatch dependencies."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "LD_PRELOAD"}
    return subprocess.run(
        [sys.executable, "-c", source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_package_import_keeps_dispatch_implementation_dormant():
    proc = _run_isolated(
        'import sys; import lib.llm_dispatch as dispatch; '
        'print("DISPATCH-PACKAGE", len(dispatch.__all__), '
        'set(dispatch.__all__) == set(dispatch._EXPORT_MODULES), '
        '"lib.llm_dispatch.api" in sys.modules, '
        '"lib.llm_dispatch.discovery" in sys.modules, '
        '"lib.llm_dispatch.dispatcher" in sys.modules, '
        '"lib.http_client" in sys.modules, "requests" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert "DISPATCH-PACKAGE 34 True False False False False False" in proc.stdout


@pytest.mark.unit
def test_package_exports_and_child_modules_remain_compatible():
    proc = _run_isolated(
        'import sys; import lib.llm_dispatch as dispatch; '
        'stream = dispatch.dispatch_stream; from lib.llm_dispatch import config; '
        'print("DISPATCH-FACADE", callable(stream), config.__name__, '
        '"lib.llm_dispatch.api" in sys.modules, '
        '"lib.llm_dispatch.discovery" in sys.modules, '
        '"lib.http_client" in sys.modules, "requests" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        "DISPATCH-FACADE True lib.llm_dispatch.config True False False False"
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_dispatch_and_http_transport_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-DISPATCH", "lib.llm_dispatch.api" in sys.modules, '
        '"lib.llm_dispatch.discovery" in sys.modules, '
        '"lib.llm_dispatch.dispatcher" in sys.modules, '
        '"lib.http_client" in sys.modules, "requests" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert "SERVER-DISPATCH False False False False False" in proc.stdout


@pytest.mark.unit
def test_retryable_transport_tuple_preserves_concrete_exception_contract():
    proc = _run_isolated(
        'import sys; import lib.llm_errors as errors; '
        'before = "requests" in sys.modules; '
        'from lib.llm_errors import _RETRYABLE; '
        'from requests.exceptions import ChunkedEncodingError, ConnectionError; '
        'print("RETRYABLE", before, isinstance(ConnectionError(), _RETRYABLE), '
        'isinstance(ChunkedEncodingError(), _RETRYABLE), '
        'errors._RETRYABLE is _RETRYABLE)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert "RETRYABLE False True True True" in proc.stdout
