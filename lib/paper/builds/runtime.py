"""Preview scheduling over the existing TaskRuntime and bounded worker executor.

Owns only document scheduling indexes and viewer subscriptions. TaskRuntime
owns lifecycle/events/abort. Execution adapters keep staging leases alive while
calling the publication callback, which fences obsolete and cancelled builds.
"""
from __future__ import annotations

import atexit
import hashlib
import threading
import time
from collections.abc import Callable

from lib.agent_core.worker_executor import RecoverableAgentExecutor
from lib.production.runtime import ProductionRuntime
from lib.log import get_logger
from lib.storage.errors import StorageError

from .budget import BuildBudget, build_budget
from .cache import LocalBuildCache
from .compiler import CompiledBuild
from .contract import limit
from .errors import BuildError, check_cancelled

_production = ProductionRuntime('paper-build', id_prefix='paper_build', ttl=3600,
                                error_source='lib.paper.builds', max_tasks=64,
                                max_events=64)
_paper_build_runtime = _production.runtime
logger = get_logger(__name__)


def _retire(callback):
    if callback is not None:
        try:
            callback()
        except Exception:
            logger.warning('Paper build retirement metadata unavailable; durable expiry remains active')


class _ViewerCancellation:
    """Cooperative cancellation also expires abandoned browser subscriptions."""
    def __init__(self, scheduler, task):
        self.scheduler, self.task = scheduler, task

    def is_set(self):
        with self.scheduler._guard:
            if not any(deadline > time.monotonic() for deadline in self.task['_build_subscribers'].values()):
                self.scheduler.production.abort(self.task['id'])
            return self.task['abort_event'].is_set()


class PreviewBuildScheduler:
    def __init__(self, cache: LocalBuildCache, *, budget: BuildBudget | None = None,
                 production: ProductionRuntime | None = None):
        self.cache = cache
        self.budget = budget or build_budget()
        self.production = production or _production
        self._guard = threading.RLock()
        self._documents: dict[tuple[int, str], dict] = {}
        self._closing = False
        self._executor = RecoverableAgentExecutor(
            max_workers=self.budget.workers, queue_capacity=self.budget.queue_capacity,
            max_abandoned_workers=1, thread_name_prefix='paper-build',
            metric_pool='paper-build', idle_retain_threads=0)

    def start(self, *, owner_user_id: int, document_id: str, request_key: str,
              subscriber_id: str, execute: Callable, meta: dict | None = None,
              on_admit: Callable | None = None, on_retire: Callable | None = None) -> dict:
        """Coalesce bursts per owned document; repeated requests join one task."""
        if not request_key or len(request_key) > 512 or not subscriber_id or len(subscriber_id) > 160:
            raise BuildError('build_request', 'Build request and subscriber identities are required')
        self.cache._document(owner_user_id, document_id)  # validate the same semantic identity
        key = (owner_user_id, document_id)
        request_hash = hashlib.sha256(request_key.encode()).hexdigest()
        with self._guard:
            if self._closing:
                raise BuildError('build_stopped', 'Paper build service is stopping')
            runtime = self.production.runtime
            for row in runtime.snapshot_owned(user_id=owner_user_id):
                if (row.get('meta', {}).get('document_id') == document_id
                        and row.get('meta', {}).get('request_hash') == request_hash):
                    task = runtime.get_owned(row['id'], user_id=owner_user_id)
                    if (task is not None and task.get('status') in ('pending', 'running')
                            and not task['abort_event'].is_set()):
                        self._subscribe(task, subscriber_id)
                        return task
            slot = self._documents.get(key)
            try:
                if on_admit is not None:
                    on_admit()
                task = self.production.create_task(
                    self.production.new_task_id(), user_id=owner_user_id,
                    meta={**(meta or {}), 'document_id': document_id, 'request_hash': request_hash},
                    fields={'_build_subscribers': {
                        subscriber_id: time.monotonic() + limit('subscription_seconds')}})
            except BaseException:
                _retire(on_retire)
                raise
            work = (task, execute, on_retire)
            if slot is not None:
                prior = slot['pending']
                if prior:
                    runtime.abort(prior[0]['id'])
                    runtime.finish(prior[0]['id'])
                    _retire(prior[2])
                slot['pending'] = work
            else:
                self._documents[key] = {'active': task['id'], 'pending': None}
                try:
                    self._submit(key, work)
                except Exception:
                    self._documents.pop(key, None)
                    runtime.abort(task['id'])
                    runtime.finish(task['id'])
                    _retire(on_retire)
                    raise
            return task

    def _subscribe(self, task: dict, subscriber_id: str) -> None:
        now = time.monotonic()
        subscribers = {value: deadline for value, deadline in task.get('_build_subscribers', {}).items()
                       if deadline > now}
        subscribers[subscriber_id] = now + limit('subscription_seconds')
        self.production.runtime.update_fields(task['id'], fields={'_build_subscribers': subscribers})

    def renew(self, task_id: str, *, owner_user_id: int, subscriber_id: str) -> bool:
        with self._guard:
            task = self.production.runtime.get_owned(task_id, user_id=owner_user_id)
            if task is None or subscriber_id not in task.get('_build_subscribers', {}):
                return False
            if task['status'] not in ('pending', 'running') or task['abort_event'].is_set():
                return False
            self._subscribe(task, subscriber_id)
            return True

    def detach(self, task_id: str, *, owner_user_id: int, subscriber_id: str) -> bool:
        with self._guard:
            runtime = self.production.runtime
            task = runtime.get_owned(task_id, user_id=owner_user_id)
            if task is None:
                return False
            if subscriber_id not in task.get('_build_subscribers', []):
                return False
            subscribers = {value: deadline for value, deadline in task['_build_subscribers'].items()
                           if value != subscriber_id and deadline > time.monotonic()}
            runtime.update_fields(task_id, fields={'_build_subscribers': subscribers})
            if not subscribers:
                runtime.abort_owned(task_id, user_id=owner_user_id)
                key = (owner_user_id, task['meta']['document_id'])
                slot = self._documents.get(key)
                if slot and slot['pending'] and slot['pending'][0]['id'] == task_id:
                    _retire(slot['pending'][2])
                    slot['pending'] = None
                    runtime.finish(task_id)
            return True

    def _submit(self, key, work) -> None:
        task, execute, on_retire = work
        self.production.runtime.submit_worker(
            task['id'], lambda task_id, _owner, callback:
                self._executor.submit_task(task_id, callback),
            self._run, key, task, execute, on_retire)

    def _run(self, key, task, execute, on_retire=None) -> None:
        cancellation = _ViewerCancellation(self, task)
        def publish(build: CompiledBuild, *, commit=None, **extra) -> dict:
            if commit is not None:
                check_cancelled(cancellation)
                if (build.receipt['owner_user_id'], build.receipt['document_id']) != key:
                    raise BuildError('build_identity', 'Adapter returned a different document owner or identity')
                # Copy/hash outside the scheduler lock. Nothing advances until
                # the short durable transaction accepts this generation.
                receipt = self.cache.publish(build, make_current=False)
                with self.cache.lease(*key, receipt['build_id']), self._guard:
                    check_cancelled(cancellation)
                    slot = self._documents.get(key)
                    current = bool(slot and slot['active'] == task['id'] and slot['pending'] is None)
                    outcome = commit() if current else {'current': False, 'published': False}
                    current = current and outcome['current']
                    if current and outcome['published']:
                        try:
                            self.cache.promote(*key, receipt['build_id'])
                        except (OSError, BuildError):
                            logger.warning('Retained paper committed; disposable cache pointer update failed')
                    return {**extra, 'receipt': receipt, 'superseded': not current,
                            'retained': bool(current and outcome['published'])}
            with self._guard:
                check_cancelled(cancellation)
                if (build.receipt['owner_user_id'], build.receipt['document_id']) != key:
                    raise BuildError('build_identity', 'Adapter returned a different document owner or identity')
                slot = self._documents.get(key)
                current = bool(slot and slot['active'] == task['id'] and slot['pending'] is None)
                receipt = self.cache.publish(build, make_current=current)
                return {**extra, 'receipt': receipt, 'superseded': not current}

        try:
            check_cancelled(cancellation)
            if not self.production.runtime.mark_running(task['id']):
                return
            result = execute({**task, 'abort_event': cancellation}, publish)
            receipt = result['receipt']
            if receipt['status'] == 'cancelled':
                self.production.abort(task['id'])
                self.production.finish(task['id'], result=result)
            elif receipt['status'] != 'success':
                if receipt['status'] == 'failed' and task.get('meta', {}).get('conversationId'):
                    from .notify import notify_build_failed
                    notify_build_failed(
                        owner_user_id=receipt['owner_user_id'],
                        conversation_id=task['meta']['conversationId'],
                        receipt=receipt)

                self.production.finish(task['id'], result=result,
                                       error=BuildError('compile_failed', 'Paper compilation failed'))
            else:
                self.production.finish(task['id'], result=result,
                                       degraded=not receipt['version_verified'],
                                       degraded_reason='source_version_unverified' if not receipt['version_verified'] else '')
        except BuildError as exc:
            if exc.code == 'cancelled':
                self.production.abort(task['id'])
                self.production.finish(task['id'])
            else:
                self.production.finish(task['id'], error=exc)
        except StorageError as exc:
            # Retention pressure/conflicts are storage failures, not TeX errors.
            self.production.finish(task['id'], error={
                'kind': 'server_busy' if exc.code == 'database_busy' else 'generic',
                'code': exc.code, 'message': str(exc), 'retryable': exc.retryable, 'hint': ''})
        except Exception:
            # Adapter/IO failures must settle the same TaskRuntime lifecycle;
            # a released queue slot must never leave its task running forever.
            self.production.finish(task['id'], error=BuildError('build_failed', 'Paper build failed'))
        finally:
            _retire(on_retire)
            with self._guard:
                slot = self._documents.get(key)
                pending = slot['pending'] if slot else None
                if pending is not None and not self._closing:
                    slot.update(active=pending[0]['id'], pending=None)
                    try:
                        self._submit(key, pending)
                    except Exception as exc:
                        self._documents.pop(key, None)
                        self.production.finish(pending[0]['id'], error=exc)
                        _retire(pending[2])
                else:
                    self._documents.pop(key, None)

    def shutdown(self) -> None:
        with self._guard:
            self._closing = True
            for key, slot in self._documents.items():
                self.production.abort(slot['active'])
                if slot['pending']:
                    self.production.abort(slot['pending'][0]['id'])
                    self.production.finish(slot['pending'][0]['id'])
                    _retire(slot['pending'][2])
        self._executor.shutdown(wait=True, cancel_futures=False)


_scheduler: PreviewBuildScheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> PreviewBuildScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            from pathlib import Path
            from lib.runtime_paths import data_root
            from .cache import default_spool_root
            cache_root = Path(data_root()) / 'cache' / 'paper-builds'
            _scheduler = PreviewBuildScheduler(
                LocalBuildCache(cache_root, spool_root=default_spool_root(cache_root)))
        return _scheduler


def shutdown_builds() -> None:
    global _scheduler
    with _scheduler_lock:
        scheduler, _scheduler = _scheduler, None
    if scheduler is not None:
        scheduler.shutdown()


atexit.register(shutdown_builds)
