"""tests/database_panel/test_database_panel_routes.py — HTTP contract for the panel API.

Mounts only the database-panel blueprint on a throwaway Quart app with
the standard bearer gate; storage-facing modules are stubbed so the test
never starts a sidecar.  Verifies status shape, busy/validation error
mapping, preference round-trip, and the personal-only fence.
"""



from __future__ import annotations

import pytest

pytestmark = pytest.mark.api

import asyncio
import unittest
from unittest.mock import patch

pytest_plugins = ('tests._credential_sidecar',)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_ENGINE = {
    'backend': 'tofudb', 'deploymentMode': 'personal', 'ready': True,
    'state': 'ready', 'lastError': '', 'dataRoot': '/data',
    'authorityPath': '/data/tofudb-authority', 'dataBytes': 1024,
    'filesystem': {'type': 'ext4', 'class': 'local-block',
                   'persistence': 'persistent', 'freeBytes': 10 ** 10,
                   'probeLatencyMs': 1.0},
    'engines': {
        'sqlite': {'available': True, 'version': '3.0', 'hasData': True},
        'tofudb': {'available': True, 'binaryPath': '/usr/bin/tofu-db',
                   'hasData': True},
    },
}

_DESCRIBE = {
    'current': 'tofudb',
    'sqlite': {'available': True, 'hasData': True, 'dataBytes': 1024},
    'tofudb': {'available': True, 'hasData': True,
               'authorityPath': '/data/tofudb-authority'},
}


class DatabasePanelRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401 — installs the full Flask→Quart shim

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.database_panel import api_v1_database_panel_bp
        cls.app.register_blueprint(api_v1_database_panel_bp)

    def _client(self):
        return self.app.test_client()

    def test_status_shape(self):
        with patch('lib.database_panel.probe.engine_status',
                   return_value=dict(_ENGINE)), \
                patch('lib.database_panel.probe.fastpath_state',
                      return_value={'supported': False, 'mode': 'unavailable',
                                    'enabled': False, 'directory': ''}), \
                patch('lib.database_panel.switch.describe',
                      return_value=dict(_DESCRIBE)), \
                patch('lib.database_panel.switch.migration_status',
                      return_value={'job': None}), \
                patch('lib.database_panel.probe.deployment_mode',
                      return_value='personal'):
            response = _run(self._client().get('/api/v1/database/status'))
        self.assertEqual(response.status_code, 200)
        data = _run(response.get_json())
        self.assertTrue(data['ok'])
        for key in ('engine', 'fastpath', 'backends', 'preferences',
                    'migration', 'searchProjection'):
            self.assertIn(key, data)
        self.assertEqual(data['engine']['backend'], 'tofudb')

    def test_status_reads_supervision_once_and_reuses_engine_snapshot(self):
        runtime = {'backend': 'tofudb', 'ready': True, 'state': 'ready'}
        with patch('lib.database_panel.probe.deployment_mode', return_value='personal'), \
                patch('lib.database_panel.probe.runtime_status', return_value=runtime) as read, \
                patch('lib.database_panel.probe.engine_status', return_value=dict(_ENGINE)) as engine, \
                patch('lib.database_panel.probe.fastpath_state', return_value={}) as fastpath, \
                patch('lib.database_panel.probe.search_projection_state', return_value={}) as projection, \
                patch('lib.database_panel.switch.describe', return_value={}) as describe, \
                patch('lib.database_panel.switch.migration_status', return_value={'job': None}), \
                patch('lib.database_panel.config_store.load_preferences', return_value={}):
            response = _run(self._client().get('/api/v1/database/status'))
        self.assertEqual(response.status_code, 200)
        read.assert_called_once_with()
        engine.assert_called_once_with(runtime)
        fastpath.assert_called_once_with('tofudb', runtime)
        projection.assert_called_once_with('tofudb', runtime)
        self.assertIs(describe.call_args.args[0], engine.return_value)

    def test_backend_switch_error_mapping(self):
        from lib.database_panel import switch

        with patch('lib.database_panel.probe.deployment_mode',
                   return_value='personal'), \
                patch.object(switch, 'request_switch',
                             side_effect=switch.SwitchError(
                                 'already_active', 'tofudb is already active')):
            response = _run(self._client().post('/api/v1/database/backend',
                                                json={'backend': 'tofudb'}))
        self.assertEqual(response.status_code, 409)

    def test_preferences_reject_relative_backup_directory(self):
        with patch('lib.database_panel.probe.deployment_mode',
                   return_value='personal'):
            response = _run(self._client().post(
                '/api/v1/database/preferences',
                json={'backup': {'enabled': True, 'directory': 'relative/dir'}}))
        self.assertEqual(response.status_code, 400)

    def test_native_fastpath_typed_lock_conflict(self):
        from lib.storage_authority_sql.native_fastpath_settings import NativeFastpathSettingsError

        with patch('lib.database_panel.probe.deployment_mode', return_value='personal'), \
                patch('lib.database_panel.probe.engine_status', return_value=dict(_ENGINE)), \
                patch('lib.database_panel.probe.fastpath_state', return_value={'supported': True}), \
                patch('lib.database_panel.probe.configure_native_fastpath', side_effect=
                      NativeFastpathSettingsError('fastpath_locked', 'Use offline fastpath-export')):
            response = _run(self._client().post('/api/v1/database/paths',
                                                json={'fastPath': {'enabled': False}}))
        self.assertEqual(response.status_code, 409)
        data = _run(response.get_json())
        self.assertIn('fastpath_locked', str(data))

    def test_native_fastpath_save_response_preserves_projection(self):
        state = {'supported': True, 'kind': 'native', 'enabled': True,
                 'restartRequired': True, 'active': False}
        with patch('lib.database_panel.probe.deployment_mode', return_value='personal'), \
                patch('lib.database_panel.probe.engine_status', return_value=dict(_ENGINE)), \
                patch('lib.database_panel.probe.fastpath_state', return_value=state), \
                patch('lib.database_panel.probe.configure_native_fastpath', return_value=state) as save, \
                patch('lib.database_panel.probe.search_projection_state', return_value={'required': True}):
            response = _run(self._client().post('/api/v1/database/paths', json={
                'fastPath': {'enabled': True, 'directory': '/local'}}))
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with(True, '/local')
        data = _run(response.get_json())
        self.assertEqual(data['fastpath']['kind'], 'native')
        self.assertTrue(data['searchProjection']['required'])

    def test_non_personal_deployment_fenced(self):
        with patch('lib.database_panel.probe.deployment_mode',
                   return_value='distributed'):
            response = _run(self._client().get('/api/v1/database/status'))
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
