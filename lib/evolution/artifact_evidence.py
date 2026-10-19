"""Explicit project snapshots; terminal outcomes own source sealing and verdicts.

Declarations persist complete bytes before acknowledgement; fingerprint streams
explicitly selected live file bytes, retaining read receipts without contents. Only a bound executor
may declare or fingerprint; reviewers read the terminal bundle through the existing source port.
No directory scan, model, new worker, implicit live-file reread or learning authority.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time

from lib.storage.errors import StorageError
from . import records
from .contracts import EvolutionContractError, EvolutionStateError
from .tool_evidence import PART_CHARS

MANIFESTS = 'evolution.artifact_manifests.v1'
PARTS = 'evolution.artifact_parts.v1'
FINGERPRINTS = 'evolution.artifact_fingerprints.v1'
SCOPE = 'declared_project_artifact_snapshots'


def _validate(value, definition):
    from .stage_outcomes import _validate as validate
    return validate(value, definition)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _key(task, owner):
    return records.entity_key(task, owner, kind='artifact manifest')


def _part_key(artifact, ordinal, owner):
    return records.entity_key(_sha(f'{artifact}:{ordinal}'.encode()), owner, kind='artifact part')


def _manifest(run, task, owner):
    value, version = records.get_versioned_record(MANIFESTS, _key(task['id'], owner))
    if value is None:
        value = {'schema_version': 1, 'run_id': run['run_id'], 'task_id': task['id'],
                 'project_path': run['project_path'], 'closed': False, 'snapshots': []}
    _validate(value, 'artifact_manifest')
    if (value['run_id'] != run['run_id'] or value['task_id'] != task['id']
            or value['project_path'] != run['project_path'] or task['_userId'] != owner):
        raise EvolutionStateError('artifact manifest owner/project binding changed')
    for snapshot in value['snapshots']:
        expected = _sha(_json({'run_id':run['run_id'], 'task_id':task['id'],
            'path':snapshot['relative_path'], 'bytes_sha256':snapshot['content_sha256']}).encode())
        if (snapshot['run_id'] != run['run_id'] or snapshot['task_id'] != task['id']
                or snapshot['artifact_id'] != expected
                or snapshot['content_parts'] != (snapshot['content_chars']+PART_CHARS-1)//PART_CHARS):
            raise EvolutionStateError('artifact snapshot scope/identity changed')
    return value, version


@contextmanager
def _open_project_file(project, path):
    """Open every component relative to an owned root without following symlinks."""
    pieces = PurePosixPath(path).parts
    if (not pieces or path != '/'.join(pieces) or path.startswith('/')
            or any(p in ('', '.', '..') or '\\' in p or '\x00' in p for p in pieces)
            or any(p.startswith('.env') or p in {'.git', '.tofu', 'node_modules', '__pycache__',
                '.project_sessions', '.encryption.key'} for p in pieces)
            or any(p.lower() in {'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519'} for p in pieces)):
        raise EvolutionStateError('artifact must be a project-relative non-runtime file')
    root = Path(project)
    from runtime_path_policy import get_runtime_base
    runtime = Path(get_runtime_base())
    target = root.joinpath(*pieces)
    private_roots = [runtime/name for name in ('data','logs','uploads')] + [runtime/'lib/.project_sessions']
    if any(target == directory or target.is_relative_to(directory) for directory in private_roots):
        raise EvolutionStateError('artifact cannot capture application runtime storage')
    # project_path was resolved at run admission; reject replacement of its root.
    if str(root.resolve(strict=True)) != str(root):
        raise EvolutionStateError('artifact project root changed')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for piece in root.parts[1:]:
            next_dir = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory);directory = next_dir
    except BaseException:
        os.close(directory);raise
    handle = None
    try:
        for piece in pieces[:-1]:
            next_dir = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory);directory = next_dir
        handle = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvolutionStateError('artifact must be a regular private project file')
        yield handle, before
    finally:
        if handle is not None:os.close(handle)
        os.close(directory)


def _stable_file(before, after, count):
    return count == before.st_size and all(getattr(before, field) == getattr(after, field)
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _read_file(project, path):
    with _open_project_file(project, path) as (handle, before):
        chunks = []
        while True:
            block = os.read(handle, 65536)
            if not block:break
            chunks.append(block)
        raw = b''.join(chunks)
        if not _stable_file(before, os.fstat(handle), len(raw)):
            raise EvolutionStateError('artifact changed during capture; declare a stable generation')
        return raw


def _frozen_scope_digest(run, field):
    from lib.research.observation_audit import normalize_measurement_scope
    try:
        scope = normalize_measurement_scope((run.get('scientific_protocol') or {}).get('measurement_scope'))
        return scope[field]
    except (ValueError, KeyError) as exc:
        raise EvolutionStateError('fingerprint requires the requested digest in the frozen measurement scope') from exc


def fingerprint(task, path, *, scope_field=None):
    """Hash stable project bytes in bounded memory; no content snapshot or replay."""
    from .coordination import authorize_task
    run, role, _ = authorize_task(task)
    if role != 'executing' or not run.get('project_path'):
        raise EvolutionStateError('file fingerprints require the current project executor')
    expected = _frozen_scope_digest(run, scope_field) if scope_field is not None else None
    _assert_capture_active(task)
    digest = hashlib.sha256();count = 0
    with _open_project_file(run['project_path'], path) as (handle, before):
        while True:
            _assert_capture_active(task)
            block = os.read(handle, 1024 * 1024)
            if not block:break
            count += len(block);digest.update(block)
            if count > before.st_size:
                raise EvolutionStateError('file changed during fingerprinting')
        if not _stable_file(before, os.fstat(handle), count):
            raise EvolutionStateError('file changed during fingerprinting')
    current, current_role, _ = authorize_task(task)
    if current['run_id'] != run['run_id'] or current_role != role or current.get('project_path') != run['project_path']:
        raise EvolutionStateError('file fingerprint authority changed')
    if scope_field is not None and _frozen_scope_digest(current, scope_field) != expected:
        raise EvolutionStateError('file fingerprint frozen scope changed during read')
    _assert_capture_active(task)
    result = _validate({'run_id':run['run_id'], 'task_id':task['id'], 'path':path,
        'algorithm':'sha256', 'sha256':digest.hexdigest(), 'bytes':count,
        **({'scope_field':scope_field, 'expected_sha256':expected,
            'matches_precommit':digest.hexdigest() == expected} if scope_field is not None else {}),
        'evidence_status':'Observed file bytes at read time; no content snapshot, execution or reproduction proof.'},
        'artifact_fingerprint_result')
    _retain_fingerprint(run, task, result)
    return result


def _content(snapshot, owner):
    chunks = []
    for ordinal in range(snapshot['content_parts']):
        part = records.get_record(PARTS, _part_key(snapshot['artifact_id'], ordinal, owner))
        if part is None:
            raise EvolutionStateError('declared artifact snapshot is incomplete')
        _validate(part, 'artifact_snapshot_part')
        if (part['artifact_id'] != snapshot['artifact_id'] or part['run_id'] != snapshot['run_id']
                or part['task_id'] != snapshot['task_id'] or part['ordinal'] != ordinal
                or len(part['text']) != min(PART_CHARS, snapshot['content_chars'] - ordinal * PART_CHARS)
                or _sha(part['text'].encode()) != part['part_sha256']):
            raise EvolutionStateError('declared artifact snapshot part changed')
        chunks.append(part['text'])
    content = ''.join(chunks)
    if len(content) != snapshot['content_chars'] or _sha(content.encode()) != snapshot['encoded_sha256']:
        raise EvolutionStateError('declared artifact snapshot digest changed')
    raw = content.encode() if snapshot['encoding'] == 'utf-8' else base64.b64decode(content, validate=True)
    if len(raw) != snapshot['bytes'] or _sha(raw) != snapshot['content_sha256']:
        raise EvolutionStateError('declared artifact original bytes changed')
    return content


def _entry(snapshot, content):
    return _json({key: snapshot[key] for key in ('artifact_id', 'relative_path', 'captured_at',
        'encoding', 'bytes', 'content_sha256', 'state')} | {'content': content}) + '\n'


def _assert_capture_active(task):
    from lib.agent_core.execution_limits import execution_limit_reason
    aborted = task.get('abort_event')
    if task.get('aborted') or (aborted is not None and aborted.is_set()):
        raise EvolutionStateError('artifact executor cancelled')
    reason = execution_limit_reason(task)
    if reason:
        raise EvolutionStateError(reason)


def declare(task, args):
    """Capture a selected file or a bounded set of independent generations.

    A batch is not a filesystem transaction. Every entry reports its own durable
    acknowledgement; later failures neither roll back earlier snapshots nor
    imply that their content or the unrequested files have been verified.
    """
    from .coordination import authorize_task
    _validate(args, 'artifact_declare_request')
    if 'fingerprint_path' in args:
        return fingerprint(task, args['fingerprint_path'], scope_field=args.get('fingerprint_scope_field'))
    if 'delivery' in args or 'delivery_source' in args:
        raise EvolutionStateError('explicit delivery requires the live tool handler')
    if 'path' in args:
        return _declare_path(task, args['path'])
    run, role, _ = authorize_task(task)
    if (role != 'executing' or not run.get('project_path')
            or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}):
        raise EvolutionStateError('artifact declarations require the current project executor')
    _assert_capture_active(task)
    manifest, _ = _manifest(run, task, task['_userId'])
    if manifest['closed']:
        raise EvolutionStateError('artifact declarations already closed')
    results = []
    for path in args['paths']:
        try:
            results.append({'path': path, 'snapshot': _declare_path(task, path)})
        except (EvolutionStateError, StorageError, OSError, ValueError, TypeError) as exc:
            results.append({'path': path, 'error': str(exc)[:400] or type(exc).__name__})
    return _validate({'run_id': run['run_id'], 'task_id': task['id'],
        'atomic': False, 'complete': all('snapshot' in item for item in results),
        'results': results}, 'artifact_declare_batch_result')


def _declare_path(task, path):
    from .coordination import authorize_task
    from .stage_outcomes import _put_once
    run, role, _ = authorize_task(task)
    if role != 'executing' or task.get('status') in {'done', 'error', 'aborted', 'interrupted'} or not run.get('project_path'):
        raise EvolutionStateError('artifact declarations require the current project executor')
    _assert_capture_active(task)
    manifest, _ = _manifest(run, task, task['_userId'])
    if manifest['closed']:raise EvolutionStateError('artifact declarations already closed')
    raw = _read_file(run['project_path'], path)
    try:content, encoding = raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:content, encoding = base64.b64encode(raw).decode('ascii'), 'base64'
    identity = _sha(_json({'run_id':run['run_id'], 'task_id':task['id'], 'path':path,
                          'bytes_sha256':_sha(raw)}).encode())
    snapshot = {'artifact_id':identity, 'run_id':run['run_id'], 'task_id':task['id'],
        'relative_path':path, 'captured_at':int(time.time()), 'encoding':encoding,
        'bytes':len(raw), 'content_sha256':_sha(raw), 'encoded_sha256':_sha(content.encode()),
        'content_chars':len(content), 'content_parts':(len(content)+PART_CHARS-1)//PART_CHARS,
        'entry_bytes':0, 'state':'collecting'}
    snapshot['entry_bytes'] = len(_entry(snapshot, content).encode()) + 64;_validate(snapshot, 'artifact_snapshot')
    # Reserve the owned generation before writing any potentially orphaned parts.
    for _ in range(4):
        _assert_capture_active(task)
        current, role, _ = authorize_task(task)
        if (role != 'executing' or current['project_path'] != run['project_path']
                or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}):
            raise EvolutionStateError('artifact executor authority changed')
        manifest, version = _manifest(run, task, task['_userId'])
        if manifest['closed']:raise EvolutionStateError('artifact declarations already closed')
        old = next((s for s in manifest['snapshots'] if s['artifact_id'] == identity), None)
        if old:
            snapshot = old
            if old['state'] == 'sealed':
                _content(old, task['_userId']);return old
            break
        snapshots = manifest['snapshots'] + [snapshot]
        updated = {**manifest, 'snapshots': snapshots};_validate(updated, 'artifact_manifest')
        try:
            records.put_record(MANIFESTS, _key(task['id'], task['_userId']), updated,
                               kind='artifact manifest', expected_version=version)
            break
        except StorageError as exc:
            if exc.code != 'database_conflict':raise
    else:raise EvolutionStateError('artifact reservation changed concurrently')
    for ordinal, offset in enumerate(range(0, len(content), PART_CHARS)):
        _assert_capture_active(task)
        text = content[offset:offset+PART_CHARS]
        part = {'artifact_id':identity, 'run_id':run['run_id'], 'task_id':task['id'],
                'ordinal':ordinal, 'text':text, 'part_sha256':_sha(text.encode())}
        _validate(part, 'artifact_snapshot_part')
        _put_once(PARTS, _part_key(identity, ordinal, task['_userId']), part, 'artifact snapshot part')
    _content(snapshot, task['_userId'])
    for _ in range(4):
        _assert_capture_active(task)
        current, role, _ = authorize_task(task)
        if role != 'executing' or task.get('status') in {'done','error','aborted','interrupted'}:
            raise EvolutionStateError('artifact executor authority changed')
        manifest, version = _manifest(run, task, task['_userId'])
        if manifest['closed']:raise EvolutionStateError('artifact declarations already closed')
        updated = {**manifest, 'snapshots': [({**item,'state':'sealed'}
            if item['artifact_id'] == identity else item) for item in manifest['snapshots']]}
        try:
            records.put_record(MANIFESTS, _key(task['id'], task['_userId']), updated,
                               kind='artifact manifest', expected_version=version)
            return next(item for item in updated['snapshots'] if item['artifact_id'] == identity)
        except StorageError as exc:
            if exc.code != 'database_conflict':raise
    raise EvolutionStateError('artifact declaration changed concurrently; inspect before retry')



def _fingerprint_key(task_id, head, owner):
    return records.entity_key(_sha(f'{task_id}:{head}'.encode()), owner, kind='file fingerprint')


def _retain_fingerprint(run, task, result):
    from .stage_inputs import _put_immutable
    from .coordination import authorize_task
    def check_authority():
        current, role, _ = authorize_task(task)
        if role != 'executing' or current['run_id'] != run['run_id'] or current.get('project_path') != run['project_path']:
            raise EvolutionStateError('file fingerprint publication authority changed')
        if result.get('scope_field') and _frozen_scope_digest(current, result['scope_field']) != result['expected_sha256']:
            raise EvolutionStateError('file fingerprint frozen scope changed before publication')
    observed_at_ns = time.time_ns()
    for _ in range(4):
        _assert_capture_active(task)
        check_authority()
        manifest, version = _manifest(run, task, task['_userId'])
        if manifest['closed']:
            raise EvolutionStateError('artifact fingerprints already closed')
        previous = manifest.get('fingerprint_log', {'head':'', 'count':0})
        node = {'schema_version':1, 'run_id':run['run_id'], 'task_id':task['id'],
            'project_path':run['project_path'], 'sequence':previous['count'] + 1,
            'previous':previous['head'], 'observed_at_ns':observed_at_ns, 'fingerprint':result}
        _validate(node, 'artifact_fingerprint_node')
        head = _sha(_json(node).encode())
        key = _fingerprint_key(task['id'], head, task['_userId'])
        try:
            _put_immutable(FINGERPRINTS, key, node, kind='file fingerprint')
        except StorageError:
            if records.get_versioned_record(FINGERPRINTS, key) != (node, 1):
                raise
        retained, node_version = records.get_versioned_record(FINGERPRINTS, key)
        if type(node_version) is not int or node_version != 1 or retained != node:
            raise StorageError('database_integrity', 'File fingerprint was not exactly acknowledged')
        updated = {**manifest, 'fingerprint_log':{'head':head, 'count':node['sequence']}}
        _validate(updated, 'artifact_manifest')
        def publication_observed():
            current, current_version = _manifest(run, task, task['_userId'])
            if current_version < version + 1:
                return False
            reference = current.get('fingerprint_log', {'head':'', 'count':0})
            current_head, sequence = reference['head'], reference['count']
            while sequence >= node['sequence']:
                if sequence == node['sequence']:
                    return current_head == head
                retained, retained_version = records.get_versioned_record(FINGERPRINTS,
                    _fingerprint_key(task['id'], current_head, task['_userId']))
                if retained is None:
                    return False
                _validate(retained, 'artifact_fingerprint_node')
                if (retained_version != 1 or _sha(_json(retained).encode()) != current_head or retained['sequence'] != sequence
                        or any(retained[name] != current[name] for name in ('run_id','task_id','project_path'))):
                    return False
                current_head, sequence = retained['previous'], sequence - 1
            return False
        _assert_capture_active(task)
        check_authority()
        try:
            records.put_record(MANIFESTS, _key(task['id'], task['_userId']), updated,
                kind='artifact manifest', expected_version=version)
        except StorageError as exc:
            if publication_observed():
                pass
            elif exc.code == 'database_conflict':
                continue
            else:
                raise
        if not publication_observed():
            raise StorageError('database_integrity', 'Fingerprint manifest was not exactly acknowledged')
        _assert_capture_active(task)
        check_authority()
        return
    raise EvolutionStateError('artifact fingerprint publication changed concurrently')


def _fingerprint_lines(manifest, owner):
    reference = manifest.get('fingerprint_log', {'head':'', 'count':0})
    head, sequence = reference['head'], reference['count']
    while sequence:
        node, version = records.get_versioned_record(FINGERPRINTS,
            _fingerprint_key(manifest['task_id'], head, owner))
        if node is None:
            raise EvolutionStateError('Retained file fingerprint is unavailable')
        _validate(node, 'artifact_fingerprint_node')
        if (type(version) is not int or version != 1 or _sha(_json(node).encode()) != head
                or node['sequence'] != sequence or any(node[name] != manifest[name]
                    for name in ('run_id','task_id','project_path'))):
            raise EvolutionStateError('Retained file fingerprint identity changed')
        report = node['fingerprint']
        if (any(report[name] != node[name] for name in ('run_id','task_id'))
                or ('matches_precommit' in report and report['matches_precommit'] != (report['sha256'] == report['expected_sha256']))):
            raise EvolutionStateError('Retained file fingerprint report binding changed')
        yield _json({'kind':'file_fingerprint', 'sequence':sequence,
            'observed_at_ns':node['observed_at_ns'], **node['fingerprint']}) + '\n'
        head, sequence = node['previous'], sequence - 1
    if head:
        raise EvolutionStateError('Retained file fingerprint chain exceeds its count')


def freeze(run, task, *, user_id):
    """Close declarations before freezing terminal metadata, including an empty set."""
    if task['config']['evolution']['stage'] != 'executing' or not run.get('project_path'):
        return None
    for _ in range(4):
        manifest, version = _manifest(run, task, user_id)
        if manifest['closed']:return manifest
        snapshots = []
        for snapshot in manifest['snapshots']:
            try:_content(snapshot, user_id);state = 'sealed'
            except (EvolutionContractError, EvolutionStateError, ValueError):state = 'unavailable'
            snapshots.append({**snapshot,'state':state})
        updated = {**manifest, 'closed':True, 'snapshots':snapshots}
        try:
            records.put_record(MANIFESTS, _key(task['id'], user_id), updated,
                               kind='artifact manifest', expected_version=version)
            return updated
        except StorageError as exc:
            if exc.code != 'database_conflict':raise
    raise EvolutionStateError('artifact declarations did not close')


def manifest_for(task, owner):
    run = {'run_id':task['config']['evolution']['run_id'],
           'project_path':task['config'].get('projectPath','')}
    manifest, _ = _manifest(run, task, owner)
    if not manifest['closed']:raise EvolutionStateError('artifact manifest is not frozen')
    return manifest


def lines(manifest, owner):
    yield from _fingerprint_lines(manifest, owner)
    for snapshot in manifest['snapshots']:
        content = None if snapshot['state'] == 'unavailable' else _content(snapshot, owner)
        line = _entry(snapshot, content)
        if len(line.encode()) > snapshot['entry_bytes']:
            raise EvolutionStateError('artifact snapshot entry identity changed')
        yield line


def describe(manifest, owner):
    if not manifest or (not manifest['snapshots'] and not manifest.get('fingerprint_log')):return None
    if not manifest['closed']:raise EvolutionStateError('artifact declarations are not frozen')
    digest = hashlib.sha256();size = 0
    for line in lines(manifest, owner):digest.update(line.encode());size += len(line)
    result = {'content_chars':size, 'content_parts':(size+PART_CHARS-1)//PART_CHARS,
        'content_sha256':digest.hexdigest(), 'artifact_count':len(manifest['snapshots']) + manifest.get('fingerprint_log', {}).get('count', 0),
        'raw_bytes':sum(s['bytes'] for s in manifest['snapshots']), 'scope':SCOPE + '_and_fingerprints' if manifest.get('fingerprint_log') else SCOPE,
        'manifest_sha256':_sha(_json(manifest).encode()),
        'complete':all(s['state']=='sealed' for s in manifest['snapshots'])}
    return _validate(result, 'artifact_evidence_descriptor')


def chunks(manifest, owner):
    pending = ''
    for line in lines(manifest, owner):
        offset = 0
        while offset < len(line):
            take = min(PART_CHARS - len(pending), len(line)-offset)
            pending += line[offset:offset+take];offset += take
            if len(pending) == PART_CHARS:yield pending;pending = ''
    if pending:yield pending


def build(ctx):
    binding = ctx.cfg.get('evolution') or {}
    if binding.get('stage') != 'executing' or not binding.get('run_id') or not binding.get('project_path') or not ctx.cfg.get('projectEnabled') or not ctx.project_enabled:return []
    from .contracts import load_schema
    definitions = load_schema('evolution_run_v1.schema.json')['$defs']
    parameters = deepcopy(definitions['artifact_declare_request'])
    parameters['properties']['delivery_source'] = deepcopy(definitions['artifact_delivery_source'])
    # Keep provider-facing parameters self-contained and unconditional, as for
    # evolution_source. The handler owns strict capture-or-delivery validation.
    parameters.pop('oneOf', None)
    return [{'type':'function','function':{'name':'evolution_artifact',
        'description':'Freeze explicitly selected project-relative generated files before independent review. '
            'Use path for one file or paths for all selected unique files in one call. Batch entries capture independently, not atomically; '
            'check every entry and complete=false failures. Successful snapshots persist despite sibling failures. '
            'A stable useful draft may be captured without delivery; unfinished checks stay explicit. '
            'A later edit needs another declaration; old generations remain. Bytes are complete, correctness and binary interpretation unverified. '
            'After observed verification, optionally supply delivery with your final reply, as the sole tool call in the round. '
            'For an exact file-body reply, first declare its final generation, then '
            'call with delivery_source={artifact_id,content_sha256} only, using the '
            'saved snapshot identity. Its sealed UTF-8 body is delivered unchanged '
            'without model recopy, from the complete captured file; binary or '
            'blank replies refuse. Do not include path, paths or delivery with delivery_source. '
            'This closes declarations and requests ordinary task completion without another model round; independent review still decides acceptance. '
            'Use fingerprint_path alone to compute SHA-256 of a stable selected file with bounded memory, without retaining source content; read receipts survive message compaction, no contents are retained and this proves neither execution nor reproduction. Optional fingerprint_scope_field compares the raw-file hash against that field of the frozen measurement_scope of this run; you cannot replace its expected value. A mismatch is an observed difference, not a failed process or authority to replay. Snapshots retain complete selected files and generations; runtime/secret paths and symlinks refused. No external write.',
        'parameters':parameters}}]


def handle(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
           cfg, project_path, project_enabled, all_tools=None):
    from .tool_handler import _entry as result_entry
    try:
        if 'delivery' in (fn_args or {}) or 'delivery_source' in (fn_args or {}):
            from .artifact_delivery import prepare
            result = prepare(task, fn_args, call_id=tc_id,
                             round_num=round_entry['llmRound'])
        else:
            result = declare(task, fn_args or {})
    except (EvolutionContractError, EvolutionStateError, StorageError, OSError, ValueError, TypeError) as exc:
        return [result_entry(fn_name, 'artifact capture refused', str(exc)[:400], fetched=False)]
    complete = result.get('complete', True)
    title = ('project file fingerprint observed' if 'fingerprint_path' in (fn_args or {}) else
             'artifact snapshot saved' if 'path' in (fn_args or {}) else
             'artifact snapshots saved' if complete else 'artifact capture partial')
    if result.get('matches_precommit') is False:
        title = 'project file differs from frozen protocol'
    if result.get('delivery_prepared'):
        title = 'artifact delivery prepared; review pending'
    return [result_entry(fn_name, title,
                         _json(result), fetched=complete)]
