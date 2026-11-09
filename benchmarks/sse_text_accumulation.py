"""Offline provider-payload accumulation with exact output and callback receipts.

Compare alternating fresh processes with --core-source pointing to frozen
_sse_core.py revisions. Every frame passes through the real JSON/parser/progress
owner; diagnostics use a silent fixture port. Timings include construction,
callbacks and finalization, excluding imports, fixture creation and validation.
Allocation runs are separate. This is not provider or whole-task latency.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc


CASES = ('ascii', 'unicode', 'scalar', 'reasoning', 'mixed', 'readback')


class QuietDiagnostics:
    enabled = False

    def payload(self, *_args):
        pass

    def line(self, *_args):
        pass

    def finish(self, **_kwargs):
        pass

    def dump_anomaly(self, *_args, **_kwargs):
        pass


def fixture(case, count):
    if case not in CASES or count < 1:
        raise ValueError('an admitted case and positive frame count are required')
    content = ('The next step is ready.\n' * 3 if case in ('ascii', 'readback') else
               '试' if case == 'scalar' else '中文🙂\r\n\x00\ud800' * 8)
    reasoning = 'Reasoning: 准备检查。\n' * 3 if case in ('reasoning', 'mixed') else ''
    signature = 'opaque+/=\udfff' if case == 'mixed' else ''
    if case == 'reasoning':
        content = ''
    delta = {'content': content, 'reasoning_content': reasoning, 'thinking_signature': signature}
    payload = json.dumps({'choices': [{'delta': delta}]})
    message = {'role': 'assistant', 'content': content * count + ('finished' if case == 'reasoning' else '')}
    if reasoning:
        message['reasoning_content'] = reasoning * count
    if signature:
        message['thinking_signature'] = signature * count
    return payload, delta, message


def _run_once(accumulator_type, case, count, payload, *, allocations=False):
    callbacks = {name: hashlib.sha256() for name in ('content', 'reasoning_content')}
    calls = dict.fromkeys(callbacks, 0)
    snapshots = 0

    def observed(name, text):
        nonlocal snapshots
        calls[name] += 1
        callbacks[name].update(text.encode('utf-8', 'surrogatepass'))
        if case == 'readback' and name == 'content':
            snapshots += len(acc.content)

    if allocations:
        tracemalloc.start()
    wall, cpu = time.perf_counter(), time.thread_time()
    acc = accumulator_type({'model': 'gpt-5', 'messages': []}, 'measure',
                           QuietDiagnostics(), None, time.time(),
                           on_content=lambda text: observed('content', text),
                           on_thinking=lambda text: observed('reasoning_content', text))
    for _ in range(count):
        acc.feed_payload(payload)
    acc.feed_payload(json.dumps({'choices': [{'delta': {'content': 'finished'} if case == 'reasoning' else {},
                                            'finish_reason': 'stop'}],
                                'usage': {'prompt_tokens': 17, 'completion_tokens': 23}}))
    acc.feed_payload('[DONE]')
    result = acc.finalize()
    cpu, wall = time.thread_time() - cpu, time.perf_counter() - wall
    memory = tracemalloc.get_traced_memory() if allocations else None
    if allocations:
        tracemalloc.stop()
    receipt = {'message': result.message, 'state': result.state.value,
               'finish_reason': result.provider_finish_reason,
               'callback_calls': calls,
               'callback_sha256': {name: digest.hexdigest() for name, digest in callbacks.items()},
               'snapshot_characters': snapshots,
               'usage': [result.usage['prompt_tokens'], result.usage['completion_tokens']],
               'content_characters': result.evidence.content_chars,
               'reasoning_characters': result.evidence.reasoning_chars,
               'malformed_frames': result.evidence.malformed_frame_count}
    return receipt, {'wall_seconds': wall, 'cpu_seconds': cpu}, memory


def measure(accumulator_type, *, case, count, samples, allocations=False):
    if samples < 1:
        raise ValueError('samples must be positive')
    payload, delta, expected = fixture(case, count)
    expected_receipt = {'message': expected, 'state': 'provider_finished', 'finish_reason': 'stop',
        'callback_calls': {name: count if delta[name] else int(case == 'reasoning' and name == 'content')
                           for name in ('content', 'reasoning_content')},
        'callback_sha256': {name: hashlib.sha256(expected.get(name, '').encode('utf-8', 'surrogatepass')).hexdigest()
                            for name in ('content', 'reasoning_content')},
        'snapshot_characters': len(delta['content']) * count * (count + 1) // 2 if case == 'readback' else 0,
        'usage': [17, 23], 'content_characters': len(expected['content']),
        'reasoning_characters': len(delta['reasoning_content']) * count, 'malformed_frames': 0}
    raw, memory = [], []
    for sample in range(samples + 1):
        receipt, clocks, sizes = _run_once(accumulator_type, case, count, payload,
                                          allocations=allocations and sample > 0)
        assert receipt == expected_receipt, 'incomplete or changed stream evidence'
        if sample:
            if allocations:
                memory.append({'retained_bytes': sizes[0], 'peak_bytes': sizes[1]})
            else:
                raw.append(clocks)
    encoded = json.dumps(expected_receipt, sort_keys=True, separators=(',', ':')).encode()
    return {'case': case, 'frames': count, 'samples': samples,
            'fixture_sha256': hashlib.sha256(payload.encode()).hexdigest(),
            'receipt_sha256': hashlib.sha256(encoded).hexdigest(),
            'raw': memory if allocations else raw,
            'medians': {name: statistics.median(row[name] for row in (memory or raw))
                        for name in (memory or raw)[0]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-source', type=Path)
    parser.add_argument('--case', choices=CASES, default='ascii')
    parser.add_argument('--count', type=int, default=10000)
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--allocations', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    source = args.core_source or root / 'lib/llm/_sse_core.py'
    frozen = {source: source.read_bytes(), root / 'lib/llm/_stream_text.py':
              (root / 'lib/llm/_stream_text.py').read_bytes()}
    spec = importlib.util.spec_from_file_location('measured_sse_core', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(frozen[source], str(source), 'exec'), module.__dict__)
    result = measure(module.SSEAccumulator, case=args.case, count=args.count,
                     samples=args.samples, allocations=args.allocations)
    assert all(path.read_bytes() == contents for path, contents in frozen.items()), 'source changed during measurement'
    result['source_sha256'] = {str(path): hashlib.sha256(contents).hexdigest() for path, contents in frozen.items()}
    result['python'] = sys.version
    result['scope'] = 'payload parsing, text accumulation, delta callbacks and terminal result; no transport or storage'
    output = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
