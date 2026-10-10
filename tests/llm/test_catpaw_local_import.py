"""CatPaw one-click local credential import.

Guards the Settings → 订阅登录 CatPaw card backend:

* lib: ``read_local_key``/``detect_local_key`` parse the fixed CLI config and
  validate the 32-char AUTHORIZATION token without ever leaking it.
* route: GET returns a secret-free availability/connection verdict; POST
  creates the provider from the compiled catpaw template (sealing the token
  into the owner credential envelope) or rotates an existing key in place.
* contract: live GET/POST envelopes decode against the generated v1 contract.
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import unittest

from unittest import mock

import pytest

from lib.api_v1_model_routing_generated import decode_response
from lib.model_routing import ModelRoutingRepository, OwnerBoundary
from tests.support.model_routing import clear_test_model_routing


pytestmark = pytest.mark.unit

OWNER = 12_937
VALID_TOKEN = 'a' * 32


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class LocalKeyLibTest(unittest.TestCase):
    """The CLI-config reader is hermetic against a tmp AUTHORIZATION file."""

    def _write(self, tmp_path, text):
        cfg = tmp_path / '.config.yaml'
        cfg.write_text(text, encoding='utf-8')
        return str(cfg)

    def test_valid_token_roundtrip(self):
        import tempfile
        from pathlib import Path
        from lib import catpaw_local_import as cli
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / '.config.yaml'
            cfg.write_text('AUTHORIZATION: %s\n' % VALID_TOKEN, encoding='utf-8')
            with mock.patch.object(cli, 'CATPAW_CLI_CONFIG', str(cfg)):
                assert cli.read_local_key() == VALID_TOKEN
                assert cli.detect_local_key() == {'available': True}

    def test_missing_file_is_unavailable(self):
        from lib import catpaw_local_import as cli
        with mock.patch.object(cli, 'CATPAW_CLI_CONFIG',
                               '/nonexistent/catpaw/.config.yaml'):
            assert cli.detect_local_key() == {'available': False}
            with self.assertRaises(cli.CatpawLocalImportError) as ctx:
                cli.read_local_key()
            self.assertEqual(ctx.exception.reason, 'cli_config_missing')

    def test_wrong_length_token_rejected(self):
        import tempfile
        from pathlib import Path
        from lib import catpaw_local_import as cli
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / '.config.yaml'
            cfg.write_text('AUTHORIZATION: tooshort\n', encoding='utf-8')
            with mock.patch.object(cli, 'CATPAW_CLI_CONFIG', str(cfg)):
                assert cli.detect_local_key() == {'available': False}
                with self.assertRaises(cli.CatpawLocalImportError) as ctx:
                    cli.read_local_key()
                self.assertEqual(ctx.exception.reason, 'authorization_invalid')

    def test_secret_envelope_shape(self):
        import json
        from lib import catpaw_local_import as cli
        env = json.loads(cli.secret_envelope(VALID_TOKEN))
        self.assertEqual(env['format'], 'tofu.credential-secret/v1')
        self.assertEqual(env['api_key'], VALID_TOKEN)
        self.assertEqual(env['oauth'], '')

    def test_kick_cli_login_captures_login_url(self):
        import io
        from lib import catpaw_local_import as cli

        class _Proc:
            stdout = io.StringIO(
                'open https://mcli.internal.example.com/user/login?uuid=abc-123 to login\n')

        with mock.patch.object(cli.shutil, 'which', return_value='/usr/bin/mc'), \
                mock.patch.object(cli.subprocess, 'Popen', return_value=_Proc()):
            result = cli.kick_cli_login(timeout_s=1.0)
        self.assertTrue(result['started'])
        self.assertTrue(result['installed'])
        self.assertEqual(
            result['login_url'],
            'https://mcli.internal.example.com/user/login?uuid=abc-123')

    def test_kick_cli_login_without_cli(self):
        from lib import catpaw_local_import as cli
        with mock.patch.object(cli.shutil, 'which', return_value=None):
            result = cli.kick_cli_login(timeout_s=0.1)
        self.assertEqual(
            result, {'started': False, 'installed': False, 'login_url': ''})


class LocalImportRouteTest(unittest.TestCase):
    """End-to-end GET status + POST create/rotate through the live route."""

    @classmethod
    def setUpClass(cls):
        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.providers import api_v1_providers_bp
        cls.app.register_blueprint(api_v1_providers_bp)
        from lib.api_keys import create_key
        _row, cls.token = create_key(
            owner_user_id=OWNER, name='catpaw-import', scopes=['providers'])

    def setUp(self):
        clear_test_model_routing(owner_user_id=OWNER)
        # Hermetic credential source: patch the names the route bound at
        # import time so no test depends on the host's real CLI config.
        self._patches = [
            mock.patch(
                'routes.api_v1.providers.catpaw_read_local_key',
                return_value=VALID_TOKEN),
            mock.patch(
                'routes.api_v1.providers.catpaw_detect_local_key',
                return_value={'available': True}),
        ]
        for patcher in self._patches:
            patcher.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for patcher in self._patches:
            patcher.stop()

    @staticmethod
    async def _data(response, operation_id):
        body = await response.get_json()
        data = {key: value for key, value in body.items() if key != 'ok'}
        decode_response(operation_id, data)
        return data

    def _headers(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _resolved_secret(self):
        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(OWNER)
        document = repository.get(boundary).document
        credentials = [
            row for row in document['credentials'] if row['secret_reference']
        ]
        self.assertEqual(len(credentials), 1)
        return repository.resolve_secret(
            boundary, credentials[0]['secret_reference'])

    def test_status_disconnected_then_connected(self):
        async def go():
            cli = self.app.test_client()
            st = await cli.get(
                '/api/v1/providers/catpaw/local-import', headers=self._headers())
            self.assertEqual(st.status_code, 200)
            data = await self._data(st, 'catpawLocalImportStatus')
            self.assertTrue(data['available'])
            self.assertFalse(data['connected'])
            self.assertEqual(data['model_count'], 0)
        _run(go())

    def test_import_creates_provider_then_rotates(self):
        async def go():
            cli = self.app.test_client()

            created = await cli.post(
                '/api/v1/providers/catpaw/local-import',
                headers=self._headers(), json={})
            self.assertEqual(created.status_code, 200)
            data = await self._data(created, 'catpawLocalImportApply')
            self.assertTrue(data['connected'])
            self.assertTrue(data['created'])
            self.assertFalse(data['rotated'])
            self.assertGreater(data['model_count'], 0)
            provider_id = data['provider_id']

            # The token was sealed into the owner envelope, not returned.
            self.assertNotIn(VALID_TOKEN, await created.get_data(as_text=True))

            rotated = await cli.post(
                '/api/v1/providers/catpaw/local-import',
                headers=self._headers(), json={})
            self.assertEqual(rotated.status_code, 200)
            data2 = await self._data(rotated, 'catpawLocalImportApply')
            self.assertFalse(data2['created'])
            self.assertTrue(data2['rotated'])
            self.assertEqual(data2['provider_id'], provider_id)
            self.assertEqual(data2['model_count'], data['model_count'])

            st = await cli.get(
                '/api/v1/providers/catpaw/local-import', headers=self._headers())
            status = await self._data(st, 'catpawLocalImportStatus')
            self.assertTrue(status['connected'])
            self.assertEqual(status['model_count'], data['model_count'])
        _run(go())

        # The sealed envelope unwraps to the catpaw token (never the raw value).
        import json
        envelope = json.loads(self._resolved_secret())
        self.assertEqual(envelope['api_key'], VALID_TOKEN)

    def test_created_provider_uses_catpaw_gateway(self):
        async def go():
            cli = self.app.test_client()
            await cli.post(
                '/api/v1/providers/catpaw/local-import',
                headers=self._headers(), json={})
        _run(go())
        repository = ModelRoutingRepository()
        document = repository.get(OwnerBoundary.create(OWNER)).document
        base_urls = {row['base_url'] for row in document['connections']}
        self.assertEqual(base_urls, {'https://mcli.internal.example.com'})
        self.assertTrue(document['offerings'])

    def test_import_without_local_key_is_400(self):
        from lib.catpaw_local_import import CatpawLocalImportError
        for patcher in self._patches:
            patcher.stop()
        self._patches = [
            mock.patch(
                'routes.api_v1.providers.catpaw_read_local_key',
                side_effect=CatpawLocalImportError('cli_config_missing')),
            mock.patch(
                'routes.api_v1.providers.catpaw_detect_local_key',
                return_value={'available': False}),
        ]
        for patcher in self._patches:
            patcher.start()

        async def go():
            cli = self.app.test_client()
            resp = await cli.post(
                '/api/v1/providers/catpaw/local-import',
                headers=self._headers(), json={})
            self.assertEqual(resp.status_code, 400)
            body = await resp.get_json()
            self.assertEqual(body.get('reason'), 'cli_config_missing')
        _run(go())


if __name__ == '__main__':
    unittest.main()
