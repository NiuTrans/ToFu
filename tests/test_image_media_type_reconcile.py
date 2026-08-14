"""Regression: _validate_image_blocks must reconcile a mislabeled data-URI
media type with the actual image bytes.

A data URI whose declared media type disagrees with the real bytes (e.g. PNG
bytes tagged ``image/jpeg``) is silently accepted by OpenAI-compat gateways but
HARD-REJECTED by the Anthropic Messages API with HTTP 400:

    messages.N.content.0.image.source.base64: The image was specified using the
    image/jpeg media type, but the image does not appear to be in that format.

That 400 surfaces as ``SSE error: messages.N.content…`` and kills the turn. The
validator now rewrites the header to the sniffed type so the outbound data URI
is self-consistent for every provider.
"""
import base64

from lib.llm.body import _validate_image_blocks

# Minimal valid magic-byte headers padded past the 32-byte floor.
_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 300
_JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 300
# WebP is a RIFF container: bytes 0-3 == 'RIFF', bytes 8-11 == 'WEBP'.
_WEBP = b'RIFF' + b'\x00\x00\x00\x00' + b'WEBP' + b'VP8 ' + b'\x00' * 300
# A RIFF container that is NOT WebP (e.g. a WAV) — must NOT be mislabeled webp.
_WAV = b'RIFF' + b'\x00\x00\x00\x00' + b'WAVE' + b'fmt ' + b'\x00' * 300


def _data_uri(mime, raw):
    return f'data:{mime};base64,{base64.b64encode(raw).decode()}'


def _img_msg(url):
    return [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}]


def _url_of(msgs):
    return msgs[0]['content'][0]['image_url']['url']


def test_png_mislabeled_as_jpeg_is_corrected():
    msgs = _img_msg(_data_uri('image/jpeg', _PNG))
    _validate_image_blocks(msgs)
    assert _url_of(msgs).startswith('data:image/png;base64,')


def test_jpeg_mislabeled_as_png_is_corrected():
    msgs = _img_msg(_data_uri('image/png', _JPEG))
    _validate_image_blocks(msgs)
    assert _url_of(msgs).startswith('data:image/jpeg;base64,')


def test_correctly_labeled_image_is_left_byte_identical():
    # A self-consistent data URI must NOT be rewritten (no needless prompt-
    # cache churn: the wire bytes must stay identical round to round).
    original = _data_uri('image/png', _PNG)
    msgs = _img_msg(original)
    _validate_image_blocks(msgs)
    assert _url_of(msgs) == original


def test_payload_bytes_are_preserved_when_header_is_rewritten():
    # Only the header changes — the base64 payload must be untouched.
    b64 = base64.b64encode(_PNG).decode()
    msgs = _img_msg(f'data:image/jpeg;base64,{b64}')
    _validate_image_blocks(msgs)
    url = _url_of(msgs)
    assert url == f'data:image/png;base64,{b64}'


def test_NC_reconcile_removed_would_leak_mislabeled_type():
    # Neutralization check: if reconciliation is defeated, the mislabeled
    # header survives — the exact condition that produced the Anthropic 400.
    # (Documents what the fix prevents; the positive tests above assert it.)
    msgs = _img_msg(_data_uri('image/jpeg', _PNG))
    _validate_image_blocks(msgs)
    # With the fix in place the declared type is NEVER left as jpeg for PNG bytes.
    assert not _url_of(msgs).startswith('data:image/jpeg;base64,')


# ── WebP RIFF-container discrimination ──
# A bare startswith(b'RIFF') matches WAV/AVI too. WebP must be sniffed by the
# full RIFF....WEBP signature, else a non-WebP RIFF payload gets mislabeled
# image/webp — a NEW media-type mismatch that would 400 on Anthropic.

def test_real_webp_correctly_labeled_is_left_byte_identical():
    original = _data_uri('image/webp', _WEBP)
    msgs = _img_msg(original)
    _validate_image_blocks(msgs)
    assert _url_of(msgs) == original


def test_real_webp_mislabeled_as_jpeg_is_corrected_to_webp():
    msgs = _img_msg(_data_uri('image/jpeg', _WEBP))
    _validate_image_blocks(msgs)
    assert _url_of(msgs).startswith('data:image/webp;base64,')


def test_riff_but_not_webp_is_dropped_not_mislabeled_webp():
    # A WAV (RIFF, but 'WAVE' at offset 8) is NOT an image. It must be dropped
    # to a text placeholder — never sniffed/relabeled as image/webp.
    msgs = _img_msg(_data_uri('image/webp', _WAV))
    _validate_image_blocks(msgs)
    block = msgs[0]['content'][0]
    assert block['type'] == 'text'
    assert 'invalid or corrupted' in block['text']


def test_swap_path_runs_reconciliation_on_prebuilt_body():
    # The dispatch pre-built-body swap path (model swapped onto Claude) must
    # also reconcile — build_body's _validate_image_blocks never ran there.
    # _adapt_stream_body_for_slot(is_body=True) is the seam.
    from types import SimpleNamespace
    from lib.llm_dispatch.api import _adapt_stream_body_for_slot
    slot = SimpleNamespace(model='aws.claude-opus-4.8', thinking_format='',
                           provider_id='sankuai', protocol='')
    body = {'model': 'some-other-model', 'max_tokens': 4096,
            'messages': _img_msg(_data_uri('image/jpeg', _PNG))}
    out = _adapt_stream_body_for_slot(
        slot, body, True, tools=None, max_tokens=4096, temperature=1.0,
        thinking_enabled=False, preset='medium', effort=None)
    url = out['messages'][0]['content'][0]['image_url']['url']
    assert url.startswith('data:image/png;base64,'), url[:40]


# ── Anthropic boundary reconciliation (openai_body_to_anthropic) ──
# This is the LAST transform before the strict Messages API and must emit a
# self-consistent source.media_type REGARDLESS of which upstream seam fed it —
# so it reconciles even when the incoming body NEVER passed _validate_image_blocks.

def _anthropic_source(mime, raw):
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    body = {'model': 'aws.claude-opus-4.8',
            'messages': _img_msg(_data_uri(mime, raw))}
    out = openai_body_to_anthropic(body)
    return out['messages'][0]['content'][0]['source']


def test_anthropic_boundary_reconciles_unvalidated_body():
    # Feed a body that skipped _validate_image_blocks entirely: PNG bytes
    # tagged image/jpeg. The boundary MUST emit source.media_type=image/png.
    src = _anthropic_source('image/jpeg', _PNG)
    assert src['media_type'] == 'image/png', src
    # base64 payload untouched.
    assert src['data'] == base64.b64encode(_PNG).decode()


def test_anthropic_boundary_reconciles_webp():
    src = _anthropic_source('image/jpeg', _WEBP)
    assert src['media_type'] == 'image/webp', src


def test_anthropic_boundary_leaves_self_consistent_media_type():
    src = _anthropic_source('image/png', _PNG)
    assert src['media_type'] == 'image/png'


def test_NC_anthropic_boundary_without_sniff_leaks_declared_type():
    # Neutralization intent: if the boundary trusted the declared header
    # (the pre-fix behaviour), a PNG-as-jpeg body would emit media_type=
    # image/jpeg → the exact HTTP 400. The fix guarantees it never does.
    src = _anthropic_source('image/jpeg', _PNG)
    assert src['media_type'] != 'image/jpeg'


# ── Source guard: conv_message_builder._build_user_message ──
# The mislabeled data URI is BORN here — the stored ``mediaType`` DB value is
# trusted verbatim. Sniff-and-correct it BEFORE constructing the data URI so
# the DB never emits a mismatched URL (root-cause layer; the three downstream
# reconciliations are then belt-and-suspenders).

def _built_user_image_url(media_type, raw):
    from lib.tasks_pkg.conv_message_builder import _build_user_message
    msg = {'role': 'user', 'content': 'hi',
           'images': [{'base64': base64.b64encode(raw).decode(),
                       'mediaType': media_type}]}
    out = _build_user_message(msg)
    # content is a list of blocks; find the image_url block.
    for b in out['content']:
        if b.get('type') == 'image_url':
            return b['image_url']['url']
    raise AssertionError('no image_url block emitted')


def test_source_guard_corrects_png_stored_as_jpeg():
    url = _built_user_image_url('image/jpeg', _PNG)
    assert url.startswith('data:image/png;base64,'), url[:40]
    # payload untouched
    assert url.endswith(base64.b64encode(_PNG).decode())


def test_source_guard_corrects_webp_stored_as_png():
    url = _built_user_image_url('image/png', _WEBP)
    assert url.startswith('data:image/webp;base64,'), url[:40]


def test_source_guard_leaves_correctly_labeled_untouched():
    url = _built_user_image_url('image/png', _PNG)
    assert url == 'data:image/png;base64,' + base64.b64encode(_PNG).decode()


def test_NC_source_guard_removed_would_emit_mislabeled_url():
    # Neutralization intent: without the sniff-and-correct, _build_user_message
    # would emit data:image/jpeg for PNG bytes — the DB-born mismatch that 400s.
    # The fix guarantees the emitted URL is never the mislabeled jpeg.
    url = _built_user_image_url('image/jpeg', _PNG)
    assert not url.startswith('data:image/jpeg;base64,')
