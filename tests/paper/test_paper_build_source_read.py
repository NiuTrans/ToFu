"""Owned paper source reads for the read-only code view."""
from __future__ import annotations

import pytest

from lib.paper.builds.document_repository import PaperDocumentRepository
from lib.paper.builds.errors import BuildError
from lib.paper.builds.source_read import read_document_source

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit


@pytest.fixture
def document(tmp_path):
    root = tmp_path / 'root'
    (root / 'paper/chapters').mkdir(parents=True)
    (root / 'paper/main.tex').write_text('\\documentclass{article}\nHi.\n')
    (root / 'paper/chapters/intro.tex').write_text('An introduction.\n')
    (root / 'paper/refs.bib').write_text('@article{x,title={T}}\n')
    (root / 'paper/figure.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    repo = PaperDocumentRepository(7)
    source = {'source': 'local', 'project_root': str(root),
              'path': 'paper/main.tex', 'main_file': 'paper/main.tex',
              'label': 'paper/main.tex'}
    registered = repo.register(source)
    return repo, registered['document_id']


def test_reads_sources_relative_to_the_snapshot_root(document):
    _repo, document_id = document
    main = read_document_source({'document_id': document_id, 'path': 'main.tex'},
                                owner_user_id=7)
    assert main['content'].startswith('\\documentclass')
    assert main['main_file'] == 'paper/main.tex'
    assert main['truncated'] is False
    nested = read_document_source({'document_id': document_id, 'path': 'chapters/intro.tex'},
                                  owner_user_id=7)
    assert nested['content'] == 'An introduction.\n'


def test_traversal_and_unknown_documents_are_refused(document):
    _repo, document_id = document
    with pytest.raises(BuildError):
        read_document_source({'document_id': document_id, 'path': '../outside.tex'},
                             owner_user_id=7)
    with pytest.raises(BuildError) as excinfo:
        read_document_source({'document_id': 'f' * 64, 'path': 'main.tex'},
                             owner_user_id=7)
    assert excinfo.value.code in {'document_not_found', 'database_not_found'}


def test_binary_and_foreign_suffixes_are_typed(document):
    _repo, document_id = document
    with pytest.raises(BuildError) as excinfo:
        read_document_source({'document_id': document_id, 'path': 'figure.png'},
                             owner_user_id=7)
    assert excinfo.value.code == 'source_binary'
    with pytest.raises(BuildError) as excinfo:
        read_document_source({'document_id': document_id, 'path': 'chapters/intro.py'},
                             owner_user_id=7)
    assert excinfo.value.code == 'source_path'


def test_owner_isolation(document):
    _repo, document_id = document
    with pytest.raises(BuildError):
        read_document_source({'document_id': document_id, 'path': 'main.tex'},
                             owner_user_id=8)
