"""tests/test_mcp_catalog_internal_only.py — opensource catalog filtering.

Pins the contract that ``internal_only`` catalog entries (hope / llm /
xuecheng — Meituan-internal MCP servers whose launchers aren't shipped to
opensource) are:

  * present in the full internal/personal build, and
  * filtered out of ``get_catalog`` / ``get_catalog_entry`` / install in an
    opensource build,

so the UI never renders a dead "Install" button — independent of where the
entries sit in the source file (belt-and-braces backstop to export.py's
source strip).
"""

from __future__ import annotations

import importlib

import pytest

import lib.mcp.registry as reg

_INTERNAL_IDS = {'hope', 'llm', 'xuecheng'}

# In an opensource build the internal launchers are stripped from the source
# catalog and ``TOFU_OPENSOURCE_BUILD`` is baked on, so the "present in the
# internal build" assertions below are not applicable.  The hidden-in-
# opensource and no-leak guards still run (and matter) in both builds.
_OPENSOURCE = reg.is_opensource_build()


def _reload_with_flag(monkeypatch, value):
    """Reload the registry module with TOFU_OPENSOURCE_BUILD set to ``value``."""
    if value is None:
        monkeypatch.delenv('TOFU_OPENSOURCE_BUILD', raising=False)
    else:
        monkeypatch.setenv('TOFU_OPENSOURCE_BUILD', value)
    return importlib.reload(reg)


@pytest.mark.skipif(
    _OPENSOURCE,
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_internal_entries_present_in_internal_build(monkeypatch):
    r = _reload_with_flag(monkeypatch, None)
    try:
        assert r.is_opensource_build() is False
        ids = {e['id'] for e in r.get_catalog()}
        assert _INTERNAL_IDS <= ids
        for sid in _INTERNAL_IDS:
            assert r.get_catalog_entry(sid) is not None
    finally:
        _reload_with_flag(monkeypatch, None)


@pytest.mark.skipif(
    _OPENSOURCE,
    reason='internal MCP launchers are stripped from opensource builds',
)
def test_internal_entries_flagged():
    # The three internal servers must carry the explicit flag so filtering
    # never relies on source ordering / the section banner.
    by_id = {e['id']: e for e in reg.CATALOG}
    for sid in _INTERNAL_IDS:
        assert by_id[sid].get('internal_only') is True, sid


def test_internal_entries_hidden_in_opensource_build(monkeypatch):
    r = _reload_with_flag(monkeypatch, '1')
    try:
        assert r.is_opensource_build() is True
        ids = {e['id'] for e in r.get_catalog()}
        assert not (_INTERNAL_IDS & ids), f'leaked: {_INTERNAL_IDS & ids}'
        # Lookup + config build must also refuse hidden ids.
        for sid in _INTERNAL_IDS:
            assert r.get_catalog_entry(sid) is None
            assert r.build_server_config(sid) is None
        # Public entries survive.
        assert 'github' in ids
        assert 'overleaf' in ids
    finally:
        _reload_with_flag(monkeypatch, None)


def test_no_internal_only_among_public_entries(monkeypatch):
    # Guard against accidentally flagging a public server internal_only.
    r = _reload_with_flag(monkeypatch, None)
    try:
        for e in r.get_catalog():
            if e.get('internal_only'):
                assert e['id'] in _INTERNAL_IDS, e['id']
    finally:
        _reload_with_flag(monkeypatch, None)


# ── Catalog hot-reload (no restart needed) ───────────────

import os as _os  # noqa: E402
import time as _time  # noqa: E402


def _seed_registry_copy(tmp_path, monkeypatch, ids):
    """Write a throwaway registry-shaped source file and point reload at it.

    Returns the path. The module's CATALOG holds one minimal card per id. We
    monkeypatch reg._registry_path so _reload_catalog_if_changed re-execs THIS
    file instead of the real registry.py — fully isolating the test.
    """
    path = tmp_path / 'reg_copy.py'
    cards = ',\n'.join(
        "    {'id': %r, 'name': %r, 'command': %r, 'args': [], 'tags': []}"
        % (i, i.upper(), f'{i}-mcp') for i in ids
    )
    path.write_text('CATALOG = [\n' + cards + '\n]\n', encoding='utf-8')
    monkeypatch.setattr(reg, '_registry_path', lambda: str(path))
    return path


def test_catalog_hot_reload_picks_up_new_card(tmp_path, monkeypatch):
    # A card appended to registry.py while running must appear on the next
    # get_catalog(), without a restart — and the CATALOG list / _CATALOG_INDEX
    # dict identities must be preserved (in-place rebuild).
    monkeypatch.setattr(reg, '_OPENSOURCE_BUILD', False)
    # Reset live containers to a known small state owned by the test.
    monkeypatch.setattr(reg, 'CATALOG', [{'id': 'seed', 'name': 'Seed',
                                          'command': 'seed-mcp', 'args': []}])
    monkeypatch.setattr(reg, '_CATALOG_INDEX', {'seed': reg.CATALOG[0]})
    list_id, index_id = id(reg.CATALOG), id(reg._CATALOG_INDEX)

    path = _seed_registry_copy(tmp_path, monkeypatch, ['seed', 'fresh-card'])
    # Baseline below the file's mtime so the gate fires.
    monkeypatch.setattr(reg, '_catalog_mtime', _os.path.getmtime(path) - 100)

    ids = {e['id'] for e in reg.get_catalog()}
    assert 'fresh-card' in ids
    assert reg.get_catalog_entry('fresh-card') is not None
    # Identity preserved → existing references stay valid.
    assert id(reg.CATALOG) == list_id
    assert id(reg._CATALOG_INDEX) == index_id


def test_catalog_hot_reload_survives_broken_edit(tmp_path, monkeypatch):
    # A syntactically broken mid-save must NOT wipe the catalog.
    monkeypatch.setattr(reg, '_OPENSOURCE_BUILD', False)
    good = [{'id': 'keep1', 'name': 'K1', 'command': 'k1-mcp', 'args': []},
            {'id': 'keep2', 'name': 'K2', 'command': 'k2-mcp', 'args': []}]
    monkeypatch.setattr(reg, 'CATALOG', list(good))
    monkeypatch.setattr(reg, '_CATALOG_INDEX', {e['id']: e for e in good})

    path = tmp_path / 'reg_broken.py'
    path.write_text('CATALOG = [ this is not valid python {{{\n', encoding='utf-8')
    monkeypatch.setattr(reg, '_registry_path', lambda: str(path))
    monkeypatch.setattr(reg, '_catalog_mtime', _os.path.getmtime(path) - 100)

    ids = {e['id'] for e in reg.get_catalog()}
    assert ids == {'keep1', 'keep2'}          # last-good preserved


def test_catalog_hot_reload_noop_when_unchanged(tmp_path, monkeypatch):
    # No mtime advance → no re-exec (cheap path).
    path = _seed_registry_copy(tmp_path, monkeypatch, ['only'])
    # Baseline ABOVE the file mtime so the gate never fires.
    monkeypatch.setattr(reg, '_catalog_mtime', _os.path.getmtime(path) + 100)
    monkeypatch.setattr(reg, '_OPENSOURCE_BUILD', False)
    sentinel = [{'id': 'sentinel', 'name': 'S', 'command': 's-mcp', 'args': []}]
    monkeypatch.setattr(reg, 'CATALOG', sentinel)
    monkeypatch.setattr(reg, '_CATALOG_INDEX', {'sentinel': sentinel[0]})

    ids = {e['id'] for e in reg.get_catalog()}
    assert ids == {'sentinel'}                # untouched, no reload from file
