"""Database path HTTP contract; scratch settings only, never a live authority."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
from unittest.mock import Mock

import pytest

from lib.database_panel import probe
from lib.storage_sidecar import projection_paths

pytestmark = pytest.mark.api
pytest_plugins = ('tests._credential_sidecar',)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import server  # noqa: F401 — normal Quart compatibility bootstrap
    from quart import Quart
    from routes.api_v1.database_panel import api_v1_database_panel_bp

    app = Quart(__name__)
    app.config['TESTING'] = True
    from lib.http_error_handlers import register_http_error_handlers
    register_http_error_handlers(app)
    from routes.api_v1.auth import attach_rate_headers, bearer_auth_before_request
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)
    app.register_blueprint(api_v1_database_panel_bp)
    monkeypatch.delenv(projection_paths.SEARCH_PROJECTION_ENV, raising=False)
    monkeypatch.delenv('TOFU_STORAGE_FASTPATH_DIR', raising=False)
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')

    monkeypatch.setenv('TOFU_DB_FASTPATH_DIR', '')
    monkeypatch.delenv('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', raising=False)
    root = tmp_path / 'data'
    runtime = {'backend': 'tofudb', 'ready': True,
               'search_projection_directory': str(root / 'projections/tofudb-search')}
    engine = {'backend': 'tofudb', 'dataRoot': str(root)}
    monkeypatch.setattr(probe, 'data_root', lambda: root)
    monkeypatch.setattr(probe, 'engine_status', lambda runtime=None: engine)
    monkeypatch.setattr(probe, 'deployment_mode', lambda: 'personal')
    monkeypatch.setattr('lib.storage.service.storage_status', lambda: dict(runtime))
    monkeypatch.setattr('lib.database_panel.switch.env_file_path', lambda: tmp_path / '.env')
    monkeypatch.setattr('lib.database_panel.switch.describe', lambda engine=None: {})
    monkeypatch.setattr('lib.database_panel.switch.migration_status', lambda: {'job': None})
    monkeypatch.setattr('lib.database_panel.config_store.load_preferences', lambda: {})
    return app.test_client(), root, runtime, engine


def _post(panel, body):
    response = _run(panel[0].post('/api/v1/database/paths', json=body))
    return response.status_code, _run(response.get_json())


def test_status_separates_required_projection_from_optional_sqlite_fastpath(panel):
    response = _run(panel[0].get('/api/v1/database/status'))
    body = _run(response.get_json())
    assert response.status_code == 200
    state = body['searchProjection']
    assert state == {
        'required': True, 'directory': panel[2]['search_projection_directory'],
        'configuredDirectory': '', 'defaultDirectory': panel[2]['search_projection_directory'],
        'pendingDirectory': panel[2]['search_projection_directory'], 'restartRequired': False,
    }
    assert body['fastpath']['supported']
    assert body['fastpath']['kind'] == 'native'
    assert body['fastpath']['active'] is False
    assert body['fastpath']['mode'] != 'builtin'
    panel[3]['backend'] = 'sqlite'
    response = _run(panel[0].get('/api/v1/database/status'))
    assert _run(response.get_json())['searchProjection'] is None


def test_apply_and_reset_persist_only_next_launch_and_return_real_state(panel, tmp_path):
    before_environment = dict(os.environ)
    active = panel[2]['search_projection_directory']
    status, body = _post(panel, {'searchProjection': {'directory': str(tmp_path / 'cache')}})
    assert status == 200
    assert body['ok'] and body['restartRequired']
    state = body['searchProjection']
    assert state['directory'] == active
    assert state['configuredDirectory'] == str(tmp_path / 'cache')
    assert state['pendingDirectory'] != active
    assert state['pendingDirectory'].startswith(str(tmp_path / 'cache/tofudb-search/'))
    assert panel[2]['search_projection_directory'] == active
    assert dict(os.environ) == before_environment
    saved = json.loads((panel[1] / 'config/server_config.json').read_text())
    assert saved[projection_paths.SEARCH_PROJECTION_CONFIG_KEY] == str(tmp_path / 'cache')
    assert not (tmp_path / '.env').exists()
    status, body = _post(panel, {'searchProjection': {'directory': ''}})
    assert status == 200
    assert not body['restartRequired']
    assert body['searchProjection']['pendingDirectory'] == active
    assert body['searchProjection']['configuredDirectory'] == ''


@pytest.mark.parametrize('enabled', [False, True])
def test_native_fastpath_cannot_disable_or_reconfigure_native_search(panel, enabled, monkeypatch):
    authority = panel[1] / 'tofudb-authority'
    authority.mkdir(parents=True)
    (authority / 'FASTPATH').write_text('retained marker')
    before = panel[2]['search_projection_directory']
    status, body = _post(panel, {'fastPath': {'enabled': enabled, 'directory': '/unused'}})
    assert status == 409
    assert body['errorKind'] == 'fastpath_locked'
    assert panel[2]['search_projection_directory'] == before
    assert not (panel[1].parent / '.env').exists()


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_non_native_projection_rejected(panel, backend):
    panel[3]['backend'] = backend
    status, body = _post(panel, {'searchProjection': {'directory': ''}})
    assert status == 409
    assert body['errorKind'] == 'search_projection_unsupported'
    assert not (panel[1] / 'config/server_config.json').exists()


@pytest.mark.parametrize('body', [
    [], 'bad', 5, {}, {'unknown': {}},
    {'searchProjection': None}, {'searchProjection': []}, {'searchProjection': {}},
    {'searchProjection': {'directory': False}}, {'searchProjection': {'directory': []}},
    {'searchProjection': {'directory': '', 'enabled': False}},
    {'searchProjection': {'directory': ''}, 'fastPath': {'enabled': False}},
    {'fastPath': None}, {'fastPath': []}, {'fastPath': {}},
    {'fastPath': {'enabled': 'false'}}, {'fastPath': {'enabled': 0}},
    {'fastPath': {'enabled': False, 'directory': []}},
    {'fastPath': {'enabled': False, 'unexpected': True}},
    {'fastPath': {'enabled': False, 'directory': 'relative'}},
    {'fastPath': {'enabled': False, 'directory': '/bad\x00dir'}},
])
def test_malformed_shapes_have_structured_400_and_no_writes(panel, body):
    status, result = _post(panel, body)
    assert status == 400
    assert result['ok'] is False
    assert 'error' in result
    assert not panel[1].exists()


def test_malformed_json_is_not_silently_applied_as_disable(panel):
    response = _run(panel[0].post('/api/v1/database/paths', data='{"fastPath":',
                                headers={'Content-Type': 'application/json'}))
    assert response.status_code == 400
    assert _run(response.get_json())['ok'] is False
    assert not panel[1].exists()


@pytest.mark.parametrize('value', ['relative/cache', '~/cache', '/bad\x00dir', '/' + 'x' * 4096])
def test_invalid_directory_has_400_without_persisting(panel, value):
    status, body = _post(panel, {'searchProjection': {'directory': value}})
    assert status == 400
    assert body['ok'] is False
    assert not panel[1].exists()


def test_overlapping_and_unwritable_destinations_have_400(panel, tmp_path, monkeypatch):
    status, _ = _post(panel, {'searchProjection': {'directory': str(panel[1])}})
    assert status == 400
    monkeypatch.setattr(projection_paths.tempfile, 'NamedTemporaryFile',
                        Mock(side_effect=PermissionError('not writable')))
    status, _ = _post(panel, {'searchProjection': {'directory': str(tmp_path / 'cache')}})
    assert status == 400
    assert not (panel[1] / 'config/server_config.json').exists()


@pytest.mark.parametrize('override', ['', '/operator/cache'])
def test_environment_override_reports_conflict_not_false_success(panel, monkeypatch, override):
    monkeypatch.setenv(projection_paths.SEARCH_PROJECTION_ENV, override)
    status, body = _post(panel, {'searchProjection': {'directory': ''}})
    assert status == 409
    assert body['errorKind'] == 'search_projection_env_override'
    assert not panel[1].exists()
    assert os.environ[projection_paths.SEARCH_PROJECTION_ENV] == override


@pytest.mark.parametrize('failure', ['file_fsync', 'replace'])
def test_persistence_failure_has_structured_500_and_preserves_state(
        panel, tmp_path, monkeypatch, failure):
    projection_paths.save_search_projection_directory(panel[1], '')
    config = panel[1] / 'config/server_config.json'
    before = config.read_bytes()
    active = dict(panel[2])
    real_fsync = os.fsync

    def fail_file_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'simulated file fsync failure')
        real_fsync(fd)

    if failure == 'file_fsync':
        monkeypatch.setattr(projection_paths.os, 'fsync', fail_file_fsync)
    else:
        monkeypatch.setattr(projection_paths.os, 'replace',
                            Mock(side_effect=OSError(errno.EIO, 'simulated replace failure')))
    status, body = _post(panel, {'searchProjection': {'directory': str(tmp_path / 'cache')}})
    assert status == 500
    assert body['ok'] is False and 'error' in body
    assert config.read_bytes() == before
    assert panel[2] == active


def test_directory_fsync_failure_has_500_without_rollback_or_live_change(panel, tmp_path, monkeypatch):
    projection_paths.save_search_projection_directory(panel[1], '')
    config = panel[1] / 'config/server_config.json'
    active = dict(panel[2])
    real_fsync = os.fsync

    def fail_parent_fsync(fd):
        if os.path.samestat(os.fstat(fd), config.parent.stat()):
            raise OSError(errno.EIO, 'simulated directory fsync failure')
        real_fsync(fd)

    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(projection_paths.os, 'replace', replace)
    monkeypatch.setattr(projection_paths.os, 'fsync', fail_parent_fsync)
    status, body = _post(panel, {'searchProjection': {'directory': str(tmp_path / 'cache')}})
    assert status == 500
    assert body['ok'] is False and 'error' in body
    assert replace.call_count == 1
    assert json.loads(config.read_text())[projection_paths.SEARCH_PROJECTION_CONFIG_KEY] == str(
        tmp_path / 'cache')
    assert panel[2] == active
    response = _run(panel[0].get('/api/v1/database/status'))
    state = _run(response.get_json())['searchProjection']
    assert state['configuredDirectory'] == str(tmp_path / 'cache')
    assert state['directory'] == active['search_projection_directory']
    assert state['restartRequired'] is True


def test_corrupt_persisted_config_is_not_replaced_by_apply(panel):
    config = panel[1] / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{broken')
    status, body = _post(panel, {'searchProjection': {'directory': ''}})
    assert status == 500
    assert body['ok'] is False
    assert config.read_text() == '{broken'


@pytest.mark.parametrize('enabled', [False, True])
def test_sqlite_fastpath_optional_directory_contract_preserved(panel, tmp_path, enabled):
    panel[3]['backend'] = 'sqlite'
    status, body = _post(panel, {'fastPath': {'enabled': enabled}})
    assert status == 200
    assert body['ok'] and body['restartRequired']
    assert body['searchProjection'] is None
    assert body['fastpath']['supported']
    assert body['fastpath']['enabled'] is enabled
    text = (tmp_path / '.env').read_text()
    assert 'TOFU_STORAGE_FASTPATH=' + ('auto' if enabled else 'off') in text
    assert 'TOFU_STORAGE_FASTPATH_DIR' not in os.environ


def test_sqlite_fastpath_valid_custom_directory_remains_supported(panel, tmp_path, monkeypatch):
    panel[3]['backend'] = 'sqlite'
    base = tmp_path / 'sqlite-cache'
    monkeypatch.setattr(probe, 'discover_paths', lambda **kwargs: {'candidates': []})
    monkeypatch.setattr(probe, 'probe_single_path', lambda *args, **kwargs: {
        'writable': True, 'persistence': 'persistent', 'free_bytes': 10 * 1024 ** 3})
    status, body = _post(panel, {'fastPath': {'enabled': True, 'directory': str(base)}})
    assert status == 200
    assert body['fastpath']['directory'] == str(base)
    assert os.environ['TOFU_STORAGE_FASTPATH_DIR'] == str(base)


def test_sqlite_persistence_failure_does_not_mutate_environment(panel, monkeypatch):
    panel[3]['backend'] = 'sqlite'
    before = dict(os.environ)
    monkeypatch.setattr('tofu_dotenv.update_dotenv_file', Mock(side_effect=OSError('failure')))
    status, body = _post(panel, {'fastPath': {'enabled': True}})
    assert status == 500 and not body['ok']
    assert dict(os.environ) == before


def test_path_apply_schema_describes_both_exclusive_inputs():
    from routes.api_v1.database_panel import database_paths_apply

    schema = database_paths_apply._api_meta['request_body']['content']['application/json']['schema']
    assert [variant['required'] for variant in schema['oneOf']] == [['searchProjection'], ['fastPath']]
