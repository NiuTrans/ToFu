"""Optional provider discovery stays dormant and publishes once on demand."""
import concurrent.futures
import threading
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def isolated_registry(monkeypatch):
    from lib.llm_dispatch import provider_registry as registry
    monkeypatch.setattr(registry, '_DIALECTS', {})
    monkeypatch.setattr(registry, '_discovery_complete', False)
    monkeypatch.setattr(registry, '_discovery_in_progress', False)
    return registry


def test_builtin_slots_and_body_building_never_scan_installed_plugins():
    import importlib.metadata
    from lib.llm_dispatch.slot import Slot, THINKING_FORMATS
    from lib.llm.body import build_body
    with patch.object(importlib.metadata, 'entry_points', side_effect=AssertionError('unwanted discovery')):
        for key in THINKING_FORMATS:
            Slot('fixture', '', 'fixture', {'text'}, thinking_format=key)
            build_body('fixture', [{'role': 'user', 'content': 'hello'}], thinking_format=key)


def test_concurrent_custom_lookup_waits_for_one_complete_discovery(monkeypatch):
    registry = isolated_registry(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    dialect = registry.BodyDialect('custom_fixture', lambda *args, **kwargs: None)
    def discover():
        calls.append(True)
        # Discovery callbacks may enumerate their own partial registry.
        assert registry.dialect_keys() == frozenset()
        entered.set()
        assert release.wait(5)
        registry.register_dialect(dialect)
        return 1
    monkeypatch.setattr(registry, 'discover_provider_plugins', discover)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registry.get_dialect, dialect.key)
        assert entered.wait(5)
        second = executor.submit(registry.is_valid_thinking_format, dialect.key)
        release.set()
        assert first.result(timeout=5) is dialect
        assert second.result(timeout=5) is True
    assert calls == [True]
    assert registry.dialect_keys() == frozenset({dialect.key})
    assert registry.all_dialects() == [dialect]
    assert registry.is_valid_thinking_format('typo_fixture') is False


def test_interrupted_discovery_remains_retryable(monkeypatch):
    registry = isolated_registry(monkeypatch)
    failure = KeyboardInterrupt('fixture interruption')
    def interrupted():
        raise failure
    monkeypatch.setattr(registry, 'discover_provider_plugins', interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        registry.get_dialect('custom_fixture')
    assert caught.value is failure
    dialect = registry.BodyDialect('custom_fixture', lambda *args, **kwargs: None)
    def retry():
        registry.register_dialect(dialect)
        return 1
    monkeypatch.setattr(registry, 'discover_provider_plugins', retry)
    assert registry.get_dialect(dialect.key) is dialect


def test_installed_entry_point_loads_before_custom_slot_admission(monkeypatch):
    import importlib.metadata
    from types import SimpleNamespace
    from lib.llm_dispatch.slot import Slot
    registry = isolated_registry(monkeypatch)
    calls = []
    dialect = registry.BodyDialect('installed_fixture', lambda *args, **kwargs: None)
    def entry_points(*, group):
        calls.append(group)
        return [SimpleNamespace(name='fixture', load=lambda: lambda register: register(dialect))]
    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    slot = Slot('fixture', '', 'fixture', {'text'}, thinking_format=dialect.key)
    assert slot.thinking_format == dialect.key
    assert registry.get_dialect(dialect.key) is dialect
    assert registry.is_valid_thinking_format('installed_typo') is False
    assert calls == ['tofu.providers']


def test_fresh_registry_import_and_builtin_body_keep_discovery_dormant():
    import os
    import subprocess
    import sys
    from pathlib import Path
    source = """
import importlib.metadata
from unittest.mock import patch
with patch.object(importlib.metadata, 'entry_points', side_effect=AssertionError('unwanted discovery')):
    from lib.llm_dispatch.provider_registry import get_dialect, is_valid_thinking_format
    from lib.llm_dispatch.slot import Slot, THINKING_FORMATS
    from lib.llm.body import build_body
    for key in THINKING_FORMATS:
        assert is_valid_thinking_format(key)
        assert get_dialect(key) is None
        Slot('fixture', '', 'fixture', {'text'}, thinking_format=key)
        build_body('fixture', [{'role': 'user', 'content': 'hello'}], thinking_format=key)
print('dormant')
"""
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    result = subprocess.run([sys.executable, '-c', source],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1200:]
    assert result.stdout.strip() == 'dormant'
