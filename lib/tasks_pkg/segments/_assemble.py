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

Mid-turn checkpoints add one live-window rule: while the TRAILING tool batch
is still in flight (``LIVE_ROUND_STATUSES``) and its prose stamp has not
landed yet — the window between the tool-card early announcement and
dispatch-parse stamping, during which the narration still sits in the
accumulators — emitting that prose at the tail would invert the wire order
(prose AFTER the tool cards it preceded). In that window, and only then, the
terminal prose is placed ahead of the trailing batch's first tool card,
matching the wire and the live repair (``_append_missing_live_tool_segments``).
A settled trailing batch (e.g. a prefetch round, never prose-stamped) keeps
the accumulator prose at the tail, where it reads as the upcoming
deliverable.

``deliverable`` rule (explicit, position-based): a ``text`` segment is
``deliverable=False`` iff it is the ``assistantContent`` of a tool-round batch;
``deliverable=True`` iff it is the terminal ``task['content']``.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tool_results.tool_round_identity import (
    execution_batch_keys,
    execution_identity,
    execution_llm_round,
    model_batch_block_suffix,
)
from lib.tool_results.tool_round_replay import (
    SUPERSEDED_PROVIDER_ATTEMPT_FIELD,
    is_superseded_provider_attempt_round,
)

from lib.tasks_pkg.segments._types import (
    CONTINUATION_PROSE_FIELD,
    INJECTED_NOTES_FIELD,
    LIVE_ROUND_STATUSES,
    NOTE_KINDS,
    NOTE_ACTIONS, NOTE_REASON_LIMIT, NOTE_TEXT_LIMIT,
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
    # The committed wire contract (TurnToolResult) types ``status`` as a
    # string when present. A round announced before its verdict exists (or
    # relayed by a producer that never stamped one) must mirror as
    # ``{'content': None}`` — never ``status: None``, which fails the
    # generated snapshot schema and 500s every later snapshot read of the
    # conversation (2026-09-20, conv mu5p3vn9x32wcd).
    result: dict[str, Any] = {'content': round_dict.get('toolContent')}
    status = round_dict.get('status')
    if isinstance(status, str) and status:
        result['status'] = status
    segment = {
        'type': SEG_TOOL_USE,
        'blockId': _tool_block_id(round_dict, position),
        'id': round_dict.get('toolCallId', ''),
        'name': round_dict.get('toolName', ''),
        'input': round_dict.get('toolArgs', ''),
        'llmRound': execution_llm_round(round_dict),
        'result': result,
        '_round': round_dict,
    }

    round_num = round_dict.get('roundNum')
    if isinstance(round_num, int) and not isinstance(round_num, bool) \
            and 0 <= round_num <= 9_007_199_254_740_991:
        segment['roundNum'] = round_num
    if round_dict.get('toolModelContent') is not None:
        segment['result']['modelContent'] = round_dict['toolModelContent']
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
    action: str = 'prompt',
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Record a display-only intervention, never mutate provider messages.

    ``prompt`` means context was appended, not that a model consumed it.
    Identical records within one attempt/round are idempotent; other rounds
    and retries with new attempt identities remain separate timeline events.
    Bodies and reason codes are bounded by the public segment contract.
    """
    if not isinstance(task, dict):
        return None
    note_kind = kind if isinstance(kind, str) and kind in NOTE_KINDS else ''
    body = text[:NOTE_TEXT_LIMIT] if isinstance(text, str) else ''
    if not note_kind or not body.strip() or action not in NOTE_ACTIONS:
        return None
    entry: dict[str, Any] = {
        'llmRound': (llm_round if isinstance(llm_round, int)
                     and not isinstance(llm_round, bool) else None),
        'kind': note_kind,
        'text': body,
        'action': action,
        **({'reason': reason[:NOTE_REASON_LIMIT]} if isinstance(reason, str) and reason else {}),
    }
    attempt_id = str(task.get('_attemptId') or task.get('attemptId') or '')
    task_id = str(task.get('id') or task.get('taskId') or '')
    if attempt_id:
        entry['attemptId'] = attempt_id
        if task_id:
            entry['taskId'] = task_id
    entries = task.setdefault(INJECTED_NOTES_FIELD, [])
    for previous in entries:
        if isinstance(previous, dict) and all(
            previous.get(key, 'prompt' if key == 'action' else None) == value
            for key, value in entry.items()
        ) and previous.get('reason') == entry.get('reason'):
            return previous
    scope = attempt_id or task_id
    entry['blockId'] = f'system-note:{scope + ":" if scope else ""}{note_kind}-{len(entries)}'
    entries.append(entry)
    return entry


def _insert_continuation_prose_segments(
    segments: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    """Interleave vetoed final answers into the round-derived segment list.

    Each entry becomes the same shape a tool batch's pre-tool prose has
    (a non-deliverable, non-terminal thinking/text pair) and lands BEFORE the
    first segment of the SAME llmRound — the head of the round the enforcer
    vetoed. It shares the ``>= lr`` batch-head anchor with the intervention
    note (both name the round ABOUT to consume/drive), and because this pass
    runs first while the note insert steps past same-round prose, wire order
    "vetoed answer, then the nudge that re-drove the model" always survives. Runs before the terminal append in ``assemble_segments``,
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
                        and seg_round >= lr):
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

    A note's ``llmRound`` is the round ABOUT to consume it (stamped in
    ``dispatch`` before that round's LLM call), NOT the round that produced
    content — opposite of every other segment, whose ``llmRound`` is the
    round that PRODUCED it. So the anchor is the first segment of the SAME
    llmRound (``seg_round >= lr``), landing the note at the HEAD of the batch
    it drove — the "user speaks first" position. The old ``> lr`` rule pushed
    a turn-start (lr=0) note past round 0's whole tool batch, which is why a
    run of turn-start interventions rendered as one trailing clump after the
    tools instead of above them.

    Continuation prose is exempt from the skip: it lands at the same batch
    head via the shared ``>= lr`` rule, and this pass (running after it) steps
    PAST that same-round prose before inserting — preserving the wire order
    "prose, then the nudge that re-drove the model". A nudge that fired with
    no consuming round yet (``lr`` past every segment) still falls through to
    the tail.
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
        if not isinstance(kind, str) or not text.strip():
            continue
        if kind not in NOTE_KINDS:
            kind = 'system-intervention'
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
                if not (isinstance(seg_round, int)
                        and not isinstance(seg_round, bool)):
                    continue
                if seg_round < lr:
                    continue
                # Same-round vetoed prose stays ahead of the nudge that
                # re-drove the model past it (see docstring).
                if (seg_round == lr
                        and segment.get('type') == SEG_TEXT
                        and str(segment.get('blockId') or '').startswith(
                            'text:continuation-')):
                    continue
                # A note already anchored at this batch head is not a
                # boundary: later entries land AFTER it, keeping injection
                # order (otherwise each new note bumps the previous one and
                # a turn-start stack renders reversed). With the prose skip
                # above this makes the head a STABLE sort — prose, then notes
                # in entry order, then the round's own content.
                if (seg_round == lr
                        and segment.get('type') == SEG_SYSTEM_NOTE):
                    continue
                index = i
                break
        segments[index:index] = [{
            'type': SEG_SYSTEM_NOTE,
            'blockId': entry.get('blockId') or f'system-note:{kind}-{position}',
            'text': text,
            'noteKind': kind,
            'action': entry.get('action') if entry.get('action') in NOTE_ACTIONS else 'prompt',
            **({'reason': entry['reason'][:NOTE_REASON_LIMIT]}
               if isinstance(entry.get('reason'), str) else {}),
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
    # Trailing-batch tracking for the live pre-tool window (see the terminal
    # placement note at the bottom of this function).
    trailing_first_tool: dict[str, Any] | None = None
    trailing_batch_live = False
    trailing_batch_prose = False

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
        new_batch = batch_key != previous_batch_key
        batch_emitted_prose = False
        if new_batch:
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
            batch_emitted_prose = bool(think or ac)
        # Every round entry becomes a tool_use segment with its result nested,
        # so a tool and its output are one renderable unit.
        tool_segment = tool_use_segment_from_round(r, idx)
        segments.append(tool_segment)
        if new_batch:
            trailing_first_tool = tool_segment
            trailing_batch_live = False
            trailing_batch_prose = batch_emitted_prose
        if r.get('status') in LIVE_ROUND_STATUSES:
            trailing_batch_live = True

    # ── Interrupted final answers vetoed by continuation enforcers ──
    # Ordered before any later llmRound's segments (and before the terminal
    # deliverable appended below) — the position they occupied on the wire.
    _insert_continuation_prose_segments(segments, task)

    # ── Engine-authored intervention notes (stall nudge / todo reminder) ──
    # After the continuation-prose pass on purpose: prose keeps the tail-of-
    # round (``> lr``) anchor, the note skips past that same-round prose to the
    # head of the batch it drove, so wire order "prose then nudge" survives.
    _insert_injected_note_segments(segments, task)

    # ── Terminal round: the deliverable prose + its thinking ──
    # task['content'] / task['thinking'] hold the LAST round's output (reset
    # each tool round). Any Sources-footer / content-filter override applied in
    # _finalize_and_emit_done is already folded into task['content'] by the time
    # we assemble, so the deliverable segment captures it verbatim.
    raw_term_think = task.get('thinking')
    term_think = raw_term_think if isinstance(raw_term_think, str) else ''
    terminal_segments: list[dict[str, Any]] = []
    if term_think:
        terminal_segments.append({
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
        terminal_segments.append(term_seg)

    # ── Live pre-tool window placement ──
    # While the TRAILING batch is still in flight and its prose stamp has not
    # landed (tool cards early-announced; the stamp lands at dispatch-parse
    # time, right before the prelude's _discard_pretool_prose zeroes the
    # accumulators), the accumulator prose IS that batch's pre-tool
    # narration. Emitting it at the tail inverts the wire order — and the
    # DELTA_RESET sync that follows the discard would then blank it until the
    # round_end checkpoint re-assembles. Place it ahead of the batch's first
    # tool card instead, matching the wire and the live repair
    # (_append_missing_live_tool_segments). Gated on LIVE + UNSTAMPED: a
    # settled trailing batch (e.g. prefetch, never prose-stamped) is not a
    # narration owner, so the accumulator prose stays at the tail as the
    # upcoming deliverable. The moved segments keep their terminal flags —
    # every wire projection skips terminal segments by flag, not position.
    if (terminal_segments and trailing_first_tool is not None
            and trailing_batch_live and not trailing_batch_prose):
        insert_at = next(
            (i for i, seg in enumerate(segments) if seg is trailing_first_tool),
            len(segments))
        segments[insert_at:insert_at] = terminal_segments
    else:
        segments.extend(terminal_segments)

    return segments
