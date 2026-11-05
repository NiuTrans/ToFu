"""Per-round preparation latency of compaction, request body and snapshot projection.

Replays one growing agent history (two messages appended per round) through
the production compaction pipeline, ``build_round_request`` (body, telemetry
and Request Inspector snapshot) and the persistence projection that every
``messages_snapshot`` passes before its durable append. Each run is measured
idle and while CPU-bound threads hold the GIL, which reproduces the serving
process: GIL-releasing per-item work (hashing a large buffer) waits for a
switch interval on every item there, while the idle measurement hides it.

The default history is synthetic and deterministic (seeded sizes taken from a
real 429-message root task) with the real built-in tool schemas. ``--fixture``
accepts a Request Inspector round payload (``data.messages``/``data.tools``);
keep such payloads outside the repository. Storage, providers and models are
never contacted.

Byte identity: every round prints a SHA-256 over the built request body, the
projected snapshot row and the compaction measurement. ``resultDigest`` folds
them; a before/after comparison is valid only when the digests are equal.

    python3 benchmarks/round_prep_body.py --messages 240 --rounds 8 --contention 4
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

MODEL = 'claude-opus-5.5'
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


def _signature(rng: random.Random, chars: int) -> str:
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    return ''.join(rng.choice(alphabet) for _ in range(chars))


def _size(rng: random.Random, median: float, sigma: float, cap: int) -> int:
    return max(1, min(cap, int(rng.lognormvariate(0, sigma) * median)))


def synthetic_history(count: int, seed: int) -> list[dict]:
    """Sizes follow task 75295e48 R140: tool p50 0.6K/p90 5.8K, reasoning p50 0.9K."""
    rng = random.Random(seed)
    messages = [{'role': 'system', 'content': _text(rng, 17_660)},
                {'role': 'user', 'content': _text(rng, 1_468)}]
    call = 0
    while len(messages) < count:
        if rng.random() < 0.03:
            messages.append({'role': 'user', 'content': _text(rng, _size(rng, 1_500, 1.0, 25_000))})
            continue
        call += 1
        call_id = f'toolu_{call:05d}'
        arguments = json.dumps({'command': _text(rng, _size(rng, 250, 1.0, 18_000)),
                                'description': _text(rng, 40)}, ensure_ascii=False)
        reasoning = _text(rng, _size(rng, 855, 1.2, 22_000))
        messages.append({
            'role': 'assistant', 'content': '',
            'reasoning_content': reasoning,
            'reasoning_details': [{'type': 'thinking', 'thinking': reasoning,
                                   'signature': _signature(rng, _size(rng, 1_200, 0.6, 9_000))}],
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': 'run_command', 'arguments': arguments}}],
        })
        messages.append({'role': 'tool', 'tool_call_id': call_id, 'name': 'run_command',
                         'content': _text(rng, _size(rng, 630, 1.4, 21_652))})
    return messages[:count]


def builtin_tools() -> list[dict]:
    """The enabled built-in registry surface, as the provider-compatibility spec builds it."""
    import lib.tasks_pkg.handlers  # noqa: F401 - handler-complete registry
    from lib.tools.registry import ToolContext, all_specs
    ctx = ToolContext(
        cfg={}, task_id='round-prep-body', project_path=str(ROOT),
        project_enabled=True, search_mode='multi', search_enabled=True,
        fetch_enabled=True, code_exec_enabled=True, browser_enabled=False,
        desktop_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=True, scheduler_enabled=False, messages=[],
        enabled_plugins=None)
    tools = {}
    for phase in ('base', 'capability'):
        for spec in all_specs():
            if spec.phase != phase or (spec.source != 'builtin' and not spec.host_owned):
                continue
            for tool in spec.build(ctx) or []:
                tools[tool['function']['name']] = tool
                ctx.current_count += 1
        ctx.has_base_tools = ctx.current_count > 0
    return list(tools.values())


def load_fixture(path: str) -> tuple[list[dict], list[dict]]:
    data = json.loads(Path(path).read_text())
    data = data.get('data', data)
    return list(data['messages']), list(data.get('tools') or [])


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         default=lambda item: f'<{type(item).__name__}>')
    return hashlib.sha256(encoded.encode('utf-8', errors='surrogatepass')).hexdigest()


def _without_turn_token(event: dict) -> dict:
    """``begin_round_evidence`` mints a random invocation token per run."""
    stable = dict(event)
    if 'turn' in stable:
        stable['turn'] = '<turn>'
    if isinstance(stable.get('inspectorRef'), dict):
        stable['inspectorRef'] = dict(stable['inspectorRef'], turn='<turn>')
    return stable


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


def _task(task_id: str) -> dict:
    return {'id': task_id, 'convId': '', '_userId': 1,
            'config': {'model': MODEL}, 'abort_event': threading.Event(),
            'aborted': False}


def run_rounds(history: list[dict], tools: list[dict], *, rounds: int,
               task_id: str) -> dict:
    """Replay ``rounds`` growing requests; returns per-step clocks and digests."""
    import lib.tasks_pkg.orchestrator._messages_snapshot as snapshot_module
    from lib.tasks_pkg.compaction._pipeline import run_compaction_pipeline
    from lib.tasks_pkg.compaction._tokens import _should_force_compact
    from lib.tasks_pkg.event_log import project_persistent_event
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request
    from lib.tasks_pkg.snapshot_delta import forget_projector_task
    from lib.token_counter.base import REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY

    captured: list[dict] = []
    original_append = snapshot_module.append_event
    snapshot_module.append_event = lambda _task, event: captured.append(event)
    forget_projector_task(task_id)
    task = _task(task_id)
    state = SimpleNamespace(model=MODEL, preset='medium', thinking_enabled=True)
    start = len(history) - 2 * rounds
    messages = [dict(message) for message in history[:start]]
    clocks = {'compaction': [], 'gate': [], 'admission': [], 'build_body': [],
              'projection': []}
    digests = []
    try:
        for round_index in range(rounds):
            messages.extend(dict(message) for message in history[start + 2 * round_index:
                                                                  start + 2 * round_index + 2])
            round_num = start + round_index
            started = time.perf_counter()
            run_compaction_pipeline(messages, round_num, task=task)
            clocks['compaction'].append(time.perf_counter() - started)

            measurement: dict = {}
            started = time.perf_counter()
            decision = _should_force_compact(messages, task, measurement_out=measurement)
            clocks['gate'].append(time.perf_counter() - started)

            # Admission belongs to the inputs step; it runs here only because
            # its identity receipts feed request telemetry as in the loop.
            started = time.perf_counter()
            admission = admit_round_context(task, messages, tools,
                                            round_num=round_num, model=MODEL)
            clocks['admission'].append(time.perf_counter() - started)

            captured.clear()
            started = time.perf_counter()
            _tools, body = build_round_request(
                task, state, messages, tools, round_num=round_num, tid=task_id[:8],
                thinking_depth=None, temperature=1.0, max_tokens=32_000,
                response_format=None,
                admitted_input_tokens=admission.get('totalTokens'),
                admitted_tool_schema_tokens=admission.get('toolSchemaTokens'),
                reusable_text_token_counts_by_identity=admission.pop(
                    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY, None))
            clocks['build_body'].append(time.perf_counter() - started)

            started = time.perf_counter()
            projected = [project_persistent_event(task_id, event) for event in captured]
            clocks['projection'].append(time.perf_counter() - started)

            gate = {key: measurement.get(key) for key in
                    ('message_tokens', 'message_count', 'gate_tokens', 'method',
                     'raw_estimate_tokens')}
            digests.append({'round': round_num, 'messages': len(messages),
                            'admittedTokens': admission.get('totalTokens'),
                            'body': _digest(body),
                            'projected': _digest([_without_turn_token(row) for row in projected]),
                            'gate': gate, 'decision': bool(decision),
                            'compactedMessages': _digest(messages)})
    finally:
        snapshot_module.append_event = original_append
        forget_projector_task(task_id)
    return {'clocks': clocks, 'digests': digests}


def _summary(seconds: list[float]) -> dict:
    values = sorted(value * 1000 for value in seconds)
    return {'p50': round(statistics.median(values), 2),
            'p90': round(values[min(len(values) - 1, int(0.9 * len(values)))], 2),
            'max': round(values[-1], 2)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--fixture', help='Request Inspector round payload (JSON)')
    parser.add_argument('--messages', type=int, default=240)
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--seed', type=int, default=20261028)
    parser.add_argument('--contention', type=int, default=4,
                        help='CPU-bound GIL contention threads for the contended pass')
    parser.add_argument('--json', help='write the full report here')
    args = parser.parse_args(argv)
    os.environ.setdefault('TOFU_DATA_DIR', tempfile.mkdtemp(prefix='round-prep-body-'))
    os.environ['TOFU_RUNTIME_REPLICA'] = 'off'

    if args.fixture:
        history, tools = load_fixture(args.fixture)
    else:
        history, tools = synthetic_history(args.messages, args.seed), builtin_tools()
    if len(history) <= 2 * args.rounds + 2:
        parser.error('history too short for the requested rounds')

    # Warm imports, tokenizer and caches outside the measured passes.
    run_rounds(history, tools, rounds=1, task_id='round-prep-body-warmup')
    report = {'python': sys.version.split()[0], 'messages': len(history),
              'tools': len(tools), 'rounds': args.rounds,
              'historyBytes': len(json.dumps(history, ensure_ascii=False)),
              'passes': {}}
    reference = None
    for label, threads in (('idle', 0), (f'contended{args.contention}', args.contention)):
        with Contention(threads):
            result = run_rounds(history, tools, rounds=args.rounds,
                                task_id='round-prep-body-measured')
        if reference is None:
            reference = result['digests']
        elif result['digests'] != reference:
            raise SystemExit('digest mismatch between passes: results depend on timing')
        report['passes'][label] = {step: _summary(values)
                                   for step, values in result['clocks'].items()}
    report['digests'] = reference
    report['resultDigest'] = _digest(reference)
    print(json.dumps({key: value for key, value in report.items() if key != 'digests'},
                     indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
