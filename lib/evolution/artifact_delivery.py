"""Prepare executor-authored delivery intent bound to sealed project generations.

The manifest CAS closes declarations and preserves the exact final reply. The
generic task port owns live acknowledgement/stream/settlement checks; ordinary
terminal checkpoints own result durability and independent review handoff.
Intent without the live port never authorizes cold completion or replay.
"""
from __future__ import annotations

from copy import deepcopy

from lib.storage.errors import StorageError
from lib.tasks_pkg.tool_delivery import (
    arguments_digest, bind_delivery, require_current_sole_call,
)
from . import artifact_evidence as artifacts, records
from .contracts import EvolutionContractError, EvolutionStateError
from .coordination import authorize_task


def _current(task):
    run, role, _ = authorize_task(task)
    if (role != 'executing' or not run.get('project_path')
            or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}):
        raise EvolutionStateError('delivery requires the current project executor')
    artifacts._assert_capture_active(task)
    return run


def _source_content(manifest, source, owner):
    snapshot = next((item for item in manifest['snapshots']
                     if item['artifact_id'] == source['artifact_id']), None)
    if (snapshot is None or snapshot['state'] != 'sealed'
            or snapshot['content_sha256'] != source['content_sha256']):
        raise EvolutionStateError('delivery source requires an exact owned sealed generation')
    if snapshot['encoding'] != 'utf-8':
        raise EvolutionStateError('delivery source must be UTF-8 text')
    content = artifacts._content(snapshot, owner)
    if not content.strip():
        raise EvolutionStateError('delivery source must contain a nonblank final reply')
    return content


def prepare(task: dict, arguments: dict, *, call_id: str, round_num: int) -> dict:
    artifacts._validate(arguments, 'artifact_declare_request')
    require_current_sole_call(task, call_id, 'evolution_artifact', arguments)
    owner = task['_userId']
    run = _current(task)
    manifest, _ = artifacts._manifest(run, task, owner)
    source = arguments.get('delivery_source')
    content = (_source_content(manifest, source, owner) if source
               else arguments['delivery'])
    intent = {'tool_call_id':call_id, 'round_num':round_num,
        'arguments_sha256':arguments_digest(arguments),
        **({'delivery_source':source} if source else {'content':content})}
    artifacts._validate(intent, 'artifact_delivery_intent')
    if manifest.get('delivery_intent') != intent:
        if manifest['closed']:
            raise EvolutionStateError('artifact declarations closed with another delivery identity')
        if source is None:
            selected = artifacts.declare(task, {k:v for k,v in arguments.items() if k != 'delivery'})
            if selected.get('complete') is False:
                # Partial captures remain immutable evidence; no finish intent is committed.
                return selected
        for _ in range(4):
            run = _current(task)
            manifest, version = artifacts._manifest(run, task, owner)
            if manifest['closed']:
                if manifest.get('delivery_intent') == intent:
                    break
                raise EvolutionStateError('artifact declarations changed during delivery')
            if not manifest['snapshots'] or any(s['state'] != 'sealed' for s in manifest['snapshots']):
                raise EvolutionStateError('delivery requires every declared generation to be complete')
            for snapshot in manifest['snapshots']:
                artifacts._content(snapshot, owner)
            if source and _source_content(manifest, source, owner) != content:
                raise EvolutionStateError('delivery source changed during preparation')
            updated = {**manifest, 'closed':True, 'delivery_intent':intent}
            artifacts._validate(updated, 'artifact_manifest')
            try:
                records.put_record(artifacts.MANIFESTS, artifacts._key(task['id'], owner),
                    updated, kind='artifact manifest', expected_version=version)
                manifest = updated
                break
            except StorageError as exc:
                if exc.code != 'database_conflict':
                    raise
        else:
            raise EvolutionStateError('delivery intent changed concurrently')
    descriptor = artifacts.describe(manifest, owner)
    if not descriptor or not descriptor['complete']:
        raise EvolutionStateError('delivery requires complete frozen artifact evidence')
    original = deepcopy(manifest)

    def verify():
        try:
            current, _ = artifacts._manifest(_current(task), task, owner)
            return current == original and artifacts.describe(current, owner) == descriptor
        except (EvolutionContractError, EvolutionStateError, StorageError, OSError, ValueError, TypeError):
            return False

    acknowledgement = 'delivery_intent:' + artifacts._sha(artifacts._json(intent).encode())
    bind_delivery(task, call_id=call_id, name='evolution_artifact', round_num=round_num,
        arguments=arguments, content=content, acknowledgement=acknowledgement,
        verify=verify)
    return {'run_id':run['run_id'], 'task_id':task['id'], 'complete':True,
        'delivery_prepared':True, 'acknowledgement':acknowledgement,
        **({'delivery_source':source, 'delivery_chars':len(content),
            'delivery_sha256':artifacts._sha(content.encode())} if source else {}),
        'artifact_evidence':descriptor, 'acceptance':'pending independent review'}
