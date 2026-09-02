"""tests/test_api_v1_byo_surface_polish.py — Surface ergonomics audit.

Covers improvements layered on top of the original BYO surface:

* /v1/models surfaces the caller's BYO providers as
  ``{id: "<model>@<prov_id>"}`` so OpenAI SDKs see them.
* /api/v1/providers rejects reserved header names in ``extra_headers``.
* /api/v1/providers' ``auto_discover`` defaults to ``false``
  (registration is fast and unconditional; probe is a separate call).
* sanitise_extra_headers() drops too-long values and non-scalars.
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import unittest


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
        from lib.byo_providers import delete_provider, list_providers
        for owner_user_id in (self.ALICE_OWNER, self.BOB_OWNER):
            for provider in list_providers(owner_user_id):
                delete_provider(provider['id'], owner_user_id)

    def test_v1_models_includes_callers_byo(self):
        async def go():
            cli = self.app.test_client()
            # Alice registers an endpoint
            r1 = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.alice}'},
                json={'name': 'A',
                      'base_url': 'http://10.0.0.5:8080/v1',
                      'models': [{'model_id': 'qwen3.5-FP8'},
                                  {'model_id': 'glm-5.1'}]})
            self.assertEqual(r1.status_code, 201,
                              await r1.get_data(as_text=True))
            prov_id = (await r1.get_json())['provider']['id']

            # Alice sees both her models with the suffix attached
            r2 = await cli.get(
                '/v1/models',
                headers={'Authorization': f'Bearer {self.alice}'})
            self.assertEqual(r2.status_code, 200)
            data = await r2.get_json()
            ids = {m['id'] for m in data['data']}
            self.assertIn(f'qwen3.5-FP8@{prov_id}', ids)
            self.assertIn(f'glm-5.1@{prov_id}', ids)

            # Bob does NOT see Alice's BYO models
            r3 = await cli.get(
                '/v1/models',
                headers={'Authorization': f'Bearer {self.bob}'})
            data3 = await r3.get_json()
            ids3 = {m['id'] for m in data3['data']}
            self.assertNotIn(f'qwen3.5-FP8@{prov_id}', ids3)

        _new_loop_run(go())

    def test_provider_register_default_does_not_auto_discover(self):
        # auto_discover defaults to False — registration is fast even
        # if the endpoint would have hung. We assert nothing tries to
        # contact the URL by giving an unroutable port and short
        # success path: the request returns 201 quickly, with empty
        # models.
        async def go():
            cli = self.app.test_client()
            import time as _time
            t0 = _time.time()
            r = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.alice}'},
                json={'name': 'fast-reg',
                      'base_url': 'http://127.0.0.1:1/v1',  # port 1 = no service
                      'api_key': '',
                      'models': []})
            elapsed = _time.time() - t0
            self.assertEqual(r.status_code, 201,
                              await r.get_data(as_text=True))
            # Should be near-instant — no synchronous probe.
            self.assertLess(elapsed, 2.0,
                             f'registration took {elapsed:.2f}s; '
                             f'auto_discover may not be off by default')
            body = await r.get_json()
            self.assertEqual(body['provider']['models'], [])

        _new_loop_run(go())

    def test_provider_register_rejects_reserved_header(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.alice}'},
                json={'name': 'evil',
                      'base_url': 'http://10.0.0.5:8080/v1',
                      'api_key': 'sk-x',
                      'extra_headers': {'Authorization': 'Bearer override'},
                      'models': [{'model_id': 'm'}]})
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('reserved', str(body))
        _new_loop_run(go())


if __name__ == '__main__':
    unittest.main()
