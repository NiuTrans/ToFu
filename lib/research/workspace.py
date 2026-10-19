"""Durable research-production workspace for one auto-research direction.

Auto-research artifacts answer "what may be worth pursuing".  This module owns
what happens after promotion: the frozen experiment protocol, bounded run log,
claim/evidence ledger, manuscript plan, and compilation/submission readiness.
It deliberately does not reuse ``lib.experiments``: that domain evaluates Tofu
product strategies, whereas these records are user-authored scientific work.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from typing import Any

from lib.identity import require_user_id
from lib.storage.errors import StorageError
from lib.storage.retry import is_transient_storage_error

from .history import ResearchHistoryRepository, compact_observations
from .persistence import _direction_read_candidates, research_direction_hash
from .program import (
    MAX_CLAIMS,
    MAX_RUNS,
    MAX_TEXT,
    PROGRAM_CONTRACT_VERSION,
    default_program_fields,
    normalize_program_fields,
    normalize_run_evidence,
    normalize_source_path,
)

WORKSPACE_CONTRACT_VERSION = PROGRAM_CONTRACT_VERSION


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _text(value: Any, *, maximum: int = MAX_TEXT) -> str:
    return str(value or '').strip()[:maximum]


def empty_workspace(direction: str, lang: str = 'en') -> dict[str, Any]:
    return {
        'contract_version': WORKSPACE_CONTRACT_VERSION,
        'revision': 0,
        'direction': _text(direction, maximum=2_000),
        'lang': 'zh' if lang == 'zh' else 'en',
        'stage': 'selection',
        'selected_idea_id': '',
        'selected_idea_source_task_id': '',
        'hypothesis': '',
        **default_program_fields(),
        'updated_at': 0,
    }


def normalize_workspace(
    direction: str,
    lang: str,
    raw: Mapping[str, Any] | None,
    *,
    revision: int,
) -> dict[str, Any]:
    base = empty_workspace(direction, lang)
    raw = raw if isinstance(raw, Mapping) else {}
    base['revision'] = max(0, int(revision))
    stage = _text(raw.get('stage'), maximum=40)
    if stage in {'selection', 'experiment', 'evidence', 'writing', 'submission'}:
        base['stage'] = stage
    base['selected_idea_id'] = _text(raw.get('selected_idea_id'), maximum=160)
    source_task_id = raw.get('selected_idea_source_task_id', '')
    if not isinstance(source_task_id, str) or len(source_task_id) > 160:
        raise ValueError('invalid selected research idea source identity')
    base['selected_idea_source_task_id'] = source_task_id
    base['hypothesis'] = _text(raw.get('hypothesis'))
    base.update(normalize_program_fields(raw))
    base['updated_at'] = max(0, int(raw.get('updated_at') or 0))
    return base


def validate_selection_source(workspace, *, user_id):
    """Resolve owned discovery selection before publication or new experiments."""
    task_id = workspace.get('selected_idea_source_task_id', '')
    if not isinstance(task_id, str) or len(task_id) > 160:
        raise ValueError('invalid selected research idea source identity')
    if task_id:
        from .records import load_idea_source
        load_idea_source(task_id, user_id=user_id, direction=workspace['direction'],
            lang=workspace['lang'], idea_id=workspace.get('selected_idea_id', ''))


def load_workspace(direction: str, lang: str = 'en', *, user_id: int) -> dict[str, Any]:
    candidates = _direction_read_candidates(direction)
    if not candidates:
        return empty_workspace(direction, lang)
    uid = require_user_id(user_id, context='research workspace owner')
    norm_lang = 'zh' if lang == 'zh' else 'en'
    for paper_hash, candidate_direction in candidates:
        row = _storage().query('research.workspace.get', {
            'user_id': uid,
            'paper_hash': paper_hash,
            'lang': norm_lang,
        })
        if isinstance(row, Mapping):
            try:
                workspace = normalize_workspace(
                    candidate_direction, lang, row.get('workspace'),
                    revision=int(row.get('revision') or 0))
            except ValueError as exc:
                raise StorageError('database_integrity', 'Stored research workspace is invalid') from exc
            if workspace.get('history', {}).get('head'):
                ResearchHistoryRepository(_storage(), user_id=uid, paper_hash=paper_hash,
                    lang=norm_lang).read_chunk(workspace['history']['head'], workspace['history']['count'])
            from .receipt_pages import ReceiptPages
            for run in workspace.get('runs', []):
                if run.get('receipt_evidence') is not None:
                    from .science_reduction import bound_receipt_evidence
                    if not bound_receipt_evidence(run):
                        raise StorageError('database_integrity', 'Research complete receipt evidence binding is invalid')
                if run.get('receipt_progress') is not None:
                    from .progress_checkpoint import ProgressCheckpointRepository
                    ProgressCheckpointRepository(_storage(),user_id=uid,paper_hash=paper_hash,lang=norm_lang,run=run).read(run)
                reference = run.get('receipt_history')
                if reference and reference['head']:
                    ReceiptPages(_storage(), user_id=uid, paper_hash=paper_hash, lang=norm_lang,
                        run_id=run['id'], hypothesis_digest=run['hypothesis_digest'],
                        protocol_digest=run['protocol_digest']).read_page(reference)
            return workspace
    return empty_workspace(direction, lang)


def save_workspace(
    direction: str,
    lang: str,
    workspace: Mapping[str, Any],
    *,
    expected_revision: int,
    user_id: int,
    compilation_result=None,
    receipt_publication=None,
    failure_settlement=None,
    success_settlement=None,
    paginate_pending_run_id=None,
) -> dict[str, Any]:
    if paginate_pending_run_id is not None and (not isinstance(paginate_pending_run_id, str)
            or not paginate_pending_run_id or len(paginate_pending_run_id) > 96):
        raise ValueError('Invalid Research automatic receipt paging run identity')
    if receipt_publication is not None and paginate_pending_run_id is not None:
        raise ValueError('Research receipt publication accepts one preparation port')
    if isinstance(expected_revision, bool) or int(expected_revision) < 0:
        raise ValueError('expected_revision must be a non-negative integer')
    paper_hash = research_direction_hash(direction)
    if not paper_hash:
        raise ValueError('direction is required')
    owner = require_user_id(user_id, context='research workspace owner')
    prior = load_workspace(direction, lang, user_id=owner)
    if prior['revision'] != expected_revision:
        raise StorageError('database_conflict', 'Research workspace revision changed')
    candidate = copy.deepcopy(workspace)
    candidate.setdefault('selected_idea_source_task_id', prior.get('selected_idea_source_task_id', ''))
    if (candidate.get('selected_idea_source_task_id'), candidate.get('selected_idea_id')) != (
            prior.get('selected_idea_source_task_id', ''), prior.get('selected_idea_id')):
        validate_selection_source({**candidate, 'direction':prior['direction'], 'lang':prior['lang']}, user_id=owner)
    # Evidence is immutable across every application write, including direct
    # workspace PUT. Derived validity is intentionally recomputed when the
    # owner changes the hypothesis or protocol; the observation itself stays.
    incoming_runs = {row['id']: row for row in
                     normalize_run_evidence(candidate.get('runs'))}
    for observation in normalize_run_evidence(prior.get('runs')):
        if (observation['execution_status'] == 'completed'
                and incoming_runs.get(observation['id']) != observation):
            raise ValueError('Completed research observations cannot be removed or rewritten; retain the original and add a new result')
    from .receipt_pages import empty_reference, ReceiptPagePublication
    previous_runs = {row['id']: row for row in normalize_run_evidence(prior.get('runs'))}
    for run in incoming_runs.values():
        old = previous_runs.get(run['id'])
        if run.get('receipt_progress') != (old.get('receipt_progress') if old else None):
            raise ValueError('Research receipt progress is server-owned')
        if run.get('receipt_evidence') != (old.get('receipt_evidence') if old else None):
            raise ValueError('Research complete receipt evidence is server-owned')
    settled_success = None
    if success_settlement is not None:
        from .receipt_settlement import ReceiptSuccessSettlement
        if not isinstance(success_settlement, ReceiptSuccessSettlement):
            raise ValueError('Research success settlement requires an application-verified result')
        projected = success_settlement.project(_storage(), candidate, prior,
            user_id=owner, paper_hash=paper_hash, lang='zh' if lang == 'zh' else 'en', revision=expected_revision)
        settled_success = projected['id']
        incoming_runs[settled_success] = projected
        candidate['runs'] = list(incoming_runs.values())
    settled_failure = None
    if failure_settlement is not None:
        from .receipt_settlement import ReceiptFailureSettlement
        if not isinstance(failure_settlement, ReceiptFailureSettlement):
            raise ValueError('Research failure settlement requires an application-verified result')
        settled_failure = failure_settlement.validate(_storage(), candidate, prior,
            user_id=owner, paper_hash=paper_hash, lang='zh' if lang == 'zh' else 'en', revision=expected_revision)
    for run in incoming_runs.values():
        old = previous_runs.get(run['id'])
        reference = run.get('receipt_history', empty_reference())
        published = old.get('receipt_history', empty_reference()) if old else empty_reference()
        if reference != published:
            raise ValueError('Research receipt history is server-owned')
        if published['head']:
            if old['execution_status'] == 'failed':
                if run != old:
                    raise ValueError('Failed paged research observations cannot be rewritten')
            elif (run['execution_status'] not in {'planned', 'queued', 'running'}
                    and not (run['id'] == settled_failure and run['execution_status'] == 'failed')
                    and not (run['id'] == settled_success and run['execution_status'] == 'completed')
                    and not (old['execution_status'] == 'completed' and run == old)):
                raise ValueError('Research paged receipt settlement requires complete evidence reduction')
            if (run['tool_receipts'][:len(old['tool_receipts'])] != old['tool_receipts']
                    or any(run[key] != old[key] for key in ('hypothesis_digest', 'protocol_digest'))):
                raise ValueError('Research published pending receipts cannot be rewritten')
    if any(run.get('receipt_history', {}).get('head') and run['id'] not in incoming_runs
           for run in previous_runs.values()):
        raise ValueError('Research published pending receipts cannot be removed')
    if receipt_publication is not None:
        if not isinstance(receipt_publication, ReceiptPagePublication):
            raise ValueError('Research receipt publication requires an application-verified result')
        candidate['runs'] = receipt_publication.for_workspace(candidate, prior,
            client=_storage(write=True), user_id=owner, paper_hash=paper_hash,
            lang='zh' if lang == 'zh' else 'en', revision=expected_revision)
        incoming_runs = {row['id']: row for row in candidate['runs']}
    if paginate_pending_run_id is not None:
        from .receipt_pages import prepare_publication
        target = incoming_runs.get(paginate_pending_run_id)
        if target is None or target['execution_status'] not in {'planned', 'queued', 'running'}:
            raise ValueError('Research automatic receipt paging requires an unfinished run')
        if target['tool_receipts']:
            publication = prepare_publication(_storage(write=True), candidate,
                user_id=owner, paper_hash=paper_hash, lang='zh' if lang == 'zh' else 'en',
                run_id=paginate_pending_run_id, revision=expected_revision, keep_inline=0)
            candidate['runs'] = publication.for_workspace(candidate, prior,
                client=_storage(write=True), user_id=owner, paper_hash=paper_hash,
                lang='zh' if lang == 'zh' else 'en', revision=expected_revision)
            incoming_runs = {row['id']: row for row in candidate['runs']}
    if paginate_pending_run_id is not None:
        target = incoming_runs[paginate_pending_run_id]
        if target.get('receipt_history', {}).get('head'):
            from .progress_checkpoint import ProgressCheckpointRepository
            progress = ProgressCheckpointRepository(_storage(write=True),user_id=owner,paper_hash=paper_hash,
                lang='zh' if lang == 'zh' else 'en',run=target)
            target['receipt_progress'] = progress.prepare(previous_runs.get(target['id']), target,
                                                         candidate.get('capability_bindings', []))
            candidate['runs'] = list(incoming_runs.values())
    # Source edits are user data. Reject an incomplete normalization instead of
    # accepting a truncated tree and later compiling a different manuscript.
    sources = candidate.get('source_files', [])
    if (not isinstance(sources, list)
            or any(not isinstance(row, dict) or not isinstance(row.get('content'), str)
                   or not isinstance(row.get('path'), str)
                   or normalize_source_path(row['path']) != row['path']
                   for row in sources)):
        raise ValueError('Invalid Research source edits')
    if sources:
        from lib.paper.builds.records import SourceRecords
        from lib.paper.builds.errors import BuildError
        try:
            SourceRecords.from_text(sources, main_file=sources[0].get('path', ''))
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
    if compilation_result is None:
        candidate['compilation'] = prior['compilation']
    else:
        from .compilation import CompilationResult
        if not isinstance(compilation_result, CompilationResult):
            raise ValueError('Research compilation requires an application-verified result')
        candidate['compilation'] = compilation_result.for_workspace(
            direction, lang, candidate, owner_user_id=owner)
    # Historical metadata is server-owned. Prepare pages before publishing the
    # pointer through the existing workspace CAS; failed preparations/competing
    # saves cannot turn orphan pages into accepted scientific evidence.
    candidate.pop('history', None)
    retained, archived = compact_observations(candidate)
    validation_candidate = dict(candidate, runs=retained)
    normalize_workspace(direction, lang, validation_candidate, revision=int(expected_revision) + 1)
    client = _storage(write=True)
    candidate['history'] = ResearchHistoryRepository(client, user_id=owner,
        paper_hash=paper_hash, lang='zh' if lang == 'zh' else 'en').prepare(
            prior.get('history'), candidate, archived)
    from .source_index import ReceiptSourceIndex, empty_reference, prepare_source_index
    repository = ResearchHistoryRepository(client, user_id=owner, paper_hash=paper_hash,
        lang='zh' if lang == 'zh' else 'en')
    source_index = ReceiptSourceIndex(client, user_id=owner, paper_hash=paper_hash,
        lang='zh' if lang == 'zh' else 'en')
    reference = prior.get('history', {}).get('source_index', empty_reference(0))
    from .receipt_pages import receipt_reader
    complete_reader = receipt_reader(client, user_id=owner, paper_hash=paper_hash,
                                    lang='zh' if lang == 'zh' else 'en')
    candidate['history']['source_index'] = prepare_source_index(source_index, reference, repository,
        prior.get('history'), list(incoming_runs.values()), normalize_run_evidence(prior.get('runs')),
        receipt_reader=complete_reader)
    candidate['runs'] = retained
    next_revision = int(expected_revision) + 1
    normalized = normalize_workspace(direction, lang, candidate, revision=next_revision)
    normalized['updated_at'] = int(time.time())
    if compilation_result is not None:
        compilation_result.ensure_live()
    with ExitStack() as retention:
        proof = None
        if compilation_result is not None and compilation_result.artifact_build is not None:
            from lib.paper.builds.artifact_repository import PaperBuildArtifactRepository
            repository = PaperBuildArtifactRepository(owner, client_factory=lambda **_: client)
            proof = retention.enter_context(compilation_result.retain_artifacts(repository))
        if compilation_result is not None:
            compilation_result.ensure_live()
        packet = {
            'user_id': owner, 'paper_hash': paper_hash, 'lang': normalized['lang'],
            'expected_revision': int(expected_revision), 'workspace': normalized,
            'updated_at': normalized['updated_at'], 'artifact_proof': proof,
        }
        command_id = f'research.workspace.put:{paper_hash}:{expected_revision}:{uuid.uuid4().hex}'
        try:
            result = client.command('research.workspace.put', packet, command_id)
        except StorageError as exc:
            if not is_transient_storage_error(exc):
                raise
            try:
                observed = client.query('research.workspace.get', {
                    'user_id': owner, 'paper_hash': paper_hash, 'lang': normalized['lang']})
            except StorageError:
                raise exc
            # An exact acknowledged document/version proves the ambiguous CAS;
            # a competing revision or different body never substitutes for it.
            if (not isinstance(observed, Mapping) or observed.get('revision') != next_revision
                    or observed.get('workspace') != normalized):
                raise exc
            result = observed
    def exact_commit(row):
        return (isinstance(row, Mapping) and type(row.get('revision')) is int
                and row['revision'] == next_revision and row.get('workspace') == normalized)
    if not exact_commit(result):
        # A success-shaped but inconsistent ACK is not publication proof. Read
        # once; never retry an effect or accept a competing document as ours.
        observed = client.query('research.workspace.get', {
            'user_id': owner, 'paper_hash': paper_hash, 'lang': normalized['lang']})
        if not exact_commit(observed):
            raise StorageError('database_integrity', 'Research workspace publication was not exactly acknowledged')
        result = observed
    return normalize_workspace(
        direction, lang, result.get('workspace'), revision=int(result.get('revision') or 0))



@contextmanager
def open_run_receipts(direction: str, lang: str = 'en', *, user_id: int, run_id: str):
    """Read complete chronological receipts from one acknowledged working-run snapshot.

    Inspection grants no execution, ownership or scientific acceptance. The caller
    must consume inside this context; temporary reversal data is always reclaimed.
    """
    workspace = load_workspace(direction, lang, user_id=user_id)
    with _open_snapshot_run_receipts(workspace, user_id=user_id, run_id=run_id) as stream:
        yield stream


@contextmanager
def _open_snapshot_run_receipts(workspace, *, user_id, run_id):
    from .receipt_pages import ReceiptPages, empty_reference
    run = next((row for row in workspace['runs'] if row['id'] == run_id), None)
    if run is None:
        raise ValueError('Research working run was not found')
    reference = run.get('receipt_history', empty_reference())
    metadata = {'revision': workspace['revision'], 'run_id': run_id,
                'direction_hash': research_direction_hash(workspace['direction']), 'lang': workspace['lang'],
                'count': reference['count'] + len(run['tool_receipts']),
                'capability_bindings': copy.deepcopy(workspace['capability_bindings']),
                'protocol': copy.deepcopy(workspace['protocol']),
                'scientific_dependencies': {key: run[key] for key in ('hypothesis_digest', 'protocol_digest')},
                'scientific_outcome': run['scientific_outcome'],
                'evidence_refs': [run['id'], run['artifact_ref'], *run['artifact_refs']]}
    from .program import dependency_digests
    metadata['basis_current'] = all(run.get(key) == value
                                    for key, value in dependency_digests(workspace).items())
    if not reference['head']:
        # Legacy unpaged runs may lack a frozen dependency binding.
        receipts = (row for row in copy.deepcopy(run['tool_receipts']))
        try:
            yield dict(metadata, receipts=receipts)
        finally:
            receipts.close()
        return
    repository = ReceiptPages(_storage(), user_id=user_id,
        paper_hash=research_direction_hash(workspace['direction']), lang=workspace['lang'],
        run_id=run_id, hypothesis_digest=run['hypothesis_digest'], protocol_digest=run['protocol_digest'])
    with repository.open_receipts(reference, run['tool_receipts']) as receipts:
        yield dict(metadata, receipts=receipts)



def read_run_execution_state(direction: str, lang: str = 'en', *, user_id: int, run_id: str):
    """Inspect published job progress; legacy snapshots retain complete receipt reads."""
    from .execution_observation import iter_execution_states, reduce_execution_state
    workspace = load_workspace(direction, lang, user_id=user_id)
    run = next((row for row in workspace['runs'] if row['id'] == run_id), None)
    if run is None:
        raise ValueError('Research working run was not found')
    if run.get('receipt_progress') is not None:
        from .progress_checkpoint import ProgressCheckpointRepository
        repository = ProgressCheckpointRepository(_storage(), user_id=user_id,
            paper_hash=research_direction_hash(workspace['direction']), lang=workspace['lang'], run=run)
        checkpoint = repository.read(run)
        state = repository.indexes.preview_status(checkpoint['index'], run['tool_receipts'],
                                                 workspace['capability_bindings'])
        return {'revision': workspace['revision'], 'run_id': run_id,
                'count': checkpoint['receipt_history']['count'] + len(run['tool_receipts']),
                'execution_status': state}
    with _open_snapshot_run_receipts(workspace, user_id=user_id, run_id=run_id) as stream:
        state = reduce_execution_state(iter_execution_states(stream['receipts'], stream['capability_bindings']))
        return {key: stream[key] for key in ('revision', 'run_id', 'count')} | {'execution_status': state}



def read_run_scientific_state(direction: str, lang: str = 'en', *, user_id: int, run_id: str):
    """Audit complete acknowledged reported evidence without settling or rerunning a task."""
    from .science_reduction import reduce_scientific_receipts
    with open_run_receipts(direction, lang, user_id=user_id, run_id=run_id) as stream:
        refs = [ref for ref in stream['evidence_refs'] if ref]
        from .original_science import original_science_loader
        original_loader = original_science_loader(_storage(), user_id=user_id,
            paper_hash=stream['direction_hash'], lang=stream['lang'], run=stream['scientific_dependencies'])
        summary = reduce_scientific_receipts(stream['receipts'],
            expected_outcome=stream['scientific_outcome'],
            protocol=stream['protocol'] if stream['basis_current'] else None, evidence_refs=refs,
            original_loader=original_loader)
        if not stream['basis_current']:
            summary['outcome_consistent'] = False
            summary['checks'] = dict.fromkeys(summary['checks'], False)
        return {key: stream[key] for key in ('revision', 'run_id', 'count', 'basis_current')} | summary


def load_workspace_response(direction: str, lang: str = 'en', *, user_id: int,
                            history_cursor=None) -> dict:
    """One acknowledged workspace snapshot and optional historical page."""
    from .history import empty_history
    from .program import readiness
    if history_cursor is not None and (type(history_cursor) is not int or history_cursor < 0):
        raise ValueError('Invalid research history cursor')
    workspace = load_workspace(direction, lang, user_id=user_id)
    result = {'workspace': workspace, 'readiness': readiness(workspace)}
    if history_cursor is not None:
        page = ResearchHistoryRepository(_storage(), user_id=user_id,
            paper_hash=research_direction_hash(workspace['direction']), lang=workspace['lang']).page(
                workspace.get('history') or empty_history(workspace),
                cursor=history_cursor or None)
        result['history_page'] = dict(page, revision=workspace['revision'])
    return result


def load_history_page(direction: str, lang: str = 'en', *, user_id: int, cursor=None) -> dict:
    """Read one bounded page reachable from the acknowledged workspace head."""
    if cursor is not None and (type(cursor) is not int or cursor <= 0):
        raise ValueError('Invalid research history cursor')
    return load_workspace_response(direction, lang, user_id=user_id,
                                   history_cursor=0 if cursor is None else cursor)['history_page']


__all__ = [
    'MAX_CLAIMS', 'MAX_RUNS', 'WORKSPACE_CONTRACT_VERSION',
    'empty_workspace', 'load_workspace', 'open_run_receipts', 'read_run_execution_state', 'read_run_scientific_state', 'load_history_page', 'load_workspace_response', 'normalize_workspace', 'save_workspace',
]
