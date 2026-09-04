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


Three extended stagnation detectors cover loop shapes the two base guards
cannot see, all derived from the completed tool rows only:

* persistent identical failure (``TOFU_LOOP_FAIL_*``): an opaque shell
  command keeps exiting non-zero with the same normalized error tail across
  rounds, even while edits happen in between;
* success polling (``TOFU_LOOP_POLL_*``): the same command set exits 0 again
  and again with no intervening state change (verification theatre);
* observation-only stall (``TOFU_LOOP_OBS_*``): many consecutive rounds that
  only inspect — read-only tools or provably side-effect-free shell
  commands — without ever acting on the evidence.

Each escalates the same way: one corrective user-lane message, a bounded
grace window, then termination.  Success polling terminates as a CLEAN
finish (``_toolLoopCleanFinish`` settles finishReason=stop downstream): the
verified state is the deliverable.  The other two stop with a tool_loop
error.  Classification always errs toward "the round did something real":
any unrecognized shell command counts as a potential state change and
resets the observation/poll streaks, so exploratory or unusual-but-
productive commands are never punished.  ``TOFU_LOOP_EXTENDED=0`` disables
all three; every threshold is env-overridable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tool_round_replay import is_superseded_provider_attempt_round

logger = get_logger(__name__)

_STATE_KEY = '_tool_loop_guard'
_READ_TOOL = 'read_files'
_SHELL_TOOL_NAMES = frozenset({'run_command', 'code_exec'})
_LINE_INFINITY = 2**63 - 1
_MAX_TRACKED_READ_PATHS = 512
_MAX_TRACKED_READ_REQUESTS = 1024
_MAX_TRACKED_EVIDENCE_IDS = 1024
_MAX_PENDING_ARTIFACT_SCOPES = 256
_ARTIFACT_CONTINUATION_TOOLS = frozenset({
    'read_tool_artifact', 'search_tool_artifact',
})
_WINDOW_ARGUMENT_KEYS = frozenset({
    'after', 'before', 'context_lines', 'cursor', 'end_line',
    'include_tool_details', 'limit', 'max_chars', 'max_results', 'offset',
    'page', 'page_size', 'raw', 'start_line', 'top_k',
})
_PROGRAMMATIC_SERIAL_READ_THRESHOLD = 3
_PROGRAMMATIC_ADOPTION_NUDGE_MAX = 4
_ROUND_TRIP_NUDGE_THRESHOLD = 6
_ROUND_TRIP_EFFICIENCY_NUDGE_MAX = 4
_ROUND_TRIP_EFFICIENCY_NUDGE_COOLDOWN = 24
_ROUND_TRIP_ELIGIBLE_TOOLS = frozenset({
    'run_command', 'code_exec', 'read_files', 'grep_search', 'find_files',
    'fetch_url', 'web_search', 'list_dir',
})

# ── Extended stagnation detectors ────────────────────────────────────────
#
# Shell commands are classified structurally.  The observation allowlist
# covers only programs that cannot plausibly mutate project state; anything
# else (interpreters, package managers, unknown binaries, output
# redirection) is treated as a potential state change.  A false
# "observation" verdict could kill a working task; a false "mutation"
# verdict merely resets a streak, which is the safe direction.
_OBSERVATION_PROGRAMS = frozenset({
    'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'rg', 'find',
    'ls', 'tree', 'file', 'stat', 'du', 'df', 'pwd', 'echo', 'printf',
    'jq', 'yq', 'sort', 'uniq', 'tr', 'cut', 'comm', 'column', 'diff',
    'which', 'type', 'uname', 'id', 'date', 'ps', 'env', 'printenv',
    'hostname', 'test', '[', 'readlink', 'realpath', 'basename', 'dirname',
    'md5sum', 'sha1sum', 'sha256sum', 'nproc', 'free', 'uptime', 'lscpu',
    'xxd', 'od', 'strings', 'less', 'more', 'true', ':',
})
_GIT_OBSERVATION_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'blame', 'grep', 'ls-files', 'ls-tree',
    'rev-parse', 'rev-list', 'describe', 'shortlog', 'cat-file', 'name-rev',
})
_DEFAULT_READONLY_TOOLS = frozenset({
    _READ_TOOL, 'grep_search', 'find_files', 'fetch_url', 'web_search',
    'get_conversation', 'list_dir', 'read_tool_artifact',
    'search_tool_artifact', 'list_conversations', 'search_memories',
})
_EXIT_CODE_RE = re.compile(r'\[exit code: (-?\d+)\]\s*$')
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_TIMESTAMP_RE = re.compile(
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?')
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_GIT_GLOBAL_VALUE_FLAGS = frozenset({'-C', '-c', '--git-dir', '--work-tree'})
_AUDIT_KEY = '_toolLoopBreakerAudit'
_AUDIT_MAX = 64

_STAGNATION_DEFAULTS = {
    'obs_nudge': ('TOFU_LOOP_OBS_NUDGE', 16),
    'obs_grace': ('TOFU_LOOP_OBS_GRACE', 8),
    'fail_nudge': ('TOFU_LOOP_FAIL_NUDGE', 4),
    'fail_grace': ('TOFU_LOOP_FAIL_GRACE', 2),
    'poll_nudge': ('TOFU_LOOP_POLL_NUDGE', 4),
    'poll_grace': ('TOFU_LOOP_POLL_GRACE', 2),
}

# Whole-command matches only.  ``true && pytest`` and ``printf true`` are real
# commands and must never be classified as no-ops.
_EXPLICIT_NOOP_RE = re.compile(
    r'^(?:(?:/usr/bin/|/bin/)?true|:|exit\s+0)\s*;?\s*(?:#.*)?$',
    re.IGNORECASE,
)

# ``read_files`` receipts are authoritative about what was actually returned.
# A growing file may contain fewer lines than an open-ended request implied,
# and a V2 envelope may expose only a bounded preview. Keep the parser tied to
# read_tools.py's explicit header and result_projection.py's typed item instead
# of treating requested bounds or hidden artifact bytes as model-visible.
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


def _bounded_count(value: Any, maximum: int) -> int:
    """Normalize damaged checkpoint counters without breaking the task."""
    try:
        count = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(maximum, count))


def _nudge_evidence(task: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return bounded, structurally valid persisted efficiency witnesses."""
    rows = task.get(key)
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, dict)]
    return valid[-_ROUND_TRIP_EFFICIENCY_NUDGE_MAX:]


def _last_efficiency_nudge_round(task: dict[str, Any]) -> int:
    """Recover the latest one-based completed-round witness across both lanes."""
    rows = (
        _nudge_evidence(task, '_programmaticAdoptionNudges')
        + _nudge_evidence(task, '_toolRoundTripNudges')
    )
    return max(
        (_bounded_count(row.get('afterRound'), 1_000_000_000)
         for row in rows),
        default=0,
    )


def _efficiency_nudge_cooldown_elapsed(
    task: dict[str, Any], round_num: int,
) -> bool:
    last_round = _last_efficiency_nudge_round(task)
    return (last_round == 0
            or round_num + 1 - last_round
            >= _ROUND_TRIP_EFFICIENCY_NUDGE_COOLDOWN)


def _rows_for_round(task: dict[str, Any], round_num: int) -> list[dict[str, Any]]:
    rows = [
        row for row in (task.get('toolRounds') or [])
        if (isinstance(row, dict)
            and row.get('llmRound') == round_num
            and not is_superseded_provider_attempt_round(row))
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
    state.setdefault('read_evidence_ids', {})
    state.setdefault('pending_artifacts_by_scope', {})
    state.setdefault('obs_only_streak', 0)
    state.setdefault('obs_nudged', False)
    state.setdefault('obs_after_nudge', 0)
    state.setdefault('fail_fp', '')
    state.setdefault('fail_fp_rounds', 0)
    state.setdefault('fail_fp_mutated', False)
    state.setdefault('fail_fp_nudged', False)
    state.setdefault('fail_fp_after_nudge', 0)
    state.setdefault('poll_sig', '')
    state.setdefault('poll_streak', 0)
    state.setdefault('poll_nudged', False)
    state.setdefault('poll_after_nudge', 0)
    for key in ('read_coverage', 'read_result_by_request',
                'read_evidence_ids', 'pending_artifacts_by_scope'):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    adoption_nudge_count = _bounded_count(
        state.get('programmatic_adoption_nudge_count'),
        _PROGRAMMATIC_ADOPTION_NUDGE_MAX,
    )
    adoption_evidence = _nudge_evidence(
        task, '_programmaticAdoptionNudges')
    adoption_nudge_count = max(
        adoption_nudge_count,
        min(_PROGRAMMATIC_ADOPTION_NUDGE_MAX, len(adoption_evidence)),
    )
    state['programmatic_adoption_nudge_count'] = adoption_nudge_count
    round_trip_evidence = _nudge_evidence(task, '_toolRoundTripNudges')
    recovered_shared_count = min(
        _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
        len(adoption_evidence) + len(round_trip_evidence),
    )
    round_trip_nudge_count = max(
        adoption_nudge_count,
        recovered_shared_count,
        _bounded_count(
            state.get('round_trip_efficiency_nudge_count'),
            _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
        ),
    )
    state['round_trip_efficiency_nudge_count'] = round_trip_nudge_count
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
    state['read_evidence_ids'] = {}
    state['pending_artifacts_by_scope'] = {}


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
    """Return only line bounds the model-visible receipt actually proves."""

    envelope = _tool_result_v2(row)
    if envelope is not None:
        items = envelope.get('items')
        if isinstance(items, list) and items:
            proven: list[tuple[str, int, int]] = []
            for requested_spec, item in zip(requested, items):
                if (not isinstance(item, dict)
                        or item.get('type') != 'file_read/v1'
                        or str(item.get('status') or '') != 'ok'
                        or bool(item.get('previewTruncated'))):
                    continue
                returned = _returned_read_bounds(
                    str(item.get('preview') or ''))
                if len(returned) == 1:
                    start, end = returned[0]
                else:
                    # The producer marks an item ``ok`` only when its complete
                    # bounded result is present. Explicit requested ranges are
                    # authoritative after read_tools.py normalizes them.
                    _, start, end = requested_spec
                proven.append((requested_spec[0], start, end))
            return proven

        # Generic partial envelopes (the incident shape) expose only a prefix
        # while the complete result lives behind artifactRef. Never credit the
        # requested [1, infinity] range as though the model saw hidden bytes.
        if (envelope.get('status') == 'partial'
                or bool(envelope.get('truncated'))):
            return []
        text = str(envelope.get('summary') or '')
    else:
        text = _outcome_text(row)

    returned_bounds = _returned_read_bounds(text)
    if len(returned_bounds) != len(requested):
        return requested
    return [
        (path, returned_start, returned_end)
        for (path, _, _), (returned_start, returned_end)
        in zip(requested, returned_bounds)
    ]


def _tool_result_v2(row: dict[str, Any]) -> dict[str, Any] | None:
    from lib.tools.result_envelope import tool_result_observation

    return tool_result_observation(
        _outcome_text(row), row.get('toolResultEvidence'))


def _returned_read_bounds(text: str) -> list[tuple[int, int]]:
    """Parse read_tools.py receipt headers from one visible text payload."""
    returned_bounds: list[tuple[int, int]] = []
    for match in _FILE_HEADER_RE.finditer(text):
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
    return returned_bounds


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
    envelope = _tool_result_v2(row)
    if envelope is not None and envelope.get('status') == 'error':
        return True
    content = (str(envelope.get('summary') or '') if envelope is not None
               else _outcome_text(row)).lstrip().lower()
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
    evidence_ids = state['read_evidence_ids']
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
        envelope = _tool_result_v2(row)
        evidence_identity = ''
        if envelope is not None:
            evidence_identity = str(
                envelope.get('evidenceId')
                or envelope.get('artifactRef') or '')
        evidence_was_seen = bool(
            evidence_identity and evidence_identity in evidence_ids)
        if evidence_identity:
            if (evidence_identity not in evidence_ids
                    and len(evidence_ids) >= _MAX_TRACKED_EVIDENCE_IDS):
                evidence_ids.pop(next(iter(evidence_ids)), None)
            evidence_ids[evidence_identity] = round(len(results))
        if _row_failed(row) or not specs:
            # A partial V2 result proves only the visible evidence identity,
            # not the requested range hidden behind its artifact. A first
            # identity/failure is evidence; the same one under cosmetically
            # different arguments is not.
            if ((evidence_identity and not evidence_was_seen)
                    or (not evidence_identity
                        and previous_outcome != outcome_digest)):
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
    if reason == 'unresolved_artifact':
        return (
            '[SYSTEM: UNCONSUMED TOOL-RESULT ARTIFACT]\n'
            'A large read returned a partial ToolResultEnvelopeV2 with an '
            'artifactRef, but subsequent calls kept re-reading the same '
            'resource with different window arguments while returning the '
            'same model-visible projection. Those calls cannot recover the '
            'evidence omitted from the prior envelope.\n\n'
            'Continue the latest result now with read_tool_artifact '
            '(artifact_ref=<artifactRef>, cursor=<cursor>) or '
            'search_tool_artifact, or rerun the source with a genuinely '
            'narrower query that returns an untruncated result. Do not page '
            'the same partial source again without consuming or replacing '
            'its recovery handle; one more unresolved retry will be stopped.'
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
        messages.append({'role': 'user', 'content': prompt, '_isMeta': True})
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


def _local_programmatic_eligible_tools(task: dict[str, Any]) -> set[str]:
    """Return this round's reviewed local-PTC authority, or an empty set."""
    latch = task.get('_ptc_local')
    if not isinstance(latch, dict):
        return set()
    decisions = task.get('_toolOrchestrationDecisions')
    latest = decisions[-1] if isinstance(decisions, list) and decisions else {}
    if not isinstance(latest, dict) or latest.get('programmaticBackend') != 'local':
        return set()
    eligible = latch.get('eligible')
    if not isinstance(eligible, (list, tuple, set, frozenset)):
        return set()
    return {str(name) for name in eligible if str(name)}


def _inject_programmatic_adoption_nudge(
    task: dict[str, Any], state: dict[str, Any], messages: list | None,
    *, round_num: int, tid: str,
) -> None:
    """Nudge a proven local-PTC serial chain under the shared sparse budget."""
    if messages is None:
        return
    adoption_nudge_count = _bounded_count(
        state.get('programmatic_adoption_nudge_count'),
        _PROGRAMMATIC_ADOPTION_NUDGE_MAX,
    )
    shared_nudge_count = _bounded_count(
        state.get('round_trip_efficiency_nudge_count'),
        _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
    )
    if (adoption_nudge_count >= _PROGRAMMATIC_ADOPTION_NUDGE_MAX
            or shared_nudge_count >= _ROUND_TRIP_EFFICIENCY_NUDGE_MAX):
        return
    if not _efficiency_nudge_cooldown_elapsed(task, round_num):
        return
    eligible = _local_programmatic_eligible_tools(task)
    if not eligible:
        return
    from lib.tasks_pkg.tool_orchestration_policy import (
        observed_programmatic_serial_chain,
    )
    chain = observed_programmatic_serial_chain(messages, eligible)
    if len(chain) < _PROGRAMMATIC_SERIAL_READ_THRESHOLD:
        return
    if not _serial_chain_has_productive_receipts(
            task, round_num=round_num, chain=chain):
        return

    # The existing correction remains useful language for the model, but the
    # request-local candidate policy no longer relies on compliance alone.
    # Latch one gateway-only request after the same authoritative receipts
    # prove the chain was productive. The following request always restores
    # the direct surface, so a model that does not adopt the gateway cannot
    # turn this correction into a long-lived availability or cost regression.
    from lib.tasks_pkg.programmatic_escalation import activate_serial_gateway
    gateway_activated = activate_serial_gateway(
        task, messages, round_num=round_num, chain=chain)

    prompt = (
        '[SYSTEM: SERIAL READ CHAIN DETECTED]\n'
        'The last three model rounds each used one reviewed read-only tool '
        'even though the local execute_tools gateway was available.\n\n'
        'For the next evidence-gathering round, execute_tools is the only '
        'visible tool. Use one call: use a program for dependent calls, or calls with '
        'execution="parallel" for independent reads, and return compact JSON. '
        'Every child still uses the normal schema, authority, and approval '
        'pipeline; keep semantic judgment as a direct action. If '
        'the evidence is already sufficient, finish this round without a tool. '
        'The full direct tool surface returns on the following round.'
    )
    messages.append({'role': 'user', 'content': prompt, '_isMeta': True})
    state['programmatic_adoption_nudge_count'] = adoption_nudge_count + 1
    state['round_trip_efficiency_nudge_count'] = shared_nudge_count + 1
    evidence = {
        'afterRound': round_num + 1,
        'targetRound': round_num + 2,
        'reason': 'serial_direct_reads',
        'chainLength': len(chain),
        'tools': list(chain[-6:]),
        'max': _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
    }
    evidence_rows = task.get('_programmaticAdoptionNudges')
    if not isinstance(evidence_rows, list):
        evidence_rows = []
        task['_programmaticAdoptionNudges'] = evidence_rows
    evidence_rows.append(evidence)
    del evidence_rows[:-_PROGRAMMATIC_ADOPTION_NUDGE_MAX]
    logger.info(
        '[%s] conv=%s Local PTC adoption correction injected after round %d: '
        'serial_read_rounds=%d tools=%s count=%d/%d cooldown=%d',
        tid, task.get('convId', ''), round_num + 1, len(chain), chain,
        shared_nudge_count + 1, _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
        _ROUND_TRIP_EFFICIENCY_NUDGE_COOLDOWN,
    )
    if gateway_activated:
        logger.info(
            '[%s] conv=%s Local PTC serial gateway activated for round %d '
            'after %d successful single-read rounds',
            tid, task.get('convId', ''), round_num + 2, len(chain),
        )


def _serial_chain_has_productive_receipts(
        task: dict[str, Any], *, round_num: int, chain: list[str]) -> bool:
    """Verify one successful authoritative receipt for every chain round."""
    first_round = round_num - len(chain) + 1
    if first_round < 0:
        return False
    for expected_name, observed_round in zip(
            chain, range(first_round, round_num + 1)):
        rows = _rows_for_round(task, observed_round)
        if (len(rows) != 1
                or str(rows[0].get('toolName') or '') != expected_name
                or _row_failed(rows[0])):
            return False
    return True


def _inject_round_trip_efficiency_nudge(
    task: dict[str, Any], state: dict[str, Any], messages: list | None,
    *, round_num: int, tid: str,
) -> None:
    """Nudge a proven serial single-tool chain under the shared sparse budget."""
    if messages is None:
        return
    nudge_count = _bounded_count(
        state.get('round_trip_efficiency_nudge_count'),
        _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
    )
    if nudge_count >= _ROUND_TRIP_EFFICIENCY_NUDGE_MAX:
        return
    if not _efficiency_nudge_cooldown_elapsed(task, round_num):
        return
    from lib.tasks_pkg.tool_orchestration_policy import (
        observed_single_tool_serial_chain,
    )
    chain = observed_single_tool_serial_chain(
        messages,
        set(_ROUND_TRIP_ELIGIBLE_TOOLS),
        minimum=_ROUND_TRIP_NUDGE_THRESHOLD,
        maximum=_ROUND_TRIP_NUDGE_THRESHOLD,
    )
    if (len(chain) != _ROUND_TRIP_NUDGE_THRESHOLD
            or not _serial_chain_has_productive_receipts(
                task, round_num=round_num, chain=chain)):
        return

    prompt = (
        '[SYSTEM: SERIAL SINGLE-TOOL CHAIN DETECTED]\n'
        'The last six model rounds each issued one inspection or command.\n\n'
        'Reduce remaining round trips when safe: emit independent direct '
        'tool calls together in one response; use dedicated batch arrays; '
        'combine independent read-only shell inspections or related '
        'verification in one bounded run_command. Keep dependencies, writes '
        'or state changes, approvals, and long-running or polling commands '
        'ordered and direct. If the evidence is sufficient, take the '
        'substantive next action or finish.'
    )
    messages.append({'role': 'user', 'content': prompt, '_isMeta': True})
    state['round_trip_efficiency_nudge_count'] = nudge_count + 1
    evidence = {
        'afterRound': round_num + 1,
        'targetRound': round_num + 2,
        'reason': 'serial_single_tool_rounds',
        'chainLength': len(chain),
        'tools': list(chain),
        'max': _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
    }
    evidence_rows = task.get('_toolRoundTripNudges')
    if not isinstance(evidence_rows, list):
        evidence_rows = []
        task['_toolRoundTripNudges'] = evidence_rows
    evidence_rows.append(evidence)
    del evidence_rows[:-_ROUND_TRIP_EFFICIENCY_NUDGE_MAX]
    logger.info(
        '[%s] conv=%s Round-trip efficiency correction injected after '
        'round %d: serial_single_tool_rounds=%d tools=%s count=%d/%d '
        'cooldown=%d',
        tid, task.get('convId', ''), round_num + 1, len(chain), chain,
        nudge_count + 1, _ROUND_TRIP_EFFICIENCY_NUDGE_MAX,
        _ROUND_TRIP_EFFICIENCY_NUDGE_COOLDOWN,
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

# ── Extended stagnation detectors ────────────────────────────────────────

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _extended_guards_enabled() -> bool:
    return (os.environ.get('TOFU_LOOP_EXTENDED') or '1').strip().lower() \
        not in {'0', 'false', 'no', 'off'}


def _stagnation_thresholds() -> dict[str, int]:
    return {
        key: _env_int(env_name, default)
        for key, (env_name, default) in _STAGNATION_DEFAULTS.items()
    }


def _has_output_redirection(command: str) -> bool:
    """True when the command writes to a file (``>``/``>>`` outside /dev/null).

    False positives (a quoted ``a > b`` pattern) classify as mutation — the
    safe direction.  ``2>&1`` and ``>/dev/null`` sinks are stripped first.
    """
    scrubbed = re.sub(r'\d?>&\d', '', command)
    scrubbed = re.sub(r'&>>?\s*/dev/null', '', scrubbed)
    scrubbed = re.sub(r'\d?>>?\s*/dev/null\b', '', scrubbed)
    return bool(re.search(r'(?<![<>&|])>>?', scrubbed))


def _segment_program(segment: list[str]) -> str | None:
    for token in segment:
        if _ENV_ASSIGN_RE.match(token):
            continue
        if re.match(r'^\d*>>?\d?$', token):
            continue
        return token.rsplit('/', 1)[-1] or None
    return None


def _git_subcommand(segment: list[str]) -> str:
    seen_program = False
    skip_next = False
    for token in segment:
        if not seen_program:
            if token.rsplit('/', 1)[-1] == 'git':
                seen_program = True
            continue
        if skip_next:
            skip_next = False
            continue
        if token in _GIT_GLOBAL_VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith('-'):
            continue
        return token
    return ''


def _shell_command_is_observation(command: str) -> bool:
    """Conservative side-effect-free verdict for one shell command string.

    Every pipeline/chain segment must start with an allowlisted read-only
    program (or a read-only git subcommand), and no output redirection may
    appear anywhere.  Any parse doubt returns False (= potential mutation).
    """
    text = command.strip()
    if not text:
        return False
    if _EXPLICIT_NOOP_RE.fullmatch(text):
        return True
    if _has_output_redirection(text):
        return False
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars='|&;')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(ch in '|&;' for ch in token):
            segments.append([])
        else:
            segments[-1].append(token)
    saw_program = False
    for segment in segments:
        program = _segment_program(segment)
        if program is None:
            continue
        saw_program = True
        if program in _OBSERVATION_PROGRAMS:
            continue
        if program == 'git':
            if _git_subcommand(segment) in _GIT_OBSERVATION_SUBCOMMANDS:
                continue
            return False
        return False
    return saw_program


def _shell_exit_code(row: dict[str, Any]) -> int | None:
    """Parse the dispatcher's terminal ``[exit code: N]`` marker, if present."""
    envelope = _tool_result_v2(row)
    candidates = []
    if envelope is not None:
        candidates.append(str(envelope.get('summary') or ''))
    candidates.append(_outcome_text(row))
    for text in candidates:
        match = _EXIT_CODE_RE.search(text or '')
        if match:
            return int(match.group(1))
    return None


def _failure_fingerprint(row: dict[str, Any]) -> str:
    """Normalized (exit code, error tail) identity for one failed shell row.

    Timestamps and whitespace are volatile; the last 400 normalized chars
    carry the actual diagnostic.  A changed fingerprint means the failure
    MOVED, which is progress and must not count toward the stall.
    """
    envelope = _tool_result_v2(row)
    text = str(envelope.get('summary') or '') if envelope is not None else ''
    if not text:
        text = _outcome_text(row)
    exit_match = _EXIT_CODE_RE.search(text)
    exit_code = exit_match.group(1) if exit_match else '?'
    tail = _ANSI_RE.sub('', text[-800:])
    tail = _TIMESTAMP_RE.sub('<TS>', tail)
    tail = re.sub(r'\s+', ' ', tail).strip().lower()
    digest = hashlib.sha256(
        f'{exit_code}\x00{tail[-400:]}'.encode('utf-8', 'replace'))
    return digest.hexdigest()[:16]


def _classify_round_rows(
    task: dict[str, Any], rows: list[dict[str, Any]],
) -> list[str]:
    """Per-row category: mutation / shell_opaque / shell_obs / observation / neutral."""
    try:
        from lib.tasks_pkg.tool_dispatch._flags import (
            _task_idempotent_tools, _task_partitions)
        write_tools, _ = _task_partitions(task)
        readonly = set(_task_idempotent_tools(task))
    except Exception as exc:  # fail open: never stop healthy work
        logger.debug('[ToolProgress] partitions unavailable: %s', exc)
        write_tools = frozenset(_BUILTIN_FILE_WRITE_TOOLS)
        readonly = set(_DEFAULT_READONLY_TOOLS)
    kinds: list[str] = []
    for row in rows:
        name = str(row.get('toolName') or '')
        if name in _SHELL_TOOL_NAMES:
            if _is_explicit_noop_row(row):
                kinds.append('shell_obs')
                continue
            command = _parse_args(row).get('command')
            if (isinstance(command, str)
                    and _shell_command_is_observation(command)):
                kinds.append('shell_obs')
            else:
                kinds.append('shell_opaque')
            continue
        if name in write_tools and _row_has_confirmed_write(row):
            kinds.append('mutation')
            continue
        if name in readonly:
            kinds.append('observation')
            continue
        kinds.append('neutral')
    return kinds


def _update_stagnation_detectors(
    task: dict[str, Any], state: dict[str, Any], rows: list[dict[str, Any]],
    *, thresholds: dict[str, int],
) -> tuple[str, str] | None:
    """Refresh streak bookkeeping; return the highest-priority trigger.

    Returns ``(detector, action)`` where action is 'nudge' / 'stop' /
    'finish', or None.  Streaks update every round; nudge/stop flags are
    left to the executor so a competing correction in the same model turn
    can safely defer without consuming the budget.
    """
    if not rows:
        return None
    kinds = _classify_round_rows(task, rows)
    has_mutation = 'mutation' in kinds
    all_observation = all(
        kind in ('observation', 'shell_obs') for kind in kinds)
    opaque_rows = [
        row for row, kind in zip(rows, kinds) if kind == 'shell_opaque']
    failing = [
        row for row in opaque_rows
        if (_shell_exit_code(row) or 0) != 0
        and _shell_exit_code(row) is not None
    ]
    opaque_succeeded = any(
        _shell_exit_code(row) == 0 for row in opaque_rows)

    # ── Persistent identical failure ──
    if failing:
        fingerprint = _failure_fingerprint(failing[0])
        if fingerprint != state['fail_fp']:
            state['fail_fp'] = fingerprint
            state['fail_fp_rounds'] = 1
            state['fail_fp_mutated'] = False
            state['fail_fp_nudged'] = False
            state['fail_fp_after_nudge'] = 0
        else:
            state['fail_fp_rounds'] += 1
            if state['fail_fp_nudged']:
                state['fail_fp_after_nudge'] += 1
    elif opaque_succeeded:
        state['fail_fp'] = ''
        state['fail_fp_rounds'] = 0
        state['fail_fp_mutated'] = False
        state['fail_fp_nudged'] = False
        state['fail_fp_after_nudge'] = 0
    if has_mutation and state['fail_fp']:
        state['fail_fp_mutated'] = True

    # ── Success polling ──
    poll_sig = ''
    if (not has_mutation
            and 'neutral' not in kinds
            and opaque_rows
            and not failing
            and all(_shell_exit_code(row) == 0 for row in opaque_rows)
            and not any(_row_failed(row) for row in rows)):
        commands = sorted(
            re.sub(r'\s+', ' ', str(
                _parse_args(row).get('command') or '').strip())
            for row, kind in zip(rows, kinds)
            if kind in ('shell_obs', 'shell_opaque'))
        poll_sig = hashlib.sha256(
            '\x00'.join(commands).encode('utf-8', 'replace')
        ).hexdigest()[:16]
    if poll_sig and poll_sig == state['poll_sig']:
        state['poll_streak'] += 1
        if state['poll_nudged']:
            state['poll_after_nudge'] += 1
    elif poll_sig:
        state['poll_sig'] = poll_sig
        state['poll_streak'] = 1
        state['poll_nudged'] = False
        state['poll_after_nudge'] = 0
    else:
        state['poll_sig'] = ''
        state['poll_streak'] = 0
        state['poll_nudged'] = False
        state['poll_after_nudge'] = 0

    # ── Observation-only stall ──
    if all_observation:
        state['obs_only_streak'] += 1
        if state['obs_nudged']:
            state['obs_after_nudge'] += 1
    else:
        state['obs_only_streak'] = 0
        state['obs_nudged'] = False
        state['obs_after_nudge'] = 0

    # ── Trigger priority: failure > success poll > observation stall ──
    if (state['fail_fp'] and not state['fail_fp_nudged']
            and state['fail_fp_rounds'] >= thresholds['fail_nudge']):
        return ('fail_fp', 'nudge')
    if (state['fail_fp_nudged']
            and state['fail_fp_after_nudge'] >= thresholds['fail_grace']):
        return ('fail_fp', 'stop')
    if (state['poll_sig'] and not state['poll_nudged']
            and state['poll_streak'] >= thresholds['poll_nudge']):
        return ('poll', 'nudge')
    if (state['poll_nudged']
            and state['poll_after_nudge'] >= thresholds['poll_grace']):
        return ('poll', 'finish')
    if (not state['obs_nudged']
            and state['obs_only_streak'] >= thresholds['obs_nudge']):
        return ('obs', 'nudge')
    if (state['obs_nudged']
            and state['obs_after_nudge'] >= thresholds['obs_grace']):
        return ('obs', 'stop')
    return None


def _audit_stagnation(
    task: dict[str, Any], *, detector: str, action: str, signature: str,
    round_num: int,
) -> None:
    rows = task.setdefault(_AUDIT_KEY, [])
    rows.append({
        'round': round_num + 1,
        'detector': detector,
        'action': action,
        'signature': signature,
    })
    del rows[:-_AUDIT_MAX]


def _inject_stagnation_nudge(
    task: dict[str, Any], state: dict[str, Any], messages: list | None,
    *, detector: str, prompt: str, signature: str, round_num: int,
    tid: str,
) -> None:
    if messages is not None:
        messages.append({'role': 'user', 'content': prompt, '_isMeta': True})
    task.setdefault('_toolLoopNudges', []).append({
        'round': round_num + 2,
        'reason': detector,
        'prompt': prompt,
        'max': 1,
    })
    _audit_stagnation(
        task, detector=detector, action='nudge', signature=signature,
        round_num=round_num)
    logger.warning(
        '[%s] conv=%s Stagnation correction injected after round %d: '
        'detector=%s signature=%s',
        tid, task.get('convId', ''), round_num + 1, detector, signature,
    )


def _force_clean_finish(
    task: dict[str, Any], rs: Any, *, round_num: int, tid: str,
    reason: str, detail: str, signature: str,
    detector: str = 'success_poll', event_reason: str = 'success_poll',
) -> bool:
    """End the turn WITHOUT an error: the verified state is the deliverable.

    ``_settle_post_loop_finish_reason`` converts the flag into
    finishReason=stop so the dangling tool_use finish is not misreported
    as an internal error.
    """
    logger.warning(
        '[%s] conv=%s CLEAN FINISH: %s round=%d model=%s signature=%s',
        tid, task.get('convId', ''), reason, round_num + 1, rs.model,
        signature,
    )
    task['_toolLoopCleanFinish'] = {
        'reason': reason, 'detail': detail, 'round': round_num + 1,
    }
    rs.exit_reason = reason
    _audit_stagnation(
        task, detector=detector, action='finish',
        signature=signature, round_num=round_num)
    append_event(task, build_event(
        EventType.ROUND_END, roundNum=round_num, reason=event_reason))
    return True


def finish_after_background_task_acceptance(
    task: dict[str, Any], rs: Any, *, round_num: int, tid: str,
) -> bool:
    """End root chat after a long-running producer accepted its task.

    The accepted worker is now the sole progress authority. The model must not
    create an expensive polling loop or claim completion from intermediate
    state. A deterministic acknowledgement also prevents an empty assistant
    bubble when the provider emitted only the tool call.
    """
    accepted = task.get('_backgroundTaskAccepted')
    if not isinstance(accepted, dict) or not accepted.get('taskId'):
        return False
    if task.get('_backgroundTaskAcceptanceConsumed'):
        return False
    task['_backgroundTaskAcceptanceConsumed'] = True
    message = str(accepted.get('message') or '').strip()
    if message and not str(task.get('content') or '').strip():
        task['content'] = message
    tool = str(accepted.get('tool') or 'background_task')[:80]
    task_id = str(accepted.get('taskId') or '')[:80]
    return _force_clean_finish(
        task, rs, round_num=round_num, tid=tid,
        reason='background_task_accepted',
        detail=(
            f'{tool} accepted task {task_id}; progress and terminal quality '
            'are owned by the background task runtime.'),
        signature=f'tool={tool} task={task_id}',
        detector='background_task', event_reason='background_task_accepted')


def _execute_stagnation_action(
    task: dict[str, Any], rs: Any, state: dict[str, Any],
    messages: list | None, *, action: tuple[str, str], round_num: int,
    tid: str, thresholds: dict[str, int],
) -> bool:
    detector, kind = action
    # Isolated callers have no message lane to deliver a correction; mirror
    # the exact-repetition guard and fail fast at the first threshold.
    if kind == 'nudge' and messages is None:
        kind = 'finish' if detector == 'poll' else 'stop'

    if detector == 'fail_fp':
        signature = (f'fp={state["fail_fp"]} '
                     f'rounds={state["fail_fp_rounds"]} '
                     f'mutated={state["fail_fp_mutated"]}')
        if kind == 'nudge':
            state['fail_fp_nudged'] = True
            state['fail_fp_after_nudge'] = 0
            prompt = (
                '[SYSTEM: PERSISTENT IDENTICAL FAILURE DETECTED]\n'
                f'The same command has now failed '
                f'{state["fail_fp_rounds"]} times with the same error, and '
                'the attempts in between have not changed the error output. '
                'Repeating this fix-and-rerun cycle is not converging.\n\n'
                'Stop retrying the same approach. Step back and form a '
                'fundamentally different hypothesis: re-read the complete '
                'error, inspect the code path or configuration it names, '
                'verify one assumption with a small probe, or finish now '
                'and report the concrete blocker. '
                f'{thresholds["fail_grace"]} more identical failure(s) will '
                'be stopped by the harness.'
            )
            _inject_stagnation_nudge(
                task, state, messages,
                detector='persistent_identical_failure', prompt=prompt,
                signature=signature, round_num=round_num, tid=tid)
            return False
        _audit_stagnation(
            task, detector='persistent_identical_failure', action='stop',
            signature=signature, round_num=round_num)
        return _force_stop(
            task, rs, round_num=round_num, tid=tid,
            reason='semantic_persistent_failure_loop',
            detail=(
                'The same command kept failing with the identical error '
                'across repeated attempts, including after a corrective '
                'instruction to change approach. The cycle was not '
                'converging, so the task was stopped instead of spending '
                'more requests.'
            ),
            raw=f'persistent_failure {signature}',
        )

    if detector == 'poll':
        signature = (f'sig={state["poll_sig"]} '
                     f'streak={state["poll_streak"]}')
        if kind == 'nudge':
            state['poll_nudged'] = True
            state['poll_after_nudge'] = 0
            prompt = (
                '[SYSTEM: REPEATED IDENTICAL VERIFICATION DETECTED]\n'
                f'The same command(s) have now succeeded '
                f'{state["poll_streak"]} times in a row with no intervening '
                'code change. Re-running them cannot produce new '
                'information.\n\n'
                'Proceed now: either take the next substantive action toward '
                'the actual goal, or finish and summarize what was verified. '
                'Do not poll the same check again; '
                f'{thresholds["poll_grace"]} more identical re-run(s) will '
                'end this turn automatically with the current verified state.'
            )
            _inject_stagnation_nudge(
                task, state, messages, detector='success_poll',
                prompt=prompt, signature=signature, round_num=round_num,
                tid=tid)
            return False
        return _force_clean_finish(
            task, rs, round_num=round_num, tid=tid,
            reason='success_poll_finish',
            detail=(
                'The same verification command(s) succeeded repeatedly with '
                'no intervening change, including after an instruction to '
                'proceed or finish. The turn was ended with the verified '
                'state instead of spending more requests.'
            ),
            signature=signature,
        )

    signature = f'streak={state["obs_only_streak"]}'
    if kind == 'nudge':
        state['obs_nudged'] = True
        state['obs_after_nudge'] = 0
        prompt = (
            '[SYSTEM: OBSERVATION-ONLY STALL DETECTED]\n'
            f'The last {state["obs_only_streak"]} tool rounds only '
            'inspected files, search results, or read-only shell output '
            'without changing any state or running a substantive command.\n\n'
            'Act on the evidence now: make the change, run the check, or '
            'finish and report your findings. If you are blocked, say so '
            'concretely. Further inspection-only rounds '
            f'({thresholds["obs_grace"]} more) will be stopped by the '
            'harness.'
        )
        _inject_stagnation_nudge(
            task, state, messages, detector='observation_stall',
            prompt=prompt, signature=signature, round_num=round_num,
            tid=tid)
        return False
    _audit_stagnation(
        task, detector='observation_stall', action='stop',
        signature=signature, round_num=round_num)
    return _force_stop(
        task, rs, round_num=round_num, tid=tid,
        reason='semantic_observation_stall',
        detail=(
            'The model spent many consecutive tool rounds only inspecting '
            'files, search results, or read-only shell output without '
            'changing any state, including after a corrective instruction '
            'to act or finish. The task was stopped instead of spending '
            'more requests.'
        ),
        raw=f'observation_stall {signature}',
    )


def _artifact_resource_scope(row: dict[str, Any]) -> str:
    """Stable source identity with pagination/range knobs removed."""
    name = str(row.get('toolName') or '')

    def _without_windows(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): _without_windows(child)
                for key, child in value.items()
                if str(key).lower() not in _WINDOW_ARGUMENT_KEYS
            }
        if isinstance(value, list):
            return [_without_windows(child) for child in value]
        return value

    try:
        payload = json.dumps(
            {'tool': name, 'resource': _without_windows(_parse_args(row))},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            default=str,
        )
    except (TypeError, ValueError):
        payload = name + '\x00' + _canonical_args(row)
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


def _artifact_ref_from_row(row: dict[str, Any]) -> str:
    envelope = _tool_result_v2(row)
    if envelope is None or envelope.get('status') != 'partial':
        return ''
    return str(envelope.get('artifactRef') or '')


def _partial_visible_digest(row: dict[str, Any]) -> str:
    """Fingerprint only evidence the model could actually inspect.

    Artifact identity, byte counts and freshness may change even when the
    bounded projection is byte-for-byte the same. Counting those hidden-side
    changes as progress recreated the history-reader loop; conversely, a
    genuinely different visible page must remain legal even if both pages are
    partial.
    """
    envelope = _tool_result_v2(row)
    if envelope is None:
        return ''
    visible = {
        'status': envelope.get('status'),
        'summary': envelope.get('summary'),
        'items': envelope.get('items'),
        'error': envelope.get('error'),
    }
    try:
        payload = json.dumps(
            visible, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str)
    except (TypeError, ValueError):
        payload = repr(visible)
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


def _handle_unresolved_artifact_retries(
    task: dict[str, Any], rs: Any, state: dict[str, Any],
    rows: list[dict[str, Any]], messages: list | None, *, round_num: int,
    tid: str, max_unresolved_artifact_retries: int,
) -> bool:
    """Require recovery when re-paging a partial source does not advance.

    This is tool-neutral and derives read-only authority from the registry.
    Window keys are ignored only for resource identity, so different search
    queries remain independent while ``get_conversation(before=...)`` and
    ``read_files(start_line=...)`` are recognized as the same source. A new
    visible page is progress; only an unchanged projection accumulates toward
    the nudge/stop threshold.
    """
    pending = state['pending_artifacts_by_scope']
    try:
        from lib.tasks_pkg.tool_dispatch._flags import _task_idempotent_tools
        idempotent_tools = _task_idempotent_tools(task)
    except Exception as exc:
        logger.debug('[ToolProgress] idempotent partition unavailable: %s', exc)
        idempotent_tools = frozenset({_READ_TOOL, 'get_conversation'})

    # A continuation in the same model round resolves the prior obligation
    # before any newly issued source read is assessed.
    consumed_refs = {
        str(_parse_args(row).get('artifact_ref') or '')
        for row in rows
        if str(row.get('toolName') or '') in _ARTIFACT_CONTINUATION_TOOLS
    }
    consumed_refs.discard('')
    for scope, observation in list(pending.items()):
        if (isinstance(observation, dict)
                and str(observation.get('artifactRef') or '') in consumed_refs):
            pending.pop(scope, None)

    # Calls in one model response are concurrent siblings: a later row in the
    # same batch could not have consumed an artifact returned by an earlier
    # sibling. Compare every row only with obligations from prior rounds, so a
    # parallel page batch counts as one retry rather than N retries.
    prior_pending = dict(pending)
    retries: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get('toolName') or '')
        if (name not in idempotent_tools
                or name in _ARTIFACT_CONTINUATION_TOOLS):
            continue
        scope = _artifact_resource_scope(row)
        previous = prior_pending.get(scope)
        artifact_ref = _artifact_ref_from_row(row)
        visible_digest = _partial_visible_digest(row)
        same_visible_projection = bool(
            isinstance(previous, dict)
            and visible_digest
            and visible_digest == str(previous.get('visibleDigest') or ''))
        if isinstance(previous, dict):
            if _row_failed(row) or (artifact_ref and same_visible_projection):
                retries[scope] = previous
            elif not artifact_ref:
                # A successful untruncated/narrow response replaces the
                # partial observation and makes its old artifact optional.
                pending.pop(scope, None)
                continue
        if artifact_ref:
            retry_count = _bounded_count(
                (previous or {}).get('retryCount')
                if same_visible_projection else 0,
                max(1, max_unresolved_artifact_retries + 1),
            )
            if scope in retries:
                retry_count += 1
            if scope not in pending and len(pending) >= _MAX_PENDING_ARTIFACT_SCOPES:
                pending.pop(next(iter(pending)), None)
            pending[scope] = {
                'artifactRef': artifact_ref,
                'toolName': name,
                'retryCount': retry_count,
                'round': round_num,
                'visibleDigest': visible_digest,
            }
            if scope in retries:
                retries[scope] = pending[scope]
        elif isinstance(previous, dict) and scope in retries:
            updated = dict(previous)
            updated['retryCount'] = _bounded_count(
                previous.get('retryCount'),
                max(1, max_unresolved_artifact_retries + 1)) + 1
            pending[scope] = updated
            retries[scope] = updated

    if not retries:
        return False
    worst_scope, worst = max(
        retries.items(), key=lambda item: int(item[1].get('retryCount') or 0))
    retry_count = int(worst.get('retryCount') or 0)
    if retry_count < max(1, max_unresolved_artifact_retries):
        return False
    logger.warning(
        '[%s] conv=%s Unresolved partial-result retry at round %d: tool=%s '
        'retries=%d/%d', tid, task.get('convId', ''), round_num + 1,
        worst.get('toolName') or '?', retry_count,
        max_unresolved_artifact_retries,
    )
    if state['semantic_nudge_count'] < 1:
        _inject_semantic_nudge(
            task, state, messages, reason='unresolved_artifact',
            round_num=round_num, tid=tid,
        )
        return False
    return _force_stop(
        task, rs, round_num=round_num, tid=tid,
        reason='semantic_unresolved_artifact_loop',
        detail=(
            'The model repeatedly re-read the same resource after partial '
            'results supplied recoverable artifactRef cursors, but the '
            'model-visible projection did not advance, including after a '
            'corrective instruction. It neither consumed the artifact nor '
            'obtained new visible evidence, so the task was stopped instead '
            'of spending more requests.'
        ),
        raw=(f'unresolved_artifact_retries={retry_count};'
             f'scope={worst_scope[:16]};'
             f'nudges={state["semantic_nudge_count"]}'),
    )


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
    max_unresolved_artifact_retries: int,
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

    if _handle_unresolved_artifact_retries(
        task, rs, state, rows, messages, round_num=round_num, tid=tid,
        max_unresolved_artifact_retries=max_unresolved_artifact_retries,
    ):
        return True

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
    max_unresolved_artifact_retries: int = 2,
    stagnation_thresholds: dict[str, int] | None = None,
) -> bool:
    """Observe the completed tool round; return True when the loop must stop."""
    state = _guard_state(task)
    corrective_nudges_before = len(task.get('_toolLoopNudges') or ())
    if _handle_exact_repetition(
        task, rs, state, messages=messages, round_num=round_num, tid=tid,
        max_consecutive_identical=max_consecutive_identical,
    ):
        return True
    if _handle_semantic_stall(
        task, rs, state, messages, round_num=round_num, tid=tid,
        max_explicit_noop_rounds=max_explicit_noop_rounds,
        max_redundant_read_rounds=max_redundant_read_rounds,
        max_unresolved_artifact_retries=max_unresolved_artifact_retries,
    ):
        return True
    # Stagnation streaks refresh on every completed tool round — even one
    # where another guard already corrected — so a state change is never
    # missed.  Executing a nudge/stop still waits for a correction-free turn.
    pending_stagnation = None
    thresholds = None
    if _extended_guards_enabled():
        thresholds = stagnation_thresholds or _stagnation_thresholds()
        pending_stagnation = _update_stagnation_detectors(
            task, state, _rows_for_round(task, round_num),
            thresholds=thresholds)
    # A safety correction and an adoption hint must never stack in one model
    # turn. The former is more urgent and gets the sole user-lane carrier.
    if len(task.get('_toolLoopNudges') or ()) == corrective_nudges_before:
        if (pending_stagnation is not None
                and _execute_stagnation_action(
                    task, rs, state, messages,
                    action=pending_stagnation, round_num=round_num, tid=tid,
                    thresholds=thresholds)):
            return True
    if len(task.get('_toolLoopNudges') or ()) == corrective_nudges_before:
        efficiency_nudges_before = state['round_trip_efficiency_nudge_count']
        _inject_programmatic_adoption_nudge(
            task, state, messages, round_num=round_num, tid=tid)
        if (state['round_trip_efficiency_nudge_count']
                == efficiency_nudges_before):
            _inject_round_trip_efficiency_nudge(
                task, state, messages, round_num=round_num, tid=tid)
    return False
