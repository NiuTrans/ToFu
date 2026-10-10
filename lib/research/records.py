"""Owner-scoped Research run snapshots on the existing Sidecar record authority.

Public snapshots are bounded, replace-in-place records, not an event journal.
Stage files remain execution checkpoints; their acknowledged snapshots are
backed up here so explicit resume can reconstruct a lost work directory.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import time
import threading
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)
def _storage(*, write=False):
    from .persistence import _storage as storage
    return storage(write=write)


NAMESPACE = 'research.runs.v1'
INDEX_NAMESPACE = 'research.run_index.v1'
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_PAPERS = 200
#: Graph payloads are the Connected-Papers surface: up to four real cites
#: edges per paper at the 200-paper ceiling, all endpoints validated.
MAX_CITATION_EDGES = 800
ACTIVE = ('pending', 'running')
_resume_lock = threading.RLock()

#: Backoff between attempts of one snapshot commit. A transient writer
#: starvation (a maintenance command holding the single SQLite writer past
#: the 2 s acquisition cap) must not sacrifice a multi-stage run: the
#: snapshot write is idempotent replace-in-place, so failures the storage
#: layer itself marked retryable get a bounded second chance. Durable
#: refusals still propagate and abort the run.
_COMMIT_RETRY_DELAYS_S = (1.0, 2.0)


def _key(task_id, user_id):
    uid = require_user_id(user_id, context='research record owner')
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', task_id):
        raise ValueError('invalid research task id')
    return f'{uid}:{task_id}'


def _put(namespace, key, value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_RECORD_BYTES:
        raise ValueError('research record exceeds 8 MiB')
    client = _storage(write=True)
    def exact_readback():
        row = client.query('record.get', {'namespace': namespace, 'key': key})
        return (isinstance(row, dict) and
                json.dumps(row.get('value'), ensure_ascii=False, sort_keys=True, separators=(',', ':')) == encoded)
    try:
        result = client.command('record.put', {
            'namespace': namespace, 'key': key, 'value': value,
        }, f'research-record:{uuid.uuid4().hex}')
        if (not isinstance(result, dict) or type(result.get('version')) is not int
                or result['version'] < 1):
            raise StorageError('database_internal', 'Research record write was not acknowledged')
    except StorageError as exc:
        # A lost response may follow a committed write. Only the exact selected
        # authority value establishes success; inspection never repeats effects.
        try:
            if exact_readback():
                return
        except StorageError:
            pass
        raise exc
    if not exact_readback():
        raise StorageError('database_integrity', 'Research record publication was not exactly acknowledged')


def paper_projection(raw):
    """Whitelist external metadata; unknown counts are never represented by zero."""
    from .literature import nonnegative_integer
    aliases = {'arxivId': 'arxiv_id', 'paperId': 'paper_id',
               'semanticScholarId': 'semantic_scholar_id', 'citationCount': 'citation_count',
               'metadataSource': 'metadata_source', 'citationCountSource': 'citation_count_source'}
    paper = {}
    for field, limit in (('arxivId', 160), ('paperId', 320), ('doi', 256),
                         ('semanticScholarId', 64), ('phash', 128), ('title', 500),
                         ('status', 40), ('error', 500), ('venue', 500),
                         ('metadataSource', 80), ('citationCountSource', 80)):
        value = raw.get(field, raw.get(aliases.get(field, field)))
        if isinstance(value, str):
            paper[field] = value[:limit]
    for field in ('year', 'citationCount', 'pageCount', 'textLength'):
        if field in raw or aliases.get(field) in raw:
            value = nonnegative_integer(raw.get(field, raw.get(aliases.get(field, field))))
            if field == 'year' and value is not None and value > 9999:
                value = None
            paper[field] = value if field in ('year', 'citationCount') else value or 0
    if 'degraded' in raw:
        paper['degraded'] = bool(raw['degraded'])
    paper.setdefault('arxivId', '')
    paper.setdefault('paperId', ('doi:' + paper['doi']) if paper.get('doi') else
                     ('arxiv:' + paper['arxivId']) if paper['arxivId'] else
                     ('s2:' + paper['semanticScholarId']) if paper.get('semanticScholarId') else '')
    return paper


def merge_paper(papers, raw):
    paper = paper_projection(raw)
    if not paper.get('paperId') and not paper.get('arxivId'):
        return
    previous = next((p for p in papers if any(p.get(k) and p.get(k) == paper.get(k)
                    for k in ('paperId', 'arxivId', 'doi', 'semanticScholarId'))), None)
    if previous is not None:
        # Sparse parser receipts cannot erase discovery metadata or graph identity.
        canonical, status = previous.get('paperId'), previous.get('status')
        previous.update({k: v for k, v in paper.items() if v not in (None, '')})
        if canonical:
            previous['paperId'] = canonical
        if status and status not in ('discovered', 'metadata_only') and paper.get('status') in ('discovered', 'metadata_only'):
            previous['status'] = status
        return previous
    elif len(papers) < MAX_PAPERS:
        papers.append(paper)
        return paper
    else:
        raise ValueError('research paper record ceiling exceeded')


def literature_projection(discovery):
    papers = []
    for raw in (discovery.get('papers') or [])[:MAX_PAPERS]:
        if isinstance(raw, dict):
            paper = paper_projection(raw)
            paper.setdefault('year', None)
            paper.setdefault('citationCount', None)
            for field in ('venue', 'metadataSource', 'citationCountSource'):
                paper.setdefault(field, '')
            paper.setdefault('status', 'discovered' if paper.get('arxivId') else 'metadata_only')
            merge_paper(papers, paper)
    ids = {p['paperId'] for p in papers}
    edges = []
    for edge in (discovery.get('citation_edges') or [])[:MAX_CITATION_EDGES]:
        if (isinstance(edge, dict) and edge.get('relation') == 'cites'
                and edge.get('from') in ids and edge.get('to') in ids
                and edge['from'] != edge['to']):
            clean = {k: edge[k] for k in ('from', 'to', 'relation')}
            if clean not in edges:
                edges.append(clean)
    statuses = []
    for raw in (discovery.get('source_statuses') or [])[:64]:
        if isinstance(raw, dict):
            receipt = {k: v[:500 if k == 'error' else 80] for k, v in raw.items()
                       if k in ('source', 'operation', 'status', 'error') and isinstance(v, str)}
            if isinstance(raw.get('retrieved'), int) and not isinstance(raw['retrieved'], bool):
                receipt['retrieved'] = max(0, raw['retrieved'])
            statuses.append(receipt)
    return {'papers': papers, 'citation_edges': edges, 'source_statuses': statuses}


def survey_failure_projection(rows):
    """Keep only bounded map error evidence; never raw provider bodies."""
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows[:200]:
        if not isinstance(row, dict) or not isinstance(row.get('batch'), int) or isinstance(row['batch'], bool):
            continue
        ids = row.get('paper_ids')
        if not 0 <= row['batch'] < 200 or not isinstance(ids, list):
            continue
        result.append({'batch': row['batch'],
                       'paper_ids': [aid[:160] for aid in ids[:25] if isinstance(aid, str)],
                       'error': str(row.get('error') or '')[:300]})
    return result


def _error_text(error):
    """Project canonical diagnostic fields only; never expose raw error bodies."""
    if isinstance(error, dict):
        error = ': '.join(dict.fromkeys(str(error.get(k) or '')[:2000]
                          for k in ('message', 'detail') if error.get(k))) or error.get('kind') or ''
    return str(error or '')[:2000]


def _recover_evidence(record):
    """Read only acknowledged historical evidence; no dispatch or library lookup."""
    stages = (record.get('checkpoint') or {}).get('stages') or {}
    harvest = (stages.get('harvest') or {}).get('artifact') or {}
    discovery = harvest.get('discovery')
    if isinstance(discovery, dict):
        projection = literature_projection(discovery)
        old = record.get('papers') or []
        discovered = projection.pop('papers')
        if record.get('citation_edges'):
            # New records already have a canonical graph. An older checkpoint
            # can enrich metadata but cannot replace its endpoint identities.
            record['papers'] = old[:MAX_PAPERS]
            for paper in discovered:
                merge_paper(record['papers'], paper)
        else:
            # Historical parser-only rows had no graph identity. The actual
            # checkpoint discovery supplies the IDs for its recovered edges.
            record['papers'] = discovered
            for paper in old[:MAX_PAPERS]:
                merge_paper(record['papers'], paper)
        for key, value in projection.items():
            if not record.get(key):
                record[key] = value
    if record.get('status') in ('error', 'interrupted', 'aborted'):

        progress = record.get('research_progress') or {}
        if progress.get('activity') == 'stage_failed' and progress.get('stage'):
            record.setdefault('failed_stage', str(progress['stage'])[:80])
        for stage, receipt in list(stages.items())[:20]:
            if isinstance(receipt, dict) and receipt.get('error'):
                record.setdefault('error', str(receipt['error'])[:2000])
                record.setdefault('failed_stage', str(stage)[:80])


def persist_record(task, *, event=None, status=None, result=None):
    """Commit before emitting business progress; never swallow write failures."""
    key = _key(task['task_id'], task['user_id'])
    record = deepcopy(task.get('_durable_research_record') or {})
    from .runtime import normalize_evolution_binding
    binding = normalize_evolution_binding((task.get('config') or {}).get('evolution'))
    prior = (record.get('request') or {}).get('evolution')
    if ((prior is not None and prior != binding)
            or (record.get('evolution_run_id') and
                (not binding or record['evolution_run_id'] != binding['run_id']))):
        raise ValueError('research evolution binding changed')
    now = int(time.time() * 1000)
    record.update({
        'schema_version': 1, 'task_id': task['task_id'],
        'direction': task.get('direction', ''), 'lang': task.get('lang', 'en'),
        'status': status or task['status'], 'created_at': record.get('created_at', now),
        'updated_at': now, 'research_progress': deepcopy(task.get('research_progress')),
        'request': {field: deepcopy(task.get(field)) for field in
                    ('n_ideas', 'seed_arxiv_ids', 'harvest_n', 'model', 'conv_id')},
    })
    limit_reason = task.get('_executionLimitReason')
    if limit_reason is not None:
        if not isinstance(limit_reason, str) or not 1 <= len(limit_reason) <= 80:
            raise ValueError('invalid research execution limit reason')
        record['execution_limit_reason'] = limit_reason
    if binding:
        record['request']['evolution'] = binding
        record['evolution_run_id'] = binding['run_id']
    papers = record.setdefault('papers', [])
    artifact = result if result is not None else task.get('result')
    if isinstance(artifact, dict):
        record['result'] = deepcopy(artifact)
    if record['status'] == 'error' and task.get('error'):
        error = _error_text(task['error'])
        if error:
            record['error'] = error
    if event and event.get('type') == 'stage_failed':
        record['failed_stage'] = str(event.get('stage') or '')[:80]
    if event and event.get('type') == 'stage_started' and event.get('stage') == record.get('failed_stage'):
        record.pop('error', None)
        record.pop('failed_stage', None)
    if event and event.get('type') == 'stage_started' and event.get('stage') == 'survey':
        record.pop('survey_batch_failures', None)
    if event and event.get('type') == 'survey_map_failed':
        record['survey_batch_failures'] = survey_failure_projection(event.get('survey_batch_failures'))
    if event and event.get('type') == 'literature_discovered':
        projection = literature_projection(event)
        aliases = {}
        for paper in projection.pop('papers'):
            merged = merge_paper(papers, paper)
            if merged:
                aliases[paper['paperId']] = merged['paperId']
        # Rediscovery may learn a DOI for an existing arXiv-only row. Stored
        # identities stay stable, so edges must use the retained IDs as well.
        projection['citation_edges'] = [{**edge,
            'from': aliases.get(edge['from'], edge['from']),
            'to': aliases.get(edge['to'], edge['to'])}
            for edge in projection['citation_edges']]
        validated = literature_projection({'papers': papers, **projection})
        record.update({k: v for k, v in validated.items() if k != 'papers'})
    if event and event.get('type') == 'paper_done':
        merge_paper(papers, event.get('result') or {})
    if event and event.get('type') in ('stage_done', 'stage_skipped',
                                       'stage_checkpoint_invalidated'):
        checkpoint = os.path.join(task['workdir'], 'pipeline_state.json')
        if os.path.isfile(checkpoint):
            if os.path.getsize(checkpoint) > MAX_RECORD_BYTES:
                raise ValueError('research checkpoint exceeds 8 MiB')
            with open(checkpoint, encoding='utf-8') as source:
                record['checkpoint'] = json.load(source)
    # A compact index avoids loading checkpoint/model bodies for the landing page.
    summary = {k: deepcopy(record[k]) for k in
               ('schema_version', 'task_id', 'direction', 'lang', 'status',
                'created_at', 'updated_at', 'research_progress')}
    for field in ('error', 'failed_stage', 'evolution_run_id', 'execution_limit_reason'):
        if field in record:
            summary[field] = record[field]
    index_key = f"{task['user_id']}:{9999999999999 - record['created_at']:013d}:{task['task_id']}"
    _commit(task, key, record, index_key, summary)
    task['_durable_research_record'] = record
    return record


def _commit(task, key, record, index_key, summary):
    """Write snapshot + index with bounded retry of storage-marked transients.

    Both puts recompute from the last acknowledged ``_durable_research_record``
    on the caller side, so repeating the pair after a mid-pair failure is
    idempotent (same keys, same values, fresh command ids).
    """
    for attempt in range(len(_COMMIT_RETRY_DELAYS_S) + 1):
        try:
            _put(NAMESPACE, key, record)
            _put(INDEX_NAMESPACE, index_key, summary)
            return
        except StorageError as exc:
            if not exc.retryable or attempt >= len(_COMMIT_RETRY_DELAYS_S):
                raise
            delay = max(_COMMIT_RETRY_DELAYS_S[attempt],
                        (exc.retry_after_ms or 0) / 1000.0)
            logger.warning('[Research:Records] transient record write failure '
                           '(attempt %d/%d): %s — retrying in %.1fs',
                           attempt + 1, len(_COMMIT_RETRY_DELAYS_S) + 1,
                           exc, delay)
            abort_event = task.get('abort_event')
            if abort_event is not None:
                if abort_event.wait(delay):
                    raise
            else:
                time.sleep(delay)


def _project(record, *, user_id):
    from .runtime import _research_runtime
    value = deepcopy(record)
    live = _research_runtime.get(value['task_id'])
    if live and int(live.get('user_id') or 0) == int(user_id):
        value['status'] = live['status']
        from .runtime import normalize_evolution_binding
        binding = normalize_evolution_binding((live.get('config') or {}).get('evolution'))
        if binding:
            if value.get('evolution_run_id') not in (None, binding['run_id']):
                raise ValueError('research evolution binding differs from live task')
            value['evolution_run_id'] = binding['run_id']

        if live['status'] == 'error' and live.get('error'):
            value['error'] = _error_text(live['error'])
    elif value['status'] in ACTIVE:
        value['status'] = 'interrupted'
    value['resumable'] = live is None and value['status'] in ('interrupted', 'error', 'aborted')
    if value['resumable'] and value.get('evolution_run_id'):
        try:
            _bound_run(value['evolution_run_id'], task_id=value['task_id'], user_id=user_id)
        except ValueError:
            value['resumable'] = False
    if value['resumable']:
        from .budget_binding import read_model_budget
        if read_model_budget(value['task_id'], user_id=user_id) is not None:
            from .discovery_owner import can_recover_discovery_execution
            value['resumable'] = can_recover_discovery_execution(value['task_id'], user_id=user_id)
    _recover_evidence(value)
    value.pop('checkpoint', None)
    value.pop('request', None)
    return value


def _bound_run(run_id, *, task_id, user_id):
    from lib.evolution.runs import get_run
    run = get_run(run_id, user_id=user_id)
    if (not run or run.get('domain') != 'research' or run.get('status') != 'executing'
            or run.get('task_id') != task_id):
        raise ValueError('owning evolution run is unavailable or no longer executing')
    return run


def resume_evolution_binding(record, *, task_id, user_id, manifest_evolution=None):
    """Recovery retains the exact live run; settled/mismatched runs never revive."""
    from .runtime import normalize_evolution_binding
    record = record or {}
    saved = (record.get('request') or {}).get('evolution')
    if saved is not None and manifest_evolution is not None and saved != manifest_evolution:
        raise ValueError('research evolution binding differs from manifest')
    binding = normalize_evolution_binding(saved if saved is not None else manifest_evolution)
    if record.get('evolution_run_id') and (not binding or binding['run_id'] != record['evolution_run_id']):
        raise ValueError('research evolution binding is missing or changed')
    if binding:
        run = _bound_run(binding['run_id'], task_id=task_id, user_id=user_id)
        if any(run.get(key) != binding[key] for key in ('goal_id', 'plan_id')):
            raise ValueError('research evolution binding does not match its run')
    return binding


def load_record(task_id, *, user_id, internal=False):
    row = _storage().query('record.get', {
        'namespace': NAMESPACE, 'key': _key(task_id, user_id)})
    if not row:
        return None
    record = row['value']
    return deepcopy(record) if internal else _project(record, user_id=user_id)


def load_completed_idea_source(task_id, *, user_id, idea_id):
    """One owned snapshot validates completed metadata and the exact selection."""
    source = load_record(task_id, user_id=user_id, internal=True)
    if (not isinstance(source, dict) or source.get('task_id') != task_id
            or source.get('status') not in ('done', 'degraded')
            or not isinstance(source.get('direction'),str) or not source['direction']
            or len(source['direction']) > 2000 or source.get('lang') not in ('en','zh')):
        raise ValueError('saved research source is unavailable or differs from the requested direction')
    result = source.get('result')
    if not isinstance(result, dict):
        raise ValueError('saved research source has no completed result')
    accepted = result.get('accepted', [])
    if not isinstance(accepted, list):
        raise ValueError('saved research source has no valid accepted idea list')
    matches = [idea for idea in accepted
               if isinstance(idea, dict) and str(idea.get('id') or '') == idea_id]
    if len(matches) != 1:
        raise ValueError('selected idea is missing or ambiguous in the saved research source')
    return source


def load_idea_source(task_id, *, user_id, direction, lang, idea_id):
    """Read one exact completed discovery source; no latest-direction fallback."""
    source = load_completed_idea_source(task_id,user_id=user_id,idea_id=idea_id)
    if source['direction'] != direction or source['lang'] != lang:
        raise ValueError('saved research source is unavailable or differs from the requested direction')
    return {**source['result'], 'found': True}


def list_records(*, user_id, limit=50):
    uid = require_user_id(user_id, context='research records owner')
    rows = _storage().query('record.list', {
        'namespace': INDEX_NAMESPACE, 'prefix': f'{uid}:',
        'limit': max(1, min(200, int(limit))),
    })
    return sorted((_project(row['value'], user_id=uid) for row in rows),
                  key=lambda r: r['updated_at'], reverse=True)


def restore_pipeline_checkpoint(record, workdir):
    """Restore acknowledged stage material before recovery task admission.

    Recipe stage validation remains authoritative for reuse. No checkpoint means
    legacy local recovery; a failed restoration must prevent worker dispatch.
    """
    if record and record.get('checkpoint'):
        from lib.json_store import write_json_atomic
        os.makedirs(workdir, exist_ok=True)
        write_json_atomic(os.path.join(workdir, 'pipeline_state.json'), record['checkpoint'])


def resume_record(task_id, *, user_id):
    """Serialize same-process admission before handing execution to TaskRuntime."""
    with _resume_lock:
        return _resume_record(task_id, user_id=user_id)


def _resume_record(task_id, *, user_id):
    """Explicit owner action only; never infer process restart auto-execution."""
    from .runtime import _research_runtime, _new_research_task
    from .engine import research_root, run_research_task
    record = load_record(task_id, user_id=user_id, internal=True)
    if record is None:
        raise ValueError('research record not found')
    live = _research_runtime.get(task_id)
    if live is not None:
        if int(live.get('user_id') or 0) != int(user_id):
            raise ValueError('research record not found')
        if live['status'] in ACTIVE:
            return {'task_id': task_id, 'deduped': True}
        # A terminal TaskRuntime identity cannot be revived. The caller can
        # start a new run; registry-loss recovery is the supported resume path.
        raise ValueError('task is still registered; start a new research run')
    if record['status'] not in (*ACTIVE, 'error', 'aborted'):
        raise ValueError('completed research cannot be resumed')
    binding = resume_evolution_binding(record, task_id=task_id, user_id=user_id)
    from .budget_binding import read_model_budget
    recovery_claim = None
    if read_model_budget(task_id, user_id=user_id) is not None:
        from .discovery_owner import recover_discovery_execution
        try:
            recovery_claim = recover_discovery_execution(task_id, user_id=user_id)
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
            raise ValueError('budgeted research requires explicit model account recovery: ' + str(error)) from error
        except ValueError as error:
            raise ValueError('budgeted research requires explicit model account recovery: ' + str(error)) from error
    workdir = os.path.join(research_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)
    restore_pipeline_checkpoint(record, workdir)
    task = _new_research_task(task_id, direction=record['direction'],
                             lang=record['lang'], workdir=workdir,
                             user_id=user_id, **{**record.get('request', {}), 'evolution': binding})
    task['_research_record_enabled'] = True
    task['_research_recovered'] = True
    if recovery_claim is not None:
        task['_research_recovery_claim'] = recovery_claim
    task['_durable_research_record'] = record
    task['research_progress'] = record.get('research_progress')
    try:
        # Birth makes cancellation able to signal this exact registry carrier.
        # Recheck after birth; the worker checks once more before the recipe.
        resume_evolution_binding(record, task_id=task_id, user_id=user_id)
        persist_record(task)
        _research_runtime.spawn(task_id, run_research_task, task)
    except Exception as exc:
        _research_runtime.finish(task_id, error=exc, error_context='research:resume')
        raise
    return {'task_id': task_id, 'deduped': False}
