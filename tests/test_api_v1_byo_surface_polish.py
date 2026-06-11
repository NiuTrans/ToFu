"""tests/test_api_v1_byo_surface_polish.py — Surface ergonomics audit.

Covers improvements layered on top of the original BYO surface:

* /v1/models surfaces the caller's BYO providers as
  ``{id: "<model>@<prov_id>"}`` so OpenAI SDKs see them.
* /api/v1/providers rejects reserved header names in ``extra_headers``.
* /api/v1/providers' ``auto_discover`` defaults to ``false``
  (registration is fast and unconditional; probe is a separate call).
* sanitise_extra_headers() drops too-long values and non-scalars.
"""

import asyncio
import os
import sys
import tempfile
import unittest


def _install_shim():
    """Install the flask→quart import shim + PROVIDE_AUTOMATIC_OPTIONS.

    This is SAFE to call at module-import time: it only rebinds
    ``sys.modules['flask']`` and patches Quart's default_config. It does
    NOT touch ``Request.get_json`` — that global clobber leaks across the
    whole pytest session and corrupts every later native-async handler
    (conversation/search PUTs persist empty bodies), so it lives in
    ``_patch_sync_get_json`` and must be reverted per-test-class.
    """
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    # Newer Flask sansio (3.1+) reads config['PROVIDE_AUTOMATIC_OPTIONS']
    # in add_url_rule, but the installed Quart dropped it from
    # default_config → bare Quart(__name__) raises KeyError on
    # construction. server.py patches this at import time; replicate it
    # here so this module builds its own app standalone.
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}


def _patch_sync_get_json():
    """Make ``Request.get_json`` sync-callable; return a restorer or None.

    PROCESS-WIDE mutation — the caller MUST run the returned restorer on
    teardown (e.g. via ``addClassCleanup``) or later async handlers break.
    """
    import inspect

    from quart.wrappers import Request as _QR
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            coro = _orig(self, *a, **kw)
            return _a.run(coro)
        _QR.get_json = _sync_get_json

        def _restore():
            _QR.get_json = _orig
        return _restore
    return None


# Install ONLY the import shim at module load time so any subsequent
# `from routes.api_v1.providers import …` (including the inline imports
# inside HeaderAllowlistTest) sees the patched flask. The get_json clobber
# is applied per-class (with restoration) in setUpClass, NOT here.
_install_shim()


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

    @classmethod
    def setUpClass(cls):
        _install_shim()
        # Sync-callable Request.get_json for this class's standalone app,
        # reverted after the class so the global patch never leaks into
        # later native-async test modules.
        _restore = _patch_sync_get_json()
        if _restore is not None:
            cls.addClassCleanup(_restore)
        cls._tmp = tempfile.TemporaryDirectory()

        from lib import api_keys, byo_providers
        cls._orig_keys = api_keys._STORE_PATH
        cls._orig_byo = byo_providers._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        byo_providers._STORE_PATH = os.path.join(cls._tmp.name, 'byo.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        os.environ['TUNNEL_TOKEN'] = 'test-no-real'

        from quart import Quart
        cls.app = Quart(__name__)
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
        _row, cls.alice = create_key(
            name='alice', scopes=['providers', 'chat'])
        _row, cls.bob = create_key(
            name='bob', scopes=['providers', 'chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys, byo_providers
        api_keys._STORE_PATH = cls._orig_keys
        byo_providers._STORE_PATH = cls._orig_byo
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        cls._tmp.cleanup()

    def setUp(self):
        from lib import byo_providers
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        try:
            os.remove(byo_providers._STORE_PATH)
        except FileNotFoundError:
            pass

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
