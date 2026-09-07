"""Model-routing v2 discovery and outbound-header surface audit.

Covers improvements layered on top of the original BYO surface:

* /v1/models projects owner-visible official models without provider suffixes.
* /api/v1/providers rejects reserved header names in ``extra_headers``.
* ProviderAccess registration is a local CAS write, not a network probe.
* sanitise_extra_headers() drops too-long values and non-scalars.
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import unittest

import pytest

from tests.support.model_routing import (
    clear_test_model_routing,
    install_native_test_model_route,
    native_test_provider_bundle,
)


pytestmark = pytest.mark.unit


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class HeaderAllowlistTest(unittest.TestCase):
    """Pure unit test for sanitise_extra_headers."""

    def test_empty_input_ok(self):
        from routes.api_v1.providers import sanitise_extra_headers
        clean, err = sanitise_extra_headers({})
        self.assertEqual(clean, {})
        self.assertIsNone(err)
        clean, err = sanitise_extra_headers(None)
        self.assertEqual(clean, {})
        self.assertIsNone(err)

    def test_passthrough_normal_header(self):
        from routes.api_v1.providers import sanitise_extra_headers
        clean, err = sanitise_extra_headers({'X-Internal-Tag': 'foo'})
        self.assertIsNone(err)
        self.assertEqual(clean, {'X-Internal-Tag': 'foo'})

    def test_authorization_rejected(self):
        from routes.api_v1.providers import sanitise_extra_headers
        for name in ('Authorization', 'authorization', 'AUTHORIZATION',
                     'X-API-Key', 'x-api-key', 'Cookie', 'cookie',
                     'Host', 'Content-Length', 'Transfer-Encoding',
                     'Proxy-Authorization'):
            _, err = sanitise_extra_headers({name: 'evil'})
            self.assertIsNotNone(err, f'{name} should be rejected')
            self.assertIn('reserved', err)

    def test_too_many_headers(self):
        from routes.api_v1.providers import sanitise_extra_headers
        many = {f'X-Tag-{i}': str(i) for i in range(20)}
        _, err = sanitise_extra_headers(many)
        self.assertIn('too many', err.lower())

    def test_value_too_long(self):
        from routes.api_v1.providers import sanitise_extra_headers
        _, err = sanitise_extra_headers({'X-Big': 'a' * 5000})
        self.assertIn('too long', err.lower())

    def test_non_scalar_value_rejected(self):
        from routes.api_v1.providers import sanitise_extra_headers
        _, err = sanitise_extra_headers({'X-Multi': ['a', 'b']})
        self.assertIn('scalar', err.lower())

    def test_scalar_coerced_to_string(self):
        from routes.api_v1.providers import sanitise_extra_headers
        clean, err = sanitise_extra_headers({'X-Int': 42, 'X-Bool': True})
        self.assertIsNone(err)
        self.assertEqual(clean['X-Int'], '42')
        self.assertEqual(clean['X-Bool'], 'True')

    def test_non_dict_rejected(self):
        from routes.api_v1.providers import sanitise_extra_headers
        _, err = sanitise_extra_headers('just a string')
        self.assertIn('object', err.lower())


class ModelsBYOSurfaceTest(unittest.TestCase):

    ALICE_OWNER = 12_021
    BOB_OWNER = 12_022

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
        from routes.compat_openai import compat_openai_bp
        cls.app.register_blueprint(api_v1_providers_bp)
        cls.app.register_blueprint(compat_openai_bp)

        from lib.api_keys import create_key
        # alice and bob — separate principals
        _row, cls.alice = create_key(owner_user_id=cls.ALICE_OWNER,
            name='alice', scopes=['providers', 'chat'])
        _row, cls.bob = create_key(owner_user_id=cls.BOB_OWNER,
            name='bob', scopes=['providers', 'chat'])

    def setUp(self):
        for owner_user_id in (self.ALICE_OWNER, self.BOB_OWNER):
            clear_test_model_routing(owner_user_id=owner_user_id)

    def test_v1_models_includes_callers_byo(self):
        async def go():
            cli = self.app.test_client()
            install_native_test_model_route(owner_user_id=self.ALICE_OWNER)

            r2 = await cli.get(
                '/v1/models',
                headers={'Authorization': f'Bearer {self.alice}'})
            self.assertEqual(r2.status_code, 200)
            data = await r2.get_json()
            models = {m['id']: m for m in data['data']}
            self.assertIn('stub-model', models)
            self.assertEqual(
                models['stub-model']['tofu']['creator_id'], 'tofu-test')
            self.assertEqual(
                models['stub-model']['tofu']['provider_ids'],
                ['test-provider'])

            r3 = await cli.get(
                '/v1/models',
                headers={'Authorization': f'Bearer {self.bob}'})
            data3 = await r3.get_json()
            ids3 = {m['id'] for m in data3['data']}
            self.assertNotIn('stub-model', ids3)

        _new_loop_run(go())

    def test_v1_models_lists_pending_identity_by_wire_request_id(self):
        async def go():
            from lib.model_routing import ModelRoutingRepository, OwnerBoundary

            cli = self.app.test_client()
            install_native_test_model_route(owner_user_id=self.ALICE_OWNER)
            repository = ModelRoutingRepository()
            boundary = OwnerBoundary.create(self.ALICE_OWNER)
            aggregate = repository.get(boundary)
            offering = aggregate.document['offerings'][0]
            offering['identity_state'] = 'pending_identity'
            offering['pending_model_id'] = 'unconfirmed-preview'
            offering.pop('model')
            repository.compare_and_swap(
                boundary,
                aggregate.document,
                expected_revision=aggregate.revision,
            )

            response = await cli.get(
                '/v1/models',
                headers={'Authorization': f'Bearer {self.alice}'})
            self.assertEqual(response.status_code, 200)
            payload = await response.get_json()
            models = {row['id']: row for row in payload['data']}
            self.assertNotIn('stub-model', models)
            self.assertEqual(
                set(models),
                {'stub-model-wire', 'stub-model-anthropic-wire'},
            )
            for row in models.values():
                self.assertEqual(
                    row['tofu']['preferred_provider_id'], 'test-provider')
                self.assertEqual(row['tofu']['offering_id'], 'test-offering')
                self.assertTrue(row['tofu']['pending_identity'])
                self.assertNotIn('@', row['id'])

        _new_loop_run(go())

    def test_v1_embeddings_uses_owner_scoped_v2_route(self):
        async def go():
            import os

            import lib.http_client as http_client
            from lib.model_routing import ModelRoutingRepository, OwnerBoundary

            cli = self.app.test_client()
            install_native_test_model_route(owner_user_id=self.ALICE_OWNER)
            repository = ModelRoutingRepository()
            boundary = OwnerBoundary.create(self.ALICE_OWNER)
            authority = repository.get(boundary)
            authority.document['models'][0]['capabilities'] = ['embedding']
            authority.document['offerings'][0]['capabilities'] = ['embedding']
            for connection in authority.document['connections']:
                if connection['protocol'] == 'openai':
                    connection['base_url'] = 'http://127.0.0.1:18080/v1'
            repository.compare_and_swap(
                boundary,
                authority.document,
                expected_revision=authority.revision,
            )

            calls = []

            class Response:
                ok = True
                status_code = 200
                text = ''

                @staticmethod
                def json():
                    return {
                        'object': 'list',
                        'data': [{'index': 0, 'embedding': [0.25, 0.75]}],
                        'model': 'stub-model-wire',
                    }

            original_post = http_client.http_post
            original_preflight = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT')
            os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
            http_client.http_post = lambda url, **kwargs: (
                calls.append((url, kwargs)) or Response())
            try:
                response = await cli.post(
                    '/v1/embeddings',
                    headers={'Authorization': f'Bearer {self.alice}'},
                    json={'model': 'stub-model', 'input': 'owner text'},
                )
                self.assertEqual(
                    response.status_code, 200,
                    await response.get_data(as_text=True),
                )
                payload = await response.get_json()
                self.assertEqual(payload['data'][0]['embedding'], [0.25, 0.75])
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    calls[0][0], 'http://127.0.0.1:18080/v1/embeddings')
                self.assertEqual(calls[0][1]['json'], {
                    'model': 'stub-model-wire',
                    'input': ['owner text'],
                })
                self.assertNotIn('Authorization', calls[0][1]['headers'])

                foreign = await cli.post(
                    '/v1/embeddings',
                    headers={'Authorization': f'Bearer {self.bob}'},
                    json={'model': 'stub-model', 'input': 'foreign text'},
                )
                self.assertEqual(foreign.status_code, 400)
                self.assertEqual(len(calls), 1)
            finally:
                http_client.http_post = original_post
                if original_preflight is None:
                    os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)
                else:
                    os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = original_preflight

        _new_loop_run(go())

    def test_provider_registration_is_local_and_does_not_probe(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.alice}'}
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']
            import time as _time
            t0 = _time.time()
            r = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    provider_id='fast-reg',
                    base_url='http://127.0.0.1:1/v1'))
            elapsed = _time.time() - t0
            self.assertEqual(r.status_code, 201,
                              await r.get_data(as_text=True))
            # Should be near-instant — no synchronous probe.
            self.assertLess(elapsed, 2.0,
                             f'registration took {elapsed:.2f}s; '
                             f'auto_discover may not be off by default')
            body = await r.get_json()
            self.assertEqual(body['provider']['offerings'], [])
            self.assertEqual(body['provider']['deployments'], [])

        _new_loop_run(go())

    def test_provider_template_compiles_meituan_faces_into_v2_connections(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.alice}'}
            listed = await cli.get(
                '/api/v1/providers/templates', headers=headers)
            self.assertEqual(listed.status_code, 200)
            templates = (await listed.get_json())['items']
            self.assertIn('meituan', {row['key'] for row in templates})

            compiled = await cli.post(
                '/api/v1/providers/templates/compile',
                headers=headers,
                json={
                    'template_key': 'meituan',
                    'selected_model_ids': [
                        'LongCat-2.0', 'claude-opus-4.8',
                    ],
                },
            )
            self.assertEqual(
                compiled.status_code, 200,
                await compiled.get_data(as_text=True))
            bundle = (await compiled.get_json())['provider_bundle']
            self.assertEqual(bundle['provider']['brand'], 'meituan')
            self.assertEqual(
                {row['protocol'] for row in bundle['connections']},
                {'openai', 'anthropic'},
            )
            connection_protocol = {
                row['connection_id']: row['protocol']
                for row in bundle['connections']
            }
            offering_by_id = {
                row['offering_id']: row for row in bundle['offerings']
            }
            claude_deployment = next(
                row for row in bundle['deployments']
                if offering_by_id[row['offering_id']].get('model', {}).get(
                    'model_id') == 'claude-opus-4.8'
            )
            self.assertEqual(
                connection_protocol[claude_deployment['connection_id']],
                'anthropic',
            )
            self.assertTrue(all(
                not row['secret_reference'] and not row['key_hint']
                for row in bundle['credentials']))
            self.assertNotIn('template-secret-placeholder', str(bundle))

        _new_loop_run(go())

    def test_provider_register_rejects_reserved_header(self):
        async def go():
            from lib.model_routing import ModelRoutingRepository, OwnerBoundary

            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.alice}'}
            repository = ModelRoutingRepository()
            boundary = OwnerBoundary.create(self.ALICE_OWNER)
            secrets_before = repository.secret_metadata(boundary)
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']
            r = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    provider_id='evil',
                    extra_headers={'Authorization': 'Bearer override'}))
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('reserved', str(body))
            self.assertEqual(
                repository.secret_metadata(boundary), secrets_before,
                'a rejected aggregate must reclaim its staged secret',
            )
        _new_loop_run(go())

    def test_provider_register_requires_explicit_cas_revision(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.alice}'},
                json={},
            )
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertEqual(body['field'], 'expected_revision')

        _new_loop_run(go())


if __name__ == '__main__':
    unittest.main()
