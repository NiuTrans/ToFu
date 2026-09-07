#!/usr/bin/env python3
"""Regression tests for the image_gen OpenAI-native no-image return contract.

The bug (found during the image_gen decomposition, ticket pt_ca27f76046794622):
``_generate_openai`` fell off the end returning ``None`` when the API responded
200 with an item that carried NO ``b64_json`` and NO ``url`` — its ``if image_b64:``
block was the last statement and had no ``else``/trailing return. Its sibling
``_edit_openai`` DID have the ``{ok:False,...}`` return but also carried an
unreachable duplicate return after it.

Why it matters: the public ``generate_image`` orchestrator does
``result.get('ok')``. A ``None`` return raises ``AttributeError`` INSIDE the
per-attempt try-block, which the generic ``except Exception`` then mislabels as
a crash and burns a hard-retry — instead of cleanly reporting "no image in
response". The contract every other generator honours is: always return a dict
with an ``ok`` key.

These tests stub ``http_post`` in lib.image_gen._openai so no network is hit.
Run standalone (``python tests/test_image_gen_openai_no_image_return.py``) or via
pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.image_gen._openai as oai


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ''

    def json(self):
        return self._payload


def _stub_http_post(payload):
    """Patch http_post in the _openai module to return a fixed payload."""
    orig = oai.http_post
    oai.http_post = lambda *a, **k: _FakeResp(payload)  # type: ignore
    return orig


def _restore(orig):
    oai.http_post = orig  # type: ignore


# ── _generate_openai ──────────────────────────────────────────────────

def test_generate_no_image_returns_dict_not_none():
    """200 with an item lacking b64_json AND url → {ok:False}, never None."""
    orig = _stub_http_post({'data': [{'revised_prompt': 'x'}]})  # no b64, no url
    try:
        r = oai._generate_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30)
    finally:
        _restore(orig)
    assert r is not None, '_generate_openai returned None (the bug)'
    assert isinstance(r, dict) and r.get('ok') is False, r
    assert r.get('error'), 'a no-image result must carry an error message'
    _ok('_generate_openai: no-image 200 → {ok:False, error:...} (not None)')


def test_generate_success_still_ok():
    """A normal inline-b64 success is unchanged (ok=True with the image)."""
    orig = _stub_http_post({'data': [{'b64_json': 'QUJD', 'revised_prompt': 'r'}]})
    try:
        r = oai._generate_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30)
    finally:
        _restore(orig)
    assert r.get('ok') is True and r.get('image_b64') == 'QUJD', r
    assert r.get('text') == 'r'
    _ok('_generate_openai: inline-b64 success unchanged (ok=True)')


def test_generate_empty_data_returns_dict():
    """Empty data[] already returned a dict — must stay a dict."""
    orig = _stub_http_post({'data': []})
    try:
        r = oai._generate_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30)
    finally:
        _restore(orig)
    assert isinstance(r, dict) and r.get('ok') is False, r
    _ok('_generate_openai: empty data[] → {ok:False} (unchanged)')


# ── _edit_openai ──────────────────────────────────────────────────────

def test_edit_no_image_returns_dict_not_none():
    """Edit no-image 200 already returned a dict; assert it still does + the
    unreachable duplicate return is gone (single reachable {ok:False})."""
    src = [{'image_b64': 'QUJD'}]
    orig = _stub_http_post({'data': [{'revised_prompt': 'x'}]})  # no b64, no url
    try:
        r = oai._edit_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30, src)
    finally:
        _restore(orig)
    assert isinstance(r, dict) and r.get('ok') is False, r
    assert r.get('error') == 'No image data in OpenAI edit response', r
    _ok('_edit_openai: no-image 200 → single {ok:False} return')


def test_edit_success_still_ok():
    src = [{'image_b64': 'QUJD'}]
    orig = _stub_http_post({'data': [{'b64_json': 'WFla'}]})
    try:
        r = oai._edit_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30, src)
    finally:
        _restore(orig)
    assert r.get('ok') is True and r.get('image_b64') == 'WFla', r
    _ok('_edit_openai: inline-b64 success unchanged (ok=True)')


def test_edit_no_source_images_returns_dict():
    r = oai._edit_openai('p', 'gpt-image-1.5', 'k', '1:1', '1K', 30, [])
    assert isinstance(r, dict) and r.get('ok') is False, r
    _ok('_edit_openai: no source images → {ok:False} (unchanged)')


def test_edit_has_no_unreachable_return():
    """Static guard: the function must not carry the old duplicated trailing
    return (dead code after the reachable {ok:False} return)."""
    import inspect
    src = inspect.getsource(oai._edit_openai)
    assert src.count("return {'ok': False, 'error': 'No image data in OpenAI") == 1, \
        'duplicate/unreachable return still present in _edit_openai'
    _ok('_edit_openai: no unreachable duplicate return (dead code removed)')


def main():
    print()
    print(_color('═══ image_gen OpenAI no-image return-contract tests ═══', '36'))
    print()
    tests = [
        test_generate_no_image_returns_dict_not_none,
        test_generate_success_still_ok,
        test_generate_empty_data_returns_dict,
        test_edit_no_image_returns_dict_not_none,
        test_edit_success_still_ok,
        test_edit_no_source_images_returns_dict,
        test_edit_has_no_unreachable_return,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
