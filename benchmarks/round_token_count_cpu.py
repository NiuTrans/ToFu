#!/usr/bin/env python3
"""Per-round token-counting CPU of the root agent loop over long histories.

Replays one growing, append-only synthetic tool loop (reasoning, tool calls,
tool results, CJK text, code and periodic images; sized by the local tokenizer)
through every production stage of a model round that counts the request:

* ``compaction`` -- ``run_compaction_pipeline`` (L1 and the compaction gate),
* ``admission`` -- ``admit_round_context`` (final prompt admission),
* ``build_body`` -- ``build_round_request`` (body, clamp, round telemetry),
* ``adapt`` -- selected-slot adaptation (its wire model differs from the
  logical model, so the clamp estimates the input as on a model change),
* ``prepare`` -- stream ingress ``prepare_request`` (admission and, on the
  Anthropic wire, the translated recount).

``counting`` is the calling-thread CPU spent inside the token-counting entry
points those stages call (outermost call only, nested counts are not added
twice). ``tokenizedTexts``/``entropyScans`` count texts handed to the tokenizer
and to the entropy estimator per round. The economic working set is disabled so
no summary compaction runs; ``--no-l1`` also disables default L1 compaction so
the counted request keeps the history's size (L1 otherwise shrinks cold tool
results and old reasoning every round). Storage, providers and the serving
thread mix are excluded.

Byte identity: each round records the gate and admission measurements, both
clamped completion budgets, the sealed payload SHA-256 and its admitted input
tokens. ``resultDigest`` folds them; a before/after comparison is valid only
when the digests are equal. Compare trees by running this file in fresh
alternating processes with ``--source-root`` naming an exported tree.

    python3 benchmarks/round_token_count_cpu.py --tokens 50000 200000 --rounds 20
"""
from __future__ import annotations

import argparse
import contextlib
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

URL = 'https://bench.invalid/v1'
MAX_TOKENS = 64_000
STAGES = ('compaction', 'admission', 'build_body', 'adapt', 'prepare')
MODELS = {'gpt-5.6': 'openai', 'claude-opus-4.6': 'anthropic'}
_WORDS = ('the', 'request', 'cache', 'tool', 'result', 'message', 'round', 'def',
          'return', 'import', 'self', 'value', 'None', 'True', 'error', 'path',
          'storage', 'event', 'task', 'compaction', '上下文', '请求', '工具', '🙂')
# Counting entry points per module; module attributes are looked up per call.
_COUNTING_ENTRY_POINTS = (
    ('lib.tasks_pkg.compaction._tokens', '_count_tokens_authoritative'),
    ('lib.tasks_pkg.compaction._prompt_admission', '_count_tokens_authoritative'),
    ('lib.tasks_pkg.compaction._tokens', '_estimate_total_tokens'),
    ('lib.tasks_pkg.compaction._tokens', '_estimate_msg_tokens'),
    ('lib.tasks_pkg.compaction._builtin_steps._toolresults', '_estimate_msg_tokens'),
    ('lib.tasks_pkg.compaction._pipeline', '_estimate_total_tokens'),
    ('lib.llm.body._admission', 'enforce_body_input_limit'),
    ('lib.token_counter.heuristic', 'cheap_estimate'),
    ('lib.token_counter', 'count_tokens'),
    ('lib.context_telemetry', 'tool_schema_tokens'),
)


def _text(rng: random.Random, chars: int) -> str:
    words = []
    total = 0
    while total < chars:
        word = rng.choice(_WORDS)
        if rng.random() < 0.08:
            word += '\n'
        elif rng.random() < 0.02:
            word = 'def f(x):\n    return x * 2\n'
        words.append(word)
        total += len(word) + 1
    return ' '.join(words)[:chars]


def _turn(rng: random.Random, index: int) -> list[dict]:
    call_id = f'call_{index}'
    turn = []
    if index % 12 == 0:
        turn.append({'role': 'user', 'content': [
            {'type': 'text', 'text': _text(rng, 160)},
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + ''.join(
                    rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/')
                    for _ in range(4_000))}}]})
    turn += [
        {'role': 'assistant', 'content': _text(rng, rng.randrange(0, 600)),
         'reasoning_content': _text(rng, rng.randrange(200, 1800)),
         'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
             'name': 'read_files',
             'arguments': json.dumps({'path': f'lib/module_{index}.py',
                                      'end_line': rng.randrange(80, 400)})}}]},
        {'role': 'tool', 'tool_call_id': call_id,
         'content': _text(rng, rng.choice((120, 600, 2400, 6000, 14000)))},
    ]
    return turn


def tools() -> list[dict]:
    rng = random.Random(11)
    return [{'type': 'function', 'function': {
        'name': f'tool_{index}', 'description': _text(rng, 900),
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': _text(rng, 160)},
            'limit': {'type': 'integer', 'minimum': 1, 'default': 200},
            'mode': {'type': 'string', 'enum': ['head', 'tail', 'all']}},
            'required': ['path']}}} for index in range(64)]


def synthetic_history(target_tokens: int, *, model: str, seed: int) -> list[dict]:
    """Deterministic history whose local tokenizer count reaches the target."""
    from lib.token_counter import count_tokens

    rng = random.Random(seed)
    history = [{'role': 'system', 'content': _text(rng, 12_000)},
               {'role': 'user', 'content': _text(rng, 1_200)}]
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


class CountingClock:
    """Outermost-call thread CPU and tokenizer/estimator text receipts."""

    def __init__(self):
        self._local = threading.local()
        self.cpu = 0.0
        self.tokenized = 0
        self.scanned = 0
        self._restore: list[tuple[object, str, object]] = []

    def _wrap(self, function):
        def counted(*args, **kwargs):
            depth = getattr(self._local, 'depth', 0)
            if depth:
                return function(*args, **kwargs)
            self._local.depth = 1
            started = time.thread_time()
            try:
                return function(*args, **kwargs)
            finally:
                if threading.current_thread() is threading.main_thread():
                    self.cpu += time.thread_time() - started
                self._local.depth = 0
        return counted

    def _patch(self, owner, name, replacement):
        self._restore.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def __enter__(self):
        import importlib

        import lib.token_counter.tiktoken_counter as counter
        wrapped: dict[int, object] = {}
        for module_name, name in _COUNTING_ENTRY_POINTS:
            module = importlib.import_module(module_name)
            original = getattr(module, name)
            replacement = wrapped.setdefault(id(original), self._wrap(original))
            self._patch(module, name, replacement)
        encode_batch = counter._encode_batch_counts
        estimate = counter._cheap_estimate_text

        def recorded_batch(encoder, texts):
            if threading.current_thread() is threading.main_thread():
                self.tokenized += len(texts)
            return encode_batch(encoder, texts)

        def recorded_estimate(text):
            if threading.current_thread() is threading.main_thread():
                self.scanned += 1
            return estimate(text)

        self._patch(counter, '_encode_batch_counts', recorded_batch)
        self._patch(counter, '_cheap_estimate_text', recorded_estimate)
        return self

    def __exit__(self, *_exc):
        for owner, name, original in reversed(self._restore):
            setattr(owner, name, original)

    def take(self) -> tuple[float, int, int]:
        taken = (self.cpu, self.tokenized, self.scanned)
        self.cpu, self.tokenized, self.scanned = 0.0, 0, 0
        return taken


def _slot(model: str, protocol: str, window: int) -> SimpleNamespace:
    return SimpleNamespace(
        model=f'{model}-bench-deployment', logical_model=model, provider_id='bench',
        routing_provider_id='bench', route_offering_id='', route_deployment_id='',
        protocol=protocol, max_output_tokens=0, thinking_format='',
        inference_profile=None, responses_profile='', context_window=window)


def _task(task_id: str, model: str, *, l1: bool) -> dict:
    return {'id': task_id, 'convId': '', '_userId': 1,
            'config': {'model': model, 'compaction': {
                'workingSetTokens': 0, 'disableDefaultL1': not l1}},
            'abort_event': threading.Event(), 'aborted': False}


def run_rounds(history: list[dict], tool_list: list[dict], *, rounds: int, model: str,
               protocol: str, window: int, task_id: str, l1: bool = True) -> dict:
    """Replay ``rounds`` growing requests; returns per-round clocks and digests."""
    import lib.tasks_pkg.orchestrator._messages_snapshot as snapshot_module
    import lib.token_counter.tiktoken_counter as counter
    from lib.llm._sse_core import prepare_request
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    import lib.tasks_pkg.compaction._tokens as token_policy
    from lib.tasks_pkg.compaction._pipeline import run_compaction_pipeline
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request
    from lib.tasks_pkg.snapshot_delta import forget_projector_task
    from lib.token_counter.base import REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY

    counter._reset_text_count_cache_for_tests()
    original_append = snapshot_module.append_event
    snapshot_module.append_event = lambda _task, _event: None
    forget_projector_task(task_id)
    task = _task(task_id, model, l1=l1)
    state = SimpleNamespace(model=model, preset='medium', thinking_enabled=True)
    slot = _slot(model, protocol, window)
    turn_ends = [index + 1 for index, message in enumerate(history)
                 if message['role'] == 'tool']
    messages = [dict(message) for message in history[:turn_ends[-rounds - 1]]]
    clocks = {name: [] for name in (*STAGES, 'counting')}
    receipts = {'tokenizedTexts': [], 'entropyScans': []}
    digests = []
    # The root loop reuses the measured schema decomposition while the round's
    # tool list object and model stay the same; this replay keeps both.
    schema_tokens = None
    # The root loop's dispatch opens this per-round scope; older trees lack it.
    round_scope = getattr(token_policy, 'task_round_text_identities',
                          lambda _task: contextlib.nullcontext())

    def stage(name, function, *args, **kwargs):
        started = time.thread_time()
        try:
            return function(*args, **kwargs)
        finally:
            clocks[name][-1] += time.thread_time() - started

    try:
        with CountingClock() as counting:
            for round_index, turn_end in enumerate(turn_ends[-rounds:]):
                messages.extend(dict(message) for message in history[len(messages):turn_end])
                for name in STAGES:
                    clocks[name].append(0.0)
                scope = round_scope(task)
                scope.__enter__()
                stage('compaction', run_compaction_pipeline, messages, round_index, task=task)
                admission = stage('admission', admit_round_context, task, messages, tool_list,
                                  round_num=round_index, model=model,
                                  precomputed_tool_schema_tokens=schema_tokens)
                schema_tokens = admission.get('toolSchemaTokens')
                _tools, body = stage(
                    'build_body', build_round_request, task, state, messages, tool_list,
                    round_num=round_index, tid=task_id[:8], thinking_depth=None,
                    temperature=1.0, max_tokens=MAX_TOKENS, response_format=None,
                    admitted_input_tokens=admission.get('totalTokens'),
                    admitted_tool_schema_tokens=admission.get('toolSchemaTokens'),
                    reusable_text_token_counts_by_identity=admission.pop(
                        REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY, None))
                adapted = stage('adapt', _adapt_stream_body_for_slot, slot, body, True,
                                tools=None, max_tokens=None, temperature=None,
                                thinking_enabled=None, preset=None, effort=None)
                plan = stage('prepare', prepare_request, adapted, base_url=URL,
                             api_protocol=protocol)
                scope.__exit__(None, None, None)
                cpu, tokenized, scanned = counting.take()
                clocks['counting'].append(cpu)
                receipts['tokenizedTexts'].append(tokenized)
                receipts['entropyScans'].append(scanned)
                sealed = plan.body
                last = task.get('_lastPromptAdmission') or {}
                digests.append({
                    'round': round_index, 'messages': len(messages),
                    'admittedTokens': admission.get('totalTokens'),
                    'toolSchemaTokens': admission.get('toolSchemaTokens'),
                    'gate': {key: last.get(key) for key in (
                        'messageTokens', 'totalTokens', 'method', 'action')},
                    'builtMaxTokens': body.get('max_tokens'),
                    'adaptedMaxTokens': adapted.get('max_tokens'),
                    'inputTokens': sealed.input_tokens,
                    'payload': hashlib.sha256(sealed.payload).hexdigest(),
                    'compactedMessages': _digest(messages)})
    finally:
        snapshot_module.append_event = original_append
        forget_projector_task(task_id)
    return {'clocks': clocks, 'receipts': receipts, 'digests': digests}


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         default=lambda item: f'<{type(item).__name__}>')
    return hashlib.sha256(encoded.encode('utf-8', errors='surrogatepass')).hexdigest()


def _summary(seconds: list[float]) -> dict:
    values = sorted(value * 1000 for value in seconds)
    return {'p50': round(statistics.median(values), 2),
            'p90': round(values[min(len(values) - 1, int(0.9 * len(values)))], 2),
            'max': round(values[-1], 2), 'first': round(seconds[0] * 1000, 2)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--tokens', type=int, nargs='+', default=[50_000, 200_000])
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--models', nargs='+', default=list(MODELS), choices=list(MODELS))
    parser.add_argument('--seed', type=int, default=20261102)
    parser.add_argument('--contention', type=int, default=4,
                        help='CPU-bound GIL contention threads for the contended pass')
    parser.add_argument('--no-l1', action='store_true',
                        help='disable default L1 compaction so the request keeps the history size')
    parser.add_argument('--source-root', help='tree whose lib/ is measured (default: this tree)')
    parser.add_argument('--json', help='write the full report here')
    args = parser.parse_args(argv)
    source_root = Path(args.source_root).resolve() if args.source_root else ROOT
    sys.path.insert(0, str(source_root))
    os.environ.setdefault('TOFU_DATA_DIR', tempfile.mkdtemp(prefix='round-token-count-'))
    os.environ['TOFU_RUNTIME_REPLICA'] = 'off'

    import lib.token_counter.tiktoken_counter as counter
    tool_list = tools()
    report = {'python': sys.version.split()[0], 'source': counter.__file__,
              'rounds': args.rounds, 'l1': not args.no_l1, 'histories': {}}
    every_digest = []
    for model in args.models:
        protocol = MODELS[model]
        for target in args.tokens:
            history = synthetic_history(target, model=model, seed=args.seed)
            window = 1_000_000
            # Warm imports, the tokenizer and module caches outside the passes.
            run_rounds(history, tool_list, rounds=2, model=model, protocol=protocol,
                       window=window, task_id='round-token-count-warmup', l1=not args.no_l1)
            reference = None
            passes = {}
            for label, threads in (('idle', 0), (f'contended{args.contention}', args.contention)):
                with Contention(threads):
                    result = run_rounds(history, tool_list, rounds=args.rounds, model=model,
                                        protocol=protocol, window=window,
                                        task_id='round-token-count-measured',
                                        l1=not args.no_l1)
                if reference is None:
                    reference = result['digests']
                elif result['digests'] != reference:
                    raise SystemExit('digest mismatch between passes: results depend on timing')
                passes[label] = {name: _summary(values)
                                 for name, values in result['clocks'].items()}
                passes[label]['receipts'] = {
                    name: {'first': values[0],
                           'p50': statistics.median(values[1:] or values),
                           'total': sum(values)}
                    for name, values in result['receipts'].items()}
            key = f'{model}/{target}'
            report['histories'][key] = {
                'messages': len(history), 'inputTokens': reference[-1]['inputTokens'],
                'historyBytes': len(json.dumps(history, ensure_ascii=False).encode()),
                'passes': passes, 'resultDigest': _digest(reference)}
            every_digest.append(reference)
            print(f'{key} messages={len(history)} inputTokens={reference[-1]["inputTokens"]} '
                  f'resultDigest={_digest(reference)[:16]}')
            for label, measured in passes.items():
                print(f'  {label:<12}' + ' '.join(
                    f'{name}={measured[name]["p50"]}' for name in ('counting', *STAGES))
                    + f'  (cpu ms p50; counting first={measured["counting"]["first"]})'
                    + f' tokenized p50={measured["receipts"]["tokenizedTexts"]["p50"]}'
                    + f' scans p50={measured["receipts"]["entropyScans"]["p50"]}')
    report['resultDigest'] = _digest(every_digest)
    print(f"source={report['source']} resultDigest={report['resultDigest']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
