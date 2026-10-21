"""Actual Rust HTML projection through owned native durable upload and reindex.

Current optimized Python is the reference. All stores, files and release loans
belong to this check. No application defaults, live data or globals are changed.
This is correctness/lifetime evidence, not performance or cancellation admission.
"""
import argparse
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def format_inputs():
    from benchmarks.knowledge_html.measure import inputs
    for index, case in enumerate(('article', 'table', 'batch')):
        for ordinal, (raw, name) in enumerate(inputs(case, index)):
            yield raw + f'<!--native document {ordinal}-->'.encode(), name
    raw = b'<h1>Nested evidence</h1><p>exact &amp; visible</p>'
    book = io.BytesIO()
    with zipfile.ZipFile(book, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('chapter.xhtml', raw)
    yield book.getvalue(), 'book.epub'
    email = EmailMessage()
    email['Subject'] = 'Nested evidence'
    email.set_content(raw.decode(), subtype='html')
    email.add_attachment(b'<h2>Attachment evidence</h2>', maintype='text', subtype='html', filename='child.html')
    # A fixed MIME boundary keeps both arms' original bytes identical.
    email.set_boundary('tofu-native-html-check-boundary')
    yield email.as_bytes(), 'message.eml'


def check(engine, archive=None, *, binding=None):
    from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection
    from lib.agent_core.native_artifact import inspect_archive
    from lib.knowledge import ingest, store
    from lib.knowledge.ingest import KnowledgeIngestError
    from lib.knowledge.native_html import NativeHtmlProjectionError
    from lib.storage.errors import StorageError
    owner = 900071
    if binding is None:
        manifest, _ = inspect_archive(archive)
        pin = hashlib.sha256(archive.read_bytes()).hexdigest()
        binding = dict(kind='archive', path=str(archive), sha256=pin,
            application_version=manifest['build']['application_version'],
            bytes=manifest['files']['lib/libtofu_runtime.so']['bytes'])
    from benchmarks.knowledge_html.native_ingest_worker import create_binding
    inputs = list(format_inputs())
    originals = (ingest.extract, ingest._html_to_text, store.add_document, store.reindex_document,
                 store._SOURCE_ROOT_OVERRIDE, store._ASSET_ROOT_OVERRIDE)
    traces, rows = {}, []
    for arm in ('python', 'native'):
        with create_binding(binding) as native:
            for name, digest in native.profile['native_sources'].items():
                assert hashlib.sha256((ROOT / 'packages/tofu-runtime/src' / name).read_bytes()).hexdigest() == digest
            calls = []
            def project(value):
                result = native.project(value) if arm == 'native' else ingest._html_to_text(value)
                calls.append(hashlib.sha256(value.encode()).hexdigest())
                return result
            with tempfile.TemporaryDirectory(prefix='tofu-native-html-ingest-check-') as directory:
                corpus = DurableHtmlCorpus(Path(directory), engine, variant='candidate',
                                           owner_user_id=owner, html_projector=project)
                try:
                    commands = []
                    command = corpus.client.command
                    def observe(operation, payload, command_id, **kwargs):
                        result = command(operation, payload, command_id, **kwargs)
                        commands.append((operation, payload, command_id, result))
                        return result
                    corpus.client.command = observe
                    pages, trace = [], []
                    for raw, name in inputs:
                        count = len(calls)
                        result = corpus.add_document(raw, name, user_id=owner)
                        assert not result['duplicate'] and len(calls) > count
                        count = len(calls)
                        duplicate = corpus.add_document(raw, name, user_id=owner)
                        assert duplicate['duplicate'] and duplicate['id'] == result['id'] and len(calls) == count
                        page = corpus.get_document_content(result['id'], user_id=owner, limit=200)
                        original = semantic_projection(page)
                        corpus.reindex_document(result['id'], user_id=owner)
                        assert len(calls) > count
                        page = corpus.get_document_content(result['id'], user_id=owner, limit=200)
                        assert semantic_projection(page) == original
                        source = list((corpus.root/'sources'/str(owner)/'sources').glob(hashlib.sha256(raw).hexdigest()+'*'))
                        assert len(source) == 1 and source[0].read_bytes() == raw
                        trace.append(semantic_projection(page))
                        pages.append((result['id'], page))
                    operation, payload, command_id, receipt = commands[0]
                    assert command(operation, payload, command_id) == receipt
                    corpus.client.command = command
                    try:
                        foreign = corpus.repository(owner+1).document(pages[0][0])
                    except StorageError as error:
                        assert error.code == 'database_forbidden' and not error.retryable
                    else: assert foreign is None
                    def files():
                        return {str(p.relative_to(corpus.root)):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (corpus.root/'sources').rglob('*') if p.is_file()}
                    before = files()
                    repository = corpus.repository(owner)
                    documents_before = repository.documents()
                    for empty in (b'', b'<script>hidden</script>'):
                        try: corpus.add_document(empty, 'empty.html', user_id=owner)
                        except KnowledgeIngestError: pass
                        else: raise AssertionError('empty evidence accepted')
                    def refused(value): return native.project(value, projection_budget=0)
                    try:
                        corpus.add_document(b'<p>new evidence</p>', 'refused.html',
                                            user_id=owner, html_projector=refused)
                    except NativeHtmlProjectionError as error:
                        assert 'PROJECTION_BUDGET' in str(error)
                    else: raise AssertionError('projection refusal accepted')
                    namespace = corpus.add_document.func.__globals__
                    prior_repository = namespace['_repository']
                    class RejectCommit:
                        def __getattr__(self, name): return getattr(repository, name)
                        def create_document(self, *args, **kwargs): raise RuntimeError('injected commit refusal')
                    namespace['_repository'] = lambda _: RejectCommit()
                    try:
                        try: corpus.add_document(b'<p>rollback original</p>', 'rollback.html', user_id=owner)
                        except RuntimeError as error: assert str(error) == 'injected commit refusal'
                        else: raise AssertionError('failed commit accepted')
                    finally: namespace['_repository'] = prior_repository
                    assert files() == before and repository.documents() == documents_before
                    native.close()
                    try: corpus.reindex_document(pages[0][0], user_id=owner, html_projector=native.project)
                    except NativeHtmlProjectionError as error: assert 'closed' in str(error)
                    else: raise AssertionError('closed projector accepted')
                    for identity, page in pages:
                        assert corpus.get_document_content(identity, user_id=owner, limit=200) == page
                    corpus.close(); corpus.open()
                    for identity, page in pages:
                        assert corpus.get_document_content(identity, user_id=owner, limit=200) == page
                    assert files() == before
                    traces[arm] = trace
                    rows.append(dict(arm=arm, documents=len(pages), projection_calls=len(calls),
                                     trace_sha256=hashlib.sha256(json.dumps(trace,sort_keys=True).encode()).hexdigest(),
                                     native_build_identity=corpus.build_identity,
                                     native_library_sha256=corpus.library_sha256,
                                     html_profile=native.profile))
                finally: corpus.close()
                subprocess.run([str(engine), 'verify', '--data-dir', str(corpus.data_dir)],
                               check=True, capture_output=True, timeout=120)
    assert traces['python'] == traces['native']
    assert originals == (ingest.extract, ingest._html_to_text, store.add_document, store.reindex_document,
                         store._SOURCE_ROOT_OVERRIDE, store._ASSET_ROOT_OVERRIDE)
    return dict(scope=__doc__,artifact=binding, rows=rows, passed=True,
                performance_admitted=False, default_selection_admitted=False)


def check_workloads(config):
    """Verify this collector's exact fixtures and joined owners before any clocks."""
    from benchmarks.knowledge_html.native_ingest_worker import Workload
    from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
    projection = check(Path(config['engine']), binding=config['binding'])
    cases = {}
    pdf = Path(config['pdf']).read_bytes()
    for case in config['cases']:
        rows = {}
        for arm in ('reference', 'candidate'):
            with tempfile.TemporaryDirectory(prefix='tofu-native-html-case-check-') as directory:
                work = Workload(directory, config['engine'], arm, case, pdf,
                                config['binding'], config['python_command'])
                try:
                    pages = work.operation(-1)
                    output = work.verify(-1, pages)
                    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished'] == 0
                    if work.corpus is not None:
                        work.corpus.close(); work.corpus.open()
                        for page in pages:
                            assert work.corpus.get_document_content(
                                page['document']['id'], user_id=900071, limit=200) == page
                finally:
                    work.close()
                assert work.closed
                rows[arm] = dict(output_sha256=output, joined_cleanup=work.closed,
                    projection_calls=work.calls, html_profile=work.profile,
                    db_identity=work.db_identity, producers=work.producer_receipts)
                expected = arm == 'candidate' and case != 'pdf'
                assert bool(work.profile) == expected and (work.calls > 0) == expected
        assert rows['reference']['output_sha256'] == rows['candidate']['output_sha256']
        cases[case] = dict(rows=rows, correctness_passed=True,
            failure_and_cleanup_passed=True,
            cancellation_scope='post-commit receipt interruption and recovery; no HTTP/pre-commit cancellation' if case == 'cancellation' else None)
    return dict(projection=projection, cases=cases, passed=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.engine.resolve(), args.archive.resolve())
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, sort_keys=True))
