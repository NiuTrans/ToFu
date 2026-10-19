"""PDF discovery/text extraction load Pillow only when image work is needed."""
import asyncio
import base64
import builtins
import io
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('operation', ['package', 'text_pdf'])
def test_non_image_work_does_not_import_pillow_in_a_fresh_process(operation):
    program = textwrap.dedent('''
        import sys
        assert 'PIL.Image' not in sys.modules
        if sys.argv[1] == 'package':
            from lib.pdf_parser.images import detect_and_clip_figures, resize_image_bytes
            assert callable(detect_and_clip_figures) and callable(resize_image_bytes)
        else:
            import pymupdf
            from lib.knowledge.ingest import extract
            from lib.knowledge.asset_spool import close_extracted_assets
            document = pymupdf.open()
            try:
                page = document.new_page()
                page.insert_textbox(pymupdf.Rect(50, 50, 500, 500),
                    'Plain searchable evidence with no visual content. ' * 12)
                raw = document.tobytes()
            finally:
                document.close()
            parsed = extract(raw, 'text-only.pdf')
            try:
                assert 'Plain searchable evidence' in parsed['text']
                assert not parsed['assets']
            finally:
                close_extracted_assets(parsed)
        assert 'PIL.Image' not in sys.modules, 'non-image work loaded the image codec'
    ''')
    result = subprocess.run([sys.executable, '-c', program, operation],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-3000:]


def refuse_pillow(monkeypatch, failure):
    ordinary = builtins.__import__

    def load(name, *arguments, **options):
        if name == 'PIL' or name.startswith('PIL.'):
            raise failure
        return ordinary(name, *arguments, **options)

    monkeypatch.setattr(builtins, '__import__', load)


@pytest.mark.parametrize('raw,media_type', [
    (b'\xff\xd8original', 'image/jpeg'),
    (b'\x89PNGoriginal', 'image/png'),
    (b'RIFForiginal', 'image/webp'),
])
def test_unavailable_codec_preserves_raw_image_fallback(monkeypatch, raw, media_type):
    from lib.pdf_parser.images import resize_image_bytes

    refuse_pillow(monkeypatch, ImportError('optional codec unavailable'))
    encoded, kind, width, height = resize_image_bytes(raw)
    assert base64.b64decode(encoded) == raw
    assert (kind, width, height) == (media_type, None, None)


@pytest.mark.parametrize('kind', [KeyboardInterrupt, asyncio.CancelledError])
def test_codec_import_control_failure_propagates(monkeypatch, kind):
    from lib.pdf_parser.images import resize_image_bytes

    failure = kind('codec import interrupted')
    refuse_pillow(monkeypatch, failure)
    with pytest.raises(kind) as caught:
        resize_image_bytes(b'\x89PNGoriginal')
    assert caught.value is failure


def test_real_image_resizing_still_loads_and_uses_the_codec():
    from PIL import Image
    from lib.pdf_parser.images import resize_image_bytes

    with Image.new('RGBA', (64, 32), (20, 60, 100, 128)) as original:
        with io.BytesIO() as source:
            original.save(source, format='PNG')
            encoded, kind, width, height = resize_image_bytes(source.getvalue(), max_width=32)
    assert (kind, width, height) == ('image/jpeg', 32, 16)
    with io.BytesIO(base64.b64decode(encoded)) as result:
        with Image.open(result) as image:
            assert image.size == (32, 16) and image.mode == 'RGB'
