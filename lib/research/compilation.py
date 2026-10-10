"""Compile exact Research workspace sources through the shared paper service.

Entry: compile_workspace holds an artifact lease through the workspace CAS.
Only this application boundary produces CompilationResult. Model responses
and capability-tool success are not compilation evidence. Workspace save retains
the leased successful artifacts before committing their verified reference.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from lib.identity import require_user_id
from lib.llm_errors import AbortedError
from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.contract import limit
from lib.paper.builds.errors import BuildError
from lib.paper.builds.records import SourceRecords
from lib.paper.builds.runtime import get_scheduler
from lib.paper.builds.service import start_record_build
from lib.paper.builds.sources import manifest_digest

from .program import source_tree_digest
from .persistence import research_direction_hash


def document_identity(direction: str, lang: str, owner_user_id: int) -> str:
    key = ['research', require_user_id(owner_user_id), research_direction_hash(direction),
           'zh' if lang == 'zh' else 'en']
    return hashlib.sha256(json.dumps(key, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class CompilationResult:
    owner_user_id: int
    document_id: str
    source_digest: str
    projection: dict
    _lease_active: threading.Event
    _cancel: object
    artifact_build: CompiledBuild | None = None

    def ensure_live(self) -> None:
        if self._cancel.is_set():
            raise AbortedError('Research compilation cancelled before commit')
        if not self._lease_active.is_set():
            raise ValueError('Research compilation artifact lease has ended')

    def for_workspace(self, direction: str, lang: str, workspace: dict, *, owner_user_id: int) -> dict:
        self.ensure_live()
        if (owner_user_id != self.owner_user_id
                or self.document_id != document_identity(direction, lang, owner_user_id)
                or self.source_digest != source_tree_digest(workspace.get('source_files'))):
            raise ValueError('Research build does not match this owner, workspace and source revision')
        return copy.deepcopy(self.projection)

    @contextmanager
    def retain_artifacts(self, repository):
        self.ensure_live()
        try:
            with repository.prepare(self.artifact_build, cancel=self._cancel) as proof:
                self.ensure_live()
                yield proof
        except BuildError:
            # A cancellation during chunk upload retains the Research action's
            # abort semantics rather than becoming a generic compilation error.
            self.ensure_live()
            raise


@contextmanager
def compile_workspace(workspace: dict, *, direction: str, lang: str, owner_user_id: int,
                      task_id: str, cancel, scheduler=None):
    """Yield a verified result; cancellation detaches exactly this consumer."""
    owner = require_user_id(owner_user_id, context='research compilation owner')
    records = SourceRecords.from_text(workspace.get('source_files') or [])
    digest = source_tree_digest(workspace['source_files'])
    document_id = document_identity(direction, lang, owner)
    subscriber = 'research:' + task_id
    scheduler = scheduler or get_scheduler()
    if cancel.is_set():
        raise AbortedError('Research compilation cancelled')
    task = start_record_build(records, owner_user_id=owner, document_id=document_id,
                              subscriber_id=subscriber, request_key=digest, scheduler=scheduler)
    deadline = time.monotonic() + limit('compile_timeout_seconds') * (scheduler.budget.queue_capacity + 1)
    renewal = time.monotonic() + 30
    try:
        while task['status'] in {'pending', 'running'}:
            if cancel.wait(0.1):
                raise AbortedError('Research compilation cancelled')
            now = time.monotonic()
            if now > deadline:
                raise BuildError('compile_timeout', 'Research compilation queue deadline expired')
            if now >= renewal:
                scheduler.renew(task['id'], owner_user_id=owner, subscriber_id=subscriber)
                renewal = now + 30
        if cancel.is_set() or task['status'] == 'aborted':
            raise AbortedError('Research compilation cancelled')
        result = task.get('result') or {}
        receipt = result.get('receipt')
        if not receipt:
            error = task.get('error') or {}
            raise BuildError('compile_failed', str(error.get('message') if isinstance(error, dict) else error)
                             or 'Research build did not produce a receipt')
        with scheduler.cache.lease(owner, document_id, receipt['build_id']) as build:
            receipt = build.receipt
            if (receipt['owner_user_id'] != owner or receipt['document_id'] != document_id
                    or receipt['source_digest'] != manifest_digest(records.manifest)):
                raise BuildError('build_identity', 'Research build does not match the frozen workspace sources')
            declared = {row['name']: row for row in receipt['artifacts']}
            for name, metadata in declared.items():
                if artifact_metadata(build.directory / name) != metadata:
                    raise BuildError('artifact_changed', 'Research build artifact failed integrity verification')
            success = receipt['status'] == 'success' and receipt['version_verified'] and not result.get('superseded')
            reference = copy.deepcopy((workspace.get('compilation') or {}).get('build'))
            if success:
                if 'document.pdf' not in declared or 'sources.json' not in declared:
                    raise BuildError('artifact_missing', 'Research build has no verified PDF and source manifest')
                if json.loads((build.directory / 'sources.json').read_text()) != records.manifest:
                    raise BuildError('source_changed', 'Research artifact source manifest differs from the workspace')
                reference = {'document_id': document_id, 'build_id': receipt['build_id'],
                             'source_digest': digest, 'manifest_digest': receipt['source_digest'],
                             'pdf_sha256': declared['document.pdf']['sha256']}
            details = '\n'.join(row['message'] for row in receipt['diagnostics'])[:4000]
            projection = {'mode': 'paper_build', 'status': 'passing' if success else 'failing',
                          'source_digest': digest, 'engine': receipt['engine'],
                          'compiled_at': receipt['completed_at'] // 1000, 'build': reference,
                          'detail': details or ('' if success else 'Compilation did not produce a verified PDF')}
            active = threading.Event()
            active.set()
            try:
                yield CompilationResult(owner, document_id, digest, projection, active, cancel,
                                        build if success else None)
            finally:
                active.clear()
    finally:
        scheduler.detach(task['id'], owner_user_id=owner, subscriber_id=subscriber)
