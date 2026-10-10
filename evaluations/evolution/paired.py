"""Run frozen JSON-delivery cases through baseline and candidate instructions.

CLI: python -m evaluations.evolution.paired --help. No provider is called
without --live. Execution uses the Chat API (or opt-in AgentRuntime), with tools and personal memory
disabled; expected answers never enter model messages. Results are evaluation
artifacts, not automatic production activation authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from lib.evolution.comparison import compare_pairs

MAX_CASES = 16
MAX_FILE_BYTES = 131072
MAX_RETAINED_ANSWER_BYTES = 65536


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def evaluation_identity():
    """Pin scoring implementation and observation contract for every report entry point."""
    root = Path(__file__).resolve().parents[2]
    return {'harness_version': 2,
            'harness_sha256': digest(Path(__file__).read_text()),
            'python_delivery_checker_sha256': digest((Path(__file__).parent / 'python_delivery.py').read_text()),
            'comparison_sha256': digest((root / 'lib/evolution/comparison.py').read_text()),
            'comparison_contract_sha256': digest((root / 'contracts/evolution_regression_v1.schema.json').read_text())}


def _decode_json(content):
    """One unambiguous JSON interpretation for frozen cases and model answers."""
    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('duplicate evaluation JSON key')
        return value
    try:
        return json.loads(content, object_pairs_hook=unique_object)
    except RecursionError as exc:
        raise ValueError('evaluation JSON exceeds decoder nesting capacity') from exc


def read_cases(path: Path) -> list[dict]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('case file exceeds 128 KiB')
    return _freeze_cases(_decode_json(path.read_text()))


def _freeze_cases(cases):
    try:
        raw = json.dumps(cases, ensure_ascii=False, allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_FILE_BYTES:
            raise ValueError('case data exceeds 128 KiB')
        cases = _decode_json(raw)
    except (TypeError, RecursionError) as exc:
        raise ValueError('case data must be bounded JSON') from exc
    if not isinstance(cases, list) or not 3 <= len(cases) <= MAX_CASES:
        raise ValueError('provide 3..16 held-out cases')
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {'id', 'objective', 'expected'}
                or not isinstance(case['id'], str) or not 1 <= len(case['id']) <= 160
                or case['id'] in ids or not isinstance(case['objective'], str)
                or not 1 <= len(case['objective']) <= 4000):
            raise ValueError('invalid or duplicate case; require id, objective, expected')
        digest(case)  # Refuse NaN/Infinity, including inside expected answers.
        ids.add(case['id'])
    return cases


def exact_json(content: str, expected) -> str:
    try:
        # Canonical bytes keep true distinct from 1 (Python equality does not).
        decoded = _decode_json(content)
        return 'pass' if digest(decoded) == digest(expected) else 'fail'
    except (ValueError, TypeError, RecursionError):
        return 'fail'


def json_numeric_equivalent(content: str, expected) -> str:
    """Exact JSON shape/value agreement; finite numbers may differ in notation.

    No tolerance, coercion, extra fields, prose stripping or array reordering.
    This is an answer-value checker, not execution or significance evidence.
    """
    import math

    def same(actual, wanted):
        if type(actual) in (int, float) and type(wanted) in (int, float):
            return (not isinstance(actual, float) or math.isfinite(actual)) and (
                not isinstance(wanted, float) or math.isfinite(wanted)) and actual == wanted
        if type(actual) is not type(wanted):
            return False
        if isinstance(actual, dict):
            return actual.keys() == wanted.keys() and all(same(actual[k], wanted[k]) for k in actual)
        if isinstance(actual, list):
            return len(actual) == len(wanted) and all(same(a, b) for a, b in zip(actual, wanted))
        return actual == wanted

    try:
        return 'pass' if same(_decode_json(content), expected) else 'fail'
    except (ValueError, TypeError, RecursionError):
        return 'fail'


def python_compile_delivery(content: str, expected) -> str:
    """Frozen compilation-only rubric; never execute or infer scientific validity."""
    from .python_delivery import check_python_delivery
    return check_python_delivery({'status':'done', 'content':content})['outcome']


def validate_checker_cases(cases, checker):
    if checker == 'python_delivery_compile_v1':
        for case in cases:
            expected = case['expected']
            if (not isinstance(expected, dict) or set(expected) != {'compiles'}
                    or expected['compiles'] is not True):
                raise ValueError('Python compilation cases require expected={"compiles":true}')


CHECKERS = {'exact_json_sha256_v1': exact_json,
            'json_numeric_equivalent_v1': json_numeric_equivalent,
            'python_delivery_compile_v1': python_compile_delivery}


def evaluate(cases, baseline: str, candidate: str, *, invoke, total_seconds=600, checker='exact_json_sha256_v1', on_observation=None, on_request=None):
    """Sequential matched inputs, fresh turns, counterbalanced arm order.

    invoke owns a bounded runtime call and returns content/status/usage. This
    injectable boundary makes scoring and failure handling testable offline.
    No retries or evaluator-generated answers are allowed.
    """
    if on_request is not None and not callable(on_request):
        raise ValueError('request checkpoint must be callable')
    if on_observation is not None and not callable(on_observation):
        raise ValueError('observation checkpoint must be callable')
    if checker not in CHECKERS:
        raise ValueError('unknown evaluation checker')
    score = CHECKERS[checker]
    if not 0 < total_seconds <= 1200:
        raise ValueError('total evaluation budget must be within 1200 seconds')
    cases = _freeze_cases(cases)
    validate_checker_cases(cases, checker)
    if not all(isinstance(t, str) and 0 < len(t) <= 8000 for t in (baseline, candidate)):
        raise ValueError('instructions must contain 1..8000 characters')
    identity = evaluation_identity()
    started = time.monotonic()
    pairs, observations = [], []
    for index, case in enumerate(cases):
        pair = {'item_id': case['id']}
        for arm in (('baseline', 'candidate') if index % 2 == 0
                    else ('candidate', 'baseline')):
            remaining = total_seconds - (time.monotonic() - started)
            observation = {'outcome': 'skip', 'tokens': None, 'wall_clock_seconds': None}
            evidence = {'item_id': case['id'], 'arm': arm, 'error': 'budget_exhausted'}
            if remaining > 0:
                if on_request is not None:
                    from copy import deepcopy
                    on_request({'item_id':case['id'], 'arm':arm}, deepcopy(observations))
                before = time.monotonic()
                try:
                    result = invoke([
                        {'role': 'system', 'content': baseline if arm == 'baseline' else candidate},
                        {'role': 'user', 'content': case['objective']},
                    ], timeout_s=min(60, remaining))
                    observation['wall_clock_seconds'] = round(time.monotonic() - before, 6)
                    if result['status'] in ('done', 'completed') and not result.get('error'):
                        observation['outcome'] = score(result['content'], case['expected'])
                    usage = result.get('usage')
                    usage = usage if isinstance(usage, dict) else {}
                    def known(keys):
                        values = [usage[key] for key in keys if key in usage]
                        return bool(values) and all(type(value) is int and value >= 0
                                                    for value in values)
                    if known(('input_tokens', 'prompt_tokens')) and known(
                            ('output_tokens', 'completion_tokens')):
                        from lib.cost import normalize_usage
                        normalized = normalize_usage(usage)
                        observation['tokens'] = normalized['input'] + normalized['output']
                    evidence = {'item_id': case['id'], 'arm': arm,
                                'task_id': result.get('task_id', ''),
                                'model': result.get('model', ''),
                                'provider_id': result.get('provider_id', ''),
                                'output_sha256': digest(result['content']),
                                'status': result['status']}
                    if 'application_boot_id' in result:
                        evidence['application_boot_id'] = result['application_boot_id']
                        evidence['deployment_identity_match'] = result.get('deployment_identity_match')
                    answer = result['content']
                    if isinstance(answer, str):
                        answer_bytes = len(answer.encode('utf-8'))
                        evidence['output_bytes'] = answer_bytes
                        evidence['output_retained'] = answer_bytes <= MAX_RETAINED_ANSWER_BYTES
                        if evidence['output_retained']:
                            evidence['output_original'] = answer
                        else:
                            evidence['output_retention_reason'] = 'answer_exceeds_64_kib'
                    else:
                        evidence['output_retained'] = False
                        evidence['output_retention_reason'] = 'answer_is_not_text'

                except Exception as exc:
                    # Error strings can contain credentials/provider URLs.
                    evidence['error'] = type(exc).__name__
                    if type(getattr(exc, 'code', None)) is int:
                        evidence['http_status'] = exc.code
                    observation['wall_clock_seconds'] = round(time.monotonic() - before, 6)
            evidence['scored_outcome'] = observation['outcome']
            evidence['tokens'] = observation['tokens']
            evidence['wall_clock_seconds'] = observation['wall_clock_seconds']
            pair[arm] = observation
            observations.append(evidence)
            if on_observation is not None:
                from copy import deepcopy
                on_observation(deepcopy(observations))
        pairs.append(pair)
    routes = {digest([o.get('model'), o.get('provider_id')]) for o in observations
              if o.get('model')}
    route_consistent = (False if len(routes) > 1 else
                        True if all(o.get('model') for o in observations) else None)
    if route_consistent is not True:
        # Provider fallback changes the experiment, even when it answers well.
        for pair in pairs:
            for arm in ('baseline', 'candidate'):
                pair[arm]['outcome'] = 'skip'
    costs = {}
    for arm in ('baseline', 'candidate'):
        costs[arm] = {}
        for metric in ('tokens', 'wall_clock_seconds'):
            values = [p[arm][metric] for p in pairs if p[arm][metric] is not None]
            costs[arm][metric] = {'observed_cases': len(values),
                                 'total': sum(values) if len(values) == len(pairs) else None}
    return {**identity, 'comparison': compare_pairs(pairs), 'observations': observations,
            'costs': costs, 'route_consistent': route_consistent,
            'cases_sha256': digest(cases), 'baseline_sha256': digest(baseline),
            'candidate_sha256': digest(candidate), 'checker': checker,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'scope': ('Python source compilation on these frozen cases; no execution or scientific validity claim'
                      if checker == 'python_delivery_compile_v1' else
                      'JSON delivery on these frozen cases; no general capability claim')}


def reserve_report(path: Path, report):
    """Admit one evaluation on a fresh output path without replacing evidence."""
    import os
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def write_report(path: Path, report):
    """Atomic report replacement; failed writes leave the previous checkpoint."""
    import os
    import tempfile
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checker', choices=list(CHECKERS), default='exact_json_sha256_v1')
    parser.add_argument('--cases', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--owner', type=int, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--json-object', action='store_true', help='request structured JSON objects for both arms')
    parser.add_argument('--runtime-config', action='store_true', help='Use an independent Runtime envelope instead of Chat settings')
    parser.add_argument('--app-url', default='http://127.0.0.1:15000')
    parser.add_argument('--model', type=json.loads, help='Optional structured Chat model reference')
    parser.add_argument('--run', help='Use the model configured for a goal role')
    parser.add_argument('--role', choices=['executor','reviewer','coordinator'], default='executor')
    args = parser.parse_args(argv)
    from lib.identity import require_user_id
    require_user_id(args.owner, context='paired evaluation owner')
    cases = read_cases(args.cases)
    validate_checker_cases(cases, args.checker)
    if args.json_object and any(not isinstance(case['expected'], dict) for case in cases):
        parser.error('--json-object requires object answers for every case')
    def instructions(path):
        if path.stat().st_size > 32000:
            raise ValueError('instruction file too large')
        value = path.read_text()
        if not 1 <= len(value) <= 8000:
            raise ValueError('instructions must contain 1..8000 characters')
        return value
    baseline, candidate = instructions(args.baseline), instructions(args.candidate)
    report = {**evaluation_identity(), 'schema_version': 1, 'mode': 'preflight', 'cases': len(cases),
              'response_format':{'type':'json_object'} if args.json_object else None,
              'cases_sha256': digest(cases), 'baseline_sha256': digest(baseline),
              'candidate_sha256': digest(candidate), 'provider_called': False, 'checker': args.checker,
              'comparison_sha256': digest(Path(__file__).parents[2].joinpath(
                  'lib/evolution/comparison.py').read_text())}
    reserve_report(args.output, {**report, 'evaluation_state':'preparing'})
    def checkpoint(observations, inflight=None):
        write_report(args.output, {**report, 'mode':'live', 'evaluation_state':'running',
            'activation_eligible':False, 'observations':observations, 'inflight':inflight,
            'provider_called':True if any(o.get('task_id') for o in observations)
                else None if inflight or observations else False})
    if args.live:
        checkpoint([])
        from tofu_agent import AgentRuntime, ModelRoutingConfig
        if args.runtime_config:
            if args.run or args.model:
                parser.error('--runtime-config cannot be combined with --run or --model')
            access = ModelRoutingConfig.from_env(required=True)
            with AgentRuntime.local(model_routing=access, owner_user_id=args.owner,
                                    max_inflight=1) as runtime:
                def invoke(messages, *, timeout_s):
                    result = runtime.run(messages, timeout_s=timeout_s, config={
                        'toolSpecAllowlist': [], 'maxTokens': 2048, 'temperature': 0,
                        **({'responseFormat':{'type':'json_object'}} if args.json_object else {}),
                        'memoryEnabled': False, 'humanGuidanceEnabled': False,
                        'executionLimits': {'max_tokens': 8192, 'max_tool_calls': 0,
                                            'deadline': time.time() + timeout_s}})
                    return {'content': result.content, 'status': result.status,
                            'usage': result.usage, 'error': result.error,
                            'model': result.model, 'provider_id': result.provider_id,
                            'task_id': result.task_id}
                report.update(evaluate(cases, baseline, candidate, invoke=invoke, checker=args.checker, on_observation=checkpoint,
                on_request=lambda request, rows: checkpoint(rows, request)))
            routing_identity=access.public_dict()
        else:
            from .chat_access import ChatEvaluator
            client=ChatEvaluator(user_id=args.owner,base_url=args.app_url,requested=args.model,run_id=args.run,role=args.role,json_object=args.json_object)
            report['application_identity'] = client.deployment_identity
            report.update(evaluate(cases,baseline,candidate,invoke=client.invoke,checker=args.checker, on_observation=checkpoint,
                on_request=lambda request, rows: checkpoint(rows, request)))
            routing_identity={'model':client.model,'source':'chat'}
            report['route_observation']='Chat completion exposes requested model; provider fallback is not independently verified'
            report['activation_eligible']=False
        report.update(mode='live', provider_called=any(o.get('task_id') for o in report['observations']), model_source='runtime' if args.runtime_config else 'chat',
                      routing_sha256=digest(routing_identity),
                      budget={'concurrency': 1, 'total_seconds': 600,
                              'arm_seconds': 60, 'max_output_tokens': 2048,
                              'max_arm_tokens': 8192, 'tool_calls': 0})
    report['evaluation_state'] = 'completed' if args.live else 'preflight'
    write_report(args.output, report)
    print(json.dumps({'mode': report['mode'], 'output': str(args.output),
                      'provider_called': report['provider_called']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
