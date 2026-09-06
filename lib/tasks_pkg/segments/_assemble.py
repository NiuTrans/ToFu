"""lib/tasks_pkg/segments/_assemble.py — build the ordered typed-segment list.

``assemble_segments`` is populated alongside the three legacy channels
(``content`` / ``thinking`` / ``toolRounds``); those channels are proved to be
loss-less *projections* of the segment list via the ``_derive`` module.

Ordering observer: the interleaving is ALREADY fully captured at finalization
time. Each llmRound batch's pre-tool prose is stamped onto the FIRST entry of
that batch as ``assistantContent`` / ``thinking`` / ``thinkingSignature``
before ``_discard_pretool_prose`` zeroes the accumulators; the terminal round's
deliverable prose survives in ``task['content']`` / ``task['thinking']``.
Prose-only rounds whose stop a continuation enforcer vetoed are the ONE shape
that record misses (no tool batch to stamp onto, accumulators later zeroed);
they are snapshotted into ``task[CONTINUATION_PROSE_FIELD]`` at nudge time
(``record_continuation_prose``) and interleaved here. So the ordered merged
list + continuation snapshots + terminal strings are a complete, lossless
record.

``deliverable`` rule (explicit, position-based): a ``text`` segment is
``deliverable=False`` iff it is the ``assistantContent`` of a tool-round batch;
``deliverable=True`` iff it is the terminal ``task['content']``.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tool_round_identity import (
    execution_batch_keys,
    execution_identity,
    execution_llm_round,
    model_batch_block_suffix,
)
from lib.tool_round_replay import (
    SUPERSEDED_PROVIDER_ATTEMPT_FIELD,
    is_superseded_provider_attempt_round,
)

from lib.tasks_pkg.segments._types import (
    CONTINUATION_PROSE_FIELD,
    INJECTED_NOTES_FIELD,
    NOTE_KINDS,
    SEG_SYSTEM_NOTE,
    SEG_THINKING, SEG_TEXT, SEG_TOOL_USE, RESUMABLE_FINISH_REASONS,
    is_synthetic_inbox_round,
)

logger = get_logger(__name__)


def _round_block_suffix(round_dict: dict[str, Any], position: int) -> str:
    """Return a stable, content-independent identity within one turn.

    ``llmRound`` is the canonical batch identity.  Older/synthetic round
    shapes may not carry it, so their durable round number is the next choice;
    position is the final compatibility key and is stable for the immutable
    merged history.  Never derive block identity from growing text.
    """
    attempt_id, task_id = execution_identity(round_dict)
    scope = attempt_id or task_id
    llm_round = execution_llm_round(round_dict)
    if llm_round is not None:
        return model_batch_block_suffix(
            llm_round, attempt_id=attempt_id, task_id=task_id)
    scope_prefix = f'attempt-{scope}:' if scope else ''
    round_number = round_dict.get('roundNum')
    if round_number is not None:
        return f'{scope_prefix}round-{round_number}'
    return f'{scope_prefix}legacy-{position}'


def _tool_block_id(round_dict: dict[str, Any], position: int) -> str:
    tool_call_id = str(round_dict.get('toolCallId') or '').strip()
    return f'tool:{tool_call_id}' if tool_call_id else (
        f'tool:{_round_block_suffix(round_dict, position)}'
    )


def tool_use_segment_from_round(
    round_dict: dict[str, Any], position: int,
) -> dict[str, Any]:
    """Build one tool block without applying finished-turn ordering.

    Live projection repair uses this constructor to append an early-announced
    tool after the already-streamed prose prefix. Keeping the block shape here
    makes final assembly and incremental repair share one source of truth.
    """
    segment = {
        'type': SEG_TOOL_USE,
        'blockId': _tool_block_id(round_dict, position),
        'id': round_dict.get('toolCallId', ''),
        'name': round_dict.get('toolName', ''),
        'input': round_dict.get('toolArgs', ''),
        'llmRound': execution_llm_round(round_dict),
        'result': {'content': round_dict.get('toolContent'),
                   'status': round_dict.get('status')},
        '_round': round_dict,
    }
    attempt_id, task_id = execution_identity(round_dict)
    if attempt_id:
        segment['attemptId'] = attempt_id
    if task_id:
        segment['taskId'] = task_id
    if is_superseded_provider_attempt_round(round_dict):
        segment[SUPERSEDED_PROVIDER_ATTEMPT_FIELD] = True
    return segment


def record_continuation_prose(
    task: dict[str, Any],
    *,
    llm_round: Any,
    content: Any,
    thinking: Any = '',
) -> dict[str, Any] | None:
    """Snapshot a prose-only round whose stop a continuation enforcer vetoed.

    The wire keeps the interrupted answer (``append_assistant_prose_message``
    runs first), but the legacy channels cannot: ``task['content']`` /
    ``task['thinking']`` are NOT reset between consecutive continuations and
    are zeroed by the next tool round's ``_discard_pretool_prose``, and the
    prose-only round owns no tool batch to stamp ``assistantContent`` onto.
    Append the exact per-round strings here so ``assemble_segments`` can
    interleave them. Sources MUST be the per-round message fields
    (``assistant_msg['content']`` / ``['reasoning_content']``), never the
    accumulators — those carry earlier rounds too and would duplicate
    already-recorded entries on a second nudge.
    """
    if not isinstance(task, dict):
        return None
    text = content if isinstance(content, str) else ''
    think = thinking if isinstance(thinking, str) else ''
    if not (text.strip() or think.strip()):
        return None
    entry: dict[str, Any] = {
        'llmRound': (llm_round if isinstance(llm_round, int)
                     and not isinstance(llm_round, bool) else None),
        'content': text,
        'thinking': think,
    }
    attempt_id = str(task.get('_attemptId') or task.get('attemptId') or '')
    task_id = str(task.get('id') or task.get('taskId') or '')
    if attempt_id:
        entry['attemptId'] = attempt_id
        if task_id:
            entry['taskId'] = task_id
    entries = task.setdefault(CONTINUATION_PROSE_FIELD, [])
    entries.append(entry)
    return entry


def record_injected_note(
    task: dict[str, Any],
    *,
    llm_round: Any,
    kind: Any,
    text: Any,
) -> dict[str, Any] | None:
    """Snapshot an engine-authored intervention (``_isMeta`` user message).

    The nudge itself rides the wire as a ``role='user'`` row appended at
    injection time; this record lets ``assemble_segments`` place a
    ``system_note`` segment at the SAME position so the durable timeline
    shows the intervention where it happened instead of hiding it behind a
    sidecar chip (or, for the todo-continuation lane, nothing at all).
    ``kind`` is closed-vocabulary (``NOTE_KINDS``); ``text`` is the verbatim
    injected content so the render shows exactly what the model was told.
    """
    if not isinstance(task, dict):
        return None
    note_kind = kind if isinstance(kind, str) and kind in NOTE_KINDS else ''
    body = text if isinstance(text, str) else ''
    if not note_kind or not body.strip():
        return None
    entry: dict[str, Any] = {
        'llmRound': (llm_round if isinstance(llm_round, int)
                     and not isinstance(llm_round, bool) else None),
        'kind': note_kind,
        'text': body,
    }
    attempt_id = str(task.get('_attemptId') or task.get('attemptId') or '')
    task_id = str(task.get('id') or task.get('taskId') or '')
    if attempt_id:
        entry['attemptId'] = attempt_id
        if task_id:
            entry['taskId'] = task_id
    entries = task.setdefault(INJECTED_NOTES_FIELD, [])
    entries.append(entry)
    return entry


def _insert_continuation_prose_segments(
    segments: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Interleave vetoed final answers into the round-derived segment list.

    Each entry becomes the same shape a tool batch's pre-tool prose has
    (a non-deliverable, non-terminal thinking/text pair) and lands BEFORE the
    first segment of a later llmRound — exactly where the interrupted answer
    sat on the wire. Runs before the terminal append in ``assemble_segments``,
    so an entry past every round still precedes the terminal deliverable.
    ``deliverable=False`` keeps ``derive_content`` byte-identical, and the
    rounds view only attaches prose to tool-bearing batches, so replay never
    re-sends these bytes (the wire row appended at nudge time already did).
    """
    entries = task.get(CONTINUATION_PROSE_FIELD)
    if not isinstance(entries, list):
        return
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        raw_text = entry.get('content')
        raw_think = entry.get('thinking')
        text = raw_text if isinstance(raw_text, str) else ''
        think = raw_think if isinstance(raw_think, str) else ''
        if not (text.strip() or think.strip()):
            continue
        lr = entry.get('llmRound')
        if not isinstance(lr, int) or isinstance(lr, bool):
            lr = None
        identity_fields = {
            **({'attemptId': entry['attemptId']}
               if isinstance(entry.get('attemptId'), str)
               and entry['attemptId'] else {}),
            **({'taskId': entry['taskId']}
               if isinstance(entry.get('taskId'), str)
               and entry['taskId'] else {}),
        }
        index = len(segments)
        if lr is not None:
            for i, segment in enumerate(segments):
                seg_round = segment.get('llmRound')
                if (isinstance(seg_round, int)
                        and not isinstance(seg_round, bool)
                        and seg_round > lr):
                    index = i
                    break
        block_suffix = f'continuation-{position}'
        inserted: list[dict[str, Any]] = []
        if think:
            inserted.append({
                'type': SEG_THINKING, 'text': think,
                'blockId': f'thinking:{block_suffix}',
                'deliverable': False, 'llmRound': lr,
                **identity_fields,
            })
        if text:
            inserted.append({
                'type': SEG_TEXT, 'text': text,
                'blockId': f'text:{block_suffix}',
                'deliverable': False, 'llmRound': lr,
                **identity_fields,
            })
        segments[index:index] = inserted


def _insert_injected_note_segments(
    segments: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Interleave engine-authored intervention notes into the segment list.

    Same anchor rule as the continuation-prose insert (before the first
    segment of a LATER llmRound; else the tail), and MUST run after it: on
    the wire the vetoed prose precedes the nudge that re-drove the model,
    and the shared ``seg_round > lr`` boundary only preserves that order
    when the prose is already in place.
    """
    entries = task.get(INJECTED_NOTES_FIELD)
    if not isinstance(entries, list):
        return
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        kind = entry.get('kind')
        raw_text = entry.get('text')
        text = raw_text if isinstance(raw_text, str) else ''
        if kind not in NOTE_KINDS or not text.strip():
            continue
        lr = entry.get('llmRound')
        if not isinstance(lr, int) or isinstance(lr, bool):
            lr = None
        identity_fields = {
            **({'attemptId': entry['attemptId']}
               if isinstance(entry.get('attemptId'), str)
               and entry['attemptId'] else {}),
            **({'taskId': entry['taskId']}
               if isinstance(entry.get('taskId'), str)
               and entry['taskId'] else {}),
        }
        index = len(segments)
        if lr is not None:
            for i, segment in enumerate(segments):
                seg_round = segment.get('llmRound')
                if (isinstance(seg_round, int)
                        and not isinstance(seg_round, bool)
                        and seg_round > lr):
                    index = i
                    break
        segments[index:index] = [{
            'type': SEG_SYSTEM_NOTE,
            'blockId': f'system-note:{kind}-{position}',
            'text': text,
            'noteKind': kind,
            'llmRound': lr,
            **identity_fields,
        }]


def _merged_rounds(task: dict[str, Any], merged: list | None) -> list:
    """Return the ordered checkpoint+current tool rounds.

    Lazy-imports ``_merge_tool_rounds`` to avoid a module-level import cycle
    (``manager`` imports this module to call ``assemble_segments``). Callers on
    the hot path (``persist_task_result``) pass the already-computed merged
    list so the merge runs once.
    """
    if merged is not None:
        return merged
    from lib.tasks_pkg.manager._persist import _merge_tool_rounds
    return _merge_tool_rounds(task)


def assemble_segments(task: dict[str, Any],
                      merged: list | None = None) -> list[dict[str, Any]]:
    """Build the ordered typed-segment list for a finished assistant turn.

    Args:
        task: the task dict (reads ``toolRounds`` / ``_checkpointToolRounds``
            via the merge, plus terminal ``content`` / ``thinking``).
        merged: optional pre-computed ``_merge_tool_rounds(task)`` output, to
            avoid a redundant merge on the persist hot path.

    Returns:
        An ordered list of segment dicts. Types: ``thinking``, ``text``
        (with ``deliverable`` bool), ``tool_use`` (with a nested ``result``).
        Each ``tool_use`` also carries ``_round`` — a reference to the original
        merged round dict — so ``derive_tool_rounds`` is byte-identical to
        ``_merge_tool_rounds`` BY CONSTRUCTION (the lossless-superset proof).
        The ``_round`` mirror is retired once readers migrate off ``toolRounds``
        (design §5 step 4/6); until then it is what lets step 1 ship dark with a
        provable byte-identity gate rather than a fragile field-by-field rebuild.
    """
    rounds = _merged_rounds(task, merged)
    segments: list[dict[str, Any]] = []
    previous_batch_key: tuple[Any, ...] | None = None
    ordered_batch_keys = execution_batch_keys(rounds)

    for idx, r in enumerate(rounds):
        if not isinstance(r, dict):
            continue
        # Wire-purity guard: frontend display-only inbox-inject rows (async
        # <swarm-update> / peer / user-steer chips) carry no tool_call data and
        # must NOT become tool_use segments — that would mint a phantom wire
        # tool and shift the prefix cache. They live in a separate display-only
        # sidecar. See _types.is_synthetic_inbox_round.
        if is_synthetic_inbox_round(r):
            continue
        lr = execution_llm_round(r)
        # Batch key: real tool-call rounds carry an integer llmRound
        # (tool_dispatch.py stamps round_entry['llmRound']). Rounds that BYPASS
        # that path — prefetch fetch_url (executor.py:532) and image-gen
        # progress rounds — have NO llmRound (None). Keying the dedup on the
        # raw llmRound would collapse EVERY None round into one phantom batch;
        # today that's harmless (None-llmRound rounds never carry
        # assistantContent/thinking) but it's fragile. Give each None round its
        # own batch identity (by position) so a future prose-bearing shape can
        # never be silently swallowed. Integer llmRounds still dedup correctly
        # (two tool calls in one assistant turn share llmRound → prose once).
        batch_key = ordered_batch_keys[idx]
        # The pre-tool prose + thinking of an llmRound batch is stamped onto the
        # FIRST entry of that batch. Emit those segments once per batch, in
        # order (thinking before the prose it preceded).
        # Tool calls from one provider response are contiguous. Deduplicate
        # prose only within that contiguous batch, not across the whole Turn:
        # a legacy resumed attempt may restart at the same llmRound before
        # attemptId stamping existed.
        if batch_key != previous_batch_key:
            previous_batch_key = batch_key
            block_suffix = _round_block_suffix(r, idx)
            # Legacy projections had no attempt scope.  When their local round
            # counter recurs after Continue, retain the familiar first block
            # id and suffix later occurrences so both remain addressable.
            if (len(batch_key) >= 2 and batch_key[-2] == 'occurrence'
                    and batch_key[-1]):
                block_suffix += f':occurrence-{batch_key[-1]}'
            attempt_id, task_id = execution_identity(r)
            identity_fields = {
                **({'attemptId': attempt_id} if attempt_id else {}),
                **({'taskId': task_id} if task_id else {}),
            }
            raw_think = r.get('thinking')
            think = raw_think if isinstance(raw_think, str) else ''
            if think:
                seg: dict[str, Any] = {
                    'type': SEG_THINKING, 'text': think,
                    'blockId': f'thinking:{block_suffix}',
                    'deliverable': False, 'llmRound': lr,
                    **identity_fields,
                }
                raw_sig = r.get('thinkingSignature')
                sig = raw_sig if isinstance(raw_sig, str) else ''
                if sig:
                    seg['signature'] = sig
                segments.append(seg)
            raw_ac = r.get('assistantContent')
            ac = raw_ac if isinstance(raw_ac, str) else ''
            if ac:
                segments.append({
                    'type': SEG_TEXT, 'text': ac,
                    'blockId': f'text:{block_suffix}',
                    'deliverable': False, 'llmRound': lr,
                    **identity_fields,
                })
        # Every round entry becomes a tool_use segment with its result nested,
        # so a tool and its output are one renderable unit.
        segments.append(tool_use_segment_from_round(r, idx))

    # ── Interrupted final answers vetoed by continuation enforcers ──
    # Ordered before any later llmRound's segments (and before the terminal
    # deliverable appended below) — the position they occupied on the wire.
    _insert_continuation_prose_segments(segments, task)

    # ── Engine-authored intervention notes (stall nudge / todo reminder) ──
    # After the continuation-prose pass on purpose: on the wire the vetoed
    # prose precedes the nudge, and both share the same anchor rule.
    _insert_injected_note_segments(segments, task)

    # ── Terminal round: the deliverable prose + its thinking ──
    # task['content'] / task['thinking'] hold the LAST round's output (reset
    # each tool round). Any Sources-footer / content-filter override applied in
    # _finalize_and_emit_done is already folded into task['content'] by the time
    # we assemble, so the deliverable segment captures it verbatim.
    raw_term_think = task.get('thinking')
    term_think = raw_term_think if isinstance(raw_term_think, str) else ''
    if term_think:
        segments.append({
            'type': SEG_THINKING, 'text': term_think,
            'blockId': 'thinking:terminal',
            'deliverable': False, 'terminal': True,
        })
    raw_term_content = task.get('content')
    term_content = raw_term_content if isinstance(raw_term_content, str) else ''
    if term_content:
        term_seg: dict[str, Any] = {
            'type': SEG_TEXT, 'text': term_content,
            'blockId': 'text:terminal',
            'deliverable': True, 'terminal': True,
        }
        # A turn cut off mid-answer leaves a RESUMABLE deliverable prefix.
        # Marked here (additive, dark) off the finish reason so a persisted
        # final row carries the signal; resume_prefill_from_segments also
        # accepts a finish_reason override for rows assembled at checkpoint
        # time (status='running', no finishReason yet).
        if (task.get('finishReason') or '') in RESUMABLE_FINISH_REASONS:
            term_seg['resumable'] = True
        segments.append(term_seg)

    return segments
