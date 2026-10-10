"""Paired pilot through AgentRuntime, with immutable input and native evidence.

Only simulated leaf I/O lives here. Production routing, provider transport,
agent loop, gateway, interpreter and settlement are unmodified. No release
claim follows from this synthetic diagnostic. CLI defaults to offline preflight.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

from .workloads import canonical as canonical, digest as digest, oracle as oracle, read_world as read_world, tool_schemas as tool_schemas, workload_pack as workload_pack, workloads as workloads


SYSTEM = ('Use the available tools to answer the request accurately. '
          'Return the requested JSON object without Markdown or commentary.')
ARMS = ('direct', 'programmatic')
ARTIFACT_LIMIT_BYTES = 16 * 1024 * 1024


def source_fingerprint(root: Path) -> str:
    """Bind retained Python/kernel contracts, including uncommitted changes."""
    result = subprocess.run(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
         'lib', 'tofu_agent', 'evaluations/programmatic_pilot', 'contracts',
         'runtime_guards.py', 'bootstrap_pkg', 'pyproject.toml', 'uv.lock'], cwd=root, check=True, capture_output=True)
    source = hashlib.sha256()
    for relative in sorted(set(result.stdout.decode().split('\0')) - {''}):
        path = root / relative
        if path.suffix not in {'.py', '.json', '.yaml', '.js', '.wasm', '.toml', '.lock'} or not path.is_file():
            continue
        source.update(relative.encode() + b'\0' + path.read_bytes() + b'\0')
    return source.hexdigest()


def config(arm: str, thinking: str) -> dict:
    if arm not in ARMS:
        raise ValueError('Unknown arm')
    return {'thinking': thinking, 'disableModelFallback': True,
            'tools': {'programmaticCalling': 'off' if arm == 'direct' else 'on',
                      'programmaticExposure': 'additive', 'toolSearch': 'off',
                      'executionScope': 'selected_only'},
            'orchestration': {'multiAgent': 'off'}, 'max_tokens': 4096,
            'executionLimits': {'max_tokens': 100_000,
                                'max_tool_calls': 32, 'deadline': None}}


def preflight(root: Path, *, thinking: str, trials: int, timeout_s: int, pack: str = 'baseline') -> dict:
    tasks = workload_pack(pack)
    return {'contractVersion': 'tofu-programmatic-pilot/v1', 'taskPack':pack,
            'diagnosticOnly': True, 'sourceSha256': source_fingerprint(root),
            'taskPackSha256': digest([t.__dict__ for t in tasks]),
            'toolSchemaSha256': (digest({t.task_id: tool_schemas(t) for t in tasks})
                                 if pack == 'declared' else digest(tool_schemas())),
            'systemSha256': digest(SYSTEM), 'trials': trials,
            'timeoutSeconds': timeout_s, 'infrastructureRetries': 0,
            'arms': {arm: config(arm, thinking) for arm in ARMS},
            'tasks': [{'id': t.task_id, 'family': t.family,
                       'sha256': t.fingerprint} for t in tasks],
            'acceptance': 'exact JSON oracle; adoption is observed, never required',
            'limits': {'artifactBytesPerTask': ARTIFACT_LIMIT_BYTES,
                       'maxOutputTokensPerCall': 4096}}


def private_json(path: Path, value) -> None:
    data = (canonical(value) + '\n').encode()
    if len(data) > ARTIFACT_LIMIT_BYTES:
        raise ValueError('Evidence exceeds preregistered artifact ceiling')
    with path.open('xb') as stream:
        path.chmod(0o600)
        stream.write(data)
        stream.flush()


def usage_cost(usage: dict, price_card: dict | None) -> float | None:
    """Price only complete provider usage with an explicit frozen USD card."""
    if price_card is None:
        return None
    rates = price_card.get('perMillionTokens', {})
    values = [rates.get(k) for k in ('input', 'cachedInput', 'output')]
    if price_card.get('currency') != 'USD' or not price_card.get('source'):
        raise ValueError('Price card needs currency=USD and primary source URL')
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('Price card rates must be finite nonnegative numbers')
    inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
    details = usage.get('prompt_tokens_details') or {}
    cached = details.get('cached_tokens', usage.get('cached_tokens'))
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in (inp, out, cached)) or cached > inp:
        return None
    from lib.benchmark_contract import public_price_cost_from_usage
    from lib.cost import normalize_usage
    if normalize_usage(usage)['cache_write']:
        return None  # This pilot card does not declare cache-write pricing.
    return public_price_cost_from_usage(usage, {
        'inputUsdPerMillion': values[0], 'cacheReadUsdPerMillion': values[1],
        'outputUsdPerMillion': values[2]})['costUsd']


async def run_task(runtime, workload, arm, trial, directory: Path,
                   thinking, timeout_s, price_card) -> dict:
    started = time.monotonic_ns()
    execution = runtime.start(
        [{'role': 'system', 'content': SYSTEM},
         {'role': 'user', 'content': workload.instruction}],
        config=config(arm, thinking), custom_tools=tool_schemas(workload),
        custom_tools_mode='exclusive', trajectory='tofu-native',
        request_id=f'ptc-pilot-{workload.task_id}-{arm}-{trial}',
        timeout_s=timeout_s)
    calls, result, failure = [], None, None
    world = json.loads(workload.world_json)
    read_coverage = {}
    event_bytes = 0
    with (directory / 'events.jsonl').open('xb') as stream:
        (directory / 'events.jsonl').chmod(0o600)
        try:
            async for event in execution.events_async(timeout_s=timeout_s):
                data = (canonical(event) + '\n').encode()
                event_bytes += len(data)
                if event_bytes > ARTIFACT_LIMIT_BYTES:
                    raise ValueError('Native event evidence exceeded ceiling')
                stream.write(data)
                stream.flush()
                if event.get('type') != 'custom_tool_call':
                    continue
                name, args = event.get('toolName'), event.get('arguments') or {}
                try:
                    output, is_error = read_world(workload, name, args), False
                except ValueError as exc:
                    output, is_error = str(exc), True
                accepted = execution.resolve_custom_tool_call(
                    event.get('callId'), output, is_error=is_error)
                calls.append({'name': name, 'arguments': args,
                              'outputBytes': len(output.encode()),
                              'outputSha256': hashlib.sha256(output.encode()).hexdigest(),
                              'isError': is_error, 'accepted': accepted})
                if not accepted:
                    raise RuntimeError('Custom result resolution was rejected')
                if name == 'custom__read_dataset' and not is_error:
                    dataset = args['dataset']
                    if isinstance(world[dataset], list):
                        offset = args.get('offset', 0)
                        indices = read_coverage.setdefault(dataset, set())
                        indices.update(range(offset, offset + len(json.loads(output))))
                    else:
                        read_coverage[dataset] = True
            result = await execution.result_async(timeout_s=1)
        except Exception as exc:
            execution.abort()
            # Retain failure type without provider exception bodies/credentials.
            failure = type(exc).__name__
    evidence = execution.evidence_snapshot()
    private_json(directory / 'evidence.json', evidence)
    private_json(directory / 'answer.json', {
        'status': result.status if result else None,
        'finishReason': result.finish_reason if result else None,
        'content': result.content if result else None,
        'runnerFailure': failure})
    private_json(directory / 'leaf-calls.json', calls)
    programs = [p for p in evidence.get('programRuns', [])
                if p.get('source') == 'execute_program']
    usage = evidence.get('usage') or {}
    read_complete = all(key in read_coverage and (
        read_coverage[key] == set(range(len(data))) if isinstance(data, list)
        else read_coverage[key] is True) for key, data in world.items())
    if workload.task_id == 'dependent-fanout':
        read_complete = read_complete and any(
            c['name'] == 'custom__list_datasets' and c['accepted']
            and not c['isError'] for c in calls)
    from .declared_workloads import DECLARED_FAMILIES, declared_reads_complete
    if workload.family in DECLARED_FAMILIES:
        read_complete = declared_reads_complete(workload, calls)
    record = {'taskId': workload.task_id, 'family': workload.family,
              'arm': arm, 'trial': trial, 'taskSha256': workload.fingerprint,
              'wallMs': (time.monotonic_ns() - started) / 1_000_000,
              'status': result.status if result else evidence.get('status'),
              'finishReason': result.finish_reason if result else None,
              'runnerFailure': failure,
              'oraclePassed': bool(result and result.status == 'done'
                                   and result.finish_reason != 'error'
                                   and read_complete and not failure
                                   and oracle(workload, result.content)),
              'requiredReadsComplete': read_complete,
              'usage': usage, 'costUsd': usage_cost(usage, price_card),
              'apiRoundCount': len(evidence.get('apiRounds') or []),
              'leafCallCount': len(calls), 'programCount': len(programs),
              'programStats': [p.get('stats') for p in programs],
              'orchestrationDecisions': evidence.get('orchestrationDecisions', [])}
    private_json(directory / 'record.json', record)
    return record


def paired_report(records: list[dict], *, trials: int, pack: str = 'baseline') -> dict:
    expected = {(t.task_id, arm, n) for t in workload_pack(pack)
                for arm in ARMS for n in range(trials)}
    keys = [(r['taskId'], r['arm'], r['trial']) for r in records]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError('Missing, duplicate or unexpected paired records')
    pairs = []
    for t in workload_pack(pack):
        for n in range(trials):
            a, b = [next(r for r in records if (r['taskId'], r['arm'], r['trial'])
                         == (t.task_id, arm, n)) for arm in ARMS]
            pairs.append({'taskId': t.task_id, 'trial': n,
                          'baselinePassed': a['oraclePassed'],
                          'candidatePassed': b['oraclePassed'],
                          'candidatePrograms': b['programCount'],
                          'wallDeltaMs': b['wallMs'] - a['wallMs'],
                          'costDeltaUsd': (b['costUsd'] - a['costUsd']
                              if a['costUsd'] is not None and b['costUsd'] is not None
                              else None)})
    return {'diagnosticOnly': True, 'releaseClaim': False, 'taskPack':pack,
            'pairedTrials': pairs,
            'allOraclesPass': all(r['oraclePassed'] for r in records),
            'costMeasured': all(r['costUsd'] is not None for r in records),
            'caveat': 'Small synthetic pilot; no population benefit established'}


async def live_run(access, manifest, output, root, thinking, trials,
                   timeout_s, price_card):
    from tofu_agent import AgentRuntime
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {**manifest, 'modelAccess': access.public_dict(),
                'priceCard': price_card, 'startedAtUnixNs': time.time_ns()}
    private_json(output / 'manifest.json', manifest)
    records = []
    with AgentRuntime.local(model_routing=access, max_inflight=1) as runtime:
        for trial in range(trials):
            for workload in workload_pack(manifest.get('taskPack', 'baseline')):
                for arm in ARMS if trial % 2 == 0 else reversed(ARMS):
                    if source_fingerprint(root) != manifest['sourceSha256']:
                        raise RuntimeError('Source changed after preregistration')
                    directory = output / f'{workload.task_id}-{arm}-{trial}'
                    directory.mkdir(mode=0o700)
                    record = await run_task(runtime, workload, arm, trial,
                        directory, thinking, timeout_s, price_card)
                    records.append(record)
                    print(canonical({k: record[k] for k in (
                        'taskId', 'arm', 'trial', 'oraclePassed', 'programCount',
                        'wallMs', 'costUsd', 'runnerFailure')}), flush=True)
                    if not runtime.in_flight == 0:
                        raise RuntimeError('Previous execution is still active')
    if source_fingerprint(root) != manifest['sourceSha256']:
        raise RuntimeError('Source changed during paired execution')
    report = paired_report(records, trials=trials, pack=manifest.get('taskPack', 'baseline'))
    private_json(output / 'report.json', report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--pack', choices=('baseline', 'adaptive', 'declared'), default='baseline')
    parser.add_argument('--model-routing', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--price-card', type=Path)
    parser.add_argument('--thinking', choices=('low', 'medium', 'high', 'xhigh'),
                        default='medium')
    parser.add_argument('--trials', type=int, choices=range(1, 6), default=1)
    parser.add_argument('--timeout-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds must be 10..600')
    root = Path(__file__).resolve().parents[2]
    manifest = preflight(root, thinking=args.thinking, trials=args.trials,
                         timeout_s=args.timeout_seconds, pack=args.pack)
    if not args.live:
        print(canonical(manifest))
        return 0
    if args.model_routing is None or args.output is None:
        parser.error('--live requires --model-routing and --output')
    from tofu_agent import ModelRoutingConfig
    access = ModelRoutingConfig.from_mapping(json.loads(args.model_routing.read_text()))
    price_card = json.loads(args.price_card.read_text()) if args.price_card else None
    usage_cost({}, price_card)  # Reject invalid cards before paid dispatch.
    asyncio.run(live_run(access, manifest, args.output.resolve(), root,
                        args.thinking, args.trials, args.timeout_seconds, price_card))
    return 0
