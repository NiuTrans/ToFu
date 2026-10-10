"""Conversation authority, document identities and host adapter source binding."""
from __future__ import annotations

import threading
import shutil
from pathlib import Path

import pytest

from lib.paper.builds import service
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as build_fixtures

pytestmark = pytest.mark.unit
scheduler = build_fixtures.scheduler


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'workspace'
    paper = root / 'paper'
    (paper / 'chapters').mkdir(parents=True)
    (paper / 'main.tex').write_text('\\documentclass{article}\n\\begin{document}\n'
                                  '\\input{chapters/intro}\n\\end{document}\n')
    (paper / 'chapters/intro.tex').write_text('A frozen paragraph.\n')
    return root


def _payload(**extra):
    return {'source': 'local', 'conv_id': 'conversation-a', 'path': 'paper/chapters/intro.tex',
            'request_id': 'edit-1', 'subscriber_id': 'viewer-a', **extra}


def test_binding_uses_owned_root_and_discriminates_owners(project):
    settings = {'projectPath': str(project)}
    first = service.bind_host_document(_payload(), owner_user_id=7, settings=settings)
    second = service.bind_host_document(_payload(), owner_user_id=8, settings=settings)
    assert first.project_root == project / 'paper'
    assert first.main_file == 'main.tex'
    assert first.edited_file == 'chapters/intro.tex'
    assert first.document_id != second.document_id
    assert service.bind_host_document(_payload(path=str(project / 'paper/chapters/intro.tex')),
                                      owner_user_id=7, settings=settings) == first


def test_binding_rejects_unowned_root_and_symlink_escape(project, tmp_path):
    outside = tmp_path / 'outside.tex'
    outside.write_text(r'\documentclass{article}')
    (project / 'alias.tex').symlink_to(outside)
    for path in (str(outside), 'alias.tex', '../outside.tex'):
        with pytest.raises(BuildError, match='outside this conversation'):
            service.bind_host_document(_payload(path=path), owner_user_id=7,
                                        settings={'projectPath': str(project)})
    with pytest.raises(BuildError, match='Choose a project directory'):
        service.bind_host_document(_payload(), owner_user_id=7, settings={})


def test_settings_lookup_carries_explicit_owner_and_absence_fails_closed(monkeypatch):
    from lib.conversations import repository
    calls = []
    def lookup(conversation_id, **kwargs):
        calls.append((conversation_id, kwargs))
        return None
    monkeypatch.setattr(repository, 'get_conversation', lookup)
    with pytest.raises(BuildError, match='unavailable'):
        service.owned_settings('conversation-a', 8)
    assert calls == [('conversation-a', {'user_id': 8, 'include_messages': False})]


def test_start_joins_revision_across_viewers_without_leaking_host_paths(project, scheduler, monkeypatch):
    monkeypatch.setattr(service, 'owned_settings', lambda conv, owner: {'projectPath': str(project)})
    started, release = threading.Event(), threading.Event()
    def execution(*_):
        def run(task, publish):
            started.set()
            assert release.wait(3)
            raise BuildError('cancelled', 'Fixture finished')
        return run
    monkeypatch.setattr(service, 'host_execution', execution)
    first = service.start_preview(_payload(), owner_user_id=7, scheduler=scheduler)
    assert started.wait(3)
    try:
        second = service.start_preview(_payload(subscriber_id='viewer-b'), owner_user_id=7, scheduler=scheduler)
        assert first['task_id'] == second['task_id']
        assert str(project) not in str(first)
    finally:
        release.set()


@pytest.mark.parametrize('pointer_failure', [False, True])
def test_chat_build_publishes_retained_artifacts_and_reopens_after_source_loss(project, scheduler, monkeypatch, tmp_path, pointer_failure):
    from lib.paper.builds import saved_documents
    from lib.paper.builds.document_repository import PaperDocumentRepository
    from tests.paper.test_paper_build_cache import _build
    from tests.paper.test_paper_build_runtime import _wait
    settings = {'projectPath': str(project)}
    monkeypatch.setattr(service, 'owned_settings', lambda *_: settings)
    monkeypatch.setattr(saved_documents, 'owned_settings', lambda *_: settings)
    def execution(document, *_):
        def run(task, publish):
            build = _build(tmp_path, document=document.document_id)
            return publish(build, pdf_url=service.artifact_url(document.document_id,
                           build.receipt['build_id'], 'document.pdf'))
        return run
    monkeypatch.setattr(service, 'host_execution', execution)
    if pointer_failure:
        def failed_pointer(*_):
            raise OSError('Injected disposable pointer failure')
        monkeypatch.setattr(scheduler.cache, 'promote', failed_pointer)
    accepted = service.start_preview(_payload(), owner_user_id=7, scheduler=scheduler)
    task = scheduler.production.runtime.get_owned(accepted['task_id'], user_id=7)
    result = _wait(task)['result']
    assert result['retained'] and not result['superseded']
    papers = PaperDocumentRepository(7)
    assert papers.get(accepted['document_id'])['receipt'] == result['receipt']
    settings.clear()
    opened = saved_documents.open_document({'conv_id': 'conversation-a'}, owner_user_id=7)
    assert opened['build']['receipt'] == result['receipt'] and not opened['can_compile']


def test_ambiguous_needle_does_not_guess_a_location():
    assert service.needle_to_line('same\nsame\n', 'same') is None
    assert service.needle_to_line('intro\n  unique\n', 'unique\nchanged block') == 2


def test_host_build_location_survives_source_edit_and_checks_artifacts(project, scheduler):
    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')
    payload = _payload(needle='A frozen paragraph.')
    document = service.bind_host_document(payload, owner_user_id=7, settings={'projectPath': str(project)})
    execute = service.host_execution(document, payload, scheduler)
    result = execute({'abort_event': threading.Event()}, lambda build, **extra: {
        **extra, 'receipt': scheduler.cache.publish(build)})
    assert result['receipt']['status'] == 'success', result
    assert result['location']['page'] == 1
    assert result['location']['build_id'] == result['receipt']['build_id']
    (project / 'paper/chapters/intro.tex').write_text('Later edit that is not compiled.')
    query = {'file': 'chapters/intro.tex', 'line': 1}
    build_id = result['receipt']['build_id']
    assert service.locate_build(document.document_id, build_id, query,
                                owner_user_id=7, scheduler=scheduler) == result['location']
    with pytest.raises(BuildError, match='not part of this paper build'):
        service.locate_build(document.document_id, build_id, {**query, 'file': '@toolchain/2'},
                             owner_user_id=7, scheduler=scheduler)
    with scheduler.cache.lease(7, document.document_id, build_id) as build:
        (build.directory / 'document.synctex.gz').write_bytes(b'tampered')
    with pytest.raises(BuildError, match='integrity'):
        service.locate_build(document.document_id, build_id, query, owner_user_id=7, scheduler=scheduler)
