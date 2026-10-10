"""Corpus evidence survives old quotas without retaining every image in RAM."""
import base64
import hashlib
import io
import sys
import time
import tracemalloc
import weakref
import zipfile
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _asset(raw, **values):
    return dict(raw=raw, size_bytes=len(raw), mime_type='image/png', suffix='.png',
                sha256=hashlib.sha256(raw).hexdigest(), **values)


def test_package_keeps_2049_assets_above_legacy_count_and_byte_quota(monkeypatch):
    from lib.knowledge import assets
    from lib.knowledge.asset_spool import OwnedAssetSequence
    monkeypatch.setenv('TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS', '1')
    monkeypatch.setenv('TOFU_KNOWLEDGE_MAX_VISUAL_BYTES', '1048576')
    monkeypatch.setattr(assets, '_asset_from_raw',
        lambda raw, _budget=None, **values: _asset(raw, **values))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as writer:
        for index in range(2049):
            writer.writestr(f'word/media/image{index}.png',
                            index.to_bytes(4, 'big') + b'x' * 1024)
    archive_raw = archive.getvalue()
    tracemalloc.start()
    started = time.monotonic()
    result, warnings = assets.extract_package_assets(archive_raw)
    elapsed = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f'2049 file-backed visuals: {elapsed:.6f}s, peak tracked bytes={peak}')
    try:
        assert isinstance(result, OwnedAssetSequence)
        assert len(result) == 2049 and warnings == []
        assert result[-1]['raw'][:4] == (2048).to_bytes(4, 'big')
        assert sum(asset['size_bytes'] for asset in result) > 1048576
    finally:
        stream = result._stream
        result.close()
    assert stream.closed


def test_spool_releases_inputs_and_updates_metadata_without_duplicating_raw():
    from lib.knowledge.asset_spool import OwnedAssetSequence
    class Payload(bytearray):
        pass
    payload = Payload(b'retained evidence')
    reference = weakref.ref(payload)
    result = OwnedAssetSequence()
    try:
        result.append(_asset(payload, caption='first'))
        del payload
        assert reference() is None
        raw_location = result._entries[0][2:]
        result.update_metadata(0, caption='updated', ocr_text='recognized')
        assert result._entries[0][2:] == raw_location
        assert result[0]['raw'] == b'retained evidence'
        assert result[0]['caption'] == 'updated'
        assert result[0]['ocr_text'] == 'recognized'
    finally:
        result.close()


def test_pdf_visuals_preserve_every_candidate_beyond_old_asset_count(monkeypatch):
    from lib.knowledge import assets
    from lib.pdf_parser import images as pdf_images
    closed = []
    class Page:
        def get_text(self, mode): return 'page text ' * 20
        def get_images(self, **kwargs): return []
    class Document:
        page_count = 1
        def __getitem__(self, index): return Page()
        def close(self): closed.append('document')
    monkeypatch.setitem(sys.modules, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    monkeypatch.setattr(pdf_images, 'detect_and_clip_figures',
        lambda *args, **kwargs: [
            {'base64': base64.b64encode(i.to_bytes(4, 'big') + b'x' * 1024).decode(),
             'caption': f'figure {i}', 'page': 1}
            for i in range(401)])
    monkeypatch.setattr(assets, '_asset_from_raw',
        lambda raw, _budget=None, **values: _asset(raw, **values))
    monkeypatch.setenv('TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS', '1')
    result, warnings = assets._extract_pdf_assets_without_admission(b'synthetic')
    try:
        assert len(result) == 401 and warnings == []
        assert result[-1]['caption'] == 'figure 400'
        assert result[-1]['raw'][:4] == (400).to_bytes(4, 'big')
        assert closed == ['document']
    finally:
        result.close()


def test_spool_io_failure_aborts_extraction_instead_of_silently_skipping(monkeypatch):
    from lib.knowledge import asset_spool, assets
    observed = []
    class FailFile:
        def seek(self, *args): return 0
        def tell(self): return 0
        def write(self, value): raise OSError('injected spool disk failure')
        def close(self): observed.append('closed')
    monkeypatch.setattr(asset_spool.tempfile, 'TemporaryFile', lambda **kwargs: FailFile())
    monkeypatch.setattr(assets, '_asset_from_raw',
        lambda raw, _budget=None, **values: _asset(raw, **values))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as writer:
        writer.writestr('word/media/valid.png', b'synthetic image')
    with pytest.raises(asset_spool.AssetSpoolError) as caught:
        assets.extract_package_assets(archive.getvalue())
    assert isinstance(caught.value.__cause__, OSError)
    assert 'injected spool disk failure' in str(caught.value.__cause__)
    assert observed == ['closed']


def test_visual_store_closes_spool_and_rolls_back_files_on_later_failure(monkeypatch):
    from lib.knowledge import store
    from lib.knowledge.asset_spool import OwnedAssetSequence
    result = OwnedAssetSequence()
    for index in range(2): result.append(_asset(bytes([index]), caption='synthetic'))
    stream = result._stream
    paths, rollbacks = [], []
    monkeypatch.setattr(store, 'visual_enrichment_enabled', lambda **kwargs: False)
    def write(raw, name, **kwargs):
        if paths:
            raise OSError('injected second durable write failure')
        paths.append(name)
        return name
    monkeypatch.setattr(store, '_write_asset', write)
    monkeypatch.setattr(store, '_unlink_files', lambda paths, **kwargs: rollbacks.extend(paths))
    with pytest.raises(OSError, match='second durable write'):
        store._prepare_visual_index('source', {'assets': result}, 'doc', 0, [], user_id=41)
    assert stream.closed and rollbacks == paths
