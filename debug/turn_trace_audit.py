#!/usr/bin/env python3
"""turn_trace_audit — where did the wall time go, across tasks and conversations.

Responsibility: aggregate DERIVED timing evidence only — the server's Turn Trace
fold (``lib/tasks_pkg/turn_trace.py``, contract
``docs/contracts/TURN_TRACE_CONTRACT.md``) — never re-read storage files or
reconstruct waits from ad-hoc log lines. Read-only.

Sources (exactly one per run):

``API`` (default)
    The running application's loopback API through
    ``debug/inspect_conversation.py``'s bounded client: same discovery from the
    configured ``PORT`` (.env, default 15000), same ``--api-url`` /
    ``--api-token-env`` bearer token rule, same loopback-only transport. It
    works for every storage backend because the service owns the read.
    Endpoints: ``GET /api/v4/conversations`` (recent conversations when no ids
    are given), ``GET /api/v4/tasks/by-conv/<conv>`` (recent tasks) and
    ``GET /api/v4/tasks/<task>/trace`` (the folded trace).
``--logs DIR``
    Offline: the one ``[TurnTraceSummary]`` line each settled task logs to
    ``DIR/app.log*`` (``docs/LOGGING.md``) — one streaming pass per file with a
    byte prefilter, no API or database access.
``--events FILE ...``
    Offline replay: re-fold exported task events with THIS checkout's fold
    (``turn_trace.fold_exported_events``). Each file is ``{"taskId": ...,
    "events": [...]}`` from paging ``GET /api/v4/tasks/<task>/events?view=durable``,
    so a fold change can be compared before/after on the same evidence.

Report: per-task and per-conversation buckets with ``waitMs`` split by span kind
(admission_wait / retry_wait / spawn_wait) and ``orchestrationMs`` split by
window (startup / round_prep / round_settle / finalize). With spans (API and
``--events``): startup stage totals, the remaining unattributed time by
position (before the first round, inside/between rounds, after the last) and
the user-visible prompt it overlapped, and the longest admission waits with
their holders. ``--json`` emits the same data.

Usage::

    python3 debug/turn_trace_audit.py [CONV_ID ...] [--recent N] [--tasks N]
    python3 debug/turn_trace_audit.py --logs logs/ [CONV_ID ...] [--since '2026-10-23 15:39']
    python3 debug/turn_trace_audit.py --events /tmp/events_*.json

Exit codes: 0 = report produced, 1 = API/IO failure, 2 = invalid usage.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from debug import inspect_conversation as inspector  # noqa: E402 - the shared loopback API client

SUMMARY_TAG = '[TurnTraceSummary]'
_SUMMARY_TAG_BYTES = SUMMARY_TAG.encode('ascii')
_LOGGED_AT_RE = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

BUCKETS = ('totalMs', 'llmMs', 'toolMs', 'waitMs', 'compactionMs',
           'approvalWaitMs', 'flowStepMs', 'orchestrationMs', 'unattributedMs')
WAIT_KINDS = (('admission_wait', 'admissionWaitMs'),
              ('retry_wait', 'retryWaitMs'),
              ('spawn_wait', 'spawnWaitMs'))
ORCHESTRATION_KINDS = (('startup', 'startupMs'),
                       ('round_prep', 'roundPrepMs'),
                       ('round_settle', 'roundSettleMs'),
                       ('finalize', 'finalizeMs'))
SPLIT_KINDS = WAIT_KINDS + ORCHESTRATION_KINDS
SUM_FIELDS = BUCKETS + tuple(field for _, field in SPLIT_KINDS) + ('ttftMs', 'rounds')
PHASE_ROWS = 15
_REQUIRED_LINE_FIELDS = ('totalMs', 'llmMs', 'toolMs', 'waitMs', 'admissionWaitMs',
                         'retryWaitMs', 'compactionMs', 'approvalWaitMs',
                         'unattributedMs', 'ttftMs', 'rounds')
ADMISSION_WAIT_ROWS = 15


# ── --logs: [TurnTraceSummary] lines ──────────────────────────────────

def parse_summary_line(line):
    """One ``[TurnTraceSummary]`` log line -> task timing record, else None.

    Tokens are ``key=value``; unknown keys are ignored and keys added after this
    reader default to 0, so the line can grow additively.
    """
    _, tag, rest = line.partition(SUMMARY_TAG)
    if not tag:
        return None
    fields = {}
    for token in rest.split():
        key, separator, value = token.partition('=')
        if separator:
            fields[key] = value
    if not fields.get('task') or any(name not in fields for name in _REQUIRED_LINE_FIELDS):
        return None
    record = {'task': fields['task'], 'conv': fields.get('conv', '-'),
              'status': fields.get('status', '-'), 'coverage': fields.get('coverage', '-')}
    try:
        for name in SUM_FIELDS:
            record[name] = int(fields.get(name, 0))
    except ValueError:
        return None
    logged_at = line[:19]
    record['loggedAt'] = logged_at if _LOGGED_AT_RE.fullmatch(logged_at) else ''
    return record


def summary_log_files(log_dir):
    """``app.log*`` regular files, oldest rotation first, the live file last."""
    paths = [path for path in Path(log_dir).glob('app.log*')
             if path.is_file() and not path.name.endswith('.lock')]
    return sorted(paths, key=lambda path: (path.name == 'app.log', path.name))


def scan_summary_logs(paths, *, since=''):
    """Stream each file once; lines without the tag are never decoded.

    A task id seen again (a later file or line) replaces the earlier record.
    Returns ``(records, duplicates)``.
    """
    records = {}
    duplicates = 0
    for path in paths:
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rb') as handle:
            for raw in handle:
                if _SUMMARY_TAG_BYTES not in raw:
                    continue
                record = parse_summary_line(raw.decode('utf-8', errors='replace'))
                if record is None or (since and record['loggedAt'] < since):
                    continue
                if record['task'] in records:
                    duplicates += 1
                    del records[record['task']]
                records[record['task']] = record
    return list(records.values()), duplicates


# ── API: folded traces through the inspector's client ─────────────────

def record_from_trace(trace, conv):
    """A settled trace document -> task timing record (same split as the server)."""
    from lib.tasks_pkg.turn_trace import trace_kind_attribution

    summary = trace['summary']
    spans = [span for span in trace.get('spans') or [] if isinstance(span, dict)]
    record = {'task': str(trace.get('taskId') or ''), 'conv': conv,
              'status': trace.get('status') or '-', 'coverage': trace.get('coverage') or '-',
              'ttftMs': int(summary.get('ttftMs') or 0),
              'rounds': sum(1 for span in spans if span.get('kind') == 'round'),
              'droppedSpans': int(trace.get('droppedSpans') or 0)}
    for name in BUCKETS:
        record[name] = int(summary.get(name) or 0)
    kinds = None
    for split in (WAIT_KINDS, ORCHESTRATION_KINDS):
        if all(type(summary.get(name)) is int for _, name in split):
            # The fold persists its exact split, whatever spans were dropped.
            for _, name in split:
                record[name] = summary[name]
            continue
        # Documents written before the persisted split: recompute from the
        # stored spans, a lower bound once spans were dropped (reported per task).
        kinds = trace_kind_attribution(trace) if kinds is None else kinds
        for kind, name in split:
            record[name] = int(kinds.get(kind) or 0)
    return record


def startup_stages_from_trace(trace):
    """Named startup stage -> ms (dispatch, executor_queue, worker_start, ...)."""
    stages = {}
    for span in trace.get('spans') or []:
        if (isinstance(span, dict) and span.get('kind') == 'startup'
                and span.get('depth') == 2 and type(span.get('tStart')) is int
                and type(span.get('tEnd')) is int):
            name = str(span.get('name') or '?')
            stages[name] = stages.get(name, 0) + max(0, span['tEnd'] - span['tStart'])
    return stages


def unattributed_by_phase(trace):
    """Where the remaining gaps sit: (position, overlapped user prompt) -> ms.

    Position is relative to round/flow-step spans; the prompt is the
    ``statusHistory`` detail key the browser could show during the gap. A
    document that dropped gaps (``droppedGaps``) yields a lower bound.
    """
    spans = [span for span in trace.get('spans') or [] if isinstance(span, dict)
             and span.get('kind') in ('round', 'flow_step')
             and type(span.get('tStart')) is int and type(span.get('tEnd')) is int]
    windows = sorted((span['tStart'], span['tEnd']) for span in spans)
    first = windows[0][0] if windows else None
    last = max((end for _, end in windows), default=None)
    prompts = []
    for entry in trace.get('statusHistory') or []:
        if not isinstance(entry, dict) or type(entry.get('tStart')) is not int:
            continue
        end = next((entry[key] for key in ('tEnd', 'lastObservedAt')
                    if type(entry.get(key)) is int), entry['tStart'])
        prompts.append((entry['tStart'], end,
                        str(entry.get('detailKey') or entry.get('phase') or '?')))
    out = {}
    for gap in trace.get('gaps') or []:
        start, end = (gap.get('tStart'), gap.get('tEnd')) if isinstance(gap, dict) else (0, 0)
        if type(start) is not int or type(end) is not int or end <= start:
            continue
        if first is None or end <= first:
            position = 'before first round'
        elif start >= last:
            position = 'after last round'
        elif any(a <= start and end <= b for a, b in windows):
            position = 'inside a round'
        else:
            position = 'between rounds'
        covered = 0
        for a, b, prompt in prompts:
            overlap = min(end, b) - max(start, a)
            if overlap > 0:
                out[(position, prompt)] = out.get((position, prompt), 0) + overlap
                covered += overlap
        if end - start > covered:
            key = (position, '(no prompt)')
            out[key] = out.get(key, 0) + end - start - covered
    return out


def collect_event_replays(paths):
    """Re-fold exported event files with this checkout's fold (offline)."""
    from lib.tasks_pkg.turn_trace import fold_exported_events

    result = {'tasks': [], 'skipped': [], 'admissionWaits': [], 'spawnWaits': [],
              'traces': [], 'incomplete': ''}
    for path in paths:
        with open(path, encoding='utf-8') as handle:
            exported = json.load(handle)
        events = exported.get('events') if isinstance(exported, dict) else exported
        task_id = (exported.get('taskId') if isinstance(exported, dict) else '') or next(
            (event.get('taskId') for event in events or () if isinstance(event, dict)
             and event.get('taskId')), '')
        conv = next((event.get('conversationId') for event in events or ()
                     if isinstance(event, dict) and event.get('conversationId')), '-')
        trace = fold_exported_events(str(task_id or path), events or [])
        if not trace.get('eventsAvailable') or not isinstance(trace.get('summary'), dict):
            result['skipped'].append({'task': str(task_id), 'conv': conv,
                                      'reason': 'no timing evidence'})
            continue
        result['tasks'].append(record_from_trace(trace, conv))
        result['admissionWaits'].extend(admission_waits_from_trace(trace, conv))
        result['spawnWaits'].extend(spawn_waits_from_trace(trace, conv))
        result['traces'].append(trace)
    return result


def admission_waits_from_trace(trace, conv):
    rows = []
    for span in trace.get('spans') or []:
        if not isinstance(span, dict) or span.get('kind') != 'admission_wait':
            continue
        start, end = span.get('tStart'), span.get('tEnd')
        if type(start) is not int or type(end) is not int:
            continue
        attrs = span.get('attrs') if isinstance(span.get('attrs'), dict) else {}
        rows.append({'task': str(trace.get('taskId') or ''), 'conv': conv,
                     'ms': max(0, end - start), 'status': span.get('status') or '-',
                     **{key: attrs.get(key) for key in (
                         'waiterTool', 'holderTool', 'holderConversation',
                         'heldMs', 'mode', 'lock', 'waiterClaims', 'holderTask',
                         'holderMode', 'holderClaims', 'holderCommand',
                         'waiterCallId', 'waiterQuery')}})
    return rows


def spawn_waits_from_trace(trace, conv):
    """announce->spawn waits with the command they delayed."""
    rows = []
    for span in trace.get('spans') or []:
        if not isinstance(span, dict) or span.get('kind') != 'spawn_wait':
            continue
        start, end = span.get('tStart'), span.get('tEnd')
        if type(start) is not int or type(end) is not int:
            continue
        attrs = span.get('attrs') if isinstance(span.get('attrs'), dict) else {}
        rows.append({'task': str(trace.get('taskId') or ''), 'conv': conv,
                     'ms': max(0, end - start),
                     **{key: attrs.get(key) for key in (
                         'toolName', 'toolCallId', 'query')}})
    return rows


def collect_api_records(api, conv_ids, *, tasks_per_conversation, recent):
    """Fold-derived records for the selected conversations' recent tasks.

    Per-task/per-conversation refusals are reported as skipped; an exhausted
    read budget stops collection and is reported as ``incomplete``.
    """
    from urllib.parse import quote

    result = {'tasks': [], 'skipped': [], 'admissionWaits': [], 'spawnWaits': [],
              'traces': [], 'incomplete': ''}
    try:
        if not conv_ids:
            page = api.get(f'/api/v4/conversations?limit={recent}')
            conv_ids = [item['id'] for item in page.get('items') or []
                        if isinstance(item, dict) and isinstance(item.get('id'), str)]
        for conv in conv_ids:
            try:
                listing = api.get(f'/api/v4/tasks/by-conv/{quote(conv, safe="")}'
                                  f'?limit={tasks_per_conversation}')
            except inspector.DiagnosticBudgetError:
                raise
            except inspector.DiagnosticAPIError as error:
                result['skipped'].append({'task': '', 'conv': conv, 'reason': str(error)})
                continue
            for row in listing.get('tasks') or []:
                task_id = row.get('taskId') if isinstance(row, dict) else None
                if not isinstance(task_id, str) or not task_id:
                    continue
                try:
                    trace = api.get(f'/api/v4/tasks/{quote(task_id, safe="")}/trace')
                except inspector.DiagnosticBudgetError:
                    raise
                except inspector.DiagnosticAPIError as error:
                    result['skipped'].append({'task': task_id, 'conv': conv,
                                              'reason': str(error)})
                    continue
                if not trace.get('eventsAvailable') or not isinstance(trace.get('summary'), dict):
                    reason = 'no timing evidence'
                elif trace.get('running'):
                    reason = 'running'
                else:
                    result['tasks'].append(record_from_trace(trace, conv))
                    result['admissionWaits'].extend(admission_waits_from_trace(trace, conv))
                    result['spawnWaits'].extend(spawn_waits_from_trace(trace, conv))
                    result['traces'].append(trace)
                    continue
                result['skipped'].append({'task': task_id, 'conv': conv, 'reason': reason})
    except inspector.DiagnosticBudgetError as error:
        result['incomplete'] = str(error)
    return result


# ── Aggregation and rendering ─────────────────────────────────────────

def _empty_totals():
    return {'tasks': 0, **{name: 0 for name in SUM_FIELDS}}


def build_report(records, *, source, skipped=(), admission_waits=(), incomplete='',
                 duplicates=0, traces=(), spawn_waits=()):
    conversations = {}
    overall = _empty_totals()
    stages, phases = {}, {}
    for trace in traces:
        for name, ms in startup_stages_from_trace(trace).items():
            stages[name] = stages.get(name, 0) + ms
        for key, ms in unattributed_by_phase(trace).items():
            phases[key] = phases.get(key, 0) + ms
    for record in records:
        totals = conversations.setdefault(record['conv'], _empty_totals())
        for target in (totals, overall):
            target['tasks'] += 1
            for name in SUM_FIELDS:
                target[name] += record[name]
    for totals in (*conversations.values(), overall):
        totals['otherWaitMs'] = totals['waitMs'] - sum(
            totals[name] for _, name in WAIT_KINDS)
    return {'source': source, 'tasks': list(records), 'conversations': conversations,
            'overall': overall, 'skipped': list(skipped), 'incomplete': incomplete,
            'duplicateLines': duplicates, 'spanEvidence': bool(traces),
            'startupStages': dict(sorted(stages.items(), key=lambda item: -item[1])),
            'unattributedByPhase': [
                {'position': position, 'prompt': prompt, 'ms': ms}
                for (position, prompt), ms in sorted(phases.items(),
                                                     key=lambda item: -item[1])],
            'admissionWaits': sorted(admission_waits, key=lambda row: -row['ms'])[
                :ADMISSION_WAIT_ROWS],
            'spawnWaits': sorted(spawn_waits, key=lambda row: -row['ms'])[
                :ADMISSION_WAIT_ROWS]}


def _seconds(ms):
    return f'{ms / 1000:9.1f}s'


def _share(ms, total):
    return f'{100 * ms / total:5.1f}%' if total else '    -'


def render_text(report, *, rows=40):
    overall = report['overall']
    total = overall['totalMs']
    reasons = {}
    for item in report['skipped']:
        reason = 'running' if item['reason'] == 'running' else (
            'no timing evidence' if item['reason'] == 'no timing evidence' else 'unavailable')
        reasons[reason] = reasons.get(reason, 0) + 1
    lines = [f"== Turn Trace audit ({report['source']}) ==",
             f"settled tasks: {overall['tasks']}; skipped: "
             + (', '.join(f'{count} {reason}' for reason, count in sorted(reasons.items()))
                or 'none')]
    if report['incomplete']:
        lines.append(f"INCOMPLETE: {report['incomplete']}")
    if report['duplicateLines']:
        lines.append(f"duplicate summary lines replaced: {report['duplicateLines']}")
    if not overall['tasks']:
        lines.append('no settled tasks with timing evidence')
        return lines
    lines.append(f"\n-- Overall: {overall['tasks']} task(s), {total / 1000:.1f}s wall --")
    for label, name in (('llm', 'llmMs'), ('tool', 'toolMs'), ('wait', 'waitMs'),
                        ('  admission_wait', 'admissionWaitMs'),
                        ('  retry_wait', 'retryWaitMs'), ('  spawn_wait', 'spawnWaitMs'),
                        ('  other wait', 'otherWaitMs'), ('compaction', 'compactionMs'),
                        ('approval', 'approvalWaitMs'), ('flow step', 'flowStepMs'),
                        ('orchestration', 'orchestrationMs'),
                        ('  startup', 'startupMs'), ('  round prep', 'roundPrepMs'),
                        ('  round settle', 'roundSettleMs'), ('  finalize', 'finalizeMs'),
                        ('unattributed', 'unattributedMs'), ('ttft (in llm)', 'ttftMs')):
        if name == 'otherWaitMs' and not overall[name]:
            continue
        lines.append(f'  {label:18s} {_seconds(overall[name])}  {_share(overall[name], total)}')
    lines.append('\n-- Per conversation --')
    lines.append(f"  {'conversation':24s} {'tasks':>5s} {'total':>10s} {'llm':>6s} "
                 f"{'tool':>6s} {'wait':>6s} {'admission':>10s} {'retry':>10s} "
                 f"{'spawn':>10s} {'unattr':>6s}")
    for conv, totals in sorted(report['conversations'].items(),
                               key=lambda item: -item[1]['totalMs']):
        conv_total = totals['totalMs']
        lines.append(
            f"  {conv[:24]:24s} {totals['tasks']:5d} {_seconds(conv_total)} "
            f"{_share(totals['llmMs'], conv_total)} {_share(totals['toolMs'], conv_total)} "
            f"{_share(totals['waitMs'], conv_total)} {_seconds(totals['admissionWaitMs'])} "
            f"{_seconds(totals['retryWaitMs'])} {_seconds(totals['spawnWaitMs'])} "
            f"{_share(totals['unattributedMs'], conv_total)}")
    ordered = sorted(report['tasks'], key=lambda record: -record['totalMs'])
    lines.append(f'\n-- Per task (largest {min(rows, len(ordered))} of {len(ordered)}) --')
    lines.append(f"  {'task':36s} {'conversation':16s} {'status':7s} {'total':>10s} "
                 f"{'llm':>10s} {'tool':>10s} {'wait':>10s} {'admission':>10s} "
                 f"{'retry':>10s} {'spawn':>10s} {'orchestr.':>10s} {'unattr':>10s} "
                 "rounds coverage")
    for record in ordered[:rows]:
        dropped = record.get('droppedSpans') or 0
        lines.append(
            f"  {record['task'][:36]:36s} {record['conv'][:16]:16s} {record['status'][:7]:7s} "
            + ' '.join(_seconds(record[name]) for name in (
                'totalMs', 'llmMs', 'toolMs', 'waitMs', 'admissionWaitMs',
                'retryWaitMs', 'spawnWaitMs', 'orchestrationMs', 'unattributedMs'))
            + f" {record['rounds']:6d} {record['coverage']}"
            + (f' droppedSpans={dropped}' if dropped else ''))
    if any(record.get('droppedSpans') for record in report['tasks']):
        lines.append('  droppedSpans: the bounded persisted trace dropped spans, so its '
                     'rounds are lower bounds. Its wait split is exact when the summary '
                     'persists it; older documents recompute it from the kept spans '
                     "(a lower bound, rest = 'other wait') and the settled task's "
                     '[TurnTraceSummary] log line (--logs) carries the exact split.')
    if report.get('startupStages'):
        lines.append('\n-- Startup stages (before the first round) --')
        for name, ms in report['startupStages'].items():
            lines.append(f'  {name:18s} {_seconds(ms)}  {_share(ms, total)}')
    if report.get('spanEvidence'):
        phases = report.get('unattributedByPhase') or []
        lines.append(f'\n-- Remaining unattributed by phase (largest {min(PHASE_ROWS, len(phases))}'
                     f' of {len(phases)}) --')
        if not phases:
            lines.append('  none: every listed gap is attributed')
        for row in phases[:PHASE_ROWS]:
            lines.append(f"  {_seconds(row['ms'])}  {row['position']:18s} {row['prompt']}")
    if report['admissionWaits']:
        lines.append('\n-- Longest admission waits --')
        for row in report['admissionWaits']:
            held = row.get('heldMs')
            lines.append(
                f"  {_seconds(row['ms'])} waiter={row.get('waiterTool') or '?'} "
                f"holder={row.get('holderTool') or 'unknown'} "
                f"holderConv={row.get('holderConversation') or '-'} "
                f"held={_seconds(held).strip() if isinstance(held, int) else '-'} "
                f"mode={row.get('mode') or '-'} {row['status']} "
                f"task={row['task']} conv={row['conv']}"
                + ''.join(f' {label}={row[key]}' for key, label in (
                    ('lock', 'lock'), ('waiterClaims', 'claims'),
                    ('waiterQuery', 'waiterCmd'),
                    ('holderTask', 'holderTask'), ('holderMode', 'holderMode'),
                    ('holderClaims', 'holderClaims'), ('holderCommand', 'holderCmd'))
                          if row.get(key)))
    if report.get('spawnWaits'):
        lines.append('\n-- Longest spawn waits (announce -> process start) --')
        for row in report['spawnWaits']:
            lines.append(
                f"  {_seconds(row['ms'])} tool={row.get('toolName') or '?'} "
                f"call={row.get('toolCallId') or '-'} task={row['task']} "
                f"cmd={str(row.get('query') or '-')[:120]}")
    return lines


# ── CLI ───────────────────────────────────────────────────────────────

def _bounded_int(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f'must be an integer in {low}..{high}') from error
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f'must be an integer in {low}..{high}')
        return number
    return parse


def _since(value):
    text = value.strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?', text):
        raise argparse.ArgumentTypeError("use 'YYYY-MM-DD[ HH:MM[:SS]]' (app.log local time)")
    return text


def _open_api(args):
    url = args.api_url
    if url is None:
        from tofu_dotenv import read_dotenv_values
        environment = {**read_dotenv_values(REPO_ROOT / '.env'), **os.environ}
        url = inspector._discover_application_api_url(environment)
    token = os.environ.get(args.api_token_env) if args.api_token_env else None
    if args.api_token_env and token is None:
        raise inspector.DiagnosticAPIError('Requested API token environment variable is unset')
    api = inspector._DiagnosticAPI(url, token)
    if args.api_max_total_mib is not None:
        api.MAX_TOTAL_BYTES = args.api_max_total_mib * 1024 * 1024
        api.MAX_REQUESTS = api.MAX_TOTAL_BYTES // (64 * 1024) + 128
    api.deadline = time.monotonic() + args.api_deadline_seconds
    return api


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Aggregate fold-derived Turn Trace timing over recent tasks (read-only).')
    parser.add_argument('conv_ids', nargs='*', type=inspector._conversation_id,
                        help='conversation ids (API default: the most recently updated)')
    parser.add_argument('--recent', type=_bounded_int(1, 100), default=10,
                        help='API: recent conversations to audit when no ids are given')
    parser.add_argument('--tasks', type=_bounded_int(1, 100), default=20,
                        help='API: most recent tasks per conversation')
    parser.add_argument('--logs', type=Path, metavar='DIR',
                        help='offline: aggregate [TurnTraceSummary] lines from DIR/app.log*')
    parser.add_argument('--since', type=_since,
                        help="--logs only: lines logged at or after 'YYYY-MM-DD[ HH:MM[:SS]]'")
    parser.add_argument('--events', type=Path, nargs='+', metavar='FILE',
                        help='offline replay: re-fold exported task events with this checkout')
    parser.add_argument('--api-url',
                        help='explicit loopback application origin; default: configured PORT')
    parser.add_argument('--api-token-env',
                        help='environment variable holding a bearer token (requires --api-url)')
    parser.add_argument('--api-max-total-mib', type=inspector._api_total_mib, default=None,
                        help='aggregate API read allowance in MiB (1..1024; default 32)')
    parser.add_argument('--api-deadline-seconds', type=float, default=120.0,
                        help='total API read deadline (default 120)')
    parser.add_argument('--rows', type=_bounded_int(1, 10_000), default=40,
                        help='per-task rows to print, largest total first')
    parser.add_argument('--json', action='store_true', help='emit the report as JSON')
    args = parser.parse_args(argv)
    if (args.logs is not None or args.events) and (args.api_url or args.api_token_env
                                                   or args.api_max_total_mib is not None):
        parser.error('--logs/--events read local files only; API flags do not apply')
    if args.logs is not None and args.events:
        parser.error('choose one source: --logs or --events')
    if args.since and args.logs is None:
        parser.error('--since applies to --logs')
    if args.api_token_env and not args.api_url:
        parser.error('--api-token-env requires --api-url')
    if not 0 < args.api_deadline_seconds <= 3600:
        parser.error('--api-deadline-seconds must be in (0, 3600]')

    if args.logs is not None:
        if not args.logs.is_dir():
            parser.error('--logs must name an existing directory')
        paths = summary_log_files(args.logs)
        try:
            records, duplicates = scan_summary_logs(paths, since=args.since or '')
        except OSError as error:
            print(f'error: cannot read logs: {error}', file=sys.stderr)
            return 1
        if args.conv_ids:
            wanted = set(args.conv_ids)
            records = [record for record in records if record['conv'] in wanted]
        report = build_report(records, duplicates=duplicates,
                              source=f'logs {args.logs} ({len(paths)} file(s))')
    elif args.events:
        try:
            collected = collect_event_replays(args.events)
        except (OSError, ValueError) as error:
            print(f'error: cannot replay events: {error}', file=sys.stderr)
            return 1
        report = build_report(collected['tasks'], skipped=collected['skipped'],
                              admission_waits=collected['admissionWaits'],
                              spawn_waits=collected['spawnWaits'],
                              traces=collected['traces'],
                              source=f'event replay ({len(args.events)} file(s))')
    else:
        try:
            api = _open_api(args)
            collected = collect_api_records(api, list(args.conv_ids),
                                            tasks_per_conversation=args.tasks,
                                            recent=args.recent)
        except inspector.DiagnosticAPIError as error:
            print(f'error: {error}', file=sys.stderr)
            return 1
        report = build_report(collected['tasks'], skipped=collected['skipped'],
                              admission_waits=collected['admissionWaits'],
                              incomplete=collected['incomplete'],
                              spawn_waits=collected['spawnWaits'],
                              traces=collected['traces'],
                              source=f'api {api.origin}')
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('\n'.join(render_text(report, rows=args.rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
