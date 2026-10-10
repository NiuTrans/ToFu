"""Explicit source snapshots preserve every byte and reject path/size aliases."""
import threading

import pytest

from lib.paper.builds.errors import BuildError
from lib.paper.builds.records import SourceRecords
from lib.paper.builds.sources import snapshot_sources

pytestmark = pytest.mark.unit


def test_explicit_records_keep_metadata_and_own_immutable_bytes(tmp_path):
    files = [{'path': 'main.tex', 'content': r'\documentclass{article}'},
             {'path': 'notes/README.md', 'content': 'Exact manuscript notes.\n'}]
    records = SourceRecords.from_text(files)
    files[0]['content'] = 'Later edit'
    snapshot = records.freeze(tmp_path / 'snapshot')
    assert (snapshot.root / 'main.tex').read_text() == r'\documentclass{article}'
    assert (snapshot.root / 'notes/README.md').read_text() == 'Exact manuscript notes.\n'
    assert len(snapshot.files) == 2
    assert not (snapshot.root / 'main.tex').stat().st_mode & 0o222


def test_record_and_filesystem_sources_share_the_same_manifest_identity(tmp_path):
    records = SourceRecords.from_text([{'path': 'main.tex', 'content': r'\documentclass{article}'}])
    explicit = records.freeze(tmp_path / 'explicit')
    scanned = snapshot_sources(explicit.root, explicit.root / 'main.tex', tmp_path / 'scanned')
    assert explicit.digest == scanned.digest
    assert explicit.files == scanned.files


@pytest.mark.parametrize('path', ['../escape.tex', '/main.tex', 'a/../main.tex', './main.tex',
                                  'C:/main.tex', 'a\\main.tex', '.', 'NUL.tex', 'main.tex'])
def test_invalid_paths_and_duplicates_are_refused_before_materialization(path):
    with pytest.raises(BuildError) as caught:
        SourceRecords.from_text([{'path': 'main.tex', 'content': 'main'}, {'path': path, 'content': 'bad'}])
    assert caught.value.code == 'source_path'


def test_case_and_directory_aliases_are_refused():
    for paths in [('Main.tex', 'main.tex'), ('main.tex', 'main.tex/part.tex')]:
        with pytest.raises(BuildError, match='Source|source'):
            SourceRecords.from_text([{'path': p, 'content': ''} for p in paths])


def test_multibyte_budget_is_bytes_and_cancel_does_not_create_a_stage(tmp_path, monkeypatch):
    from lib.paper.builds import records as module
    prior = module.limit
    monkeypatch.setattr(module, 'limit', lambda key: 4 if key == 'source_file_bytes' else prior(key))
    with pytest.raises(BuildError) as caught:
        SourceRecords.from_text([{'path': 'main.tex', 'content': '论文字'}])
    assert caught.value.code == 'source_limit'
    records = SourceRecords.from_text([{'path': 'main.tex', 'content': 'ok'}])
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(BuildError) as caught:
        records.freeze(tmp_path / 'cancelled', cancel=cancelled)
    assert caught.value.code == 'cancelled'
    assert not (tmp_path / 'cancelled').exists()
