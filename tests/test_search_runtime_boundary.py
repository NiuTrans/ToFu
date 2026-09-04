"""Executable contract for tofu-search's lazy, bounded activation boundary."""

from __future__ import annotations

import os
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.unit
def test_server_registration_and_bootstrap_schema_do_not_load_search():
    proc = _run_isolated(
        'import sys; import server; '
        'from lib.tools.search import ('
        'build_browser_download_url_to_server_tool, build_search_tool); '
        'tool = build_search_tool(); '
        'download = build_browser_download_url_to_server_tool(); '
        'props = tool["function"]["parameters"]["properties"]; '
        'print("TOFU", "tofu_search" in sys.modules, '
        '"CORE", "lib.tasks_pkg.handlers.search._core" in sys.modules, '
        '"ENUM", props["vertical"]["enum"], '
        '"DOWNLOAD", download["function"]["name"])')
    assert proc.returncode == 0, proc.stderr[-800:]
    assert (
        "TOFU False CORE False ENUM ['auto', 'off'] "
        'DOWNLOAD browser_download_url_to_server'
    ) in proc.stdout


@pytest.mark.unit
def test_server_boot_does_not_load_dormant_v4_validators():
    proc = _run_isolated(
        'import sys; import server; '
        'print("V4-GENERATED", "lib.api_v4_generated" in sys.modules, '
        '"PYDANTIC", "pydantic" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-800:]
    assert 'V4-GENERATED False PYDANTIC False' in proc.stdout


@pytest.mark.unit
def test_unrelated_hot_reload_does_not_cold_import_search():
    proc = _run_isolated(
        'import sys; import lib; '
        'from lib.search_runtime import sync_search_config_if_loaded; '
        'print("SYNC", sync_search_config_if_loaded()); '
        'lib.reload_config(); '
        'print("TOFU", "tofu_search" in sys.modules, '
        '"BRIDGE", "lib.search_bridge" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-800:]
    assert 'SYNC False' in proc.stdout
    assert 'TOFU False BRIDGE False' in proc.stdout


@pytest.mark.unit
def test_direct_optional_import_applies_classic_pdf_policy_without_threads():
    proc = _run_isolated(
        'import sys, threading; '
        'from lib.search_runtime import prepare_search_dependency_import; '
        'prepare_search_dependency_import(); import tofu_search; '
        'print("LAYOUT-BLOCKED", sys.modules.get("pymupdf.layout", "missing") '
        'is None, "THREADS", threading.active_count())')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'LAYOUT-BLOCKED True THREADS 1' in proc.stdout


@pytest.mark.unit
def test_direct_bridge_policy_failure_is_fail_soft_and_observable(
    monkeypatch,
    caplog,
):
    import runtime_guards
    import lib.search_bridge as bridge

    def fail_policy():
        raise RuntimeError('synthetic policy failure')

    monkeypatch.setattr(
        runtime_guards, 'install_pymupdf_classic_policy', fail_policy)
    with caplog.at_level(logging.WARNING, logger='lib.search_bridge'):
        assert bridge._install_search_import_policy() is False

    assert 'classic PyMuPDF policy installation failed: RuntimeError' in caplog.text


@pytest.mark.unit
def test_linkage_forensics_failure_preserves_bounded_diagnostic(
    monkeypatch,
    caplog,
):
    import lib.search_runtime as runtime
    import lib.server_linkage_forensics as forensics

    def fail_forensics():
        raise OSError('synthetic maps failure')

    monkeypatch.setattr(forensics, 'capture_linkage_forensics', fail_forensics)
    with caplog.at_level(logging.DEBUG, logger='lib.search_runtime'):
        diagnostic = runtime._linkage_diagnostic(
            ImportError('GLIBCXX symbol is unavailable'))

    assert diagnostic == ' | LINKAGE: unavailable'
    assert 'Native linkage forensics unavailable: OSError' in caplog.text


@pytest.mark.unit
def test_bridge_first_install_is_exactly_once_under_concurrency(monkeypatch):
    import lib.search_bridge as bridge

    calls: list[str] = []
    calls_lock = threading.Lock()

    def record(name):
        def _record(_value=None):
            with calls_lock:
                calls.append(name)
            if name == 'sync':
                time.sleep(0.02)
        return _record

    fake_runtime = SimpleNamespace(
        register_browser_provider=record('browser'),
        register_site_search_provider=record('site_search'),
        register_auth_source_provider=record('auth'),
        register_site_knowledge_provider=record('knowledge'),
        register_site_drift_listener=record('drift'),
    )
    monkeypatch.setattr(bridge, '_installed', False)
    monkeypatch.setattr(bridge, 'tofu_search', fake_runtime)
    monkeypatch.setattr(bridge, 'sync_search_config', record('sync'))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _index: bridge.install_search_bridge(), range(24)))

    assert calls.count('sync') == 1
    for provider in ('browser', 'site_search', 'auth', 'knowledge', 'drift'):
        assert calls.count(provider) == 1
