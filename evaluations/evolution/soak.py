"""Bounded, browser-free Evolution run probe against the real v4 HTTP API.

Entry point: ``python -m evaluations.evolution.soak --live ...``. This runner
never substitutes task or model state; it samples the application from a
separate process and writes one compact JSONL receipt per objective. No provider
traffic occurs without ``--live``.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import statistics as statistics
import sys
import time
import urllib.error
import urllib.request
import uuid

TERMINAL = frozenset({'completed', 'needs_attention', 'failed', 'cancelled'})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _request(base_url: str, token: str, method: str, path: str, body=None,
             *, idempotency_key: str = '') -> dict:
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    request = urllib.request.Request(base_url.rstrip('/') + path, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Response bodies can contain user work or provider details. Only the
        # status crosses the evaluation report boundary.
        raise RuntimeError(f'HTTP {exc.code} at {method} {path.split("?")[0]}') from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError('Evolution response exceeded 4 MiB')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError('Evolution response was not an object')
    # The deployed v4 API uses {data, meta}; compact test apps may expose the
    # legacy flat body. Keep one client projection for both response modes.
    if isinstance(value.get('data'), dict):
        value = value['data']
    return value


def _rss_bytes(pid: int) -> int | None:
    if not pid:
        return None
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _valid_completion(run: dict) -> bool:
    review = run.get('review') or {}
    criteria = review.get('criterion_results') or []
    requirements = run.get('acceptance_requirements') or []
    results = review.get('requirement_results') or []
    if requirements:
        if (len(results) != len(requirements)
                or {row.get('requirement_id') for row in results} != set(requirements)
                or any(row.get('outcome') != 'pass' for row in results)):
            return False
        if run.get('require_requirement_originals') and any(
                not row.get('original_sources') for row in results):
            return False
    return (run.get('status') == 'completed' and bool(run.get('result'))
            and review.get('outcome') == 'success' and len(criteria) == 3
            and all(item.get('outcome') == 'pass' for item in criteria)
            and bool(run.get('episode_id')) and bool(run.get('receipts')))


class ProbeFailure(RuntimeError):
    """A failed observation with retained owned-run accounting and cleanup facts."""
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


class ProbeTimeoutFailure(ProbeFailure, TimeoutError):
    """Preserve the timeout contract while retaining the failed probe receipt."""


def observe_run(request, *, objective: str, key: str, poll_seconds: float,
                timeout_seconds: float, rss_pid: int = 0,
                model: dict | None = None, max_tokens: int | None = None,
                launch_policy: dict | None = None, now=time.monotonic, sleep=time.sleep) -> dict:
    """Observe one owned run; failures retain lower bounds and explicit cleanup."""
    started = now()
    run_id = None
    producer_status = ''
    cancellation_attempted = False
    actuals = {'tokens': 0, 'tool_calls': 0}

    def observed_request(method, path, body, idempotency_key, *, validate_accounting=True):
        nonlocal run_id, producer_status, cancellation_attempted
        if run_id is not None and method == 'POST' and path == f'/api/v4/evolution/runs/{run_id}/cancel':
            cancellation_attempted = True
        response = request(method, path, body, idempotency_key)
        value = response.get('run') if isinstance(response, dict) else None
        if isinstance(value, dict):
            identity = value.get('run_id')
            if run_id is None and method == 'POST' and path == '/api/v4/evolution/runs':
                if (not isinstance(identity, str) or not 1 <= len(identity) <= 160
                        or any(char in identity for char in '/?#')):
                    raise RuntimeError('Invalid admitted run identity')
                run_id = identity
            if run_id is not None and identity == run_id:
                producer_status = str(value.get('status') or '')[:40]
                reported = value.get('actuals')
                if isinstance(reported, dict):
                    invalid_field = None
                    for field in actuals:
                        count = reported.get(field)
                        if type(count) is int and count >= 0:
                            actuals[field] = max(actuals[field], count)
                        elif validate_accounting:
                            invalid_field = invalid_field or field
                    if invalid_field is not None:
                        raise RuntimeError(f'Invalid or missing {invalid_field} accounting on the bound run')
                elif validate_accounting:
                    raise RuntimeError('Missing cumulative accounting on the bound run')
        return response

    try:
        return _observe_run(observed_request, objective=objective, key=key,
            poll_seconds=poll_seconds, timeout_seconds=timeout_seconds,
            rss_pid=rss_pid, model=model, max_tokens=max_tokens,
            launch_policy=launch_policy, now=now, sleep=sleep)
    except (RuntimeError, TimeoutError, ValueError, KeyError, TypeError, AttributeError, OverflowError,
            urllib.error.URLError) as exc:
        if run_id is None:
            raise
        cancellation_confirmed = False
        cleanup_error = ''
        if producer_status not in TERMINAL and not cancellation_attempted:
            try:
                cancelled = observed_request('POST', f'/api/v4/evolution/runs/{run_id}/cancel', {}, '', validate_accounting=False)
                candidate = cancelled.get('run') or {}
                if not isinstance(candidate, dict) or candidate.get('run_id') != run_id or candidate.get('status') != 'cancelled':
                    raise RuntimeError('Cancellation identity or status unconfirmed')
                persisted = observed_request('GET', f'/api/v4/evolution/runs/{run_id}', None, '', validate_accounting=False)
                candidate = persisted.get('run') or {}
                if not isinstance(candidate, dict) or candidate.get('run_id') != run_id or candidate.get('status') != 'cancelled':
                    raise RuntimeError('Cancellation durable readback unconfirmed')
                cancellation_confirmed = True
            except (RuntimeError, TimeoutError, ValueError, KeyError, TypeError, AttributeError, OverflowError,
                    urllib.error.URLError) as cleanup:
                cleanup_error = type(cleanup).__name__
        receipt = {'run_id': run_id, 'status': 'probe_error', 'complete': False,
            'producer_status': producer_status,
            'cancellation_confirmed': cancellation_confirmed,
            'cleanup_error': cleanup_error, 'provider_usage_unknown': True,
            'admitted_max_tokens': max_tokens, **actuals,
            'elapsed_seconds': round(now() - started, 3),
            'error': type(exc).__name__ + ': ' + str(exc)[:200]}
        failure = ProbeTimeoutFailure if isinstance(exc, TimeoutError) else ProbeFailure
        raise failure(str(exc), receipt) from exc


def _observe_run(request, *, objective: str, key: str, poll_seconds: float,
                timeout_seconds: float, rss_pid: int = 0,
                model: dict | None = None, max_tokens: int | None = None,
                launch_policy: dict | None = None, now=time.monotonic, sleep=time.sleep) -> dict:
    """Exercise create → idempotent retry → stage handoff → durable result."""
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise ValueError('probe token allowance must be a positive integer')
    started = now()
    outage_count = 0
    outage_seconds = 0.0
    observation_gap_at_timeout = False

    def call(method, path, body, idempotency_key):
        nonlocal outage_count, outage_seconds
        unavailable_since = None
        while True:
            try:
                response = request(method, path, body, idempotency_key)
                if unavailable_since is not None:
                    outage_seconds += now() - unavailable_since
                return response
            except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                temporary_http_error = isinstance(exc, RuntimeError) and str(exc).startswith(
                    ('HTTP 502 ', 'HTTP 503 ', 'HTTP 504 '))
                if not isinstance(exc, (urllib.error.URLError, TimeoutError)) and not temporary_http_error:
                    raise
                if unavailable_since is None:
                    unavailable_since = now()
                    outage_count += 1
                remaining = timeout_seconds - (now() - started)
                if remaining <= 0:
                    outage_seconds += now() - unavailable_since
                    raise TimeoutError(f'probe timed out while {method} {path} was unavailable') from exc
                sleep(min(poll_seconds, remaining))

    # The production intake remains the validator for these launch fields.
    # Freeze caller-owned policy once so admission retries cannot change it.
    policy = deepcopy(launch_policy) if launch_policy is not None else {}
    allowed = {'project_path', 'executor_plugins', 'executor_schema_budget_tokens', 'budget', 'review_reserve',
               'max_attempts', 'max_review_format_retries', 'acceptance_requirements',
               'require_requirement_originals', 'require_command_execution'}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError('unsupported probe launch policy')
    budget = policy.get('budget', {})
    if not isinstance(budget, dict):
        raise ValueError('probe launch budget must be an object')
    if 'max_tokens' in budget and budget['max_tokens'] != max_tokens:
        raise ValueError('launch policy cannot override probe token allowance')
    if max_tokens is not None:
        policy['budget'] = {**budget, 'max_tokens': max_tokens}
    payload = {**policy, 'objective': objective, **({'model': model} if model else {})}
    created = call('POST', '/api/v4/evolution/runs', payload, key)
    run = created['run']
    run_id = run['run_id']
    try:
        repeated = call('POST', '/api/v4/evolution/runs', payload, key)
        if repeated['run']['run_id'] != run_id:
            raise RuntimeError('Idempotent retry created a second run')
    except TimeoutError:
        if run.get('status') in TERMINAL:
            raise TimeoutError(f'terminal run {run_id} retry confirmation was unavailable')
        observation_gap_at_timeout = True
    status_times = {str(run['status']): 0.0}
    task_ids = {str(run.get('task_id') or '')} - {''}
    start_rss = _rss_bytes(rss_pid)
    peak_rss = start_rss
    previous_actuals = {'tokens': 0, 'tool_calls': 0}
    timed_out = False
    timeout_stage = ''
    while run.get('status') not in TERMINAL:
        if now() - started >= timeout_seconds:
            timeout_stage = str(run.get('status') or '')
            try:
                cancelled = request('POST', f'/api/v4/evolution/runs/{run_id}/cancel', {}, '')
                run = cancelled['run']
            except Exception as exc:
                raise TimeoutError(f'run {run_id} exceeded timeout; cancel failed: {type(exc).__name__}') from exc
            if run.get('run_id') != run_id:
                raise TimeoutError(f'run {run_id} exceeded timeout; cancel response changed identity, cancellation unconfirmed')
            if run.get('status') != 'cancelled':
                raise TimeoutError(f"run {run_id} exceeded timeout; cancel status={run.get('status')}")
            timed_out = True
            status_times.setdefault('cancelled', round(now() - started, 3))
            break
        sleep(min(poll_seconds, max(0.0, timeout_seconds - (now() - started))))
        try:
            run = call('GET', f'/api/v4/evolution/runs/{run_id}', None, '')['run']
        except TimeoutError:
            # Observation expiry does not prove producer termination. Route
            # the known run through the same explicit timeout cancellation.
            observation_gap_at_timeout = True
            continue
        if run.get('run_id') != run_id:
            raise RuntimeError('Run detail changed identity')
        status_times.setdefault(str(run.get('status')), round(now() - started, 3))
        if run.get('task_id'):
            task_ids.add(str(run['task_id']))
        actuals = run.get('actuals') or {}
        for field in previous_actuals:
            current = int(actuals.get(field) or 0)
            if current < previous_actuals[field]:
                raise RuntimeError(f'{field} counter decreased')
            previous_actuals[field] = current
        rss = _rss_bytes(rss_pid)
        if rss is not None:
            peak_rss = max(peak_rss or 0, rss)
    # A second read proves the result was committed beyond the live task view.
    persisted = call('GET', f'/api/v4/evolution/runs/{run_id}', None, '')['run']
    if persisted.get('status') != run['status'] or persisted.get('run_id') != run_id:
        raise RuntimeError('Terminal run changed on durable readback')
    receipt_ids = [row.get('task_id') for row in persisted.get('receipts') or []]
    if len(receipt_ids) != len(set(receipt_ids)):
        raise RuntimeError('Duplicate stage receipts')
    error = ('run_timeout_cancelled' if timed_out else str(persisted.get('error') or ''))[:300]
    return {
        'run_id': run_id, 'status': persisted['status'],
        'admitted_max_tokens': max_tokens,
        'complete': _valid_completion(persisted),
        'timed_out': timed_out,
        'timeout_stage': timeout_stage,
        'elapsed_seconds': round(now() - started, 3),
        'outage_count': outage_count,
        'outage_seconds': round(outage_seconds, 3),
        'stage_started_seconds': status_times,
        'task_count': len(task_ids | set(receipt_ids)), 'receipt_count': len(receipt_ids),
        'stage_usage': [
            {'stage': row.get('stage'), 'model': row.get('model'),
             'tokens': int((row.get('actuals') or {}).get('tokens') or 0),
             'tool_calls': int((row.get('actuals') or {}).get('tool_calls') or 0)}
            for row in persisted.get('receipts') or []],
        'tokens': int((persisted.get('actuals') or {}).get('tokens') or 0),
        'tool_calls': int((persisted.get('actuals') or {}).get('tool_calls') or 0),
        'provider_usage_unknown': (persisted.get('actuals_complete') is False
                                   or any(row.get('actuals_complete') is False for row in persisted.get('receipts') or [])
                                   or bool(persisted.get('stage_losses'))
                                   or observation_gap_at_timeout or 'task_unavailable' in error
                                   or (timed_out and timeout_stage != 'settling')),
        'server_rss_start_bytes': start_rss,
        'server_rss_end_bytes': _rss_bytes(rss_pid),
        'peak_server_rss_bytes': peak_rss,
        'error': error,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + .5))], 3)


def main(argv=None, *, now=time.monotonic, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='authorize real model tasks')
    parser.add_argument('--base-url', default='http://127.0.0.1:15000')
    parser.add_argument('--token-env', default='TOFU_API_KEY')
    parser.add_argument('--objective', default='Explain binary search with one worked example and its time complexity.')
    parser.add_argument('--model-ref', default='',
                        help='optional structured JSON model reference to pin all attempts')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--duration-seconds', type=int, default=0)
    parser.add_argument('--poll-seconds', type=float, default=3.0)
    parser.add_argument('--run-timeout-seconds', type=float, default=3600.0)
    parser.add_argument('--max-total-tokens', type=int, default=1_000_000)
    parser.add_argument('--reserve-tokens-per-run', type=int, default=250_000,
                        help='require this much unspent reported-token allowance before another run')
    parser.add_argument('--min-start-interval-seconds', type=float, default=0,
                        help='pace run starts across a long soak, regardless of quick completions')
    parser.add_argument('--server-pid', type=int, default=0)
    parser.add_argument('--output', type=Path, default=Path('output/evolution-soak/receipts.jsonl'))
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('real runs require --live; no task was submitted')
    if not (1 <= args.runs <= 1000 and 0 <= args.duration_seconds <= 604800
            and .2 <= args.poll_seconds <= 60 and 1 <= args.run_timeout_seconds <= 86400
            and 1 <= args.max_total_tokens <= 100_000_000
            and 0 <= args.reserve_tokens_per_run <= args.max_total_tokens
            and 0 <= args.min_start_interval_seconds <= 3600
            and 0 <= args.server_pid):
        parser.error('runs, duration, poll, timeout, token cap or PID outside bounded range')
    model = None
    if args.model_ref:
        try:
            model = json.loads(args.model_ref)
        except ValueError:
            parser.error('--model-ref must be JSON')
        if (not isinstance(model, dict)
                or set(model) not in ({'creator_id', 'model_id'},
                                      {'provider_id', 'offering_id'})
                or any(not isinstance(value, str) or not value for value in model.values())):
            parser.error('--model-ref requires a complete creator/model or provider/offering pair')
    token = os.environ.get(args.token_env, '')
    probe_id = uuid.uuid4().hex
    objective_sha256 = hashlib.sha256(args.objective.encode('utf-8')).hexdigest()
    probe_started = now()
    deadline = probe_started + args.duration_seconds if args.duration_seconds else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    total_tokens = 0
    def request(method, path, body, key):
        return _request(args.base_url, token, method, path, body, idempotency_key=key)
    with args.output.open('w', encoding='utf-8') as stream:
        for index in range(args.runs):
            next_start = probe_started + index * args.min_start_interval_seconds
            if deadline is not None and next_start >= deadline:
                break
            if (total_tokens >= args.max_total_tokens
                    or total_tokens + args.reserve_tokens_per_run > args.max_total_tokens):
                break
            if next_start > now():
                sleep(next_start - now())
            if deadline is not None and now() >= deadline:
                break
            key = f'evolution-soak:{probe_id}:{index}'
            try:
                result = observe_run(request, objective=args.objective, key=key,
                                     poll_seconds=args.poll_seconds,
                                     timeout_seconds=min(args.run_timeout_seconds, deadline - now()) if deadline is not None
                                     else args.run_timeout_seconds,
                                     rss_pid=args.server_pid, model=model,
                                     max_tokens=args.max_total_tokens - total_tokens,
                                     now=now, sleep=sleep)
            except ProbeFailure as exc:
                result = exc.receipt
            except (RuntimeError, TimeoutError, ValueError, KeyError, TypeError, AttributeError, OverflowError,
                    urllib.error.URLError) as exc:
                result = {'status': 'probe_error', 'complete': False,
                          'provider_usage_unknown': True,
                          'error': type(exc).__name__ + ': ' + str(exc)[:200]}
            result['index'] = index
            result['probe_id'] = probe_id
            result['objective_sha256'] = objective_sha256
            result['requested_model_ref'] = model
            result['harness_version'] = 7
            results.append(result)
            total_tokens += int(result.get('tokens') or 0)
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
            stream.flush()
            if not result['complete'] or result.get('provider_usage_unknown'):
                break
    elapsed = [row['elapsed_seconds'] for row in results if 'elapsed_seconds' in row]
    probe_elapsed = max(now() - probe_started, .001)
    first_rss = results[0].get('server_rss_start_bytes') if results else None
    last_rss = results[-1].get('server_rss_end_bytes') if results else None
    completed_count = sum(row['complete'] for row in results)
    completed_tokens = [row['tokens'] for row in results
                        if row['complete'] and not row.get('provider_usage_unknown')]
    summary = {'runs': len(results), 'completed': completed_count,
               'tokens': total_tokens,
               'tokens_are_lower_bound': any(row.get('provider_usage_unknown') for row in results),
               'p50_seconds': _percentile(elapsed, .5),
               'unknown_usage_runs': sum(bool(row.get('provider_usage_unknown')) for row in results),
               'p95_seconds': _percentile(elapsed, .95),
               'p50_tokens_per_completed_goal': _percentile(completed_tokens, .5),
               'p95_tokens_per_completed_goal': _percentile(completed_tokens, .95),
               'elapsed_seconds': round(probe_elapsed, 3),
               'runs_per_hour': round(completed_count * 3600 / probe_elapsed, 3),
               'tokens_per_second': round(total_tokens / probe_elapsed, 3),
               'server_rss_growth_bytes': last_rss - first_rss if first_rss is not None and last_rss is not None else None,
               'peak_server_rss_bytes': max((row.get('peak_server_rss_bytes') or 0 for row in results), default=0),
               'receipts': str(args.output)}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if results and all(row['complete'] for row in results) else 1


if __name__ == '__main__':
    sys.exit(main())
