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
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


class _FakeSlot:
    def __init__(self, base_url):
        self.base_url = base_url


class _RetrySlot(_FakeSlot):
    provider_id = 'test-provider'
    extra_headers = None

    def record_error(self, **_kwargs):
        return None

    def record_success(self, _latency_ms):
        return None


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


@pytest.mark.skipif(
    is_opensource_build(),
    reason="'friday' is an internal provider whose gateway host is sanitized "
           'away in opensource builds — there is nothing to detect there')
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


def test_background_image_dispatch_has_finite_429_ceiling(monkeypatch):
    import lib.image_gen._generate as orchestrator

    slot = _RetrySlot('https://images.example.test/v1')
    calls = []
    monkeypatch.setattr(
        orchestrator, '_pick_image_slot', lambda **_kwargs: ('key', 'm', slot))

    def rate_limited(*_args, **_kwargs):
        calls.append(1)
        raise orchestrator._RateLimitError('busy')

    monkeypatch.setattr(orchestrator, '_generate_chat_completions', rate_limited)
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda _seconds: None)

    result = orchestrator.generate_image(
        'bounded', max_retries=0, max_429_attempts=3)

    assert len(calls) == 3
    assert result['ok'] is False
    assert result['rate_limited'] is True


def test_background_image_dispatch_discards_late_reply_after_abort(monkeypatch):
    import lib.image_gen._generate as orchestrator

    slot = _RetrySlot('https://images.example.test/v1')
    event = threading.Event()
    monkeypatch.setattr(
        orchestrator, '_pick_image_slot', lambda **_kwargs: ('key', 'm', slot))

    def late_success(*_args, **_kwargs):
        event.set()
        return {'ok': True, 'image_b64': 'bGF0ZQ==',
                'mime_type': 'image/png'}

    monkeypatch.setattr(orchestrator, '_generate_chat_completions', late_success)
    result = orchestrator.generate_image(
        'cancelled', abort_check=event.is_set, max_429_attempts=3)

    assert result['ok'] is False
    assert result['aborted'] is True
    assert 'image_b64' not in result


def test_owner_scoped_image_dispatch_pins_and_disposes_route(monkeypatch):
    import lib.image_gen._generate as orchestrator
    import lib.model_routing as model_routing
    from lib.llm_dispatch.provider_pin import get_pinned_provider

    slot = _RetrySlot('http://127.0.0.1:18100/v1')
    slot.api_key = ''
    group = SimpleNamespace(pin_id='owner-image-pin')
    captured = {}

    def mint(_repository, boundary, capability, **kwargs):
        captured['boundary'] = boundary
        captured['capability'] = capability
        captured['kwargs'] = kwargs
        return 'local-image-v1', group

    def pick(**kwargs):
        captured['pin_during_pick'] = get_pinned_provider()
        captured['prefer_model'] = kwargs['prefer_model']
        return '', 'local-image-v1', slot

    monkeypatch.setattr(model_routing, 'mint_capability_slot_group', mint)
    monkeypatch.setattr(
        model_routing, 'dispose_routed_slot_group',
        lambda value: captured.setdefault('disposed', value) is value,
    )
    monkeypatch.setattr(orchestrator, '_pick_image_slot', pick)
    monkeypatch.setattr(
        orchestrator, '_generate_chat_completions',
        lambda *_args, **_kwargs: {
            'ok': True, 'image_b64': 'aW1hZ2U=', 'mime_type': 'image/png'},
    )

    result = orchestrator.generate_image(
        'owner image',
        owner_user_id=73,
        tenant_id='tenant-images',
        preferred_provider_id='provider-images',
        max_retries=0,
    )

    assert result['ok'] is True
    assert captured['boundary'].owner_user_id == 73
    assert captured['boundary'].tenant_id == 'tenant-images'
    assert captured['capability'] == 'image_gen'
    assert captured['kwargs']['preferred_provider_id'] == 'provider-images'
    assert captured['pin_during_pick'] == 'owner-image-pin'
    assert captured['prefer_model'] == 'local-image-v1'
    assert captured['disposed'] is group
    assert get_pinned_provider() is None


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '3'])
def test_image_dispatch_rejects_invalid_429_ceiling(value):
    from lib.image_gen import generate_image

    with pytest.raises(ValueError, match='positive integer'):
        generate_image('x', max_429_attempts=value)


def test_generated_image_url_download_is_stream_bounded(monkeypatch):
    import lib.image_gen as image_gen
    import lib.image_gen._errors as errors

    class Response:
        headers = {'Content-Type': 'image/png'}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size > 0
            yield b'a' * 60
            yield b'b' * 60

    @contextmanager
    def stream(*_args, **_kwargs):
        yield Response()

    monkeypatch.setattr(errors, '_MAX_GENERATED_IMAGE_BYTES', 100)
    monkeypatch.setattr('lib.http_client.http_stream', stream)

    encoded, mime = image_gen._download_image(
        'https://example.test/generated.png')

    assert encoded is None
    assert mime == 'image/png'


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
