#!/usr/bin/env python3
"""Per-request CPU of model-request preparation over long agent histories.

Replays one growing, append-only synthetic tool loop (reasoning, tool calls,
tool results, CJK text and periodic images; sized by the local tokenizer)
through the production request path of every model round: ``build_body`` with
the round's admission count, selected-slot adaptation
(``_adapt_stream_body_for_slot``; the slot's wire model differs from the
logical model, so its completion clamp has no admitted count and estimates the
input, as on a model change) and stream ingress (``prepare_request``:
detachment, semantic identity, local admission, wire translation, sealing and
transport diagnostics). Each stage is measured in calling-thread CPU and wall
time, idle and beside CPU-bound GIL-contention threads.

Byte identity: every request records SHA-256 of the sealed payload, its
semantic and provider-prefix identities, the admitted input tokens and both
clamped completion budgets. ``resultDigest`` folds them; a before/after
comparison is valid only when the digests are equal. Compare trees by running
each tree's copy of this file in fresh processes. Providers, storage and the
real serving thread mix are excluded.

    python3 benchmarks/model_request_cpu.py --tokens 50000 200000 --rounds 6 --contention 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL = 'gpt-5.6'
# Each slot's wire model is the logical model plus this suffix.
WIRE_SUFFIX = '-bench-deployment'
URL = 'https://bench.invalid/v1'
MAX_TOKENS = 128_000
STAGES = ('build_body', 'adapt', 'prepare')
_WORDS = ('the', 'request', 'cache', 'tool', 'result', 'message', 'round', 'def',
          'return', 'import', 'self', 'value', 'None', 'True', 'error', 'path',
          'storage', 'event', 'task', 'compaction', '上下文', '请求', '工具')


def _text(rng: random.Random, chars: int) -> str:
    words = []
    total = 0
    while total < chars:
        word = rng.choice(_WORDS)
        if rng.random() < 0.08:
            word += '\n'
        words.append(word)
        total += len(word) + 1
    return ' '.join(words)[:chars]


def _turn(rng: random.Random, index: int) -> list[dict]:
    call_id = f'call_{index}'
    arguments = json.dumps({'path': f'lib/module_{index}.py', 'start_line': 1,
                            'end_line': rng.randrange(80, 400)})
    turn = []
    if index % 12 == 0:
        turn.append({'role': 'user', 'content': [
            {'type': 'text', 'text': _text(rng, 160)},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + ''.join(
                    rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/')
                    for _ in range(24_000))}}]})
    # Every turn ends with its tool result, so a replayed request never
    # carries an unanswered tool call.
    turn += [
        {'role': 'assistant', 'content': _text(rng, rng.randrange(80, 600)),
         'reasoning_content': _text(rng, rng.randrange(200, 1800)),
         'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
             'name': 'read_files', 'arguments': arguments}}]},
        {'role': 'tool', 'tool_call_id': call_id,
         'content': _text(rng, rng.choice((600, 2400, 6000, 14000)))},
    ]
    return turn


def tools() -> list[dict]:
    rng = random.Random(11)
    return [{'type': 'function', 'function': {
        'name': f'tool_{index}', 'description': _text(rng, 600),
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': _text(rng, 120)},
            'limit': {'type': 'integer', 'minimum': 1, 'default': 200},
            'mode': {'type': 'string', 'enum': ['head', 'tail', 'all']}},
            'required': ['path']}}} for index in range(32)]


def synthetic_history(target_tokens: int, *, seed: int, model: str = MODEL) -> list[dict]:
    """Deterministic history whose local tokenizer count reaches the target."""
    from lib.token_counter import count_tokens

    rng = random.Random(seed)
    history = [{'role': 'system', 'content': _text(rng, 6000)},
               {'role': 'user', 'content': _text(rng, 1200)}]
    index = 0
    while count_tokens(history, model=model, mode='tiktoken')['tokens'] < target_tokens:
        index += 1
        history.extend(_turn(rng, index))
    return history


class Contention:
    """CPU-bound Python threads that keep the GIL requested."""

    def __init__(self, threads: int):
        self.threads = threads
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    def _burn(self) -> None:
        value = 0
        while not self._stop.is_set():
            for item in range(2_000):
                value += item * item

    def __enter__(self):
        self._workers = [threading.Thread(target=self._burn, daemon=True,
                                          name=f'contention-{index}')
                         for index in range(self.threads)]
        for worker in self._workers:
            worker.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        for worker in self._workers:
            worker.join()


def _slot(window: int, protocol: str, model: str) -> SimpleNamespace:
    return SimpleNamespace(
        model=model + WIRE_SUFFIX, logical_model=model, provider_id='bench',
        routing_provider_id='bench', route_offering_id='', route_deployment_id='',
        protocol=protocol, max_output_tokens=0, thinking_format='',
        inference_profile=None, responses_profile='', context_window=window)


def _stage(clocks: dict, name: str, function, *args, **kwargs):
    wall, cpu = time.perf_counter(), time.thread_time()
    result = function(*args, **kwargs)
    clocks[name]['cpu'].append(time.thread_time() - cpu)
    clocks[name]['wall'].append(time.perf_counter() - wall)
    return result


def run_requests(history: list[dict], tool_list: list[dict], *, rounds: int,
                 protocol: str, window: int, model: str = MODEL) -> dict:
    """Replay ``rounds`` growing requests; returns per-stage clocks and digests."""
    from lib.llm import build_body
    from lib.llm._sse_core import prepare_request
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    from lib.token_counter import count_tokens
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY

    slot = _slot(window, protocol, model)
    turn_ends = [index + 1 for index, message in enumerate(history)
                 if message['role'] == 'tool']
    messages = [dict(message) for message in history[:turn_ends[-rounds - 1]]]
    clocks = {name: {'cpu': [], 'wall': []} for name in STAGES}
    digests = []
    for round_index, turn_end in enumerate(turn_ends[-rounds:]):
        messages.extend(dict(message) for message in history[len(messages):turn_end])
        # Round preparation admitted this prompt before the request path.
        admitted = count_tokens(messages, model=model, tools=tool_list,
                                mode='tiktoken')['tokens']
        body = _stage(clocks, 'build_body', build_body, model, messages,
                      max_tokens=MAX_TOKENS, tools=tool_list, stream=True,
                      context_window=window, precomputed_input_tokens=admitted)
        body[ADMITTED_INPUT_TOKENS_KEY] = admitted
        built_max_tokens = body.get('max_tokens')
        adapted = _stage(clocks, 'adapt', _adapt_stream_body_for_slot, slot, body, True,
                         tools=None, max_tokens=None, temperature=None,
                         thinking_enabled=None, preset=None, effort=None)
        plan = _stage(clocks, 'prepare', prepare_request, adapted, base_url=URL,
                      api_protocol=protocol)
        sealed = plan.body
        digests.append({'round': round_index, 'messages': len(messages),
                        'payload': hashlib.sha256(sealed.payload).hexdigest(),
                        'payloadBytes': len(sealed.payload),
                        'semanticId': sealed.semantic_id,
                        'prefixId': sealed.provider_prefix_id,
                        'inputTokens': sealed.input_tokens,
                        'builtMaxTokens': built_max_tokens,
                        'adaptedMaxTokens': adapted.get('max_tokens')})
    return {'clocks': clocks, 'digests': digests}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _summary(seconds: list[float]) -> dict:
    values = sorted(value * 1000 for value in seconds)
    return {'p50': round(statistics.median(values), 2),
            'p90': round(values[min(len(values) - 1, int(0.9 * len(values)))], 2),
            'max': round(values[-1], 2)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--tokens', type=int, nargs='+', default=[50_000, 200_000])
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--protocols', nargs='+', default=['openai', 'anthropic', 'responses'],
                        choices=['openai', 'anthropic', 'responses'])
    parser.add_argument('--seed', type=int, default=20261101)
    parser.add_argument('--model', default=MODEL,
                        help='logical model; Claude exercises cache markers and '
                             'Claude message adaptation')
    parser.add_argument('--contention', type=int, default=4,
                        help='CPU-bound GIL contention threads for the contended pass')
    parser.add_argument('--json', help='write the full report here')
    args = parser.parse_args(argv)
    os.environ.setdefault('TOFU_DATA_DIR', tempfile.mkdtemp(prefix='model-request-cpu-'))
    os.environ['TOFU_RUNTIME_REPLICA'] = 'off'

    import lib.llm.request as request_module
    tool_list = tools()
    report = {'python': sys.version.split()[0], 'source': request_module.__file__,
              'model': args.model, 'rounds': args.rounds, 'histories': {}}
    every_digest = []
    for target in args.tokens:
        history = synthetic_history(target, seed=args.seed, model=args.model)
        # Size the window so the clamp trims every request's completion budget.
        window = target + target // 4 + 64_000
        entry = {'messages': len(history), 'window': window,
                 'historyBytes': len(json.dumps(history, ensure_ascii=False).encode()),
                 'protocols': {}}
        for protocol in args.protocols:
            # Warm imports, tokenizer and caches outside the measured passes.
            run_requests(history, tool_list, rounds=1, protocol=protocol, window=window,
                         model=args.model)
            reference = None
            passes = {}
            for label, threads in (('idle', 0),
                                   (f'contended{args.contention}', args.contention)):
                with Contention(threads):
                    result = run_requests(history, tool_list, rounds=args.rounds,
                                          protocol=protocol, window=window,
                                          model=args.model)
                if reference is None:
                    reference = result['digests']
                elif result['digests'] != reference:
                    raise SystemExit('digest mismatch between passes: results depend on timing')
                passes[label] = {
                    stage: {clock: _summary(values)
                            for clock, values in result['clocks'][stage].items()}
                    for stage in STAGES}
                passes[label]['total'] = {
                    clock: _summary([sum(parts) for parts in zip(
                        *(result['clocks'][stage][clock] for stage in STAGES))])
                    for clock in ('cpu', 'wall')}
            entry['protocols'][protocol] = {
                'passes': passes, 'inputTokens': reference[-1]['inputTokens'],
                'payloadBytes': reference[-1]['payloadBytes'],
                'resultDigest': _digest(reference)}
            every_digest.append(reference)
        report['histories'][str(target)] = entry
    report['resultDigest'] = _digest(every_digest)
    print(f"source={report['source']} python={report['python']} model={args.model} "
          f"rounds={args.rounds}")
    for target, entry in report['histories'].items():
        for protocol, measured in entry['protocols'].items():
            print(f"history={target} messages={entry['messages']} "
                  f"inputTokens={measured['inputTokens']} "
                  f"payloadBytes={measured['payloadBytes']} protocol={protocol} "
                  f"resultDigest={measured['resultDigest'][:16]}")
            for label, stages in measured['passes'].items():
                print(f'  {label:<12}' + ' '.join(
                    f"{stage}={clocks['cpu']['p50']}/{clocks['wall']['p50']}ms"
                    for stage, clocks in stages.items()) + '  (cpu/wall p50)')
    print(f"resultDigest={report['resultDigest']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
