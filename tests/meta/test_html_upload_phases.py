"""Actual upload diagnostics preserve private bindings and native ownership.

Native cases require TOFUDB_BIN and TOFU_NATIVE_HTML_TEST_INSTALLATION; their
authorities and source files are disposable and never use application storage.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from benchmarks.knowledge_html import upload_phases

pytestmark = pytest.mark.unit


@pytest.fixture
def engine():
    value = os.environ.get('TOFUDB_BIN')
    if not value:
        pytest.skip('explicit source-matched native engine required')
    return Path(value)


@pytest.mark.parametrize('variant', ['reference', 'candidate', 'native'])
def test_actual_upload_phases_keep_bound_parser_sources_and_reopen(engine, tmp_path, variant):
    from lib.knowledge import store
    from lib.knowledge.ingest import KnowledgeIngestError
    from lib.knowledge.native_html import NativeHtmlProjector

    original_globals = dict(vars(store))
    binding = None
    corpus = None
    calls = []
    if variant == 'native':
        installation = os.environ.get('TOFU_NATIVE_HTML_TEST_INSTALLATION')
        if not installation:
            pytest.skip('explicit pinned HTML installation required')
        receipt = json.loads(Path(installation).read_text())
        binding = NativeHtmlProjector.from_library(
            library=Path(receipt['html_library']), sha256=receipt['html_library_sha256'],
            binary_bytes=receipt['bytes'])

    def project(text):
        calls.append(text)
        return binding.project(text)

    try:
        corpus = upload_phases.ProfiledHtmlCorpus(
            tmp_path, engine, variant='reference' if variant == 'reference' else 'candidate',
            owner_user_id=900812, html_projector=project if binding else None)
        with pytest.raises(KnowledgeIngestError, match='Empty file'):
            corpus.add_document(b'', 'empty.html', user_id=900812)
        assert corpus.phase_samples == []
        raw = b'<h1>Observed upload</h1><p>exact &amp; durable evidence</p>'
        result = corpus.add_document(raw, 'fixture.html', user_id=900812)
        assert corpus.phase_samples == []  # A sample includes its content read.
        page = corpus.get_document_content(result['id'], user_id=900812, limit=200)
        assert not page['pagination']['has_more']
        assert 'exact & durable evidence' in '\n'.join(c['content'] for c in page['chunks'])
        assert len(corpus.phase_samples) == 1
        phases = corpus.phase_samples[0]
        assert set(phases) == {'digest_lookup', 'extract', 'chunk', 'search_index',
            'source_write_fsync', 'native_visual_settings', 'native_create',
            'native_content_read', 'upload_total', 'public_read_total'}
        assert all(value >= 0 for value in phases.values())
        sources = list((tmp_path / 'sources' / '900812' / 'sources').glob(
            hashlib.sha256(raw).hexdigest() + '*'))
        assert len(sources) == 1 and sources[0].read_bytes() == raw
        assert len(calls) == int(binding is not None)
        corpus.close()
        corpus.open()
        assert corpus.get_document_content(result['id'], user_id=900812, limit=200) == page
        assert corpus.phase_samples == [phases]
    finally:
        try:
            if corpus is not None:
                corpus.close()
        finally:
            if binding is not None:
                binding.close()
    assert all(vars(store).get(name) is value for name, value in original_globals.items())


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_instrumentation_releases_acquired_corpus(monkeypatch, cleanup_fails):
    opened, closed = [], []
    primary = RuntimeError('instrumentation unavailable')
    cleanup = OSError('close unavailable')

    def acquire(self):
        opened.append(self)

    def instrument(self):
        assert opened == [self]
        raise primary

    def close(self):
        closed.append(self)
        if cleanup_fails:
            raise cleanup

    monkeypatch.setattr(upload_phases.DurableHtmlCorpus, '__init__', acquire)
    monkeypatch.setattr(upload_phases.DurableHtmlCorpus, 'close', close)
    monkeypatch.setattr(upload_phases.ProfiledHtmlCorpus, '_instrument_upload', instrument)
    with pytest.raises(BaseExceptionGroup if cleanup_fails else RuntimeError) as caught:
        upload_phases.ProfiledHtmlCorpus()
    assert closed == opened and len(closed) == 1
    if cleanup_fails:
        assert caught.value.exceptions == (primary, cleanup)
    else:
        assert caught.value is primary
