"""Resource bounds for optional project gitignore suggestions."""

from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_registry():
    import lib.project_mod.gitignore_suggest as suggestions

    with suggestions._lock:
        suggestions._registry.clear()
    yield
    with suggestions._lock:
        suggestions._registry.clear()


def test_registry_bounds_project_roots_and_does_not_retain_empty_probes(
        tmp_path, monkeypatch):
    import lib.project_mod.gitignore_suggest as suggestions

    roots = [tmp_path / name for name in ('a', 'b', 'c', 'empty')]
    for root in roots:
        root.mkdir()
    now = {'value': 100.0}
    monkeypatch.setattr(suggestions, '_MAX_BASES', 2)
    monkeypatch.setattr(suggestions.time, 'time', lambda: now['value'])
    monkeypatch.setattr(suggestions, 'audit_log', lambda *_a, **_k: None)
    monkeypatch.setattr(
        suggestions,
        '_probe_top_dirs',
        lambda base, _ignored: [] if base.endswith('empty') else [{
            'dir': f'generated-{os.path.basename(base)}',
            'entry_count': 2_000,
        }],
    )

    for root in roots[:3]:
        suggestions.record_timeout_and_probe(str(root))
        now['value'] += 1
    assert suggestions.get_suggestions(str(roots[0])) == []
    assert suggestions.get_suggestions(str(roots[1]))
    assert suggestions.get_suggestions(str(roots[2]))

    suggestions.record_timeout_and_probe(str(roots[3]))
    assert suggestions.get_suggestions(str(roots[3])) == []
    assert suggestions.get_suggestions(str(roots[1]))
    assert suggestions.get_suggestions(str(roots[2]))


def test_registry_reaps_expired_roots_before_admitting_a_new_one(
        tmp_path, monkeypatch):
    import lib.project_mod.gitignore_suggest as suggestions

    old_root = tmp_path / 'old'
    new_root = tmp_path / 'new'
    old_root.mkdir()
    new_root.mkdir()
    now = {'value': 100.0}
    monkeypatch.setattr(suggestions, '_MAX_BASES', 1)
    monkeypatch.setattr(suggestions, '_SUGGESTION_TTL_S', 10.0)
    monkeypatch.setattr(suggestions.time, 'time', lambda: now['value'])
    monkeypatch.setattr(suggestions, 'audit_log', lambda *_a, **_k: None)
    monkeypatch.setattr(
        suggestions,
        '_probe_top_dirs',
        lambda *_args: [{'dir': 'build', 'entry_count': 2_000}],
    )

    suggestions.record_timeout_and_probe(str(old_root))
    now['value'] = 111.0
    suggestions.record_timeout_and_probe(str(new_root))
    assert suggestions.get_suggestions(str(old_root)) == []
    assert suggestions.get_suggestions(str(new_root))
