"""Exact native HTML upload/read/reopen checks; never opens application data."""
import argparse
from pathlib import Path
import sys
import tempfile
import subprocess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.knowledge_html.durable import DurableHtmlCorpus, semantic_projection
from benchmarks.knowledge_html.measure import inputs
from lib.knowledge import ingest, store
from lib.knowledge.ingest import KnowledgeIngestError
from lib.storage.errors import StorageError


def check(engine):
    original = (ingest.extract, ingest._html_to_text, store.add_document,
                store._SOURCE_ROOT_OVERRIDE, store._ASSET_ROOT_OVERRIDE)
    traces = {}
    for variant in ('reference', 'candidate'):
        with tempfile.TemporaryDirectory(prefix='tofu-html-native-check-') as directory:
            corpus = DurableHtmlCorpus(Path(directory), engine, variant=variant)
            try:
                commands = []
                command = corpus.client.command
                def observe(operation, payload, command_id, **options):
                    result = command(operation, payload, command_id, **options)
                    commands.append((operation, payload, command_id, result))
                    return result
                corpus.client.command = observe
                trace = []
                persisted = []
                for index, case in enumerate(('article', 'table', 'batch')):
                    for raw, name in inputs(case, index):
                        result = corpus.add_document(raw, name, user_id=1)
                        page = corpus.get_document_content(result['id'], user_id=1, limit=200)
                        trace.append(semantic_projection(page))
                        persisted.append((result['id'], page))
                        duplicate = corpus.add_document(raw, name, user_id=1)
                        assert duplicate['duplicate'] is True
                        assert duplicate['id'] == result['id']
                operation, payload, command_id, receipt = commands[0]
                assert command(operation, payload, command_id) == receipt
                corpus.client.command = command
                try:
                    foreign = corpus.repository(2).document(persisted[0][0])
                except StorageError as error:
                    assert error.code == 'database_forbidden' and not error.retryable
                else:
                    assert foreign is None
                before = sorted((corpus.root / 'sources').rglob('*'))
                for raw in (b'', b'<script>hidden</script>'):
                    try:
                        corpus.add_document(raw, 'empty.html', user_id=1)
                    except KnowledgeIngestError:
                        pass
                    else:
                        raise AssertionError('empty evidence accepted')
                assert before == sorted((corpus.root / 'sources').rglob('*'))
                repository = corpus.repository(1)
                documents_before = repository.documents()
                class RejectCommit:
                    def __getattr__(self, name):
                        return getattr(repository, name)
                    def create_document(self, *args, **kwargs):
                        raise RuntimeError('injected commit rejection')
                namespace = corpus.add_document.__globals__
                original_repository = namespace['_repository']
                namespace['_repository'] = lambda _: RejectCommit()
                try:
                    try:
                        corpus.add_document(b'<p>unique rollback evidence</p>', 'fault.html', user_id=1)
                    except RuntimeError as error:
                        assert str(error) == 'injected commit rejection'
                    else:
                        raise AssertionError('failed commit accepted')
                finally:
                    namespace['_repository'] = original_repository
                assert before == sorted((corpus.root / 'sources').rglob('*'))
                assert repository.documents() == documents_before
                corpus.close()
                corpus.open()
                for document_id, expected in persisted:
                    assert corpus.get_document_content(document_id, user_id=1, limit=200) == expected
                traces[variant] = trace
            finally:
                corpus.close()
            subprocess.run([str(engine), 'verify', '--data-dir', str(corpus.data_dir)],
                           check=True, capture_output=True, timeout=120)
    assert traces['reference'] == traces['candidate']
    assert original == (ingest.extract, ingest._html_to_text, store.add_document,
                        store._SOURCE_ROOT_OVERRIDE, store._ASSET_ROOT_OVERRIDE)
    print('PASS: native uploads, exact content, receipt replay, duplicate reuse, owner isolation, rollback, reopen, integrity, globals intact')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    check(parser.parse_args().engine.resolve())
