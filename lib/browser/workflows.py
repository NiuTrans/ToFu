"""Bounded read workflows over native adapters, leases and content receipts.

Entry points: session_status, validate_session, execute_workflow. No credential
registry, arbitrary scripts, persistent cache or secondary browser authority.
Each workflow owns its pages; independent reads start on demand.
"""
from lib.agent_core.worker_executor import RecoverableAgentExecutor
from concurrent.futures import ThreadPoolExecutor
import json
import base64
import threading
import time

from . import adapters
from .access import is_read_allowed
from .metrics import measure_browser
from .network_evidence import redact_value
from .page import BrowserPage
from .sessions import acquire_browser_lease
from .workflow_contract import SESSION_SCHEMA, SESSION_REF_SCHEMA, WORKFLOW_SCHEMA

from lib.log import get_logger

logger = get_logger(__name__)

def session_status(adapter, runtime):
    from .queue import get_connected_clients
    clients = get_connected_clients(owner_user_id=runtime.owner_user_id)
    client = next((row for row in clients if row['client_id'] == runtime.client_id), None)
    permitted = all(is_read_allowed(runtime.owner_user_id, domain) for domain in adapter.domains)
    health = adapters.adapter_health(adapter, client_id=runtime.client_id) if client else {}
    status = ('blocked' if not permitted else 'unavailable' if not client else
              'upgrade_required' if not health.get('healthy') else 'ready')
    # This descriptor is deliberately not a bearer token. Every invocation
    # still authenticates the owner, checks connectivity, capabilities/policy.
    reference = {'owner': str(runtime.owner_user_id), 'device': runtime.client_id,
                 'profile': str((client or {}).get('profile') or ''),
                 'site': adapter.id, 'expires_at': round(time.time() + 120, 3)}
    encoded = base64.urlsafe_b64encode(json.dumps(reference, separators=(',', ':')).encode()).decode()
    return {'status': status, 'authentication': 'unknown',
            'session_ref': encoded if status == 'ready' else None,
            'next_action': ('Execute a declared read operation to verify access; cookie presence is not proof of login.'
                            if status == 'ready' else 'Check browser connection, capabilities and site access policy.'),
            'missing_capabilities': health.get('missing_capabilities', [])}


def validate_session(reference, adapter, runtime):
    adapters._validate_value(SESSION_REF_SCHEMA, reference, path='session_ref')
    try:
        reference = json.loads(base64.b64decode(reference, altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as exc:
        raise adapters.AdapterValidationError('Invalid session routing reference') from exc
    adapters._validate_value(SESSION_SCHEMA, reference, path='session_ref')
    current = session_status(adapter, runtime)
    expected = json.loads(base64.urlsafe_b64decode(current['session_ref'])) if current['session_ref'] else None
    if (expected is None or not time.time() < reference['expires_at'] <= time.time() + 121
            or any(reference[key] != expected[key] for key in ('owner', 'device', 'profile', 'site'))):
        raise adapters.AdapterExecutionError('session_stale', 'Session routing reference expired or changed; request status again')


def _validate_workflow(adapter, workflow):
    adapters._validate_value(WORKFLOW_SCHEMA, workflow, path='workflow')
    if len(json.dumps(workflow, ensure_ascii=False)) > 65536:
        raise adapters.AdapterValidationError('Workflow input exceeds 65536 characters')
    known = set()
    for step in workflow['steps']:
        if step['id'] in known:
            raise adapters.AdapterValidationError('Workflow step IDs must be unique')
        command = adapter.command(step['operation'])
        if command.access != 'read' or command.session != 'ephemeral' or command.window_mode == 'current':
            raise adapters.AdapterValidationError('Workflows support ephemeral read operations only')
        each = step.get('foreach')
        if each:
            if each['step'] not in known:
                raise adapters.AdapterValidationError('foreach must reference an earlier step')
            if each['parameter'] not in command.input_schema.get('properties', {}):
                raise adapters.AdapterValidationError('foreach parameter is not declared by the operation')
        else:
            adapters._validate_params(command.input_schema, step.get('params', {}))
        known.add(step['id'])


def execute_workflow(adapter, workflow, runtime):
    _validate_workflow(adapter, workflow)
    started = time.monotonic()
    deadline = started + workflow.get('timeout_s', 120)
    completed = []
    values = {}
    stopped = threading.Event()
    last_start = [None]

    def check_budget():
        cancellation = getattr(runtime, 'cancel_event', None)
        if cancellation is not None and cancellation.is_set():
            raise adapters.AdapterExecutionError('cancelled', 'Browser task cancelled; remaining reads were not started')
        if stopped.is_set() or time.monotonic() >= deadline:
            raise adapters.AdapterExecutionError('timeout', 'Workflow deadline reached; remaining reads were not started')

    def open_page():
        check_budget()
        lease = acquire_browser_lease(owner_user_id=runtime.owner_user_id,
                                      client_id=runtime.client_id, session='ephemeral',
                                      timeout=max(1, deadline - time.monotonic()) + 10)
        return BrowserPage(lease, reuse_tab=True, deadline=deadline,
                           cancel_event=getattr(runtime, 'cancel_event', None))

    def invoke(operation, params, page, collector):
        check_budget()
        command = adapter.command(operation)
        # Respect the declared normal interval inside one workflow. A real
        # cooldown/other caller's pacing still fails through adapter admission.
        if command.min_interval_s and last_start[0] is not None:
            delay = command.min_interval_s - (time.monotonic() - last_start[0])
            if delay > 0:
                if time.monotonic() + delay >= deadline:
                    raise adapters.AdapterExecutionError('rate_limited', 'Workflow budget cannot fit the site interval', retryable=True)
                wait_until = time.monotonic() + delay
                while time.monotonic() < wait_until:
                    check_budget()
                    stopped.wait(min(.25, max(0, wait_until - time.monotonic())))
                check_budget()
        last_start[0] = time.monotonic()
        with measure_browser(collector):
            result = adapters.invoke_adapter(adapter.id, operation, params,
                                             owner_user_id=runtime.owner_user_id,
                                             client_id=runtime.client_id, _page=page,
                                             cancel_event=getattr(runtime, 'cancel_event', None))
        # Credential redaction/admission stays at the browser evidence boundary;
        # model presentation sizing belongs to shared L0, not workflow arguments.
        return redact_value(result['result'], max_string_chars=80000)

    def independent(operation, params, collector):
        try:
            with open_page() as own_page:
                return invoke(operation, params, own_page, collector)
        except Exception:
            stopped.set()
            raise

    error = None
    with measure_browser() as timings:
        try:
            with open_page() as page:
                for step in workflow['steps']:
                    check_budget()
                    calls = [step.get('params', {})]
                    each = step.get('foreach')
                    if each:
                        previous = values[each['step']]
                        if not isinstance(previous, list):
                            raise adapters.AdapterValidationError('foreach source must be an array result')
                        calls = []
                        for row in previous[:each.get('limit', len(previous))]:
                            if not isinstance(row, dict) or each['field'] not in row:
                                raise adapters.AdapterValidationError('foreach source record is missing the requested field')
                            calls.append({**step.get('params', {}), each['parameter']: row[each['field']]})
                    # Validate every expanded input before executing this group.
                    for params in calls:
                        adapters._validate_params(adapter.command(step['operation']).input_schema, params)
                    results = []
                    entry = {'id': step['id'], 'status': 'partial', 'result': results,
                             'completed_items': []}
                    completed.append(entry)
                    command = adapter.command(step['operation'])
                    parallel = each and each.get('parallelism', len(calls)) > 1 and not command.min_interval_s
                    if parallel and calls:
                        # Explicit caller concurrency is a workload preference;
                        # absent that preference, every independent read starts.
                        requested = each.get('parallelism')
                        executor = (ThreadPoolExecutor(max_workers=min(requested, len(calls)),
                                        thread_name_prefix='browser-workflow')
                                    if requested is not None else
                                    RecoverableAgentExecutor(max_workers=len(calls), queue_capacity=0,
                                        max_abandoned_workers=1, idle_retain_threads=0,
                                        thread_name_prefix='browser-workflow', metric_pool='browser-workflow'))
                        with executor as pool:
                            futures = [pool.submit(independent, step['operation'], params, timings)
                                       for params in calls]
                            failure = None
                            for index, future in enumerate(futures):
                                try:
                                    item = future.result()
                                    results.append(item)
                                    entry['completed_items'].append(index)
                                except Exception as exc:
                                    # Only the first failure is re-raised below;
                                    # log each one so later failures are not lost.
                                    from .log_safety import text_for_log
                                    logger.debug(
                                        '[Browser:Workflow] parallel step failed: %s',
                                        redact_value(text_for_log(exc, max_chars=256)))
                                    failure = failure or exc
                            if failure is not None:
                                raise failure
                    else:
                        for index, params in enumerate(calls):
                            results.append(invoke(step['operation'], params, page, timings))
                            entry['completed_items'].append(index)
                    value = results if each else results[0]
                    entry.update(status='ok', result=value)
                    values[step['id']] = value
        except Exception as exc:
            from .log_safety import text_for_log
            cancellation = getattr(runtime, 'cancel_event', None)
            code = 'cancelled' if cancellation is not None and cancellation.is_set() else getattr(exc, 'code', 'execution_failed')
            error = {'code': code,
                     'message': redact_value(text_for_log(exc, max_chars=256)),
                     'retryable': bool(getattr(exc, 'retryable', False))}
        finally:
            stopped.set()
        has_results = any(row['status'] == 'ok' or row['completed_items'] for row in completed)
        receipt = {'status': 'partial' if error and has_results else 'unavailable' if error else 'ok',
                'steps': completed, 'error': error,
                'next_action': ('Resume only unfinished reads after resolving the reported error.' if error else 'complete'),
                'coverage': {'completed_steps': sum(row['status'] == 'ok' for row in completed),
                             'requested_steps': len(workflow['steps'])},
                'metrics': {**timings.summary(), 'elapsed_ms': round((time.monotonic() - started) * 1000),
                            'model_calls': 1}}
        return receipt
