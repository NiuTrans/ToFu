"""Record worker drift; run repair only at the admitted search's owner boundary.

The listener never launches work. Scoped duplicate/cooldown suppression retains
only reconstructible attempt metadata, never credentials or executable rules.
The search handler owns execution lifetime, cancellation and model accounting.
"""
from __future__ import annotations

import os
import threading
import time

from lib.log import audit_log, get_logger
from .runner import RepairExecution, run_doctor

logger = get_logger(__name__)
_ENV_OFF = ('0', 'false', 'no', 'off')
_lock = threading.Lock()
_last_attempt = {}
_in_flight = set()


def _doctor_enabled():
    return os.environ.get('TOFU_SITE_DOCTOR', '1').strip().lower() not in _ENV_OFF


def _cooldown_s():
    try:
        return int(os.environ.get('TOFU_SITE_DOCTOR_COOLDOWN_S', '10800'))
    except ValueError:
        return 10800


def on_site_drift(site, url, evidence):
    """Fast observation only, including inside speculative/discarded workers."""
    try:
        if not _doctor_enabled():
            return
        from tofu_search.providers import get_browser_provider
        browser = get_browser_provider()
        scope = getattr(browser, 'knowledge_scope', None)
        if scope is not None:
            scope.observe(site, url, evidence or {}, browser)
    except Exception:
        logger.debug('[SiteDoctor] drift observation unavailable', exc_info=True)


def repair_observation(scope, browser, site, url, evidence):
    """Called synchronously by the completed, admitted search occurrence."""
    if not _doctor_enabled():
        return
    from lib.agent_core.execution_budget import ExecutionBudget, model_budget_admission
    from lib.tasks_pkg.manager import make_task_abort_check
    parent_abort = make_task_abort_check(scope.task, nonblocking_storage=True)
    def abort():
        return (scope.task.get('_principalContext') != scope.principal_payload
                or scope.task.get('_executionBudget') is not scope.budget
                or parent_abort())
    if not isinstance(scope.budget, ExecutionBudget):
        scope.emit({'stage': 'site_repair', 'site': site,
                    'detail': 'Site repair requires the search execution account',
                    'result': {'pinned': False, 'detail': 'search execution account unavailable'}})
        return
    if abort():
        return
    from lib.browser.access import require_existing_write_access
    def authorize():
        require_existing_write_access(scope.repository.owner_id, url,
                       client_id=scope.binding.client_id, profile=scope.binding.profile)
    try:
        authorize()
    except PermissionError as error:
        scope.emit({'stage': 'site_repair', 'site': site,
                    'detail': str(error),
                    'result': {'pinned': False, 'detail': str(error)}})
        return
    key = (scope.repository.owner_id, scope.principal_payload.get('tenant_id'),
           scope.binding.client_id, scope.binding.profile, site)
    with _lock:
        now = time.monotonic()
        cooldown = _cooldown_s()
        # Expired observations confer no admission rights and need no retention.
        for old, stamp in tuple(_last_attempt.items()):
            if now - stamp >= cooldown:
                _last_attempt.pop(old, None)
        if key in _in_flight or key in _last_attempt:
            return
        _in_flight.add(key)
        _last_attempt[key] = now
    started = False
    try:
        admission = model_budget_admission(scope.task, cancelled=abort)
        execution = RepairExecution(scope.repository, browser, admission, abort, scope.emit, authorize)
        audit_log('site_doctor_triggered', domain=site, owner_user_id=scope.repository.owner_id)
        started = True
        result = run_doctor(site, url, evidence, execution=execution,
                            model=scope.task.get('model') or (scope.task.get('config') or {}).get('model'))
        scope.emit({'stage': 'site_repair', 'site': site, 'result': result,
                    'detail': 'Site repair: ' + result['detail']})
        audit_log('site_doctor_done', domain=site, owner_user_id=scope.repository.owner_id,
                  pinned=result['pinned'], rounds=result['rounds'], tokens=result['tokens'])
    except Exception as error:
        logger.warning('[SiteDoctor] owned repair failed for %s', site, exc_info=True)
        try:
            scope.emit({'stage': 'site_repair', 'site': site,
                        'detail': 'Site repair failed: ' + type(error).__name__})
            audit_log('site_doctor_error', domain=site,
                      owner_user_id=scope.repository.owner_id,
                      error=f'{type(error).__name__}: {error}'[:200])
        except Exception:
            logger.debug('[SiteDoctor] failure observation unavailable', exc_info=True)
    finally:
        with _lock:
            _in_flight.discard(key)
            if not started and _last_attempt.get(key) == now:
                _last_attempt.pop(key, None)
