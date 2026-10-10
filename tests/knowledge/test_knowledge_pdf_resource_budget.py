"""Knowledge PDF extraction retains caller output policy and owned lifetimes."""

from __future__ import annotations

import io
import sys
import types

import pytest


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


def _classic_environment(**overrides: str) -> dict[str, str]:
    environment = {
        'TOFU_PDF_PROCESSES': '1',
        'TOFU_PDF_PARSE_CAPACITY': '1',
        'TOFU_PDF_MAX_PAGES': '12',
        'TOFU_PDF_MAX_TEXT_MIB': '2',
        'TOFU_PDF_PARSE_TIMEOUT': '300',
        'TOFU_PDF_WORKER_IDLE_SECONDS': '0',
    }
    environment.update(overrides)
    return environment


def test_individual_visual_validation_remains_explicit():
    from lib.knowledge.resource_policy import resolve_knowledge_visual_budget

    budget = resolve_knowledge_visual_budget(_classic_environment(
        TOFU_KNOWLEDGE_VISUAL_MAX_PAGES='999999',
        TOFU_KNOWLEDGE_OCR_MAX_PAGES='999999',
        TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS='999999',
        TOFU_KNOWLEDGE_MAX_VISUAL_BYTES=str(999999 * _MIB),
        TOFU_KNOWLEDGE_MAX_ASSET_BYTES=str(999999 * _MIB),
        TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS='999999999',
    ))

    assert budget.max_asset_bytes == 100 * _MIB
    assert budget.max_image_pixels == 100_000_000


def test_knowledge_pdf_holds_one_lease_across_text_ocr_and_visuals(
    monkeypatch,
):
    from lib.knowledge import ingest
    from lib.pdf_parser import admission as admission_module
    from lib.pdf_parser import text as pdf_text
    from lib.pdf_parser.admission import _ParseAdmission

    admission = _ParseAdmission()
    monkeypatch.setattr(
        admission_module, 'CLASSIC_PDF_ADMISSION', admission)
    for name, value in _classic_environment().items():
        monkeypatch.setenv(name, value)

    observed = []

    def assert_admitted(stage: str) -> None:
        assert admission.snapshot()['unfinished'] == 1
        observed.append(stage)

    monkeypatch.setattr(
        pdf_text,
        'validate_pdf_bytes',
        lambda _raw: (assert_admitted('validate') or (True, 2, '')),
    )
    monkeypatch.setattr(
        pdf_text,
        '_extract_pdf_text_with_meta_without_admission',
        lambda *_args, **_kwargs: (
            assert_admitted('text') or ('', 'error')),
    )

    def fake_ocr(_raw, page_limit, max_chars):
        assert_admitted('ocr')
        assert page_limit is None
        assert max_chars >= 100_000
        return '## Page 1\n\nlocally recognized text', []

    monkeypatch.setattr(ingest, '_ocr_scanned_pdf', fake_ocr)

    def fake_visuals(_raw, *, _budget):
        assert_admitted('visuals')
        assert _budget.max_asset_bytes > 0
        return [], []

    monkeypatch.setattr(
        ingest, '_extract_pdf_assets_without_admission', fake_visuals)

    result = ingest.extract(b'%PDF-admission-test', 'bounded.pdf')

    assert result['method'] == 'pymupdf-ocr'
    assert observed == ['validate', 'text', 'ocr', 'visuals']
    assert admission.snapshot()['unfinished'] == 0





def test_scanned_pdf_ocr_stops_at_text_budget(monkeypatch):
    from lib.knowledge import ingest

    pages_requested = []

    class FakePage:
        def get_textpage_ocr(self, **_kwargs):
            return object()

        def get_text(self, *_args, **_kwargs):
            return 'recognized ' * 100

    class FakeDocument:
        page_count = 10

        def __getitem__(self, page_number):
            pages_requested.append(page_number)
            return FakePage()

        def close(self):
            return None

    fake_pymupdf = types.SimpleNamespace(
        open=lambda **_kwargs: FakeDocument())
    monkeypatch.setitem(sys.modules, 'pymupdf', fake_pymupdf)

    text, warnings = ingest._ocr_scanned_pdf(
        b'%PDF-ocr-budget-test', page_limit=5, max_chars=64)

    assert len(text) == 64
    assert pages_requested == [0]
    assert any('64-character text budget' in warning for warning in warnings)
    assert not any('OCR read 5' in warning for warning in warnings)





def test_document_pipeline_holds_pdf_lease_through_repository_commit(
    monkeypatch,
    tmp_path,
):
    from lib.knowledge import store
    from lib.pdf_parser import admission as admission_module
    from lib.pdf_parser.admission import _ParseAdmission

    admission = _ParseAdmission()
    monkeypatch.setattr(
        admission_module, 'CLASSIC_PDF_ADMISSION', admission)
    for name, value in _classic_environment().items():
        monkeypatch.setenv(name, value)
    observed = []

    def assert_admitted(stage):
        assert admission.snapshot()['unfinished'] == 1
        observed.append(stage)

    class FakeRepository:
        def document_by_digest(self, _digest):
            return None

        def create_document(self, _document, *, command_id):
            assert command_id
            assert_admitted('repository')
            raise RuntimeError('commit boundary sentinel')

    monkeypatch.setattr(store, '_repository', lambda _user_id: FakeRepository())

    def fake_extract(_raw, _name, *, _pdf_already_admitted, **_kwargs):
        assert _pdf_already_admitted is True
        assert_admitted('parse')
        return {
            'text': 'bounded searchable knowledge',
            'kind': '.pdf',
            'method': 'test-pdf',
            'warnings': [],
            'pages': 1,
            'assets': [],
        }

    monkeypatch.setattr(store, 'extract', fake_extract)

    def fake_prepare(
        _name, _parsed, _document_id, _now, chunks, **_kwargs,
    ):
        assert_admitted('visual-persist')
        return chunks, [], []

    monkeypatch.setattr(store, '_prepare_visual_index', fake_prepare)
    source_path = tmp_path / 'candidate.pdf'

    def fake_write(_raw, _stored_name, *, user_id):
        assert user_id == 1
        assert_admitted('source-persist')
        source_path.write_bytes(b'candidate')
        return source_path

    monkeypatch.setattr(store, '_write_source', fake_write)

    with pytest.raises(RuntimeError, match='commit boundary sentinel'):
        store.add_document(
            b'%PDF-full-lifecycle-test', 'lifecycle.pdf', user_id=1)

    assert observed == [
        'parse', 'visual-persist', 'source-persist', 'repository']
    assert admission.snapshot()['unfinished'] == 0
    assert not source_path.exists()



    # The v4 wire is a strict RFC 7807 problem document (the generated
    # Problem DTO forbids extension members), so the legacy top-level
    # ``retryable`` flag rides only on the v1 body; on v4 the retryable
    # signal is the 503 + Retry-After pair asserted above.



    # See the reindex case above: v4 keeps retryable as 503 + Retry-After.
