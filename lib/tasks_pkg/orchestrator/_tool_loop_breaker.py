"""Tool-progress circuit breaker for the main orchestrator loop.

This module owns two complementary guards at the post-dispatch seam:

* exact repetition: the same calls return the same outcomes in consecutive
  LLM rounds;
* semantic stalling: syntactically different calls still make no progress.

The second guard closes the gap exposed by conversation ``mt2hn4018vm5rh``.
The model repeatedly narrated an imminent ``edit_file`` call, but emitted
``run_command(command='true', description='noop')`` interleaved with narrower
``read_files`` ranges.  Every call executed successfully, so a harness that
equated successful execution with progress let 43 provider rounds run.  The
older exact-digest guard also reset on every changed read range.

Classification is structural, never based on the model's prose:

* an entire shell command that is exactly ``true``, ``:``, or ``exit 0`` is
  an explicit sacrificial no-op;
* a successful ``read_files`` range wholly covered by earlier reads is
  redundant unless the same request now returned changed content;
* a real state-changing tool clears the semantic episode and read coverage.

One bounded corrective user-lane message is injected before force-stop.  It
also covers exact repetition: after the fourth byte-identical call+outcome the
model gets one explicit instruction to change strategy; one more identical
round stops.  Semantic stalls receive the same bounded treatment.  Normal
adjacent/chunked reads and polling calls whose output changes remain
productive.

All breaker bookkeeping lives under the task-owned transient key
``_tool_loop_guard``.  It deliberately does not expand ``RoundState``: that
carrier has an owner-ruled 14-field wire-parity contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

_STATE_KEY = '_tool_loop_guard'
_READ_TOOL = 'read_files'
_SHELL_TOOL_NAMES = frozenset({'run_command', 'code_exec'})
_LINE_INFINITY = 2**63 - 1
_MAX_TRACKED_READ_PATHS = 512
_MAX_TRACKED_READ_REQUESTS = 1024

# Whole-command matches only.  ``true && pytest`` and ``printf true`` are real
# commands and must never be classified as no-ops.
_EXPLICIT_NOOP_RE = re.compile(
    r'^(?:(?:/usr/bin/|/bin/)?true|:|exit\s+0)\s*;?\s*(?:#.*)?$',
    re.IGNORECASE,
)

# ``read_files`` receipts are authoritative about what was actually returned.
# This matters because small project files auto-expand a requested range to the
# whole file, while a growing file may contain fewer lines than an open-ended
# request implied.  Keep the parser tied to read_tools.py's explicit header
# contract instead of guessing from body text.
_FILE_HEADER_RE = re.compile(
    r'^File:\s+.+?\s+\((?P<meta>[^)\n]+)\)(?P<suffix>[^\n]*)$',
    re.MULTILINE,
)
_RANGE_META_RE = re.compile(
    r'^lines\s+(?P<start>\d+)-(?P<end>\d+)\s+of\s+\d+$')
_TOTAL_META_RE = re.compile(r'^(?P<total>\d+)\s+lines(?:,|$)')
_PREVIEW_SUFFIX_RE = re.compile(r'showing first\s+(?P<shown>\d+)\s+lines')

_VISIBLE_ACTION_ORDER = (
    'edit_file', 'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents', 'run_command',
)
_BUILTIN_FILE_WRITE_TOOLS = frozenset({
    'edit_file', 'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})
_BATCH_WRITE_RECEIPT_RE = re.compile(
    r'(?:^|\n)(?:Applied|Inserted)\s+(?P<ok>\d+)/\d+\s+edits\b',
    re.IGNORECASE,
)


def _rows_for_round(task: dict[str, Any], round_num: int) -> list[dict[str, Any]]:
    rows = [
        row for row in (task.get('toolRounds') or [])
        if isinstance(row, dict) and row.get('llmRound') == round_num
    ]
    rows.sort(key=lambda row: row.get('roundNum') or 0)
    return rows


def _parse_args(row: dict[str, Any]) -> dict[str, Any]:
    args = row.get('toolArgs')
    if isinstance(args, dict):
        return args
    if not isinstance(args, str) or not args.strip():
        return {}
    try:
        parsed = json.loads(args)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _canonical_args(row: dict[str, Any]) -> str:
    args = row.get('toolArgs')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return args
    try:
        return json.dumps(
            args or {}, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), default=str,
        )
    except (TypeError, ValueError):
        return str(args or '')


def _outcome_text(row: dict[str, Any]) -> str:
    content = row.get('toolContent')
    if content is not None:
        return str(content)
    return json.dumps(
        row.get('results') or [], sort_keys=True,
        ensure_ascii=False, default=str,
    )


def _round_loop_digest(task: dict[str, Any], round_num: int) -> str:
    """Hash every call and outcome produced by one LLM round."""
    rows = _rows_for_round(task, round_num)
    if not rows:
        return ''
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row.get('toolName') or '').encode('utf-8', 'replace'))
        digest.update(b'\x00')
        digest.update(_canonical_args(row).encode('utf-8', 'replace'))
        digest.update(b'\x00')
        digest.update(str(row.get('status') or '').encode('utf-8', 'replace'))
        digest.update(b'\x00')
        digest.update(_outcome_text(row).encode('utf-8', 'replace'))
        digest.update(b'\x01')
    return digest.hexdigest()


def _first_repeated_call_label(task: dict[str, Any], round_num: int) -> str:
    """Return a bounded human-readable label for the round's first call."""
    rows = _rows_for_round(task, round_num)
    if not rows:
        return '?'
    first = rows[0]
    return '%s(%.120s)' % (
        first.get('toolName') or '?', _canonical_args(first),
    )


def _guard_state(task: dict[str, Any]) -> dict[str, Any]:
    state = task.get(_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        task[_STATE_KEY] = state
    state.setdefault('last_round_digest', '')
    state.setdefault('identical_repeat_count', 0)
    state.setdefault('exact_nudge_digest', '')
    state.setdefault('explicit_noop_rounds', [])
    state.setdefault('semantic_nudge_count', 0)
    state.setdefault('redundant_read_streak', 0)
    state.setdefault('read_coverage', {})
    state.setdefault('read_result_by_request', {})
    return state


def _is_explicit_noop_row(row: dict[str, Any]) -> bool:
    if str(row.get('toolName') or '') not in _SHELL_TOOL_NAMES:
        return False
    command = _parse_args(row).get('command')
    return isinstance(command, str) and bool(
        _EXPLICIT_NOOP_RE.fullmatch(command.strip()))


def _write_result_meta_verdict(row: dict[str, Any]) -> bool | None:
    """Read the project handler's authoritative per-write result metadata."""
    saw_verdict = False
    for result in row.get('results') or []:
        if not isinstance(result, dict):
            continue
        write_ok = result.get('writeOk')
        if isinstance(write_ok, bool):
            saw_verdict = True
            if write_ok:
                return True
        summaries = result.get('editSummaries')
        if isinstance(summaries, list):
            saw_verdict = True
            if any(
                isinstance(summary, dict) and summary.get('status') == 'ok'
                for summary in summaries
            ):
                return True
    return False if saw_verdict else None


def _row_has_confirmed_write(row: dict[str, Any]) -> bool:
    """True only when a completed write row proves some state was written."""
    if _row_failed(row):
        return False
    status = str(row.get('status') or '').lower()
    if status and status != 'done':
        return False

    name = str(row.get('toolName') or '')
    if name not in _BUILTIN_FILE_WRITE_TOOLS:
        # Registry-declared writes outside the built-in project family do not
        # share its receipt contract.  A successful terminal verdict is the
        # strongest generic evidence available for those extension tools.
        return True

    meta_verdict = _write_result_meta_verdict(row)
    if meta_verdict is not None:
        return meta_verdict

    # Compatibility for old/checkpointed rows without result metadata.  These
    # prefixes are the model-facing contracts in project_mod/tools.py.
    content = _outcome_text(row).lstrip()
    batch_match = _BATCH_WRITE_RECEIPT_RE.search(content)
    if batch_match:
        return int(batch_match.group('ok')) > 0
    low = content.lower()
    if name == 'write_file':
        return low.startswith(('file created:', 'file updated:'))
    if name == 'apply_diff':
        return low.startswith('applied diff to ')
    if name == 'insert_content':
        return low.startswith('inserted ')
    return False


def _round_action_kind(
    task: dict[str, Any], rows: list[dict[str, Any]],
) -> str:
    """Return ``confirmed_write``, ``opaque_action``, or empty string.

    The dispatcher registry is the classification authority.  ``code_exec``
    is included explicitly because project ``run_command`` rows can be stored
    under that display name after execution.  A shell command is deliberately
    NOT promoted to ``confirmed_write`` merely because the dispatcher's
    concurrency partition treats all run_command calls as writes: ``pwd`` and
    test commands must not erase a prior no-op warning.  They can still make
    old file observations stale, so the caller clears read coverage for them.
    """
    try:
        from lib.tasks_pkg.tool_dispatch._flags import _task_partitions
        write_tools, _ = _task_partitions(task)
    except Exception as exc:  # fail open: never stop healthy work
        logger.debug('[ToolProgress] write partition unavailable: %s', exc)
        write_tools = frozenset(_VISIBLE_ACTION_ORDER)
    saw_opaque_action = False
    for row in rows:
        name = str(row.get('toolName') or '')
        if _is_explicit_noop_row(row):
            continue
        if name in _SHELL_TOOL_NAMES:
            saw_opaque_action = True
            continue
        if name in write_tools:
            if _row_has_confirmed_write(row):
                return 'confirmed_write'
            saw_opaque_action = True
    return 'opaque_action' if saw_opaque_action else ''


def _clear_read_observations(state: dict[str, Any]) -> None:
    state['redundant_read_streak'] = 0
    state['read_coverage'] = {}
    state['read_result_by_request'] = {}


def _reset_semantic_episode(state: dict[str, Any]) -> None:
    """A real action invalidates old read evidence and earns a fresh budget."""
    state['explicit_noop_rounds'] = []
    state['semantic_nudge_count'] = 0
    _clear_read_observations(state)


def _line_number(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _read_specs(row: dict[str, Any]) -> list[tuple[str, int, int]]:
    args = _parse_args(row)
    raw_specs = args.get('reads')
    if not isinstance(raw_specs, list):
        raw_specs = [args] if args.get('path') else []
    specs: list[tuple[str, int, int]] = []
    for raw in raw_specs:
        if isinstance(raw, str):
            raw = {'path': raw}
        if not isinstance(raw, dict):
            continue
        path = str(raw.get('path') or '').strip()
        if not path:
            continue
        while path.startswith('./'):
            path = path[2:]
        start = _line_number(raw.get('start_line'), 1)
        end = _line_number(raw.get('end_line'), _LINE_INFINITY)
        if start > end:
            start, end = end, start
        specs.append((path, start, end))
    return specs


def _effective_read_specs(
    row: dict[str, Any], requested: list[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    """Replace requested bounds with receipt bounds when they align by count."""
    returned_bounds: list[tuple[int, int]] = []
    for match in _FILE_HEADER_RE.finditer(_outcome_text(row)):
        meta = match.group('meta').strip()
        range_match = _RANGE_META_RE.fullmatch(meta)
        if range_match:
            returned_bounds.append((
                int(range_match.group('start')),
                int(range_match.group('end')),
            ))
            continue
        total_match = _TOTAL_META_RE.match(meta)
        if not total_match:
            continue
        preview_match = _PREVIEW_SUFFIX_RE.search(match.group('suffix') or '')
        end = (int(preview_match.group('shown')) if preview_match
               else int(total_match.group('total')))
        returned_bounds.append((1, max(1, end)))

    if len(returned_bounds) != len(requested):
        return requested
    return [
        (path, returned_start, returned_end)
        for (path, _, _), (returned_start, returned_end)
        in zip(requested, returned_bounds)
    ]


def _add_read_coverage(
    coverage: dict[str, list[list[int]]], path: str, start: int, end: int,
) -> bool:
    """Merge one interval and report whether it covers any previously unseen line."""
    if path not in coverage and len(coverage) >= _MAX_TRACKED_READ_PATHS:
        coverage.pop(next(iter(coverage)), None)
    existing = coverage.get(path) or []
    added = not any(left <= start and right >= end for left, right in existing)
    merged: list[list[int]] = []
    next_left, next_right = start, end
    for left, right in existing:
        if right + 1 < next_left:
            merged.append([left, right])
        elif next_right + 1 < left:
            merged.append([next_left, next_right])
            next_left, next_right = left, right
        else:
            next_left = min(next_left, left)
            next_right = max(next_right, right)
    merged.append([next_left, next_right])
    coverage[path] = merged
    return added


def _row_failed(row: dict[str, Any]) -> bool:
    status = str(row.get('status') or '').lower()
    if status in {'error', 'rejected', 'aborted', 'timeout', 'failed'}:
        return True
    content = _outcome_text(row).lstrip().lower()
    return content.startswith((
        'error:', 'rejected:', 'timed out', 'file not found:',
        'file too large', 'error reading ',
    ))


def _read_round_added_evidence(
    rows: list[dict[str, Any]], state: dict[str, Any],
) -> bool:
    """Record read observations and return True iff this round learned something new."""
    coverage = state['read_coverage']
    results = state['read_result_by_request']
    added_evidence = False
    for row in rows:
        request_key = hashlib.sha256(
            _canonical_args(row).encode('utf-8', 'replace')).hexdigest()
        outcome_digest = hashlib.sha256(
            (str(row.get('status') or '') + '\x00' + _outcome_text(row))
            .encode('utf-8', 'replace')).hexdigest()
        previous_outcome = results.get(request_key)
        if request_key not in results and len(results) >= _MAX_TRACKED_READ_REQUESTS:
            results.pop(next(iter(results)), None)
        results[request_key] = outcome_digest

        requested_specs = _read_specs(row)
        specs = _effective_read_specs(row, requested_specs)
        if _row_failed(row) or not specs:
            # A first failure is evidence; repeating the same failure is not.
            if previous_outcome != outcome_digest:
                added_evidence = True
            continue

        coverage_added = False
        # Do not use ``any(generator)`` here: it short-circuits after the
        # first novel batch member and would leave later paths unrecorded.
        for path, start, end in specs:
            if _add_read_coverage(coverage, path, start, end):
                coverage_added = True
        same_request_changed = (
            previous_outcome is not None and previous_outcome != outcome_digest
        )
        if coverage_added or same_request_changed:
            added_evidence = True
    return added_evidence


def _visible_action_tools(task: dict[str, Any]) -> list[str]:
    catalog = task.get('_tool_schema')
    if not isinstance(catalog, list):
        catalog = task.get('_executable_tool_catalog')
    names: set[str] = set()
    for tool in catalog or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function')
        name = (fn.get('name') if isinstance(fn, dict) else '') \
            or tool.get('name') or ''
        if name:
            names.add(str(name))
    return [name for name in _VISIBLE_ACTION_ORDER if name in names]


def _nudge_text(task: dict[str, Any], reason: str) -> str:
    tools = _visible_action_tools(task)
    visible = (', '.join(tools) if tools
               else 'a substantive tool that is visible in this turn')
    if reason == 'exact_repetition':
        return (
            '[SYSTEM: IDENTICAL TOOL LOOP DETECTED]\n'
            'The same tool call has now returned the exact same outcome four '
            'rounds in a row. Repeating it again cannot make progress.\n\n'
            'Change strategy now: inspect genuinely new evidence, use '
            'different arguments or another available tool, or finish and '
            'state the concrete blocker. Do not issue the identical call '
            'again; one more identical round will be stopped by the harness.'
        )
    if reason == 'explicit_noop':
        observation = (
            'The previous shell tool executed only an explicit no-op '
            '(`true`, `:`, or `exit 0`), so it changed no state and returned '
            'no evidence.'
        )
    else:
        observation = (
            'The recent read_files calls only re-read file lines already '
            'returned in this turn, and no intervening state change made the '
            'old observation stale.'
        )
    return (
        '[SYSTEM: TOOL ROUND MADE NO SEMANTIC PROGRESS]\n'
        f'{observation}\n\n'
        'Do not issue placeholder tools or re-read covered evidence merely to '
        'obtain another model round. Continue reasoning in this response, then '
        f'either issue the real next action ({visible}), inspect genuinely new '
        'evidence, or finish and state the concrete blocker. A further '
        'no-progress tool round will be stopped by the harness.'
    )


def _inject_semantic_nudge(
    task: dict[str, Any], state: dict[str, Any], messages: list | None,
    *, reason: str, round_num: int, tid: str,
) -> None:
    prompt = _nudge_text(task, reason)
    if messages is not None:
        messages.append({'role': 'user', 'content': prompt})
    state['semantic_nudge_count'] += 1
    task.setdefault('_toolLoopNudges', []).append({
        'round': round_num + 2,
        'reason': reason,
        'prompt': prompt,
        'max': 1,
    })
    logger.warning(
        '[%s] conv=%s Tool-progress correction injected after round %d: '
        'reason=%s visible_actions=%s',
        tid, task.get('convId', ''), round_num + 1, reason,
        _visible_action_tools(task),
    )


def _force_stop(
    task: dict[str, Any], rs: Any, *, round_num: int, tid: str,
    reason: str, detail: str, raw: str,
) -> bool:
    logger.error(
        '[%s] conv=%s FORCE STOP: tool loop reason=%s round=%d '
        'model=%s',
        tid, task.get('convId', ''), reason, round_num + 1, rs.model,
    )
    from lib.error_envelope import make_envelope
    task['error'] = make_envelope(
        'tool_loop', detail=detail, model=rs.model,
        context='tool-loop', source='orchestrator', raw=raw,
    )
    rs.exit_reason = reason
    append_event(task, build_event(
        EventType.ROUND_END, roundNum=round_num, reason='tool_loop'))
    return True


def _handle_exact_repetition(
    task: dict[str, Any], rs: Any, state: dict[str, Any], *,
    messages: list | None, round_num: int, tid: str,
    max_consecutive_identical: int,
) -> bool:
    digest = _round_loop_digest(task, round_num)
    if not digest or digest != state['last_round_digest']:
        state['identical_repeat_count'] = 0
        state['last_round_digest'] = digest
        state['exact_nudge_digest'] = ''
        return False

    state['identical_repeat_count'] += 1
    repeats = state['identical_repeat_count']
    logger.warning(
        '[%s] conv=%s Identical tool round at round %d — same calls, same '
        'outcomes (%d/%d consecutive repeats) model=%s call=%s',
        tid, task.get('convId', ''), round_num + 1, repeats,
        max_consecutive_identical, rs.model,
        _first_repeated_call_label(task, round_num),
    )
    if repeats < max_consecutive_identical:
        return False

    # Production always supplies the model message list. Give an exact loop
    # one bounded chance to recover before surfacing a terminal error; isolated
    # callers without a message lane retain the fail-fast safety behavior.
    if messages is not None and state['exact_nudge_digest'] != digest:
        state['exact_nudge_digest'] = digest
        _inject_semantic_nudge(
            task, state, messages, reason='exact_repetition',
            round_num=round_num, tid=tid,
        )
        return False

    repeated = _first_repeated_call_label(task, round_num)
    return _force_stop(
        task, rs, round_num=round_num, tid=tid,
        reason=f'consecutive_identical_rounds_{repeats}',
        detail=(
            f'The model repeated the exact same tool call {repeats + 1} '
            'times in a row and every repetition returned the same result. '
            'The task was stopped instead of spending more requests. '
            f'Repeated call: {repeated}'
        ),
        raw=f'consecutive_identical_rounds={repeats}',
    )


def _handle_semantic_stall(
    task: dict[str, Any], rs: Any, state: dict[str, Any],
    messages: list | None, *, round_num: int, tid: str,
    max_explicit_noop_rounds: int, max_redundant_read_rounds: int,
) -> bool:
    rows = _rows_for_round(task, round_num)
    if not rows:
        state['redundant_read_streak'] = 0
        return False

    action_kind = _round_action_kind(task, rows)
    if action_kind == 'confirmed_write':
        _reset_semantic_episode(state)
        return False
    if action_kind == 'opaque_action':
        # Shell and failed writes are opaque: they may have partially changed
        # files, so stale reads cannot be compared safely.  Neither is proof
        # that the model recovered from a sacrificial-noop episode, however.
        _clear_read_observations(state)
        return False

    if any(_is_explicit_noop_row(row) for row in rows):
        noop_rounds = state['explicit_noop_rounds']
        noop_rounds.append(round_num)
        if (len(noop_rounds) >= max_explicit_noop_rounds
                or state['semantic_nudge_count'] >= 1):
            return _force_stop(
                task, rs, round_num=round_num, tid=tid,
                reason='semantic_noop_tool_loop',
                detail=(
                    'The model used an explicit shell no-op as a placeholder '
                    'after the harness had already instructed it to perform a '
                    'substantive action, inspect new evidence, or finish. The '
                    'task was stopped to prevent a sacrificial-tool loop.'
                ),
                raw=(f'explicit_noop_rounds={noop_rounds};'
                     f'nudges={state["semantic_nudge_count"]}'),
            )
        _inject_semantic_nudge(
            task, state, messages, reason='explicit_noop',
            round_num=round_num, tid=tid,
        )
        return False

    is_read_only = all(
        str(row.get('toolName') or '') == _READ_TOOL for row in rows
    )
    if not is_read_only:
        state['redundant_read_streak'] = 0
        return False

    if _read_round_added_evidence(rows, state):
        state['redundant_read_streak'] = 0
        return False

    state['redundant_read_streak'] += 1
    streak = state['redundant_read_streak']
    logger.warning(
        '[%s] conv=%s Redundant read-only round %d: already-covered evidence '
        '(%d/%d) model=%s',
        tid, task.get('convId', ''), round_num + 1, streak,
        max_redundant_read_rounds, rs.model,
    )
    if streak < max_redundant_read_rounds:
        return False
    if state['semantic_nudge_count'] < 1:
        _inject_semantic_nudge(
            task, state, messages, reason='redundant_reads',
            round_num=round_num, tid=tid,
        )
        return False
    return _force_stop(
        task, rs, round_num=round_num, tid=tid,
        reason='semantic_redundant_read_loop',
        detail=(
            'The model continued calling read_files only for line ranges '
            'already returned in this turn after a corrective instruction. '
            'No new evidence or state change was produced, so the task was '
            'stopped instead of spending more requests.'
        ),
        raw=(f'redundant_read_streak={streak};'
             f'nudges={state["semantic_nudge_count"]}'),
    )


def handle_tool_loop_circuit_breaker(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
    messages: list | None = None,
    max_consecutive_identical: int = 3,
    max_explicit_noop_rounds: int = 2,
    max_redundant_read_rounds: int = 3,
) -> bool:
    """Observe the completed tool round; return True when the loop must stop."""
    state = _guard_state(task)
    if _handle_exact_repetition(
        task, rs, state, messages=messages, round_num=round_num, tid=tid,
        max_consecutive_identical=max_consecutive_identical,
    ):
        return True
    return _handle_semantic_stall(
        task, rs, state, messages, round_num=round_num, tid=tid,
        max_explicit_noop_rounds=max_explicit_noop_rounds,
        max_redundant_read_rounds=max_redundant_read_rounds,
    )
