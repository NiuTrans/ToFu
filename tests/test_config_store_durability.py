"""Atomicity/concurrency contracts for durable JSON configuration stores."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest import mock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def mcp_store(tmp_path, monkeypatch):
    from lib.mcp import config as mcp_config

    path = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(mcp_config, '_config_path', lambda: str(path))
    return mcp_config, path


def test_mcp_config_is_private_and_does_not_mutate_input(mcp_store):
    mcp_config, path = mcp_store
    original = {'srv': {'command': 'one', 'env': {'TOKEN': 'secret'}}}

    assert mcp_config.save_mcp_config(original) is True

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert original == {
        'srv': {'command': 'one', 'env': {'TOKEN': 'secret'}},
    }


def test_concurrent_mcp_upserts_preserve_every_server(mcp_store):
    mcp_config, _path = mcp_store

    def upsert(index):
        mcp_config.upsert_server(
            f'server-{index}', {'command': f'command-{index}'})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(upsert, range(32)))

    config = mcp_config.load_mcp_config()
    assert set(config) == {f'server-{index}' for index in range(32)}


def test_failed_mcp_upsert_preserves_last_good_config(mcp_store):
    mcp_config, path = mcp_store
    mcp_config.upsert_server('old', {'command': 'old-command'})
    before = path.read_bytes()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk failure')):
        with pytest.raises(OSError, match='injected disk failure'):
            mcp_config.upsert_server('new', {'command': 'new-command'})

    assert path.read_bytes() == before
    assert set(mcp_config.load_mcp_config()) == {'old'}


def test_concurrent_mcp_row_patches_preserve_distinct_fields(mcp_store):
    mcp_config, _path = mcp_store
    mcp_config.upsert_server('shared', {
        'command': 'runner', 'enabled': True, 'disabled_tools': [],
    })

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: mcp_config.patch_server('shared', *item), [
            ({'enabled': False},),
            ({'disabled_tools': ['dangerous']},),
        ]))

    row = mcp_config.load_mcp_config()['shared']
    assert row['command'] == 'runner'
    assert row['enabled'] is False
    assert row['disabled_tools'] == ['dangerous']


@pytest.fixture
def feature_store(tmp_path, monkeypatch):
    import lib
    from lib import features_store

    path = tmp_path / 'features.json'
    monkeypatch.setattr(
        features_store, '_config_path', lambda _name: str(path))
    flags = [
        ('pptx_translate_enabled', 'PPTX_TRANSLATE_ENABLED'),
        ('cache_extended_ttl', 'CACHE_EXTENDED_TTL'),
        ('debug_mode', 'DEBUG_MODE'),
    ]
    monkeypatch.setattr(features_store, '_managed_flags', lambda: flags)
    monkeypatch.setattr(lib, 'PPTX_TRANSLATE_ENABLED', False)
    monkeypatch.setattr(lib, 'CACHE_EXTENDED_TTL', False)
    monkeypatch.setattr(lib, 'DEBUG_MODE', False)
    return features_store, path


@pytest.fixture
def feature_admin_principal():
    from lib.identity import PrincipalContext

    return PrincipalContext.user(
        subject_id='feature-admin-23',
        owner_user_id=23,
        scopes={'admin'},
    )


def test_concurrent_feature_updates_preserve_distinct_flags(
        feature_store, feature_admin_principal):
    features_store, _path = feature_store
    updates = [
        {'pptx_translate_enabled': True},
        {'cache_extended_ttl': True},
        {'debug_mode': True},
    ]

    with ThreadPoolExecutor(max_workers=3) as pool:
        apply_update = partial(
            features_store.apply_feature_updates,
            principal=feature_admin_principal,
        )
        results = list(pool.map(apply_update, updates))

    assert all('error' not in result for result in results)
    assert features_store.read_features() == {
        'pptx_translate_enabled': True,
        'cache_extended_ttl': True,
        'debug_mode': True,
    }


def test_failed_feature_update_preserves_old_file_and_live_value(
        feature_store, feature_admin_principal):
    import lib

    features_store, path = feature_store
    assert 'error' not in features_store.apply_feature_updates(
        {'debug_mode': False}, principal=feature_admin_principal)
    before = path.read_bytes()
    lib.DEBUG_MODE = False

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk failure')):
        result = features_store.apply_feature_updates(
            {'debug_mode': True}, principal=feature_admin_principal)

    assert result == {'error': 'internal_error'}
    assert path.read_bytes() == before
    assert lib.DEBUG_MODE is False


def test_concurrent_feature_hot_reload_matches_last_persisted_value(
        feature_store, feature_admin_principal):
    import lib

    features_store, _path = feature_store
    updates = [{'debug_mode': bool(index % 2)} for index in range(24)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        apply_update = partial(
            features_store.apply_feature_updates,
            principal=feature_admin_principal,
        )
        list(pool.map(apply_update, updates))

    assert lib.DEBUG_MODE is features_store.read_features()['debug_mode']


def test_feature_updates_default_deny_before_file_or_live_mutation(
        feature_store):
    import lib
    from lib.identity import PrincipalContext

    features_store, path = feature_store
    principals = (
        (None, TypeError),
        (
            PrincipalContext.user(
                subject_id='feature-reader-23', owner_user_id=23,
                scopes={'chat'}),
            PermissionError,
        ),
        (
            PrincipalContext.system(
                subject_id='ownerless-feature-admin', scopes={'admin'}),
            PermissionError,
        ),
    )
    for principal, error in principals:
        with pytest.raises(error):
            features_store.apply_feature_updates(
                {'debug_mode': True}, principal=principal)

    assert not path.exists()
    assert lib.DEBUG_MODE is False


def test_optimizer_feature_toggle_uses_the_authenticated_owner(
        feature_store, feature_admin_principal, monkeypatch):
    import lib
    import lib.scheduler.manager as scheduler_manager

    features_store, _path = feature_store
    monkeypatch.setattr(
        features_store, '_managed_flags',
        lambda: [('optimizer_enabled', 'OPTIMIZER_ENABLED')])
    monkeypatch.setattr(lib, 'OPTIMIZER_ENABLED', False)
    calls = []

    class Scheduler:
        def list_tasks(self, *, user_id, include_disabled):
            calls.append(('list', user_id, include_disabled))
            return [{
                'id': 'owner-23-optimizer',
                'task_type': 'optimizer',
                'name': 'Daily Optimizer',
            }]

        def toggle_task(self, task_id, *, user_id, enabled):
            calls.append(('toggle', task_id, user_id, enabled))

    monkeypatch.setattr(scheduler_manager, 'get_scheduler', Scheduler)
    result = features_store.apply_feature_updates(
        {'optimizer_enabled': True}, principal=feature_admin_principal)

    assert result['saved']['optimizer_enabled'] is True
    assert calls == [
        ('list', 23, True),
        ('toggle', 'owner-23-optimizer', 23, True),
    ]


def test_search_updates_merge_with_concurrent_server_config_writer(
        tmp_path, monkeypatch):
    import lib
    from lib import search_settings
    from lib.json_store import update_json_atomic

    path = tmp_path / 'server_config.json'
    monkeypatch.setattr(search_settings, '_CONFIG_FILE', str(path))
    monkeypatch.setattr(search_settings, 'audit_log', mock.Mock())
    monkeypatch.setattr(lib, 'reload_config', mock.Mock())

    def update_search():
        result = search_settings.apply_updates({'fetch_top_n': 9})
        assert result['ok'] is True

    def update_provider():
        def mutate(config):
            config['providers'] = [{'id': 'provider-concurrent'}]
            return config

        update_json_atomic(str(path), mutate, default={}, strict=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn: fn(), [update_search, update_provider]))

    from lib.json_store import read_json
    saved = read_json(str(path), default={})
    assert saved['providers'] == [{'id': 'provider-concurrent'}]
    assert saved['search']['fetch_top_n'] == 9


@pytest.fixture
def desktop_store(tmp_path, monkeypatch):
    from lib.desktop_agent import config as agent_config

    path = tmp_path / 'private' / 'desktop_agent.json'
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(path))
    return agent_config, path


def test_desktop_config_is_private_and_concurrent_updates_merge(desktop_store):
    agent_config, path = desktop_store

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                agent_config.save_remote_server,
                'https://tofu.example', 'bridge-secret'),
            pool.submit(
                agent_config.save_computer_control,
                True, {'allow_exec': True}),
        ]
        for future in futures:
            future.result()

    stored = agent_config.load_config()
    assert stored['remote_server'] == {
        'url': 'https://tofu.example', 'secret': 'bridge-secret',
    }
    assert stored['computer_control']['enabled'] is True
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_failed_desktop_update_preserves_last_good_config(desktop_store):
    agent_config, path = desktop_store
    agent_config.save_remote_server('https://old.example', 'old-secret')
    before = path.read_bytes()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk failure')):
        with pytest.raises(OSError, match='injected disk failure'):
            agent_config.save_computer_control(True, {'allow_exec': True})

    assert path.read_bytes() == before
    assert agent_config.remote_server() == (
        'https://old.example', 'old-secret')


def test_concurrent_agent_id_initialization_uses_one_stable_id(desktop_store):
    from lib.desktop_agent import _run

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _index: _run._ensure_agent_id(), range(16)))

    assert len(set(ids)) == 1


def test_desktop_ui_field_writers_never_use_stale_full_snapshots(
        desktop_store, monkeypatch):
    from desktop import agent_launcher, role_window

    agent_config, _path = desktop_store
    agent_config.save_remote_server('https://tofu.example', 'secret')

    # Public load_config is deliberately poisoned.  Field writers must enter
    # update_config directly; load→edit→save can clobber a concurrent field.
    monkeypatch.setattr(
        agent_config, 'load_config',
        mock.Mock(side_effect=AssertionError('stale snapshot path used')))
    role_window.persist_show_at_startup(False)
    agent_launcher._persist_autostart(False)

    stored = agent_config.update_config(lambda cfg: cfg)
    assert stored['show_role_window'] is False
    assert stored['autostart'] is False
    assert stored['remote_server'] == {
        'url': 'https://tofu.example', 'secret': 'secret',
    }


def test_autostart_reconcile_resolves_absence_inside_the_transaction(
        desktop_store, monkeypatch):
    from desktop import agent_launcher

    agent_config, _path = desktop_store
    agent_config.save_remote_server('https://tofu.example', 'secret')
    monkeypatch.setattr(
        agent_config, 'load_config',
        mock.Mock(side_effect=AssertionError('stale snapshot path used')))
    monkeypatch.setattr(agent_launcher, '_autostart_get', lambda: True)
    apply = mock.Mock()
    monkeypatch.setattr(agent_launcher, '_autostart_apply', apply)

    agent_launcher._reconcile_autostart()

    stored = agent_config.update_config(lambda cfg: cfg)
    assert stored['autostart'] is True
    assert stored['remote_server']['secret'] == 'secret'
    apply.assert_called_once_with(True)
