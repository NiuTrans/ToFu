#!/usr/bin/env python3
"""tests/test_search_settings.py — lib/search_settings.py + update_search_settings tool.

Pins the single-source-of-truth contract shared by the Settings UI status
projection and the agent tool:

  * apply_updates validates / clamps / persists / hot-reloads / audit-logs.
  * max_download_mb is the MB alias of max_bytes (humans think in MB).
  * block_domain/unblock_domain edit skip_domains with the SAME normaliser
    the optimizer's block_search_domain action uses.
  * A no-change call is a PURE READ (no file write, no reload, no audit).
  * An env var that shadows a saved knob is reported in ``notes`` — the
    caller must never believe a change landed when it did not.
  * status_payload carries the live backend facts the Settings strip shows.

The config file is redirected to tmp_path; reload_config and audit_log are
stubbed on the module namespace. No network, no live LLM. Deterministic.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import lib as _lib
import lib.search_settings as ss

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


# ═══════════════════════════════════════════════════════════
#  1. Pure read — zero changes writes NOTHING
# ═══════════════════════════════════════════════════════════

class TestPureRead:
    def test_no_changes_writes_nothing(self, cfg_file):
        res = ss.apply_updates({})
        assert res['ok'] is True
        assert res['applied'] == {} and res['errors'] == {}
        assert not cfg_file['path'].exists(), 'pure read must not create the file'
        cfg_file['reload'].assert_not_called()
        cfg_file['audit'].assert_not_called()

    def test_effective_reflects_lib_attrs(self, cfg_file, monkeypatch):
        monkeypatch.setattr(_lib, 'FETCH_TOP_N', 9, raising=False)
        monkeypatch.setattr(_lib, 'SKIP_DOMAINS', {'a.com'}, raising=False)
        res = ss.apply_updates({})
        assert res['effective']['fetch_top_n'] == 9
        assert res['effective']['skip_domains'] == ['a.com']


# ═══════════════════════════════════════════════════════════
#  2. Write path — validate / clamp / persist / reload / audit
# ═══════════════════════════════════════════════════════════

class TestWritePath:
    def test_apply_persists_and_reloads(self, cfg_file):
        res = ss.apply_updates({'fetch_top_n': 8, 'llm_content_filter': False})
        assert res['ok'] is True
        saved = _saved(cfg_file['path'])
        assert saved['search']['fetch_top_n'] == 8
        assert saved['search']['llm_content_filter'] is False
        cfg_file['reload'].assert_called_once()
        cfg_file['audit'].assert_called_once()
        assert cfg_file['audit'].call_args[0][0] == 'search_settings_update'

    def test_ints_are_clamped_not_stored_out_of_range(self, cfg_file):
        res = ss.apply_updates({'fetch_top_n': 99, 'fetch_timeout': 1,
                                'max_chars_search': 10**9})
        assert res['applied']['fetch_top_n'] == 20
        assert res['applied']['fetch_timeout'] == 5
        assert res['applied']['max_chars_search'] == 500_000
        saved = _saved(cfg_file['path'])['search']
        assert saved['fetch_top_n'] == 20 and saved['fetch_timeout'] == 5

    def test_digit_string_coerced(self, cfg_file):
        res = ss.apply_updates({'fetch_timeout': '30'})
        assert res['applied']['fetch_timeout'] == 30
        assert res['errors'] == {}

    def test_bool_rejects_non_bool(self, cfg_file):
        res = ss.apply_updates({'llm_content_filter': 'yes'})
        assert res['ok'] is False
        assert 'llm_content_filter' in res['errors']
        assert not cfg_file['path'].exists()
        cfg_file['reload'].assert_not_called()

    def test_bool_is_not_accepted_as_int(self, cfg_file):
        res = ss.apply_updates({'fetch_top_n': True})
        assert 'fetch_top_n' in res['errors']

    def test_unknown_key_rejected_and_nothing_written(self, cfg_file):
        res = ss.apply_updates({'rm_rf': 1})
        assert res['ok'] is False
        assert 'rm_rf' in res['errors']
        assert not cfg_file['path'].exists()
        cfg_file['reload'].assert_not_called()

    def test_partial_valid_invalid_applies_valid_reports_error(self, cfg_file):
        res = ss.apply_updates({'fetch_top_n': 7, 'nope': 1})
        assert res['applied']['fetch_top_n'] == 7
        assert 'nope' in res['errors']
        assert _saved(cfg_file['path'])['search']['fetch_top_n'] == 7

    def test_merge_does_not_clobber_other_sections(self, cfg_file):
        cfg_file['path'].write_text(json.dumps({
            'providers': [{'id': 'p1'}],
            'search': {'fetch_timeout': 33},
        }))
        ss.apply_updates({'fetch_top_n': 4})
        saved = _saved(cfg_file['path'])
        assert saved['providers'] == [{'id': 'p1'}]
        assert saved['search']['fetch_timeout'] == 33
        assert saved['search']['fetch_top_n'] == 4

    def test_selecting_profile_clears_legacy_profile_knobs(self, cfg_file):
        cfg_file['path'].write_text(json.dumps({
            'search': {'fetch_top_n': 9, 'max_chars_search': 70000,
                       'fetch_timeout': 33},
        }))
        res = ss.apply_updates({'profile': 'fast'})
        saved = _saved(cfg_file['path'])['search']
        assert res['applied']['profile'] == 'fast'
        assert saved['profile'] == 'fast' and saved['overrides'] == {}
        assert 'fetch_top_n' not in saved and 'max_chars_search' not in saved
        assert saved['fetch_timeout'] == 33


# ═══════════════════════════════════════════════════════════
#  3. Aliases — MB download size + domain block/unblock
# ═══════════════════════════════════════════════════════════

class TestAliases:
    def test_max_download_mb_becomes_bytes(self, cfg_file):
        res = ss.apply_updates({'max_download_mb': 20})
        assert res['applied']['max_bytes'] == 20 * 1024 * 1024
        assert _saved(cfg_file['path'])['search']['max_bytes'] == 20971520

    def test_max_download_mb_rejects_junk(self, cfg_file):
        res = ss.apply_updates({'max_download_mb': -5})
        assert 'max_download_mb' in res['errors']
        assert not cfg_file['path'].exists()

    def test_block_domain_normalises_and_dedupes(self, cfg_file):
        res = ss.apply_updates({'block_domain': 'https://www.Spam.com:8080/x'})
        assert res['applied']['block_domain'] == ['spam.com']
        saved = _saved(cfg_file['path'])['search']['skip_domains']
        assert 'spam.com' in saved
        # again — stays deduped
        ss.apply_updates({'block_domain': 'spam.com'})
        saved2 = _saved(cfg_file['path'])['search']['skip_domains']
        assert saved2.count('spam.com') == 1

    def test_block_domain_invalid_rejected(self, cfg_file):
        res = ss.apply_updates({'block_domain': 'not-a-domain'})
        assert 'block_domain' in res['errors']

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


# ═══════════════════════════════════════════════════════════
#  4. Env-shadow honesty — saved but overridden is REPORTED
# ═══════════════════════════════════════════════════════════

class TestEnvOverride:
    def test_env_set_produces_note(self, cfg_file, monkeypatch):
        monkeypatch.setenv('FETCH_TOP_N', '3')
        res = ss.apply_updates({'fetch_top_n': 8})
        assert res['applied']['fetch_top_n'] == 8          # still persisted
        assert any('FETCH_TOP_N' in n for n in res['notes'])

    def test_env_unset_no_note(self, cfg_file, monkeypatch):
        monkeypatch.delenv('FETCH_TOP_N', raising=False)
        res = ss.apply_updates({'fetch_top_n': 8})
        assert res['notes'] == []


# ═══════════════════════════════════════════════════════════
#  5. status_payload + normalise_domain + the tool handler
# ═══════════════════════════════════════════════════════════

class TestStatusAndSharedBits:
    def test_status_payload_shape(self):
        st = ss.status_payload()
        assert st['ok'] is True
        for key in ('tofu_search_version', 'searxng_instances', 'filter_mode',
                    'filter_model', 'search_deadline_secs',
                    'fetch_url_deadline_secs', 'extension_connected'):
            assert key in st, key
        assert isinstance(st['searxng_instances'], int)

    def test_normalise_domain_shared_with_optimizer(self):
        from lib.optimizer.actions import block_search_domain as b
        for raw in ('https://www.Spammy.Example:8080/path', '  HTTP://Foo.com  ', ''):
            assert b._normalise_domain(raw) == ss.normalise_domain(raw)

    def test_handler_roundtrip(self, cfg_file, monkeypatch):
        """The tool handler is a thin translator: args in → applied text out."""
        from lib.tasks_pkg.handlers.search import _settings as h
        monkeypatch.setattr(h, '_finalize_tool_round', lambda *a, **k: None)
        task, round_entry = {}, {'query': 'update_search_settings'}
        _id, content, ok = h._handle_update_search_settings(
            task, None, 'update_search_settings', 'tc1',
            {'fetch_top_n': 5, 'max_download_mb': 10}, 1, round_entry,
            {}, None, False)
        assert ok is True
        assert 'fetch_top_n=5' in content
        assert 'max_download_mb=10' in content
        saved = _saved(cfg_file['path'])['search']
        assert saved['fetch_top_n'] == 5 and saved['max_bytes'] == 10 * 1024 * 1024

    def test_handler_read_mode(self, cfg_file, monkeypatch):
        from lib.tasks_pkg.handlers.search import _settings as h
        monkeypatch.setattr(h, '_finalize_tool_round', lambda *a, **k: None)
        _id, content, ok = h._handle_update_search_settings(
            {}, None, 'update_search_settings', 'tc1', {}, 1,
            {'query': 'update_search_settings'}, {}, None, False)
        assert ok is True
        assert 'no changes requested' in content
        assert not cfg_file['path'].exists()
