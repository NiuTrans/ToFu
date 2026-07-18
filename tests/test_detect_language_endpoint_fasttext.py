"""Regression: /api/v1/text/detect-language must force fastText on request.

The frontend auto-translate skip gate (``_isAlreadyInTarget`` in
static/js/translation.js) decides whether to translate an assistant reply. It
calls this endpoint and compares ``detected.code`` to the target language. If
the endpoint runs only the script+heuristic tier (the default backend), a
kanji-heavy Japanese reply resolves ``zh`` — sharing the CJK-ideograph block
with Chinese — and the gate wrongly skips it as "already Chinese", the exact
bug the server-side safety net force-fixes. This test proves the
``forceFasttext`` body flag is LOAD-BEARING via a NEUTER twin:

  * WITHOUT the flag, under the default ``script`` backend, the same JP text
    still resolves ``zh`` (the bug's entry point).
  * WITH the flag, it resolves ``ja`` via ``detected.source == 'fasttext'``.

Skipped when ``fast_langdetect`` is unavailable (guarded-optional dep) so the
suite stays green on a vanilla box.
"""

import asyncio
import os
import sys
import unittest

import pytest

pytestmark = pytest.mark.unit

# Kanji-heavy Japanese: >30% CJK ideographs → script/heuristic calls it 'zh'.
_JA_KANJI = '本日は晴天なり。日本語の文章を翻訳する必要があります。全部確認してください。'


def _ft_available() -> bool:
    try:
        import fast_langdetect  # noqa: F401
        return True
    except Exception:
        return False


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    import inspect
    from quart.wrappers import Request as _QR
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            return _a.run(_orig(self, *a, **kw))
        _QR.get_json = _sync_get_json


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipUnless(_ft_available(), 'fast_langdetect not installed')
class DetectLanguageForceFasttextTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        # Pin the DEFAULT backend so the neuter twin genuinely exercises the
        # script+heuristic tier (the production default), not an env leak.
        cls._prev_backend = os.environ.get('TOFU_LANGDETECT_BACKEND')
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
        from lib.text_lang import reset_for_test
        reset_for_test()

        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys
        cls._orig_store = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.logs import api_v1_logs_bp
        cls.app.register_blueprint(api_v1_logs_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(name='detect-bot', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_store
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()
        if cls._prev_backend is None:
            os.environ.pop('TOFU_LANGDETECT_BACKEND', None)
        else:
            os.environ['TOFU_LANGDETECT_BACKEND'] = cls._prev_backend
        from lib.text_lang import reset_for_test
        reset_for_test()

    def _detect(self, text, **body):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/text/detect-language',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'text': text, **body})
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            return await r.get_json()
        return _new_loop_run(go())

    def test_forceFasttext_resolves_japanese_as_ja(self):
        body = self._detect(_JA_KANJI, forceFasttext=True)
        self.assertEqual(body['detected']['code'], 'ja',
                         'forceFasttext must resolve kanji-heavy JP as ja')
        self.assertEqual(body['detected']['source'], 'fasttext',
                         'the statistical model must have actually run')

    def test_neuter_without_flag_still_says_zh(self):
        """NEUTER twin: without the flag, under the default script backend the
        SAME text resolves zh — proving forceFasttext is what fixes it, and the
        old frontend gate (no flag) would still misfire."""
        body = self._detect(_JA_KANJI)  # no forceFasttext
        self.assertEqual(body['detected']['code'], 'zh',
                         'default script+heuristic tier misreads JP as zh — '
                         'this is the bug the flag fixes')
        self.assertNotEqual(body['detected']['source'], 'fasttext')

    def test_snake_case_alias_accepted(self):
        body = self._detect(_JA_KANJI, force_fasttext=True)
        self.assertEqual(body['detected']['code'], 'ja')


if __name__ == '__main__':
    unittest.main()
