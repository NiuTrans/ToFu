"""Reproducible coding smoke through the ordinary Evolution HTTP runtime.

Default: offline fixture preflight. --live explicitly admits bounded model calls
and local code execution against a same-host server. Each fresh project uses
the common Coordinator/Worker tools; soak.observe_run owns polling and cleanup.
Reports measure a synthetic task, not benchmark leadership or causal learning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
import uuid

from evaluations.evolution import soak
from evaluations.evolution.chat_access import ChatEvaluator
from evaluations.evolution.coding_task import (
    OBJECTIVE, STARTER, check_delivery, digest, task_identity,
)
from evaluations.paths import validate_output_path


def harness_identity() -> dict:
    return {'version': 1, 'source_sha256': {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('coding.py', 'coding_task.py', 'soak.py', 'chat_access.py')}}


def request_diagnostics(request, run: dict) -> dict:
    """Keep metadata-only stage requests; never retain prompts or user memory."""
    result = {'complete': False, 'scope': 'executor and reviewer; excludes child requests',
              'requests': [], 'learning_ok': None, 'learning_complete': None}
    if not run.get('run_id'):
        return result
    try:
        snapshot = request('GET', f"/api/v4/evolution/runs/{run['run_id']}", None, '')['run']
        if snapshot.get('run_id') != run['run_id']:
            raise ValueError('diagnostic run identity mismatch')
        learning = snapshot.get('learning') or {}
        result.update(learning_ok=learning.get('ok'), learning_complete=learning.get('complete'))
        receipts = snapshot.get('receipts') or []
        if len(receipts) > 16:
            return result
        for receipt in receipts:
            task_id = receipt.get('task_id')
            if not isinstance(task_id, str) or any(c in task_id for c in '/?#'):
                raise ValueError('invalid stage task identity')
            requests = request('GET', f'/api/v4/tasks/{task_id}/requests', None, '')
            if requests.get('coverage') != 'full' or len(requests.get('requests', [])) > 128:
                return result
            for row in requests.get('requests', []):
                if len(row.get('attempts', [])) > 16:
                    return result
                for attempt in row.get('attempts', []):
                    wire = attempt.get('wireProjection') or {}
                    result['requests'].append({'stage': receipt.get('stage'), 'task_id': task_id,
                        'round': row.get('roundNum'), 'model': attempt.get('model'),
                        **{key: attempt.get(key) for key in
                           ('tokensIn', 'tokensOut', 'cacheRead', 'cacheWrite', 'streamElapsedMs')},
                        **{key: wire.get(key) for key in ('schemaTokens', 'toolCount')}})
        result['complete'] = bool(result['requests'])
    except (ValueError, RuntimeError, KeyError, TypeError, AttributeError, OSError) as exc:
        result['error'] = type(exc).__name__
    return result


def summarize(rows: list[dict]) -> dict:
    accepted = [row for row in rows if row['accepted']]
    known = [row for row in accepted if not row['run'].get('provider_usage_unknown')]
    return {'runs': len(rows), 'accepted': len(accepted),
            'acceptance_rate': len(accepted) / len(rows) if rows else None,
            'review_completed': sum(bool(row['run'].get('complete')) for row in rows),
            'code_passed': sum(bool(row['checker'].get('passed')) for row in rows),
            'tokens': sum(row['run'].get('tokens', 0) for row in rows),
            'tokens_are_lower_bound': any(row['run'].get('provider_usage_unknown') for row in rows),
            'elapsed_seconds': round(sum(row['run'].get('elapsed_seconds', 0) for row in rows), 3),
            'p50_tokens_per_accepted': soak._percentile([row['run']['tokens'] for row in known], .5)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--base-url', default='http://127.0.0.1:15000')
    parser.add_argument('--user-id', type=int)
    parser.add_argument('--model-ref', help='JSON creator/model or provider/offering reference')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--executor-schema-budget-tokens', type=int,
                        help='soft tool-schema target; 0 is the uncapped control, omission keeps deployment policy')
    parser.add_argument('--max-total-tokens', type=int, default=60000)
    parser.add_argument('--timeout-seconds', type=int, default=600)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    if not (1 <= args.runs <= 10 and 12000 <= args.max_total_tokens <= 600000
            and 60 <= args.timeout_seconds <= 1800
            and (args.executor_schema_budget_tokens is None
                 or 0 <= args.executor_schema_budget_tokens <= 128000)):
        parser.error('runs, token allowance or timeout outside bounded range')
    manifest = {'task': task_identity(), 'harness': harness_identity(),
                'python_runtime': sys.version,
                'objective': OBJECTIVE, 'limits': {
                    'runs': args.runs, 'max_total_tokens': args.max_total_tokens,
                    'timeout_seconds_per_run': args.timeout_seconds,
                    'executor_schema_budget_tokens': args.executor_schema_budget_tokens,
                    'max_tool_calls_per_run': 80, 'max_attempts': 1},
                'evidence_scope': 'synthetic coding smoke; repeated runs may share learned guidance'}
    if not args.live:
        print(json.dumps({'mode': 'offline_preflight', **manifest}, ensure_ascii=False))
        return 0
    if args.user_id is None or not args.model_ref:
        parser.error('--live requires an explicit --user-id and --model-ref')
    parsed = urlparse(args.base_url)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('coding projects require a same-host loopback application')
    try:
        model = json.loads(args.model_ref)
        if (not isinstance(model, dict) or set(model) not in (
                {'creator_id', 'model_id'}, {'provider_id', 'offering_id'})
                or any(not isinstance(value, str) or not value for value in model.values())):
            raise ValueError('model reference requires an exact identity pair')
        access = ChatEvaluator(user_id=args.user_id, base_url=args.base_url, requested=model)
        output = validate_output_path(args.output_dir or
            Path('output/evolution-coding') / uuid.uuid4().hex)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest.update(owner_id=args.user_id, requested_model=model,
                    deployment=access.deployment_identity,
                    evaluation_id=uuid.uuid4().hex)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    remaining = args.max_total_tokens
    rows = []
    token = os.environ.get('TOFU_API_KEY', '')
    admitted = set()
    admission_errors = []

    def request(method, path, body, key):
        response = soak._request(args.base_url, token, method, path, body, idempotency_key=key)
        run_id = (response.get('run') or {}).get('run_id')
        if method == 'POST' and path == '/api/v4/evolution/runs' and run_id and run_id not in admitted:
            # Persist ownership immediately, even if the evaluator is interrupted.
            try:
                with (output / 'admissions.jsonl').open('a', encoding='utf-8') as admission:
                    admission.write(json.dumps({'run_id': run_id, 'idempotency_key': key}) + '\n')
            except OSError as exc:
                # Do not hide a created run from the observer's cleanup owner.
                admission_errors.append(type(exc).__name__)
            admitted.add(run_id)
        return response

    with (output / 'receipts.jsonl').open('x', encoding='utf-8') as stream:
        for index in range(args.runs):
            if remaining < 12000:
                break
            try:
                current_deployment = access._deployment_identity()
            except (ValueError, RuntimeError, OSError):
                break
            if current_deployment != manifest['deployment']:
                break
            project = output / f'project-{index}'
            project.mkdir(mode=0o700)
            (project / 'planner.py').write_text(STARTER)
            (project / 'README.md').write_text(OBJECTIVE)
            # Verify the fixture can fail before spending any model tokens.
            baseline = check_delivery(project, scratch=output)
            if baseline['passed'] or baseline.get('error'):
                raise RuntimeError('broken fixture was not behaviorally checked')
            policy = {'project_path': str(project), 'executor_plugins': [],
                      'max_attempts': 1, 'max_review_format_retries': 1,
                      'require_command_execution': True,
                      'budget': {'max_tool_calls': 80,
                                 'max_wall_clock_seconds': args.timeout_seconds},
                      'review_reserve': {'tokens': 4000, 'tool_calls': 8,
                                         'wall_clock_seconds': 30}}
            if args.executor_schema_budget_tokens is not None:
                policy['executor_schema_budget_tokens'] = args.executor_schema_budget_tokens
            try:
                run = soak.observe_run(request, objective=OBJECTIVE,
                    key=f"coding:{manifest['evaluation_id']}:{index}",
                    model=model, max_tokens=remaining, launch_policy=policy,
                    poll_seconds=2, timeout_seconds=args.timeout_seconds)
            except soak.ProbeFailure as exc:
                run = exc.receipt
            except (RuntimeError, ValueError, OSError) as exc:
                run = {'complete': False, 'status': 'probe_error', 'tokens': 0,
                       'provider_usage_unknown': True, 'error': type(exc).__name__}
            # Unconfirmed cleanup must never race a still-writing producer.
            stopped = run.get('status') in soak.TERMINAL or run.get('cancellation_confirmed')
            checker = check_delivery(project, scratch=output) if stopped else {
                'passed': False, 'error': 'producer_termination_unconfirmed'}
            try:
                stable = access._deployment_identity() == manifest['deployment']
            except (ValueError, RuntimeError, OSError):
                stable = False
            row = {'index': index, 'task_id': manifest['task']['task_id'],
                   'admitted_max_tokens': remaining, 'launch_policy': policy,
                   'launch_policy_sha256': digest(policy),
                   'baseline': baseline, 'run': run, 'checker': checker,
                   'diagnostics': request_diagnostics(request, run) if stopped else {'complete': False},
                   'deployment_identity_match': stable,
                   'accepted': bool(run.get('complete') and checker['passed'] and stable)}
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            remaining -= run.get('tokens', 0)
            if run.get('provider_usage_unknown') or not stable or not stopped:
                break
    summary = {**summarize(rows), 'requested_runs': args.runs, 'output_dir': str(output),
               'admission_record_errors': admission_errors}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if len(rows) == args.runs and all(row['accepted'] for row in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
