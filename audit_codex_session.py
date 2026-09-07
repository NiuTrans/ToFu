#!/usr/bin/env python3
"""Measure model-turn and tool-call amplification in a Codex rollout JSONL.

This is deliberately read-only.  It accepts both ordinary function calls and
code-mode custom calls, reports token/cache totals from the final token event,
and identifies the common ``exec yielded -> wait`` extra-round pattern.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


_CALL_TYPES = frozenset({'custom_tool_call', 'function_call'})
_OUTPUT_TYPES = frozenset({'custom_tool_call_output', 'function_call_output'})
_INNER_TOOL_RE = re.compile(r'\btools\.([A-Za-z0-9_]+)\s*\(')
_RUNNING_MARKER = 'Script running with cell ID'


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        return str(value)


def audit_session(path: str | Path) -> dict[str, Any]:
    """Return stable efficiency metrics for one Codex rollout JSONL."""
    source = Path(path)
    events: list[dict[str, Any]] = []
    invalid_lines = 0
    with source.open('r', encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if isinstance(item, dict):
                events.append(item)

    calls: dict[str, dict[str, Any]] = {}
    event_types: Counter[str] = Counter()
    response_item_types: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    inner_tools: Counter[str] = Counter()
    output_chars = 0
    max_output_chars = 0
    yielded_execs = 0
    completed_durations: list[float] = []
    compactions = 0
    token_events = 0
    billed_model_turns = 0
    last_inputs: list[int] = []
    final_usage: dict[str, Any] = {}
    task_duration_ms = 0

    for event in events:
        event_type = event.get('type')
        event_types[str(event_type or '<unknown>')] += 1
        payload = event.get('payload')
        if not isinstance(payload, dict):
            payload = {}
        payload_type = payload.get('type')

        if event_type == 'compacted':
            compactions += 1
        if event_type == 'event_msg' and payload_type == 'task_complete':
            try:
                task_duration_ms = max(
                    task_duration_ms, int(payload.get('duration_ms') or 0))
            except (TypeError, ValueError):
                pass
        if event_type == 'event_msg' and payload_type == 'token_count':
            token_events += 1
            info = payload.get('info')
            if not isinstance(info, dict):
                continue
            final_usage = (info.get('total_token_usage')
                           if isinstance(info.get('total_token_usage'), dict)
                           else final_usage)
            last = info.get('last_token_usage')
            if not isinstance(last, dict):
                continue
            try:
                input_tokens = int(last.get('input_tokens') or 0)
                total_tokens = int(last.get('total_tokens') or 0)
            except (TypeError, ValueError):
                continue
            if total_tokens > 0:
                billed_model_turns += 1
                last_inputs.append(input_tokens)

        if event_type != 'response_item':
            continue
        response_item_types[str(payload_type or '<unknown>')] += 1
        if payload_type in _CALL_TYPES:
            call_id = str(payload.get('call_id') or '')
            name = str(payload.get('name') or '<unknown>')
            if call_id:
                calls[call_id] = {
                    'name': name,
                    'started': _timestamp(event.get('timestamp')),
                }
            tool_names[name] += 1
            if payload_type == 'custom_tool_call':
                raw_input = str(payload.get('input') or '')
                inner_tools.update(_INNER_TOOL_RE.findall(raw_input))
            continue
        if payload_type not in _OUTPUT_TYPES:
            continue
        text = _output_text(payload.get('output'))
        size = len(text)
        output_chars += size
        max_output_chars = max(max_output_chars, size)
        call = calls.get(str(payload.get('call_id') or ''))
        if not call:
            continue
        if call['name'] == 'exec' and _RUNNING_MARKER in text:
            yielded_execs += 1
        ended = _timestamp(event.get('timestamp'))
        started = call.get('started')
        if started is not None and ended is not None:
            completed_durations.append(max(0.0, (ended - started).total_seconds()))

    usage = {
        key: int(final_usage.get(key) or 0)
        for key in (
            'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
            'output_tokens', 'reasoning_output_tokens', 'total_tokens')
    }
    uncached = max(0, usage['input_tokens'] - usage['cached_input_tokens'])
    cache_ratio = (usage['cached_input_tokens'] / usage['input_tokens']
                   if usage['input_tokens'] else 0.0)
    wait_calls = tool_names.get('wait', 0)
    first_at = _timestamp(events[0].get('timestamp')) if events else None
    last_at = _timestamp(events[-1].get('timestamp')) if events else None

    tool_call_count = sum(tool_names.values())
    event_count = len(events)
    return {
        'path': str(source),
        'events': event_count,
        'events_by_type': dict(event_types.most_common()),
        'response_items_by_type': dict(response_item_types.most_common()),
        'protocol_events_per_tool_call': (
            event_count / tool_call_count if tool_call_count else 0.0),
        'invalid_lines': invalid_lines,
        'session_span_s': (
            max(0.0, (last_at - first_at).total_seconds())
            if first_at is not None and last_at is not None else 0.0),
        'task_duration_s': task_duration_ms / 1000.0,
        'model_turns': billed_model_turns,
        'token_events': token_events,
        'tool_calls': tool_call_count,
        'tool_calls_by_name': dict(tool_names.most_common()),
        'nested_tool_calls': sum(inner_tools.values()),
        'nested_tool_calls_by_name': dict(inner_tools.most_common()),
        'compactions': compactions,
        'yielded_execs': yielded_execs,
        'wait_calls': wait_calls,
        'avoidable_wait_rounds': min(yielded_execs, wait_calls),
        'tool_output_chars': output_chars,
        'max_tool_output_chars': max_output_chars,
        'max_tool_duration_s': max(completed_durations, default=0.0),
        'tokens': {**usage, 'uncached_input_tokens': uncached},
        'cache_hit_ratio': cache_ratio,
        'average_input_tokens_per_turn': (
            sum(last_inputs) / len(last_inputs) if last_inputs else 0.0),
        'max_input_tokens_per_turn': max(last_inputs, default=0),
    }


def _shadow_cost(metrics: dict[str, Any], args) -> float | None:
    prices = (
        args.uncached_input_per_million,
        args.cached_input_per_million,
        args.output_per_million,
    )
    if any(value is None for value in prices):
        return None
    tokens = metrics['tokens']
    return (
        tokens['uncached_input_tokens'] * prices[0]
        + tokens['cached_input_tokens'] * prices[1]
        + tokens['output_tokens'] * prices[2]
    ) / 1_000_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    parser.add_argument('--json', action='store_true', dest='as_json')
    parser.add_argument('--max-model-turns', type=int)
    parser.add_argument('--max-tool-calls', type=int)
    parser.add_argument('--max-wait-rounds', type=int)
    parser.add_argument('--uncached-input-per-million', type=float)
    parser.add_argument('--cached-input-per-million', type=float)
    parser.add_argument('--output-per-million', type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    metrics = audit_session(args.session)
    cost = _shadow_cost(metrics, args)
    if cost is not None:
        metrics['shadow_cost'] = cost
    if args.as_json:
        print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    else:
        print(f"protocol events: {metrics['events']} "
              f"({metrics['protocol_events_per_tool_call']:.1f} per tool call)")
        print(f"model turns: {metrics['model_turns']}")
        print(f"tool calls: {metrics['tool_calls']} "
              f"({metrics['tool_calls_by_name']})")
        print(f"nested calls: {metrics['nested_tool_calls']} "
              f"({metrics['nested_tool_calls_by_name']})")
        print(f"compactions: {metrics['compactions']}")
        print('avoidable exec/wait rounds: '
              f"{metrics['avoidable_wait_rounds']}")
        print('input tokens: '
              f"{metrics['tokens']['input_tokens']} "
              f"(cache hit {metrics['cache_hit_ratio']:.1%})")
        if cost is not None:
            print(f'shadow cost: {cost:.4f}')

    failed = False
    limits = (
        ('model_turns', args.max_model_turns),
        ('tool_calls', args.max_tool_calls),
        ('avoidable_wait_rounds', args.max_wait_rounds),
    )
    for metric, limit in limits:
        if limit is not None and metrics[metric] > limit:
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
