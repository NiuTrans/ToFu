"""tests/test_attachment_resolve.py — centralized uploaded-attachment re-access.

Covers the fix for the "model invents /dev/null" bug: a chat-uploaded image
has no filesystem path, so ``inspect_image`` must accept the backend-computed
``/api/images/<f>`` reference and resolve the ORIGINAL bytes via the
centralized ``lib.attachments.resolve_attachment``. PDF/TXT uploads keep only
extracted text, re-readable by a stable content-hash ref.

Assertions:
  - ``/api/images/<f>`` ref → inspect_image crops the original → __screenshot__.
  - PDF/TXT ``att_txt_<hash>`` ref → resolve_attachment returns the stored text.
  - The user message emits the stable refs the model needs.
  - Original-bug regression: an uploaded image no longer falls to
    "File not found"; a bogus /dev/null still fails (control).
  - NEUTER: with attachment-ref recognition disabled, the /api/images ref
    falls through to path resolution and fails → proves the seam is
    load-bearing.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_attachment_resolve.py -v
"""
from __future__ import annotations

import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PIL = pytest.importorskip('PIL')
from PIL import Image  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────

def _write_image(path, *, w, h, color=(120, 40, 200)):
    Image.new('RGB', (w, h), color).save(path)


def _decode_view(result):
    assert isinstance(result, dict), f'expected dict, got {type(result).__name__}: {result!r}'
    assert result.get('__screenshot__') is True
    b64 = result['dataUrl'].split(',', 1)[1]
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)), raw


@pytest.fixture()
def uploads_dir(tmp_path, monkeypatch):
    """Point the attachment resolver's uploads/images dir at a tmp dir."""
    import lib.attachments as att
    d = tmp_path / 'uploads' / 'images'
    d.mkdir(parents=True)
    monkeypatch.setattr(att, '_images_dir', lambda: str(d))
    return d


# ═══════════════════════════════════════════════════════════════════════
#  resolve_attachment — image refs
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveImageRef:

    def test_api_images_ref_reads_disk(self, uploads_dir):
        from lib.attachments import resolve_attachment
        _write_image(str(uploads_dir / 'shot.png'), w=300, h=200)
        out = resolve_attachment('/api/images/shot.png')
        assert out and out['kind'] == 'image'
        assert out['mime_type'] == 'image/png'
        assert out['raw'][:4] == b'\x89PNG'
        # base64 round-trips to the same bytes
        assert base64.b64decode(out['image_b64']) == out['raw']

    def test_data_uri_ref(self):
        from lib.attachments import resolve_attachment
        raw = io.BytesIO()
        Image.new('RGB', (10, 10), (1, 2, 3)).save(raw, format='PNG')
        b64 = base64.b64encode(raw.getvalue()).decode('ascii')
        out = resolve_attachment(f'data:image/png;base64,{b64}')
        assert out and out['kind'] == 'image' and out['mime_type'] == 'image/png'

    def test_missing_api_image_returns_none(self, uploads_dir):
        from lib.attachments import resolve_attachment
        assert resolve_attachment('/api/images/nope.png') is None

    def test_devnull_ref_is_not_an_attachment(self):
        # /dev/null is NOT an attachment ref — the model should never pass it,
        # and is_attachment_ref must not claim it.
        from lib.attachments import is_attachment_ref
        assert is_attachment_ref('/dev/null') is False
        assert is_attachment_ref('/api/images/x.png') is True


# ═══════════════════════════════════════════════════════════════════════
#  Reverse-proxy prefix tolerance (the mrvn167fid7kdw crop bug)
#  Uploaded URLs get a '/proxy/<port>/' base path baked in under a VS Code /
#  code-server reverse proxy. The stored URL is '/proxy/15002/api/images/<f>',
#  which a bare startswith('/api/images/') missed → no ref hint emitted → the
#  model fabricated a bogus path → File not found.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProxyPrefixTolerance:

    _PROXIED = '/proxy/15002/api/images/1784697853816_4f7e49b5.jpg'

    def test_canonical_image_ref_strips_proxy_prefix(self):
        from lib.attachments import canonical_image_ref
        assert canonical_image_ref(self._PROXIED) == '/api/images/1784697853816_4f7e49b5.jpg'
        # Bare canonical URL is returned unchanged.
        assert canonical_image_ref('/api/images/a.png') == '/api/images/a.png'
        # Non-image refs → '' (falsy recognition predicate).
        assert canonical_image_ref('/dev/null') == ''
        assert canonical_image_ref('') == ''
        assert canonical_image_ref(None) == ''

    def test_is_attachment_ref_accepts_proxied(self):
        from lib.attachments import is_attachment_ref
        assert is_attachment_ref(self._PROXIED) is True

    def test_resolve_proxied_reads_disk(self, uploads_dir):
        from lib.attachments import resolve_attachment
        _write_image(str(uploads_dir / '1784697853816_4f7e49b5.jpg'), w=120, h=90)
        out = resolve_attachment(self._PROXIED)
        assert out and out['kind'] == 'image'
        assert out['mime_type'] == 'image/jpeg'

    def test_inspect_image_on_proxied_ref_succeeds(self, uploads_dir):
        # End-to-end: the reported bug. A proxy-prefixed uploaded ref must
        # crop the original, NOT return 'File not found'.
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / '1784697853816_4f7e49b5.jpg'), w=1000, h=800)
        out = tool_inspect_image('/proj/base', self._PROXIED,
                                 crop=[0.1, 0.5, 0.5, 0.9])
        img, _ = _decode_view(out)
        assert out['sourceSize'] == [1000, 800]

    def test_NEUTER_startswith_regresses_proxied(self, uploads_dir, monkeypatch):
        # NEUTER: reverting is_attachment_ref to the old startswith() check
        # makes a proxied ref UNrecognised → falls through to path resolution
        # and fails. Proves the .find() marker tolerance is load-bearing.
        import lib.attachments as att
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / '1784697853816_4f7e49b5.jpg'), w=200, h=200)
        monkeypatch.setattr(
            att, 'is_attachment_ref',
            lambda ref: bool(ref) and isinstance(ref, str) and (
                ref.startswith(att._DIRECT_IMAGE_PREFIXES) or ref.startswith(att._TEXT_REF_PREFIX)))
        out = tool_inspect_image('/proj/base', self._PROXIED)
        assert isinstance(out, str) and out.startswith('Error:')


# ═══════════════════════════════════════════════════════════════════════
#  resolve_attachment — text (PDF/TXT/doc) refs
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveTextRef:

    def test_pdf_text_readback(self):
        from lib.attachments import attachment_text_ref, resolve_attachment
        pdf = {'name': 'report.pdf', 'text': 'Hematology dept ranking body...', 'pages': 3}
        ref = attachment_text_ref(pdf)
        assert ref.startswith('att_txt_')
        messages = [{'role': 'user', 'content': 'see attached', 'pdfTexts': [pdf]}]
        out = resolve_attachment(ref, messages=messages)
        assert out and out['kind'] == 'text'
        assert out['text'] == 'Hematology dept ranking body...'
        assert out['name'] == 'report.pdf'

    def test_txt_ref_is_stable_across_calls(self):
        from lib.attachments import attachment_text_ref
        doc = {'name': 'notes.txt', 'text': 'line1\nline2'}
        assert attachment_text_ref(doc) == attachment_text_ref(dict(doc))

    def test_text_ref_not_found(self):
        from lib.attachments import resolve_attachment
        out = resolve_attachment('att_txt_deadbeef0000', messages=[{'role': 'user', 'pdfTexts': []}])
        assert out is None

    def test_text_ref_without_messages(self):
        from lib.attachments import resolve_attachment
        assert resolve_attachment('att_txt_deadbeef0000') is None


# ═══════════════════════════════════════════════════════════════════════
#  inspect_image on an uploaded /api/images/ ref (the reported bug)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInspectImageOnUploadedRef:

    def test_ref_crop_success(self, uploads_dir):
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / 'skin.png'), w=1000, h=800)
        out = tool_inspect_image('/proj/base', '/api/images/skin.png',
                                 crop=[0.5, 0.0, 1.0, 0.5])
        img, _ = _decode_view(out)
        assert img.size == (500, 400)
        assert out['sourceSize'] == [1000, 800]

    def test_ref_zoom_success(self, uploads_dir):
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / 'z.png'), w=400, h=400)
        out = tool_inspect_image('/proj/base', '/api/images/z.png', zoom=2.0)
        img, _ = _decode_view(out)
        assert img.size == (200, 200)

    def test_uploaded_ref_never_file_not_found(self, uploads_dir):
        # The ORIGINAL bug: model called inspect_image on an uploaded image
        # and got "File not found: /dev/null". With a real /api/images ref it
        # must succeed, NOT return a File-not-found error string.
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / 'ok.png'), w=200, h=200)
        out = tool_inspect_image('/proj/base', '/api/images/ok.png')
        assert isinstance(out, dict) and out.get('__screenshot__') is True

    def test_bogus_devnull_still_fails(self):
        # Control: a bogus /dev/null (not an attachment ref) still errors —
        # we did not blanket-accept everything.
        from lib.project_mod import tool_inspect_image
        out = tool_inspect_image('/proj/base', '/dev/null')
        assert isinstance(out, str) and out.startswith('Error:')

    def test_text_ref_rejected_by_inspect_image(self):
        from lib.attachments import attachment_text_ref
        from lib.project_mod import tool_inspect_image
        pdf = {'name': 'a.pdf', 'text': 'hi'}
        ref = attachment_text_ref(pdf)
        messages = [{'role': 'user', 'pdfTexts': [pdf]}]
        out = tool_inspect_image('/proj/base', ref, messages=messages)
        assert isinstance(out, str) and out.startswith('Error:')
        assert 'text attachment' in out

    def test_missing_ref_surfaces_clear_error(self, uploads_dir):
        from lib.project_mod import tool_inspect_image
        out = tool_inspect_image('/proj/base', '/api/images/gone.png')
        assert isinstance(out, str) and out.startswith('Error:')
        assert 'resolve attachment reference' in out

    def test_NEUTER_disabling_ref_recognition_breaks_it(self, uploads_dir, monkeypatch):
        # NEUTER control: if tool_inspect_image no longer recognises the
        # attachment ref, an /api/images/ path falls through to filesystem
        # resolution and FAILS — proving the ref seam is load-bearing.
        import lib.attachments as att
        from lib.project_mod import tool_inspect_image
        _write_image(str(uploads_dir / 'n.png'), w=200, h=200)
        monkeypatch.setattr(att, 'is_attachment_ref', lambda ref: False)
        out = tool_inspect_image('/proj/base', '/api/images/n.png')
        assert isinstance(out, str) and out.startswith('Error:')


# ═══════════════════════════════════════════════════════════════════════
#  execute_tool dispatch — the real call path (task.messages plumbed in)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDispatchWiring:

    def test_execute_tool_image_ref(self, uploads_dir):
        from lib.project_mod import execute_tool
        _write_image(str(uploads_dir / 'e.png'), w=600, h=400)
        out = execute_tool('inspect_image', {'path': '/api/images/e.png', 'crop': [0, 0, 0.5, 1.0]},
                           None, conv_id='c1', task_id='t1', task={'messages': []})
        img, _ = _decode_view(out)
        assert img.size == (300, 400)


# ═══════════════════════════════════════════════════════════════════════
#  Message builder emits the stable refs the model needs
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMessageBuilderRefs:

    def test_image_ref_emitted_in_user_message(self):
        from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
        msg = {'role': 'user', 'content': 'look',
               'images': [{'url': '/api/images/abc.png', 'mediaType': 'image/png'}]}
        built = _build_user_message(msg)
        blocks = built['content']
        assert any(b.get('type') == 'image_url' for b in blocks)
        ref_texts = [b['text'] for b in blocks if b.get('type') == 'text']
        assert any('/api/images/abc.png' in t and 'inspect_image' in t for t in ref_texts)

    def test_pdf_ref_emitted_in_user_message(self):
        from lib.attachments import attachment_text_ref
        from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
        pdf = {'name': 'r.pdf', 'text': 'body text here', 'pages': 2}
        msg = {'role': 'user', 'content': 'summarize', 'pdfTexts': [pdf]}
        built = _build_user_message(msg)
        # No images → content is a plain string
        assert isinstance(built['content'], str)
        assert attachment_text_ref(pdf) in built['content']
        assert 'attachment ref:' in built['content']

    def test_NEUTER_no_ref_when_ref_helper_stubbed_empty(self, monkeypatch):
        # Control: the image ref line is only emitted for /api/images/ urls.
        # A base64-only image (no disk url) must NOT emit a bogus ref.
        from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
        b64 = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 32).decode('ascii')
        msg = {'role': 'user', 'content': 'x',
               'images': [{'base64': b64, 'mediaType': 'image/png'}]}
        built = _build_user_message(msg)
        ref_texts = [b.get('text', '') for b in built['content'] if b.get('type') == 'text']
        assert not any('image ref:' in t for t in ref_texts)

    def test_proxied_url_emits_canonical_ref(self):
        # The crop bug: a proxy-prefixed uploaded URL must STILL emit the
        # inspect_image ref hint, canonicalized to the bare /api/images/ tail
        # (so the model passes a resolvable path, not a fabricated one).
        from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
        msg = {'role': 'user', 'content': 'look',
               'images': [{'url': '/proxy/15002/api/images/abc.png',
                           'mediaType': 'image/png'}]}
        built = _build_user_message(msg)
        ref_texts = [b['text'] for b in built['content'] if b.get('type') == 'text']
        # Emits a ref, canonicalized (no /proxy/ prefix leaked into the hint).
        assert any('path="/api/images/abc.png"' in t and 'inspect_image' in t
                   for t in ref_texts)
        assert not any('/proxy/' in t for t in ref_texts)


# ═══════════════════════════════════════════════════════════════════════
#  inspect_image collapsed-header display label — no 'uploaded]' junk
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInspectImageDisplayLabel:

    def test_bracketed_ref_arg_shows_real_filename(self):
        # The model sometimes passes the whole hint string verbatim as `path`.
        # The label must show the real uploaded filename, NOT basename junk
        # like 'uploaded]' from the reported bug.
        from lib.tasks_pkg.tool_display._renderers import _tool_display_inspect_image
        disp, extra = _tool_display_inspect_image(
            'inspect_image',
            {'path': '[image ref: /api/images/uploaded]', 'crop': [0.1, 0.5, 0.5, 0.9]},
            'tc1', '')
        assert extra['toolName'] == 'inspect_image'
        assert 'uploaded]' not in disp
        assert disp.startswith('uploaded')  # real filename
        assert 'crop' in disp

    def test_proxied_path_arg_shows_real_filename(self):
        from lib.tasks_pkg.tool_display._renderers import _tool_display_inspect_image
        disp, _ = _tool_display_inspect_image(
            'inspect_image',
            {'path': '/proxy/15002/api/images/shot_1234.jpg', 'zoom': 2},
            'tc1', '')
        assert disp.startswith('shot_1234.jpg')

    def test_plain_filesystem_path_unchanged(self):
        # Control: an ordinary local image path still shows its basename.
        from lib.tasks_pkg.tool_display._renderers import _tool_display_inspect_image
        disp, _ = _tool_display_inspect_image(
            'inspect_image', {'path': 'diagrams/schema.png', 'crop': [0, 0, 1, 1]}, 'tc1', '')
        assert disp.startswith('schema.png')
