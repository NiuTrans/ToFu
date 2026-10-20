"""Executable contracts for the lazy LLM transport package facade."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.unit
@pytest.mark.parametrize('child_first', [True, False])
def test_chat_export_is_callable_in_either_import_order(child_first):
    proc = _run_isolated(textwrap.dedent(f'''
        import importlib
        import inspect
        import sys
        from unittest.mock import patch

        if {child_first!r}:
            importlib.import_module('lib.llm.chat')
        from lib.llm import chat
        child = importlib.import_module('lib.llm.chat')
        import lib.llm as llm
        assert callable(chat), type(chat)
        assert chat is llm.chat is child.chat
        assert inspect.signature(chat).parameters['timeout'].default is None
        assert sys.modules['lib.llm.chat'] is child
        assert not any(name in sys.modules for name in (
            'requests', 'lib.http_transport.client', 'lib.llm._transport'))
        with patch.object(llm, 'chat', return_value=('patched', {{}})) as patched:
            importlib.import_module('lib.llm.chat')
            from lib.llm import chat as imported
            assert imported([]) == ('patched', {{}})
            patched.assert_called_once_with([])
        assert llm.chat is chat
    '''))
    assert proc.returncode == 0, proc.stderr[-1800:]


@pytest.mark.unit
def test_stream_result_child_import_does_not_initialize_transports():
    proc = _run_isolated(
        'import sys; from lib.llm.stream_result import ProviderStreamState; '
        'print("STREAM-RESULT", ProviderStreamState.UNKNOWN.value, '
        '"lib.llm._transport" in sys.modules, '
        '"requests" in sys.modules, '
        '"lib.llm.stream" in sys.modules, '
        '"lib.llm.astream" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'STREAM-RESULT unknown False False False False' in proc.stdout


@pytest.mark.unit
def test_package_exports_and_child_module_import_remain_compatible():
    proc = _run_isolated(
        'import sys; import lib.llm as llm; '
        'before = "lib.llm.stream_result" in sys.modules; '
        'state = llm.ProviderStreamState; body = llm.build_body; '
        'from lib.llm import diagnostics; '
        'print("FACADE", before, state.UNKNOWN.value, callable(body), '
        'diagnostics.__name__, "lib.llm._transport" in sys.modules, '
        '"lib.llm.astream" in sys.modules, "lib.llm.stream" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'FACADE False unknown True lib.llm.diagnostics False False False' \
        in proc.stdout


@pytest.mark.unit
def test_server_verdict_boot_keeps_llm_transports_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-LLM", "lib.llm.stream_result" in sys.modules, '
        '"lib.llm._transport" in sys.modules, '
        'callable(sys.modules["lib.llm"].chat), '
        '"lib.llm.stream" in sys.modules, '
        '"lib.llm.astream" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-LLM True False True False False' in proc.stdout
