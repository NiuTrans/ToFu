#!/usr/bin/env python3
"""Measure model-turn and tool-call amplification in a Codex rollout JSONL.

This is deliberately read-only.  It accepts both ordinary function calls and
code-mode custom calls, reports token/cache totals from the final token event,
and records runtime item completions separately from tool-call observations.
Contract: contracts/codex_session_audit_v2.schema.json. CLI and audit_session
share this standard-library-only owner; input code is never executed or counted
as evidence of a nested invocation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any


_CALL_TYPES = frozenset({'custom_tool_call', 'function_call'})
_OUTPUT_TYPES = frozenset({'custom_tool_call_output', 'function_call_output'})
_RUNNING_MARKER = 'Script running with cell ID'
METRICS_SCHEMA_VERSION = 2


@dataclass
class _RuntimeItems:
    """Observed item identities; tool nesting and successful work are not inferred."""

    seen: set[tuple[str, str, str]] = field(default_factory=set)
    by_type: Counter[str] = field(default_factory=Counter)
    duplicates: int = 0
    unidentified: int = 0

    def observe(self, payload: dict[str, Any]) -> None:
        item = payload.get('item')
        if not isinstance(item, dict):
            self.unidentified += 1
            return
        kind = item.get('type')
        identity = (payload.get('thread_id'), payload.get('turn_id'), item.get('id'))
        if not all(isinstance(value, str) and value for value in (*identity, kind)):
            self.unidentified += 1
            return
        if identity in self.seen:
            self.duplicates += 1
            return
        self.seen.add(identity)
        self.by_type[kind] += 1

    def metrics(self) -> dict[str, Any]:
        return {
            'distinct_items': len(self.seen),
            'by_type': dict(self.by_type.most_common()),
            'duplicate_notifications': self.duplicates,
            'unidentified_notifications': self.unidentified,
        }


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


@dataclass
class _Usage:
    """Recorded token snapshots and task durations; no history payloads retained."""

    token_events: int = 0
    model_turns: int = 0
    input_tokens_sum: int = 0
    max_input_tokens: int | None = None
    final_usage: dict[str, Any] = field(default_factory=dict)
    task_duration_ms: int = 0

    def observe(self, payload: dict[str, Any]) -> None:
        kind = payload.get('type')
        if kind == 'task_complete':
            try:
                self.task_duration_ms = max(
                    self.task_duration_ms, int(payload.get('duration_ms') or 0))
            except (TypeError, ValueError):
                pass
        if kind != 'token_count':
            return
        self.token_events += 1
        info = payload.get('info')
        if not isinstance(info, dict):
            return
        totals = info.get('total_token_usage')
        if isinstance(totals, dict):
            self.final_usage = totals
        last = info.get('last_token_usage')
        if not isinstance(last, dict):
            return
        try:
            input_tokens = int(last.get('input_tokens') or 0)
            total_tokens = int(last.get('total_tokens') or 0)
        except (TypeError, ValueError):
            return
        if total_tokens > 0:
            self.model_turns += 1
            self.input_tokens_sum += input_tokens
            if self.max_input_tokens is None or input_tokens > self.max_input_tokens:
                self.max_input_tokens = input_tokens

    def metrics(self) -> dict[str, Any]:
        usage = {
            key: int(self.final_usage.get(key) or 0)
            for key in (
                'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')
        }
        uncached = max(0, usage['input_tokens'] - usage['cached_input_tokens'])
        cache_ratio = (usage['cached_input_tokens'] / usage['input_tokens']
                       if usage['input_tokens'] else 0.0)
        return {
            'task_duration_s': self.task_duration_ms / 1000.0,
            'model_turns': self.model_turns,
            'token_events': self.token_events,
            'tokens': {**usage, 'uncached_input_tokens': uncached},
            'cache_hit_ratio': cache_ratio,
            'average_input_tokens_per_turn': (
                self.input_tokens_sum / self.model_turns if self.model_turns else 0.0),
            'max_input_tokens_per_turn': (
                self.max_input_tokens if self.max_input_tokens is not None else 0),
        }


@dataclass
class _ToolCalls:
    """Response-item call/output observations and their correlation metadata."""

    calls: dict[str, tuple[str, datetime | None]] = field(default_factory=dict)
    response_types: Counter[str] = field(default_factory=Counter)
    names: Counter[str] = field(default_factory=Counter)
    output_chars: int = 0
    max_output_chars: int = 0
    yielded_execs: int = 0
    max_duration_s: float = 0.0

    def observe(self, payload: dict[str, Any], timestamp: Any) -> None:
        kind = payload.get('type')
        self.response_types[str(kind or '<unknown>')] += 1
        if kind in _CALL_TYPES:
            call_id = str(payload.get('call_id') or '')
            name = str(payload.get('name') or '<unknown>')
            if call_id:
                self.calls[call_id] = (name, _timestamp(timestamp))
            self.names[name] += 1
            return
        if kind not in _OUTPUT_TYPES:
            return
        text = _output_text(payload.get('output'))
        size = len(text)
        self.output_chars += size
        self.max_output_chars = max(self.max_output_chars, size)
        call = self.calls.get(str(payload.get('call_id') or ''))
        if call is None:
            return
        name, started = call
        if name == 'exec' and _RUNNING_MARKER in text:
            self.yielded_execs += 1
        ended = _timestamp(timestamp)
        if started is not None and ended is not None:
            self.max_duration_s = max(
                self.max_duration_s, (ended - started).total_seconds())

    def metrics(self) -> dict[str, Any]:
        wait_calls = self.names.get('wait', 0)
        return {
            'response_items_by_type': dict(self.response_types.most_common()),
            'tool_calls': sum(self.names.values()),
            'tool_calls_by_name': dict(self.names.most_common()),
            'yielded_execs': self.yielded_execs,
            'wait_calls': wait_calls,
            'avoidable_wait_rounds': min(self.yielded_execs, wait_calls),
            'tool_output_chars': self.output_chars,
            'max_tool_output_chars': self.max_output_chars,
            'max_tool_duration_s': self.max_duration_s,
        }


def audit_session(path: str | Path) -> dict[str, Any]:
    """Stream observed metrics; retain identities/counters, never payload history."""
    source = Path(path)
    usage = _Usage()
    tools = _ToolCalls()
    runtime_items = _RuntimeItems()
    event_types: Counter[str] = Counter()
    event_count = invalid_lines = compactions = 0
    first_timestamp = last_timestamp = None

    with source.open('r', encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            last_timestamp = event.get('timestamp')
            if event_count == 1:
                first_timestamp = last_timestamp
            event_type = event.get('type')
            event_types[str(event_type or '<unknown>')] += 1
            payload = event.get('payload')
            if not isinstance(payload, dict):
                payload = {}
            if event_type == 'compacted':
                compactions += 1
            elif event_type == 'event_msg':
                usage.observe(payload)
                if payload.get('type') == 'item_completed':
                    runtime_items.observe(payload)
            elif event_type == 'response_item':
                tools.observe(payload, last_timestamp)

    usage_metrics = usage.metrics()
    first_at, last_at = _timestamp(first_timestamp), _timestamp(last_timestamp)
    tool_metrics = tools.metrics()
    tool_call_count = tool_metrics['tool_calls']
    return {
        'schema_version': METRICS_SCHEMA_VERSION,
        'path': str(source),
        'events': event_count,
        'events_by_type': dict(event_types.most_common()),
        'protocol_events_per_tool_call': (
            event_count / tool_call_count if tool_call_count else 0.0),
        'invalid_lines': invalid_lines,
        'session_span_s': (
            max(0.0, (last_at - first_at).total_seconds())
            if first_at is not None and last_at is not None else 0.0),
        'compactions': compactions,
        'nested_tool_calls': None,
        'nested_tool_calls_by_name': None,
        'nested_tool_calls_observation': 'unavailable',
        'runtime_item_completions': runtime_items.metrics(),
        **tool_metrics,
        **usage_metrics,
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
        print('nested calls: unavailable (no complete nested invocation receipts)')
        items = metrics['runtime_item_completions']
        print(f"runtime item completions: {items['distinct_items']} "
              f"({items['by_type']})")
        print(f"compactions: {metrics['compactions']}")
        print('exec/wait heuristic upper bound: '
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
