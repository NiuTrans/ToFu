"""Regression tests for lib/llm/body.py::_downscale_oversized_images.

The mechanism under test: for Claude models, every base64 image is capped at a
single uniform ``_CLAUDE_IMAGE_MAX_PX`` (1568px) long edge — Claude's own
vision-tower processing resolution.

The load-bearing invariant these tests protect is the CACHE-CLIFF FIX: the old
design used a two-tier cap (7999px single / 1999px for ≥5 images), so the round
the 5th image arrived RETROACTIVELY re-encoded already-sent-and-cached images
1–4 → a guaranteed prompt-cache miss (and per-round churn in image-gen chats).
A count-independent cap re-encodes an image at most ONCE (its first send above
the cap) and then leaves it byte-identical forever, regardless of how many
images accumulate. That "byte-identical across the 5th-image boundary" property
is what the cache relies on and is the primary NC below.
"""

import base64
import io

import pytest

pytest.importorskip('PIL')
from PIL import Image  # noqa: E402

from lib.llm.body import _CLAUDE_IMAGE_MAX_PX, _downscale_oversized_images  # noqa: E402

pytestmark = pytest.mark.unit

_CLAUDE_MODEL = 'claude-opus-4-8'


def _png_data_uri(w: int, h: int) -> str:
    """A solid-colour PNG of the given dimensions as an image_url data URI."""
    img = Image.new('RGB', (w, h), (123, 45, 67))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'


def _img_block(url: str) -> dict:
    return {'type': 'image_url', 'image_url': {'url': url}}


def _long_side(data_uri: str) -> int:
    b64 = data_uri.split(',', 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    return max(img.size)


def _user_msg(*urls: str) -> dict:
    return {'role': 'user',
            'content': [{'type': 'text', 'text': 'x'}, *(_img_block(u) for u in urls)]}


# ── Oversized images ARE shrunk to the uniform cap ──

def test_oversized_single_image_shrunk_to_cap():
    """One 3000px image → downscaled to the 1568px cap (no two-tier 7999px)."""
    big = _png_data_uri(3000, 2000)
    msgs = [_user_msg(big)]
    _downscale_oversized_images(msgs, _CLAUDE_MODEL)
    out = msgs[0]['content'][1]['image_url']['url']
    # int() floor in the resize can land on cap or cap-1; the invariant is
    # "never exceeds cap" and "actually shrank from 3000".
    assert _long_side(out) <= _CLAUDE_IMAGE_MAX_PX
    assert _long_side(out) >= _CLAUDE_IMAGE_MAX_PX - 1


def test_cap_is_count_independent():
    """The cap does NOT depend on image count. A 2048px image is shrunk to
    1568px whether it is the only image or one of six — the old design left it
    at ≤7999px for <5 images then dropped to 1999px at the 5th."""
    for n in (1, 4, 5, 6):
        urls = [_png_data_uri(2048, 1200) for _ in range(n)]
        msgs = [_user_msg(*urls)]
        _downscale_oversized_images(msgs, _CLAUDE_MODEL)
        for i in range(n):
            out = msgs[0]['content'][1 + i]['image_url']['url']
            ls = _long_side(out)
            assert _CLAUDE_IMAGE_MAX_PX - 1 <= ls <= _CLAUDE_IMAGE_MAX_PX, (
                f'n={n} img={i}: expected ~cap {_CLAUDE_IMAGE_MAX_PX}, got {ls}')


# ── Images already at/under the cap are LEFT BYTE-IDENTICAL (idempotent) ──

def test_under_cap_image_untouched():
    """An image already ≤ cap (e.g. a 1024px generate_image thumbnail) is not
    re-encoded — bytes are identical in and out."""
    small = _png_data_uri(1024, 768)
    msgs = [_user_msg(small)]
    _downscale_oversized_images(msgs, _CLAUDE_MODEL)
    assert msgs[0]['content'][1]['image_url']['url'] == small


# ── THE CACHE-CLIFF NC: the 5th image does NOT re-encode images 1–4 ──

def test_fifth_image_does_not_retroactively_reencode_capped_images():
    """The core fix. Send 4 already-capped (1568px) images, then a 5th. The
    first four MUST come out byte-identical — crossing the old ≥5 threshold no
    longer retro-shrinks them, so the prompt cache holds.

    Under the old two-tier design the 5th image dropped max_px to 1999 which,
    while it would not have shrunk a 1568px image either, DID shrink anything
    in the 1600–1999px band that rode through at the single-image cap. With a
    fixed 1568px cap there is no band that survives to <5 and dies at ≥5 — the
    boundary simply does not exist.
    """
    capped = [_png_data_uri(_CLAUDE_IMAGE_MAX_PX, 1000) for _ in range(4)]
    before = list(capped)
    msgs = [_user_msg(*capped, _png_data_uri(2048, 1200))]  # 5th is oversized
    _downscale_oversized_images(msgs, _CLAUDE_MODEL)
    for i in range(4):
        assert msgs[0]['content'][1 + i]['image_url']['url'] == before[i], (
            f'image {i} was re-encoded when the 5th image arrived — '
            f'cache cliff regressed')
    # The 5th (oversized) one IS shrunk to the cap.
    assert _long_side(msgs[0]['content'][5]['image_url']['url']) <= _CLAUDE_IMAGE_MAX_PX


def test_band_image_stable_across_fifth_boundary():
    """A 1600px image (in the OLD 1600–1999 survives-single-but-dies-at-many
    band) is now shrunk to 1568 on its FIRST send regardless of count, so it is
    byte-identical whether 4 or 5 images are present — no boundary re-encode."""
    band = _png_data_uri(1600, 1000)
    # First send alongside 3 others (4 total): shrunk to cap once.
    msgs4 = [_user_msg(band, *[_png_data_uri(1024, 768) for _ in range(3)])]
    _downscale_oversized_images(msgs4, _CLAUDE_MODEL)
    band_capped = msgs4[0]['content'][1]['image_url']['url']
    assert _long_side(band_capped) <= _CLAUDE_IMAGE_MAX_PX
    # Now the SAME already-capped image among 5: must be untouched.
    msgs5 = [_user_msg(band_capped, *[_png_data_uri(1024, 768) for _ in range(4)])]
    _downscale_oversized_images(msgs5, _CLAUDE_MODEL)
    assert msgs5[0]['content'][1]['image_url']['url'] == band_capped


# ── Non-Claude models are left entirely alone ──

def test_non_claude_model_untouched():
    big = _png_data_uri(3000, 2000)
    msgs = [_user_msg(big)]
    _downscale_oversized_images(msgs, 'gpt-4o')
    assert msgs[0]['content'][1]['image_url']['url'] == big
