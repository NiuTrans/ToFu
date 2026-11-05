"""Regression tests for lib/llm/body/_images.py::_downscale_oversized_images.

For Claude models every base64 image is held to a per-model, COUNT-INDEPENDENT
long-edge cap plus a per-image base64 byte budget
(https://platform.claude.com/docs/en/build-with-claude/vision):

* Claude generation 4.7+ (any line): 2,000 px. Their high-resolution tier
  reaches 2,576 px, but a request with more than 20 images must keep every
  image at or under 2,000 px on every platform;
* every other Claude model: 1,568 px, the size those models downscale to;
* at most 5 MB of base64 per image (Amazon Bedrock; the Claude API allows 10).

The load-bearing invariant these tests protect is the CACHE-CLIFF FIX: no limit
depends on how many images a request carries, so an image is re-encoded at most
ONCE (its first send over a limit) and then stays byte-identical forever. The
old 7999/1999 px two-tier design retro-shrank images 1-4 on the round the 5th
arrived (a guaranteed prompt-cache miss); a 2,576 px cap would do the same at
the 21st image.
"""

import base64
import io
import random

import pytest

pytest.importorskip('PIL')
from PIL import Image  # noqa: E402

from lib.llm.body import (  # noqa: E402
    _CLAUDE_HIGH_RES_IMAGE_MAX_PX,
    _CLAUDE_IMAGE_MAX_BASE64_BYTES,
    _CLAUDE_STANDARD_IMAGE_MAX_PX,
    _downscale_oversized_images,
)

pytestmark = pytest.mark.unit

_HIGH_RES_MODEL = 'claude-opus-4-8'
_STANDARD_MODEL = 'claude-sonnet-4-6'
_TIERS = [
    pytest.param(_STANDARD_MODEL, _CLAUDE_STANDARD_IMAGE_MAX_PX, id='standard'),
    pytest.param(_HIGH_RES_MODEL, _CLAUDE_HIGH_RES_IMAGE_MAX_PX, id='high-res'),
]


def _png_data_uri(w: int, h: int, mode: str = 'RGB') -> str:
    """A solid-colour PNG of the given dimensions as an image_url data URI."""
    img = Image.new(mode, (w, h), (123, 45, 67, 200)[:len(mode)])
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'


def _noise_png_data_uri(w: int, h: int, mode: str) -> str:
    """Incompressible pixels: a PNG far over the base64 budget for its size."""
    pixels = random.Random(f'{w}x{h}{mode}').randbytes(w * h * len(mode))
    buf = io.BytesIO()
    Image.frombytes(mode, (w, h), pixels).save(buf, format='PNG',
                                               compress_level=1)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'


def _img_block(url: str) -> dict:
    return {'type': 'image_url', 'image_url': {'url': url}}


def _decode(data_uri: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data_uri.split(',', 1)[1])))


def _long_side(data_uri: str) -> int:
    return max(_decode(data_uri).size)


def _user_msg(*urls: str) -> dict:
    return {'role': 'user',
            'content': [{'type': 'text', 'text': 'x'}, *(_img_block(u) for u in urls)]}


def _prepared(url: str, model: str) -> str:
    msgs = [_user_msg(url)]
    msgs = _downscale_oversized_images(msgs, model)
    return msgs[0]['content'][1]['image_url']['url']


# ── The cap follows the Claude generation, not a substring needle ──

@pytest.mark.parametrize('model', [
    'claude-opus-4-7', 'claude-opus-4-8', 'aws.claude-opus-4.7',
    'us.anthropic.claude-opus-4-7-v1:0', 'kunlunbedrock.claude-opus-5.5-m17',
    'claude-sonnet-5', 'claude-haiku-5-5', 'fable-5', 'claude-mythos-5',
])
def test_generation_4_7_and_later_get_the_2000px_cap(model):
    edge = _png_data_uri(1999, 20)
    assert _prepared(edge, model) == edge
    assert _long_side(_prepared(_png_data_uri(2600, 20), model)) == 2000


@pytest.mark.parametrize('model', [
    'claude-opus-4-6', 'claude-sonnet-4-6', 'aws.claude-sonnet-4.5',
    'claude-haiku-4-5-20251001', 'claude-opus-4-20250514',
    'claude-3-opus-20240229', 'claude-3-7-sonnet-20250219',
])
def test_earlier_generations_keep_the_1568px_cap(model):
    assert _long_side(_prepared(_png_data_uri(1999, 20), model)) == 1568


def test_high_res_model_keeps_1999px_and_shrinks_2600px_to_2000():
    for long_side in (1999, 2000):
        kept = _png_data_uri(long_side, 1200)
        assert _prepared(kept, _HIGH_RES_MODEL) == kept
    shrunk = _decode(_prepared(_png_data_uri(2600, 1700), _HIGH_RES_MODEL))
    assert shrunk.size == (2000, 1307)


def test_standard_model_still_shrinks_to_1568():
    assert _decode(_prepared(_png_data_uri(2600, 1700), _STANDARD_MODEL)).size == (
        1568, 1025)
    assert _long_side(_prepared(_png_data_uri(1999, 1200), _STANDARD_MODEL)) == 1568


# ── Count independence: no boundary re-encodes an already-sent image ──

@pytest.mark.parametrize('model,cap', _TIERS)
def test_cap_is_count_independent(model, cap):
    """A 2048px image lands on the same cap alone, as the 5th, or as the 21st."""
    for n in (1, 5, 21):
        msgs = [_user_msg(*[_png_data_uri(2048, 120) for _ in range(n)])]
        msgs = _downscale_oversized_images(msgs, model)
        for i in range(n):
            assert _long_side(msgs[0]['content'][1 + i]['image_url']['url']) == cap, (
                f'n={n} img={i}')


@pytest.mark.parametrize('model,cap', _TIERS)
@pytest.mark.parametrize('earlier', [4, 20])
def test_boundary_image_does_not_retroactively_reencode_capped_images(
        model, cap, earlier):
    """Crossing the old 5-image or the >20-image boundary leaves every
    already-capped image byte-identical, so the prompt cache holds."""
    capped = [_png_data_uri(cap, 1000) for _ in range(earlier)]
    msgs = [_user_msg(*capped, _png_data_uri(2600, 1200))]
    msgs = _downscale_oversized_images(msgs, model)
    for i, before in enumerate(capped):
        assert msgs[0]['content'][1 + i]['image_url']['url'] == before, (
            f'image {i} was re-encoded when image {earlier + 1} arrived')
    assert _long_side(msgs[0]['content'][1 + earlier]['image_url']['url']) == cap


@pytest.mark.parametrize('model,cap', _TIERS)
def test_band_image_stable_across_boundaries(model, cap):
    """An image just over the cap is shrunk on its FIRST send; the shrunk
    bytes then survive any later image count untouched."""
    band = _png_data_uri(cap + 100, 1000)
    first = [_user_msg(band, *[_png_data_uri(1024, 768) for _ in range(3)])]
    first = _downscale_oversized_images(first, model)
    band_capped = first[0]['content'][1]['image_url']['url']
    assert _long_side(band_capped) == cap
    later = [_user_msg(band_capped, *[_png_data_uri(1024, 768) for _ in range(21)])]
    later = _downscale_oversized_images(later, model)
    assert later[0]['content'][1]['image_url']['url'] == band_capped


@pytest.mark.parametrize('model,cap', _TIERS)
def test_under_cap_image_untouched(model, cap):
    """An image within every limit (e.g. a 1024px generate_image thumbnail)
    is not re-encoded — bytes are identical in and out."""
    small = _png_data_uri(1024, 768)
    assert _prepared(small, model) == small


# ── Per-image base64 budget ──

@pytest.mark.parametrize('model,cap', _TIERS)
def test_over_budget_jpeg_path_is_reencoded_within_budget(model, cap):
    source = _noise_png_data_uri(1400, 1400, 'RGB')
    assert len(source.split(',', 1)[1]) > _CLAUDE_IMAGE_MAX_BASE64_BYTES
    out = _prepared(source, model)
    assert out.startswith('data:image/jpeg;base64,')
    assert len(out.split(',', 1)[1]) <= _CLAUDE_IMAGE_MAX_BASE64_BYTES
    assert _long_side(out) <= min(1400, cap)
    assert _prepared(out, model) == out


def test_over_budget_alpha_image_stays_png_and_shrinks_into_budget():
    source = _noise_png_data_uri(1100, 1100, 'RGBA')
    assert len(source.split(',', 1)[1]) > _CLAUDE_IMAGE_MAX_BASE64_BYTES
    out = _prepared(source, _HIGH_RES_MODEL)
    assert out.startswith('data:image/png;base64,')
    assert len(out.split(',', 1)[1]) <= _CLAUDE_IMAGE_MAX_BASE64_BYTES
    decoded = _decode(out)
    assert decoded.mode == 'RGBA' and max(decoded.size) < 1100
    assert _prepared(out, _HIGH_RES_MODEL) == out


# ── Transparency survives every re-encode ──

def _line_art_data_uri(w: int, h: int, mode: str) -> str:
    """An opaque black stroke on a fully transparent black background,
    carried as palette transparency (``P`` + tRNS) or an ``LA`` alpha."""
    if mode == 'P':
        img = Image.new('P', (w, h), 0)
        img.putpalette([0, 0, 0] * 256)
        stroke = 1
    else:
        img = Image.new('LA', (w, h), (0, 0))
        stroke = (0, 255)
    img.paste(stroke, (w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    buf = io.BytesIO()
    img.save(buf, format='PNG', **({'transparency': 0} if mode == 'P' else {}))
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _noise_alpha_data_uri(w: int, h: int, mode: str) -> str:
    """Incompressible P + tRNS (half the palette transparent) or LA pixels."""
    rng = random.Random(f'{w}x{h}{mode}')
    img = Image.frombytes(mode, (w, h), rng.randbytes(w * h * len(mode)))
    options = {}
    if mode == 'P':
        img.putpalette(rng.randbytes(768))
        options['transparency'] = bytes([0] * 128 + [255] * 128)
    buf = io.BytesIO()
    img.save(buf, format='PNG', compress_level=1, **options)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


@pytest.mark.parametrize('model,cap', _TIERS)
@pytest.mark.parametrize('mode', ['P', 'LA'])
def test_over_cap_transparent_image_keeps_its_alpha_as_png(model, cap, mode):
    """Flattening palette or gray+alpha transparency into JPEG painted the
    transparent background black, erasing the black stroke drawn on it."""
    out = _prepared(_line_art_data_uri(2600, 1300, mode), model)
    assert out.startswith('data:image/png;base64,')
    decoded = _decode(out)
    assert decoded.mode == 'RGBA' and decoded.size == (cap, cap // 2)
    assert decoded.getpixel((2, 2))[3] == 0
    assert decoded.getpixel((cap // 2, cap // 4)) == (0, 0, 0, 255)
    assert _prepared(out, model) == out


@pytest.mark.parametrize('model,cap', _TIERS)
@pytest.mark.parametrize('mode', ['P', 'LA'])
def test_over_budget_transparent_image_inside_the_cap_stays_png(
        monkeypatch, model, cap, mode):
    """The byte budget re-encodes images already inside the pixel cap; a
    transparent one still keeps its alpha and shrinks into the budget."""
    from lib.llm.body import _images

    budget = 96 * 1024
    monkeypatch.setattr(_images, '_CLAUDE_IMAGE_MAX_BASE64_BYTES', budget)
    source = _noise_alpha_data_uri(400, 400, mode)
    assert len(source.split(',', 1)[1]) > budget
    out = _prepared(source, model)
    assert out.startswith('data:image/png;base64,')
    assert len(out.split(',', 1)[1]) <= budget
    decoded = _decode(out)
    assert decoded.mode == 'RGBA' and max(decoded.size) < 400
    assert decoded.getchannel('A').getextrema()[0] < 255
    assert _prepared(out, model) == out


@pytest.mark.parametrize('model,cap', _TIERS)
@pytest.mark.parametrize('mode', ['RGB', 'RGBA'])
def test_reencode_is_idempotent(model, cap, mode):
    once = _prepared(_png_data_uri(2600, 1700, mode), model)
    assert _long_side(once) == cap
    assert _prepared(once, model) == once


# ── Non-Claude models are left entirely alone ──

def test_non_claude_model_untouched():
    big = _png_data_uri(3000, 2000)
    assert _prepared(big, 'gpt-4o') == big
