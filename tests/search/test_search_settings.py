#!/usr/bin/env python3
"""tests/search/test_search_settings.py — lib/search/search_settings.py + update_search_settings tool.

Pins the contract of the one saved search behaviour, the fetch blocklist:

  * block_domain/unblock_domain edit skip_domains with the SAME normaliser
    the optimizer's block_search_domain action uses, then persist, hot-reload
    and audit-log.
  * A no-change call is a PURE READ (no file write, no reload, no audit).
  * Search limits are fixed tofu-search defaults: retired tuning keys are
    rejected with a reason and nothing is written.

The config file is redirected to tmp_path; reload_config and audit_log are
stubbed on the module namespace. No network, no live LLM. Deterministic.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import lib as _lib
import lib.search.search_settings as ss
from lib.tools.search import build_update_search_settings_tool

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Redirect the config file; stub reload + audit; return the path."""
    path = tmp_path / 'server_config.json'
    monkeypatch.setattr(ss, '_CONFIG_FILE', str(path))
    reload_mock = mock.Mock()
    monkeypatch.setattr(_lib, 'reload_config', reload_mock)
    audit_mock = mock.Mock()
    monkeypatch.setattr(ss, 'audit_log', audit_mock)
    return {'path': path, 'reload': reload_mock, 'audit': audit_mock}


def _saved(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


class TestPureRead:
    def test_no_changes_writes_nothing(self, cfg_file):
        res = ss.apply_updates({})
        assert res['ok'] is True
        assert res['applied'] == {} and res['errors'] == {}
        assert not cfg_file['path'].exists(), 'pure read must not create the file'
        cfg_file['reload'].assert_not_called()
        cfg_file['audit'].assert_not_called()

    def test_effective_reflects_live_blocklist(self, cfg_file, monkeypatch):
        monkeypatch.setattr(_lib, 'SKIP_DOMAINS', {'b.com', 'a.com'}, raising=False)
        assert ss.apply_updates({})['effective'] == {'skip_domains': ['a.com', 'b.com']}


class TestToolSchema:
    def test_model_contract_is_the_server_wide_blocklist(self):
        schema = build_update_search_settings_tool()['function']
        desc = schema['description'].lower()
        for phrase in ('server-wide', 'pure read', 'persist', 'hot-reload',
                       'every conversation', 'user asks', 'fixed defaults',
                       'explain the outcome'):
            assert phrase in desc, phrase
        assert set(schema['parameters']['properties']) == {'block_domain', 'unblock_domain'}

    def test_blocklist_change_is_rendered_as_a_global_write(self):
        from lib.tasks_pkg.tool_dispatch._approval import (
            _approval_meta_update_search_settings,
        )

        meta = {}
        _approval_meta_update_search_settings(meta, {'block_domain': 'spam.example'})

        assert meta['path'] == 'Setting: block_domain'
        rendered = json.dumps(meta, ensure_ascii=False)
        assert 'spam.example' in rendered and 'SERVER-WIDE' in rendered
        assert 'read-only' not in rendered


class TestBlocklistWrites:
    def test_block_domain_normalises_persists_reloads_and_dedupes(self, cfg_file):
        res = ss.apply_updates({'block_domain': 'https://www.Spam.com:8080/x'})
        assert res['ok'] is True
        # A path in the entry is a URL-PREFIX rule and is preserved: stripping
        # it would silently widen "block one repository/directory" into
        # "block the whole host". Host-only entries still drop scheme/www/port.
        assert res['applied']['block_domain'] == ['spam.com/x']
        assert 'spam.com/x' in _saved(cfg_file['path'])['search']['skip_domains']
        cfg_file['reload'].assert_called_once()
        assert cfg_file['audit'].call_args[0][0] == 'search_settings_update'
        ss.apply_updates({'block_domain': 'spam.com/x'})
        assert _saved(cfg_file['path'])['search']['skip_domains'].count('spam.com/x') == 1

    def test_block_host_entry_still_strips_scheme_www_and_port(self, cfg_file):
        res = ss.apply_updates({'block_domain': 'https://www.Spam.com:8080'})
        assert res['applied']['block_domain'] == ['spam.com']
        ss.apply_updates({'block_domain': 'spam.com'})
        assert _saved(cfg_file['path'])['search']['skip_domains'].count('spam.com') == 1

    def test_block_domain_invalid_rejected(self, cfg_file):
        res = ss.apply_updates({'block_domain': 'not-a-domain'})
        assert res['ok'] is False and 'block_domain' in res['errors']
        assert not cfg_file['path'].exists()

    def test_unblock_removes(self, cfg_file, monkeypatch):
        monkeypatch.setattr(_lib, 'SKIP_DOMAINS', {'keep.com', 'drop.com'},
                            raising=False)
        ss.apply_updates({'block_domain': 'drop.com'})
        res = ss.apply_updates({'unblock_domain': 'www.drop.com'})
        assert res['applied']['unblock_domain'] == ['drop.com']
        saved = _saved(cfg_file['path'])['search']['skip_domains']
        assert 'drop.com' not in saved and 'keep.com' in saved

    def test_skip_domains_seed_never_shrinks_effective_set(self, cfg_file,
                                                           monkeypatch):
        """No saved list: seed from lib.SKIP_DOMAINS so blocking one host can
        never silently drop the built-in blocklist."""
        monkeypatch.setattr(_lib, 'SKIP_DOMAINS', {'builtin1.com', 'builtin2.com'},
                            raising=False)
        ss.apply_updates({'block_domain': 'new.com'})
        saved = set(_saved(cfg_file['path'])['search']['skip_domains'])
        assert saved == {'builtin1.com', 'builtin2.com', 'new.com'}

    def test_merge_does_not_clobber_other_sections(self, cfg_file):
        cfg_file['path'].write_text(json.dumps({
            'providers': [{'id': 'p1'}],
            'search': {'gateway': {'enabled': False}},
        }))
        ss.apply_updates({'block_domain': 'spam.com'})
        saved = _saved(cfg_file['path'])
        assert saved['providers'] == [{'id': 'p1'}]
        assert saved['search']['gateway'] == {'enabled': False}


@pytest.mark.parametrize('key', ['fetch_top_n', 'profile', 'llm_content_filter',
                                 'max_download_mb', 'fetch_timeout', 'rm_rf'])
def test_retired_or_unknown_keys_are_rejected_without_any_write(cfg_file, key):
    res = ss.apply_updates({key: 7})
    assert res['ok'] is False
    assert 'fixed defaults' in res['errors'][key]
    assert not cfg_file['path'].exists()
    cfg_file['reload'].assert_not_called()
    cfg_file['audit'].assert_not_called()


def test_valid_block_still_applies_beside_a_rejected_key(cfg_file):
    res = ss.apply_updates({'block_domain': 'spam.com', 'fetch_top_n': 9})
    assert res['applied']['block_domain'] == ['spam.com']
    assert 'fetch_top_n' in res['errors']
    saved = _saved(cfg_file['path'])['search']
    assert 'spam.com' in saved['skip_domains'] and 'fetch_top_n' not in saved


def test_normalise_domain_shared_with_optimizer():
    from lib.optimizer.actions import block_search_domain as b
    for raw in ('https://www.Spammy.Example:8080/path', '  HTTP://Foo.com  ', ''):
        assert b._normalise_domain(raw) == ss.normalise_domain(raw)


class TestToolHandler:
    def test_handler_roundtrip(self, cfg_file, monkeypatch):
        """The tool handler is a thin translator: args in → applied text out."""
        import lib.tasks_pkg.handlers.search._settings as h
        presented = []
        monkeypatch.setattr(h, '_finalize_tool_round', lambda *a, **k: presented.extend(a[3]))
        _id, content, is_search = h._handle_update_search_settings(
            {}, None, 'update_search_settings', 'tc1',
            {'block_domain': 'spam.example'}, 1, {'query': 'update_search_settings'},
            {}, None, False)
        # The third element declares web evidence, never the update's success.
        assert is_search is False and presented[0]['settingsOk'] is True
        assert 'block_domain=spam.example' in content
        assert 'spam.example' in _saved(cfg_file['path'])['search']['skip_domains']

    def test_handler_read_mode(self, cfg_file, monkeypatch):
        import lib.tasks_pkg.handlers.search._settings as h
        presented = []
        monkeypatch.setattr(h, '_finalize_tool_round', lambda *a, **k: presented.extend(a[3]))
        monkeypatch.setattr(_lib, 'SKIP_DOMAINS', {'a.com'}, raising=False)
        _id, content, is_search = h._handle_update_search_settings(
            {}, None, 'update_search_settings', 'tc1', {}, 1,
            {'query': 'update_search_settings'}, {}, None, False)
        assert is_search is False and presented[0]['settingsOk'] is True
        assert 'no changes requested' in content and 'a.com' in content
        assert not cfg_file['path'].exists()

    def test_handler_reports_rejection_without_mutation(self, cfg_file, monkeypatch):
        import lib.tasks_pkg.handlers.search._settings as h
        presented = []
        monkeypatch.setattr(h, '_finalize_tool_round', lambda *a, **k: presented.extend(a[3]))
        _id, content, _ = h._handle_update_search_settings(
            {}, None, 'update_search_settings', 'tc1', {'max_chars_search': 1000}, 1,
            {'query': 'update_search_settings'}, {}, None, False)
        assert presented[0]['settingsOk'] is False
        assert 'Rejected:' in content and 'max_chars_search' in content
        assert not cfg_file['path'].exists()
