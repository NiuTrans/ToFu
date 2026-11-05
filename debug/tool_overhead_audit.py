#!/usr/bin/env python3
"""tool_overhead_audit — Tofu-side time around each tool call (read-only).

Responsibility: from one task's committed event log, measure per tool type the
time between the model finishing a tool call and the tool starting, and between
the tool ending and the next model request: the dispatch overhead that a faster
tool cannot remove. Event fields follow ``docs/contracts/EVENTS.md``; every
clock is the task execution clock (``executionAtMs`` domain). Never reads
storage files.

Sources (exactly one per run):

``--task TASK_ID`` (repeatable)
    The running application's loopback API through
    ``debug/inspect_conversation.py``'s bounded client, paging
    ``GET /api/v4/tasks/<task>/events``. A trimmed replay window yields only
    the rounds it still holds.
``--events FILE``
    A JSON list of events (or an object with an ``events`` list), e.g. a saved
    page dump; used offline and by the focused test.

Intervals per tool call (milliseconds):

``lead``      provider stream end - ``tool_start``; positive when the call was
              complete before the stream ended (streaming prefetch window).
``dispatch``  ``tool_exec`` phase - max(tool_start, stream end): stream
              settlement, round gates and the ``tools_in_flight`` event
              durability barrier before any non-prefetched tool runs.
``entry``     effect intent (``tool_effect`` entered) - ``tool_exec`` phase:
              pipeline admission, approval, hooks and handler resolution.
``spawn``     first ``tool_progress.execStartTs`` - effect intent - workspace
              admission waits: the effect-intent durability barrier plus
              command planning and process spawn (run_command only).
``admission`` workspace lease waits (``workspace_admission_wait.waitedMs``).
``span``      ``tEnd`` - max(tool_start, stream end): the call's whole
              model-visible tool phase, including the overheads above.
``settle``    ``tool_complete`` - max(``tEnd``, ``tool_exec``): result
              post-processing and terminal tool events.
Per round (attributed to the call that completed last):
``close``     ``round_end`` - last ``tool_complete``: post-tool gates and the
              ``ready`` durability barrier.
``next``      next logical ``model_request_start`` - ``round_end``: request
              preparation for the following round, including the pre-request
              checkpoint's domain sink (it no longer awaits task events).

Usage::

    python3 debug/tool_overhead_audit.py --task TASK_ID [--task ...] [--json]
    python3 debug/tool_overhead_audit.py --events saved_events.json

Exit codes: 0 = report produced, 1 = API/IO failure, 2 = invalid usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CALL_INTERVALS = ('lead', 'dispatch', 'entry', 'spawn', 'admission', 'span', 'settle')
ROUND_INTERVALS = ('close', 'next')
INTERVALS = CALL_INTERVALS + ROUND_INTERVALS
_TOOL_EVENT_TYPES = frozenset({
    'tool_start', 'tool_effect', 'tool_progress', 'tool_result', 'tool_complete',
    'workspace_admission_wait'})


def _clock(event):
    value = event.get('executionAtMs')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class _Round:
    __slots__ = ('number', 'stream_end', 'tool_exec', 'round_end', 'next_request', 'calls')

    def __init__(self, number):
        self.number = number
        self.stream_end = None
        self.tool_exec = None
        self.round_end = None
        self.next_request = None
        self.calls = []


def _observe_model_event(rounds, current, event):
    """Return the round that owns later tool events; model events open rounds."""
    kind = event['type']
    level = event.get('requestLevel')
    at = _clock(event)
    if kind == 'model_request_start' and level == 'logical':
        number = event.get('roundNum')
        if (current is not None and current.number != number
                and current.next_request is None and current.calls):
            current.next_request = at
        if number not in rounds:
            rounds[number] = _Round(number)
        return rounds[number]
    if current is None:
        return current
    if kind == 'model_request_complete' and event.get('status') == 'succeeded':
        # A succeeded provider attempt is the stream end; the logical
        # completion only stands in when no attempt-level frame exists.
        if level == 'provider_attempt':
            current.stream_end = at
        elif level == 'logical' and current.stream_end is None:
            current.stream_end = at
    elif kind == 'phase' and event.get('phase') == 'tool_exec' and current.tool_exec is None:
        current.tool_exec = at
    elif kind == 'round_end':
        # A retry closes its attempt early; the tool round's own end is last.
        current.round_end = at
    return current


def _observe_tool_event(calls, current, event):
    call_id = event.get('toolCallId')
    if not call_id:
        return
    kind = event['type']
    at = _clock(event)
    call = calls.get(call_id)
    if call is None:
        if kind != 'tool_start' or current is None:
            return
        call = {'id': call_id, 'tool': event.get('toolName') or '?',
                'round': current, 'announced': at, 'admission': 0.0}
        calls[call_id] = call
        current.calls.append(call)
        return
    if kind == 'tool_effect':
        effect = event.get('executionEffect') or {}
        if effect.get('state') == 'entered' and 'effect' not in call:
            call['effect'] = at
    elif kind == 'tool_progress':
        started = _number(event.get('execStartTs'))
        if started is not None and 'exec_start' not in call:
            call['exec_start'] = started
    elif kind == 'workspace_admission_wait':
        call['admission'] += _number(event.get('waitedMs')) or 0.0
    elif kind == 'tool_result':
        call['end'] = _number(event.get('tEnd')) or at
    elif kind == 'tool_complete':
        call['complete'] = at


def collect_rounds(events):
    """Group a task's events (in sequence order) into rounds with their calls."""
    rounds = {}
    calls = {}
    current = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            continue
        if event['type'] in _TOOL_EVENT_TYPES:
            _observe_tool_event(calls, current, event)
        else:
            current = _observe_model_event(rounds, current, event)
    return [rounds[key] for key in rounds if rounds[key].calls]


def _difference(later, earlier):
    if later is None or earlier is None:
        return None
    return later - earlier


def _call_row(call, round_):
    ready = call['announced'] if round_.stream_end is None else max(
        call['announced'], round_.stream_end)
    end = call.get('end')
    effect = call.get('effect')
    spawn = None
    if call.get('exec_start') is not None and effect is not None:
        spawn = call['exec_start'] - effect - call['admission']
    settle_base = None if end is None else max(end, round_.tool_exec or end)
    return {
        'tool': call['tool'], 'round': round_.number, 'toolCallId': call['id'],
        'lead': _difference(round_.stream_end, call['announced']),
        'dispatch': _difference(round_.tool_exec, ready),
        'entry': _difference(effect, round_.tool_exec),
        'spawn': spawn,
        'admission': call['admission'],
        'span': _difference(end, ready),
        'settle': _difference(call.get('complete'), settle_base),
        'close': None, 'next': None,
    }


def tool_overhead_rows(events):
    """One row per settled tool call; round intervals sit on its last call."""
    rows = []
    for round_ in collect_rounds(events):
        settled = [call for call in round_.calls if call.get('end') is not None]
        if not settled:
            continue
        round_rows = [_call_row(call, round_) for call in settled]
        last_index = max(range(len(settled)), key=lambda index: (
            settled[index].get('complete') or settled[index]['end']))
        last_done = settled[last_index].get('complete') or settled[last_index]['end']
        round_rows[last_index]['close'] = _difference(round_.round_end, last_done)
        round_rows[last_index]['next'] = _difference(round_.next_request, round_.round_end)
        rows.extend(round_rows)
    return rows


def _quantile(sorted_values, fraction):
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


def summarize(rows):
    """Per tool type: count plus n/p50/p90/mean/sum for every interval."""
    by_tool = {}
    for row in rows:
        by_tool.setdefault(row['tool'], []).append(row)
    summary = {}
    for tool in sorted(by_tool, key=lambda name: (-len(by_tool[name]), name)):
        tool_rows = by_tool[tool]
        stats = {}
        for interval in INTERVALS:
            values = sorted(row[interval] for row in tool_rows if row[interval] is not None)
            if not values:
                continue
            stats[interval] = {
                'n': len(values), 'p50': _quantile(values, 0.5),
                'p90': _quantile(values, 0.9), 'mean': sum(values) / len(values),
                'sum': sum(values)}
        summary[tool] = {'calls': len(tool_rows), 'intervals': stats}
    return summary


def render_text(summary, *, source):
    lines = [f'tool overhead audit — {source}',
             'ms per call: p50/p90 (n); round intervals sit on the last call of a round']
    header = f"{'tool':<16}{'calls':>6}  " + '  '.join(f'{name:>16}' for name in INTERVALS)
    lines.append(header)
    for tool, entry in summary.items():
        cells = []
        for interval in INTERVALS:
            stats = entry['intervals'].get(interval)
            cells.append(f'{"-":>16}' if stats is None else
                         f"{stats['p50']:>7.0f}/{stats['p90']:<5.0f}({stats['n']})".rjust(16))
        lines.append(f'{tool:<16}{entry["calls"]:>6}  ' + '  '.join(cells))
    return lines


# ── sources ────────────────────────────────────────────────────────────

def load_events_file(path):
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    events = payload.get('events') if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError('events file must hold a list or an object with an "events" list')
    return events


_KEPT_FIELDS = ('type', 'executionAtMs', 'roundNum', 'requestLevel', 'status', 'phase',
                'toolCallId', 'toolName', 'execStartTs', 'waitedMs', 'tEnd')


def slim_event(event):
    """Only the fields this audit reads; message snapshots dominate page bytes."""
    if not isinstance(event, dict):
        return None
    kept = {key: event[key] for key in _KEPT_FIELDS if key in event}
    effect = event.get('executionEffect')
    if isinstance(effect, dict) and 'state' in effect:
        kept['executionEffect'] = {'state': effect['state']}
    return kept


def fetch_task_events(api, task_id, *, max_pages=100_000):
    """Page one task's replay log from cursor 0 until caught up."""
    from urllib.parse import quote
    cursor = 0
    events = []
    for _page in range(max_pages):
        page = api.get(f'/api/v4/tasks/{quote(task_id, safe="")}/events?cursor={cursor}')
        batch = page.get('events') or []
        events.extend(event for event in map(slim_event, batch) if event is not None)
        following = page.get('next_cursor')
        if page.get('caught_up') or not batch or not isinstance(following, int) \
                or following <= cursor:
            return events
        cursor = following
    return events


def _open_api(args):
    from debug import inspect_conversation as inspector
    url = args.api_url
    if url is None:
        from tofu_dotenv import read_dotenv_values
        environment = {**read_dotenv_values(REPO_ROOT / '.env'), **os.environ}
        url = inspector._discover_application_api_url(environment)
    token = os.environ.get(args.api_token_env) if args.api_token_env else None
    if args.api_token_env and token is None:
        raise inspector.DiagnosticAPIError('Requested API token environment variable is unset')
    api = inspector._DiagnosticAPI(url, token)
    api.MAX_TOTAL_BYTES = args.api_max_total_mib * 1024 * 1024
    api.MAX_REQUESTS = api.MAX_TOTAL_BYTES // (64 * 1024) + 128
    api.deadline = time.monotonic() + args.api_deadline_seconds
    return api


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Per-tool Tofu dispatch overhead from task event logs (read-only).')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--task', action='append', metavar='TASK_ID',
                        help='task id to read through the loopback API (repeatable)')
    source.add_argument('--events', type=Path, metavar='FILE',
                        help='offline: JSON list of task events')
    parser.add_argument('--api-url', help='explicit loopback application origin')
    parser.add_argument('--api-token-env',
                        help='environment variable holding a bearer token (requires --api-url)')
    parser.add_argument('--api-max-total-mib', type=int, default=2048,
                        help='aggregate API read allowance in MiB (default 2048; '
                             'replay pages carry full message snapshots)')
    parser.add_argument('--api-deadline-seconds', type=float, default=600.0,
                        help='total API read deadline (default 600)')
    parser.add_argument('--rows', action='store_true', help='include per-call rows in --json')
    parser.add_argument('--json', action='store_true', help='emit the report as JSON')
    args = parser.parse_args(argv)
    if args.api_token_env and not args.api_url:
        parser.error('--api-token-env requires --api-url')
    if not 1 <= args.api_max_total_mib <= 4096:
        parser.error('--api-max-total-mib must be in 1..4096')
    if not 0 < args.api_deadline_seconds <= 3600:
        parser.error('--api-deadline-seconds must be in (0, 3600]')
    return args


def main(argv=None):
    args = _parse_args(argv)
    rows = []
    if args.events is not None:
        try:
            rows = tool_overhead_rows(load_events_file(args.events))
        except (OSError, ValueError) as error:
            print(f'error: cannot read events: {error}', file=sys.stderr)
            return 1
        source = f'events {args.events}'
    else:
        from debug import inspect_conversation as inspector
        try:
            api = _open_api(args)
            for task_id in args.task:
                rows.extend(tool_overhead_rows(fetch_task_events(api, task_id)))
        except inspector.DiagnosticAPIError as error:
            print(f'error: {error}', file=sys.stderr)
            return 1
        source = f'api {api.origin} tasks={",".join(args.task)}'
    summary = summarize(rows)
    if args.json:
        report = {'source': source, 'summary': summary}
        if args.rows:
            report['rows'] = rows
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('\n'.join(render_text(summary, source=source)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
