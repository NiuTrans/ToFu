"""Research compilation authority: native PDF evidence, source CAS and owner fences."""
from __future__ import annotations
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.llm_errors import AbortedError
from lib.paper.builds.errors import BuildError
from lib.research.compilation import compile_workspace
from lib.research.program import normalize_program_fields, readiness, source_tree_digest
from lib.research.workspace import empty_workspace, load_workspace, save_workspace
from lib.storage.errors import StorageError
from tests.support import paper_builds as build_fixtures
from tests.support.envelope import v1_get_json
from tests.research import test_research_workspace as workspace_fixtures
from tests.paper.test_paper_build_compiler import _require_tex

pytestmark = pytest.mark.unit
DIRECTION = 'Verified compilation fixture'
scheduler = build_fixtures.scheduler
storage = workspace_fixtures.storage


@pytest.fixture
def source():
    draft = empty_workspace(DIRECTION)
    draft['source_files'] = [
        {'path': 'main.tex', 'content': r'\documentclass{article}\begin{document}\input{section}\end{document}'},
        {'path': 'section.tex', 'content': 'Exact research evidence.'},
        {'path': 'README.md', 'content': 'This metadata is part of the frozen source tree.'}]
    return draft


def _compile(source, scheduler, cancel=None):
    _require_tex()
    return compile_workspace(source, direction=DIRECTION, lang='en', owner_user_id=1,
                             task_id='research-proof-fixture', cancel=cancel or threading.Event(),
                             scheduler=scheduler)


def _compile_gate(workspace):
    return next(row['ok'] for row in readiness(workspace)['gates'] if row['id'] == 'compile')


def test_native_build_commits_verified_receipt_and_client_cannot_replace_it(source, storage, scheduler):
    with _compile(source, scheduler) as compiled:
        saved = save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1,
                               compilation_result=compiled)
        assert _compile_gate(saved)
        reference = saved['compilation']['build']
        with scheduler.cache.open_artifact(1, reference['document_id'], reference['build_id'], 'document.pdf') as (stream, meta):
            assert stream.read(5) == b'%PDF-'
            assert reference['pdf_sha256'] == meta['sha256']
    spoofed = copy.deepcopy(saved)
    spoofed['compilation']['build']['pdf_sha256'] = 'f' * 64
    spoofed['compilation']['engine'] = 'invented'
    preserved = save_workspace(DIRECTION, 'en', spoofed, expected_revision=1, user_id=1)
    assert preserved['compilation'] == saved['compilation']
    changed = copy.deepcopy(preserved)
    changed['source_files'][1]['content'] = 'Edited after the build.'
    changed['compilation']['source_digest'] = source_tree_digest(changed['source_files'])
    changed['compilation']['build']['source_digest'] = changed['compilation']['source_digest']
    current = save_workspace(DIRECTION, 'en', changed, expected_revision=2, user_id=1)
    assert not _compile_gate(current)
    assert current['compilation']['build'] == saved['compilation']['build']


def test_client_cannot_create_first_passing_receipt(source, storage):
    from tests.support.research_compilation import compilation_projection
    source['compilation'] = compilation_projection(source['source_files'])
    assert _compile_gate(source), 'fixture must bypass mere shape/digest validation'
    saved = save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1)
    assert not _compile_gate(saved)
    assert saved['compilation']['build'] is None


def test_workspace_http_rejects_a_well_formed_forged_success(source, storage):
    import server
    from tests.support.research_compilation import compilation_projection
    source['compilation'] = compilation_projection(source['source_files'])
    response = workspace_fixtures._run(server.app.test_client().put('/api/v4/research/workspace', json={
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': 0, 'workspace': source}))
    assert response.status_code == 200
    body = workspace_fixtures._run(v1_get_json(response))
    assert body['workspace']['compilation']['build'] is None
    assert not next(row['ok'] for row in body['readiness']['gates'] if row['id'] == 'compile')


@pytest.mark.parametrize('fault', ['file_count', 'file_text', 'total_text', 'duplicate_path', 'path_normalization'])
def test_workspace_never_silently_drops_source_edits(source, storage, fault):
    from lib.research.program import MAX_SOURCE_FILE_CHARS, MAX_SOURCE_FILES
    if fault == 'file_count':
        source['source_files'] = [{'path': f'{n}.tex', 'content': 'source'}
                                  for n in range(MAX_SOURCE_FILES + 1)]
    elif fault == 'file_text':
        source['source_files'][0]['content'] = 'x' * (MAX_SOURCE_FILE_CHARS + 1)
    elif fault == 'total_text':
        source['source_files'] = [{'path': f'{n}.tex', 'content': 'x' * MAX_SOURCE_FILE_CHARS}
                                  for n in range(7)]
    elif fault == 'path_normalization':
        source['source_files'][0]['path'] = 'would-be-dropped%.tex'
    else:
        source['source_files'].append(dict(source['source_files'][0]))
    with pytest.raises(ValueError):
        save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1)
    assert load_workspace(DIRECTION, 'en', user_id=1)['revision'] == 0


@pytest.mark.parametrize('mode', ['local_tectonic', 'bound_tool', 'overleaf_mcp', 'paper_build'])
def test_legacy_or_unproven_success_never_passes(source, mode):
    source['compilation'].update(mode=mode, status='passing',
                                source_digest=source_tree_digest(source['source_files']))
    assert not _compile_gate(source)
    assert normalize_program_fields(source)['compilation']['status'] == 'not_run'


def test_model_and_tool_completion_cannot_manufacture_a_compile_result(source):
    from lib.research.action import apply_action_result
    with pytest.raises(ValueError, match='Model output cannot'):
        apply_action_result(action='compile', workspace=source, payload={'status': 'passing'},
                            receipts=[{'tool': 'mcp__compiler__build', 'status': 'done'}],
                            bindings=[{'capability': 'manuscript.compile', 'tool': 'mcp__compiler__build'}],
                            task_id='invented')


def test_build_proof_rejects_other_owner_direction_edits_and_expired_lease(source, scheduler):
    with _compile(source, scheduler) as result:
        for direction, owner, candidate in [(DIRECTION, 2, source), ('Another direction', 1, source),
                                            (DIRECTION, 1, {**source, 'source_files': []})]:
            with pytest.raises(ValueError, match='does not match'):
                result.for_workspace(direction, 'en', candidate, owner_user_id=owner)
    with pytest.raises(ValueError, match='lease has ended'):
        result.for_workspace(DIRECTION, 'en', source, owner_user_id=1)


def test_cancelled_build_proof_cannot_commit(source, storage, scheduler):
    cancel = threading.Event()
    with _compile(source, scheduler, cancel) as result:
        cancel.set()
        with pytest.raises(AbortedError):
            save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1,
                           compilation_result=result)
    assert load_workspace(DIRECTION, 'en', user_id=1)['revision'] == 0


def test_stale_build_cannot_overwrite_a_new_workspace_revision(source, storage, scheduler):
    with _compile(source, scheduler) as result:
        newer = copy.deepcopy(source)
        newer['source_files'][1]['content'] = 'A concurrent edit survives.'
        save_workspace(DIRECTION, 'en', newer, expected_revision=0, user_id=1)
        with pytest.raises(StorageError) as caught:
            save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1,
                           compilation_result=result)
        assert caught.value.code == 'database_conflict'
    assert load_workspace(DIRECTION, 'en', user_id=1)['source_files'][1]['content'] == 'A concurrent edit survives.'


@pytest.mark.parametrize('fault', ['unverified', 'manifest'])
def test_unverified_or_different_manifest_cannot_pass(source, scheduler, monkeypatch, fault):
    from lib.paper.builds import service
    from lib.paper.builds.compiler import artifact_metadata
    original = service.compile_snapshot

    def inject(*args, **kwargs):
        build = original(*args, **kwargs)
        assert build.receipt['status'] == 'success'
        if fault == 'unverified':
            build.receipt['version_verified'] = False
        else:
            path = build.directory / 'sources.json'
            rows = json.loads(path.read_text())
            rows[0]['sha256'] = 'f' * 64
            path.write_text(json.dumps(rows))
            build.receipt['artifacts'] = [artifact_metadata(path) if row['name'] == path.name else row
                                         for row in build.receipt['artifacts']]
        return build

    monkeypatch.setattr(service, 'compile_snapshot', inject)
    if fault == 'manifest':
        with pytest.raises(BuildError, match='differs from the workspace'):
            with _compile(source, scheduler):
                pytest.fail('tampered manifest reached workspace persistence')
    else:
        with _compile(source, scheduler) as result:
            assert result.projection['status'] == 'failing'
            assert result.projection['build'] is None


def test_cancel_detaches_consumer_and_retires_the_shared_worker(source, scheduler, monkeypatch):
    from lib.paper.builds import service
    entered, retired, cancel = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        try:
            while not kwargs['cancel'].is_set():
                retired.wait(0.01)
            raise BuildError('cancelled', 'Fixture compiler stopped')
        finally:
            retired.set()

    def compile_until_cancelled():
        # This compiler is controlled above; real TeX availability is unrelated
        # to subscriber cancellation and worker retirement.
        with compile_workspace(source,direction=DIRECTION,lang='en',owner_user_id=1,
                task_id='research-cancel-fixture',cancel=cancel,scheduler=scheduler):
            pytest.fail('cancelled work published a result')

    monkeypatch.setattr(service, 'compile_snapshot', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(compile_until_cancelled)
        assert entered.wait(5)
        cancel.set()
        with pytest.raises(AbortedError):
            future.result(timeout=5)
        assert retired.wait(5)


def test_research_action_compiles_without_a_model_or_mcp_binding(source, storage, scheduler, monkeypatch):
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    import lib.research.compilation as compilation
    _require_tex()
    saved = save_workspace(DIRECTION, 'en', source, expected_revision=0, user_id=1)
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    monkeypatch.setattr(compilation, 'get_scheduler', lambda: scheduler)
    monkeypatch.setattr(action, '_run_agent', Mock(side_effect=AssertionError('unexpected model call')))
    monkeypatch.setattr(action, 'build_action_tool_epoch', Mock(side_effect=AssertionError('unexpected MCP discovery')))
    action.run_research_action({'task_id': 'research-action-fixture', 'user_id': 1,
                               'action': 'compile', 'direction': DIRECTION, 'lang': 'en',
                               'expected_revision': saved['revision'], 'confirm_external_writes': True,
                               'abort_event': threading.Event()})
    result = owner.finish.call_args.kwargs
    assert 'error' not in result, result
    assert _compile_gate(result['result']['workspace'])
    assert result['result']['workspace']['revision'] == 2
