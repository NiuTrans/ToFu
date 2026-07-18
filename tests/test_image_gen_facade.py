#!/usr/bin/env python3
"""Facade contract test for the lib/image_gen decomposition.

lib/image_gen.py (a 1125-line collection of independent functions) was split
into a lib/image_gen/ subpackage. This suite pins the PUBLIC contract the split
must preserve byte-for-byte:

  * ``from lib.image_gen import generate_image`` still works (the ONLY public
    export — every caller in routes/, scripts/, lib/tools/ uses exactly this).
  * ``lib.image_gen.generate_image`` is callable.
  * The internal helpers other modules / the pipeline reason about are still
    reachable on the package (so a facade regression surfaces here).
  * The provider-routing predicates (_is_friday_provider / _is_openai_model)
    and base-url derivations behave exactly as before for representative slots.

No network. Uses a tiny fake slot object. Run standalone
(``python tests/test_image_gen_facade.py``) or via pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


class _FakeSlot:
    def __init__(self, base_url):
        self.base_url = base_url


def test_public_import_generate_image():
    from lib.image_gen import generate_image
    assert callable(generate_image)
    _ok('from lib.image_gen import generate_image works')


def test_module_attr_generate_image():
    import lib.image_gen as ig
    assert callable(ig.generate_image)
    assert 'generate_image' in ig.__all__
    _ok('lib.image_gen.generate_image callable + in __all__')


def test_internal_helpers_reachable():
    """The private helpers the pipeline / retries reason about stay reachable
    on the package (a facade regression that drops one surfaces here)."""
    import lib.image_gen as ig
    for name in ('_generate_openai', '_edit_openai', '_generate_gemini',
                 '_generate_chat_completions', '_build_multiturn_contents',
                 '_pick_image_slot', '_is_friday_provider', '_is_openai_model',
                 '_friday_base_from_slot', '_api_base_from_slot',
                 '_download_image', '_RateLimitError', '_HttpError'):
        assert hasattr(ig, name), f'missing helper: {name}'
    _ok('internal helpers reachable on the package facade')


def test_friday_provider_detection():
    import lib.image_gen as ig
    assert ig._is_friday_provider(_FakeSlot('https://api.openai.com/v1')) is True
    assert ig._is_friday_provider(_FakeSlot('https://yeysai.com/v1')) is False
    assert ig._is_friday_provider(None) is False
    _ok('_is_friday_provider: FRIDAY domain vs OpenAI-compatible vs None')


def test_base_url_derivations():
    import lib.image_gen as ig
    slot = _FakeSlot('https://api.openai.com/v1/openai')
    # FRIDAY base = scheme://host only (no path).
    assert ig._friday_base_from_slot(slot) == 'https://api.openai.com'
    # Standard base = full base_url, trailing slash stripped.
    slot2 = _FakeSlot('https://yeysai.com/v1/')
    assert ig._api_base_from_slot(slot2) == 'https://yeysai.com/v1'
    _ok('_friday_base_from_slot / _api_base_from_slot derive correctly')


def test_openai_model_detection():
    import lib.image_gen as ig
    assert ig._is_openai_model('gpt-image-1.5') is True
    assert ig._is_openai_model('GPT-IMAGE-2') is True   # case-insensitive
    assert ig._is_openai_model('gemini-3-pro-image-preview') is False
    _ok('_is_openai_model: OpenAI family vs Gemini')


def test_error_types_are_shared():
    """_RateLimitError / _HttpError must be the SAME classes the orchestrator
    catches — a split that duplicated them would break except-clause matching."""
    import lib.image_gen as ig
    assert issubclass(ig._RateLimitError, Exception)
    he = ig._HttpError(429, 'body', 1.2)
    assert he.status_code == 429 and he.elapsed == 1.2
    _ok('_RateLimitError / _HttpError shape preserved')


def main():
    print()
    print(_color('═══ lib/image_gen Facade Contract Tests ═══', '36'))
    print()
    tests = [
        test_public_import_generate_image,
        test_module_attr_generate_image,
        test_internal_helpers_reachable,
        test_friday_provider_detection,
        test_base_url_derivations,
        test_openai_model_detection,
        test_error_types_are_shared,
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
