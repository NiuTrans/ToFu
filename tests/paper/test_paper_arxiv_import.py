"""arXiv e-print source import: download → extract → discover → select."""
from __future__ import annotations

import io
import tarfile

import pytest

from lib.paper.builds import imports
from lib.paper.builds.errors import BuildError

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit


def _eprint_tarball(path):
    with tarfile.open(path, 'w:gz') as bundle:
        for name, text in [('main.tex', '\\documentclass{article}\n'
                            '\\begin{document}\nHi.\n\\end{document}\n'),
                           ('refs.bib', '@article{x,title={T}}')]:
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    monkeypatch.setattr(imports, 'owned_settings',
                        lambda conv, owner: {'projectPath': str(root)})
    import lib.paper.builds.documents as documents
    monkeypatch.setattr(documents, 'owned_settings',
                        lambda conv, owner: {'projectPath': str(root)})
    import lib.paper.builds.overleaf_capabilities as capabilities
    monkeypatch.setattr(capabilities, 'discover_sources',
                        lambda settings, owner_user_id: ([], []))
    return root


def test_import_extracts_discovers_and_selects(workspace, monkeypatch):
    def fake_fetch(arxiv_id, destination, **kw):
        assert arxiv_id == '2301.12345'
        _eprint_tarball(destination)
        return destination

    monkeypatch.setattr(imports, 'fetch_eprint', fake_fetch)
    selected = {}

    def fake_select(payload, *, owner_user_id):
        selected['payload'] = payload
        return {'selected': {'source': 'local', 'path': payload['path'],
                             'project_root': payload['project_root'],
                             'main_file': payload['main_file'],
                             'label': payload['path'], 'document_id': 'd' * 64}}

    monkeypatch.setattr(imports, 'select_document', fake_select)
    result = imports.import_arxiv_source(
        {'conv_id': 'conv-1', 'arxiv': '2301.12345'}, owner_user_id=7)
    assert result['files'] == 2
    assert result['directory'].startswith('arxiv-2301.12345')
    assert result['candidates'] == ['arxiv-2301.12345/main.tex']
    assert selected['payload']['path'] == 'arxiv-2301.12345/main.tex'
    assert result['document']['document_id'] == 'd' * 64
    tree = workspace / result['directory']
    assert (tree / 'main.tex').is_file() and (tree / 'refs.bib').is_file()


def test_import_rejects_non_arxiv_input(workspace):
    with pytest.raises(BuildError) as excinfo:
        imports.import_arxiv_source({'conv_id': 'c', 'arxiv': 'not-an-id'},
                                    owner_user_id=7)
    assert excinfo.value.code == 'arxiv_unavailable'


def test_import_requires_local_root(monkeypatch):
    monkeypatch.setattr(imports, 'owned_settings', lambda conv, owner: {})
    with pytest.raises(BuildError) as excinfo:
        imports.import_arxiv_source({'conv_id': 'c', 'arxiv': '2301.12345'},
                                    owner_user_id=7)
    assert excinfo.value.code == 'project_root'


def test_download_failure_is_typed_and_leaves_no_tree(workspace, monkeypatch):
    def boom(arxiv_id, destination, **kw):
        raise BuildError('arxiv_unavailable', 'arXiv returned HTTP 404')

    monkeypatch.setattr(imports, 'fetch_eprint', boom)
    with pytest.raises(BuildError) as excinfo:
        imports.import_arxiv_source({'conv_id': 'c', 'arxiv': '2301.12345'},
                                    owner_user_id=7)
    assert excinfo.value.code == 'arxiv_unavailable'
    assert list(workspace.iterdir()) == []


def test_readonly_root_is_refused(workspace, monkeypatch):
    import lib.project_mod.write_tools as write_tools
    monkeypatch.setattr(write_tools, '_enforce_not_readonly',
                        lambda *a, **kw: (_ for _ in ()).throw(
                            ValueError('root is read-only')))
    with pytest.raises(BuildError) as excinfo:
        imports.import_arxiv_source({'conv_id': 'c', 'arxiv': '2301.12345'},
                                    owner_user_id=7)
    assert excinfo.value.code == 'project_root'
