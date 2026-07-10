"""lib/tasks_pkg/segments.py — the ordered typed-segment model (SoT groundwork).

Board epic ``pt_cb8f98b0cb9b47fb`` (design: docs/EPIC_SEGMENT_TIMELINE_DESIGN.md).

An assistant turn is stored today as THREE parallel channels on the task dict:
``task['content']`` (deliverable string), ``task['thinking']`` (reasoning
string), and ``task['toolRounds']`` (ordered per-round dicts). They are not
interleaved, so the chronological order the model produced
"thinking → prose → tool → prose → answer" is lost — which is the root cause of
both the headless "narrator" leak (streaming compat forwards every delta, incl.
scaffolding prose) and the grouped-by-type UI layout.

This module introduces the replacement: ONE ordered, append-only list of typed
segments (the Anthropic content-blocks shape we already half-emit). Each text
segment carries a ``deliverable`` flag — the structural boundary between the
answer and inter-round narration that the three-channel model lacks.

**Step 1 discipline (this file): SHIPS DARK.** ``assemble_segments`` is
populated alongside the three channels; the three channels are proved to be
loss-less *projections* of the segment list via ``derive_content`` /
``derive_thinking`` / ``derive_tool_rounds``. NOTHING reads the segments yet
(the compat surfaces, DB persistence, and the frontend are migrated in later
steps). The golden test ``tests/test_segment_model.py`` pins byte-identity so
none of the ~40 measured backend readers can drift.

Ordering observer (the "single seam" question): the interleaving is ALREADY
fully captured at finalization time — no token-level hook is needed. Each
llmRound batch's pre-tool prose is stamped onto the FIRST entry of that batch
as ``assistantContent`` / ``thinking`` / ``thinkingSignature``
(``lib/tasks_pkg/tool_dispatch.py`` ~725) *before* ``_discard_pretool_prose``
zeroes the accumulators; the terminal round's deliverable prose survives in
``task['content']`` / ``task['thinking']``. So the ordered
``_merge_tool_rounds(task)`` list + the terminal strings are a complete,
lossless record. This module is a pure function of that record.

``deliverable`` rule (explicit, position-based, INDEPENDENT of
``_discard_pretool_prose`` having run): a ``text`` segment is
``deliverable=False`` iff it is the ``assistantContent`` of a tool-round batch
(prose emitted in a round that went on to call tools); it is
``deliverable=True`` iff it is the terminal ``task['content']`` (prose from the
round that ended the loop with no tool calls). Because assembly reads the
pre-tool snapshot (``assistantContent``) and the terminal string separately,
the classification would still be correct even if the discard reset were
deleted — the NC-1 in the golden test pins exactly that.

Pure functions; no Flask, no DB, no LLM. No logger side effects in the hot
projections (they run at finalization, once per turn).
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.model_info import model_supports_assistant_prefill

logger = get_logger(__name__)


# Segment type constants — the closed vocabulary (Anthropic content-blocks shape).
SEG_THINKING = 'thinking'
SEG_TEXT = 'text'
SEG_TOOL_USE = 'tool_use'

# Finish reasons under which the terminal deliverable text is a RESUMABLE
# prefix (a turn cut off mid-answer), not a settled answer. Continue can feed
# this tail back as an assistant prefill so a capable provider continues the
# SAME tokens rather than regenerating from scratch. ``length`` (model hit
# max_tokens) is the canonical Continue case; the three interrupt reasons cover
# a dropped transport / server crash / frontend stop.
RESUMABLE_FINISH_REASONS = frozenset({
    'interrupted', 'server_offline', 'premature_close', 'length',
})


def _merged_rounds(task: dict[str, Any], merged: list | None) -> list:
    """Return the ordered checkpoint+current tool rounds.

    Lazy-imports ``_merge_tool_rounds`` to avoid a module-level import cycle
    (``manager`` imports this module to call ``assemble_segments``). Callers on
    the hot path (``persist_task_result``) pass the already-computed merged
    list so the merge runs once.
    """
    if merged is not None:
        return merged
    from lib.tasks_pkg.manager import _merge_tool_rounds
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
    seen_batches: set = set()

    for idx, r in enumerate(rounds):
        if not isinstance(r, dict):
            continue
        lr = r.get('llmRound')
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
        batch_key = lr if lr is not None else ('__no_llmround__', idx)
        # The pre-tool prose + thinking of an llmRound batch is stamped onto the
        # FIRST entry of that batch. Emit those segments once per batch, in
        # order (thinking before the prose it preceded).
        if batch_key not in seen_batches:
            seen_batches.add(batch_key)
            think = r.get('thinking')
            if think:
                seg: dict[str, Any] = {
                    'type': SEG_THINKING, 'text': think,
                    'deliverable': False, 'llmRound': lr,
                }
                sig = r.get('thinkingSignature')
                if sig:
                    seg['signature'] = sig
                segments.append(seg)
            ac = r.get('assistantContent')
            if ac:
                segments.append({
                    'type': SEG_TEXT, 'text': ac,
                    'deliverable': False, 'llmRound': lr,
                })
        # Every round entry becomes a tool_use segment with its result nested,
        # so a tool and its output are one renderable unit.
        segments.append({
            'type': SEG_TOOL_USE,
            'id': r.get('toolCallId', ''),
            'name': r.get('toolName', ''),
            'input': r.get('toolArgs', ''),
            'llmRound': lr,
            'result': {'content': r.get('toolContent'),
                       'status': r.get('status')},
            '_round': r,
        })

    # ── Terminal round: the deliverable prose + its thinking ──
    # task['content'] / task['thinking'] hold the LAST round's output (reset
    # each tool round). Any Sources-footer / content-filter override applied in
    # _finalize_and_emit_done is already folded into task['content'] by the time
    # we assemble, so the deliverable segment captures it verbatim.
    term_think = task.get('thinking') or ''
    if term_think:
        segments.append({
            'type': SEG_THINKING, 'text': term_think,
            'deliverable': False, 'terminal': True,
        })
    term_content = task.get('content') or ''
    if term_content:
        term_seg: dict[str, Any] = {
            'type': SEG_TEXT, 'text': term_content,
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


def derive_content(segments: list[dict[str, Any]]) -> str:
    """Project the deliverable answer string from the segment list.

    Byte-identical to today's ``task['content']``: the concatenation of
    ``text`` segments flagged ``deliverable`` (only the terminal round produces
    one in the current pipeline). Inter-round narration (``deliverable=False``)
    is excluded — this is the boundary the headless narrator fix (step 3) keys
    on.
    """
    return ''.join(
        s.get('text', '') for s in segments
        if s.get('type') == SEG_TEXT and s.get('deliverable')
    )


def derive_thinking(segments: list[dict[str, Any]]) -> str:
    """Project the reasoning string from the segment list.

    Byte-identical to today's ``task['thinking']`` (the terminal round's
    reasoning accumulator — per-round thinking lives on the tool_use rounds and
    is NOT part of this projection, matching the current channel semantics).
    """
    for s in segments:
        if s.get('type') == SEG_THINKING and s.get('terminal'):
            return s.get('text', '')
    return ''


def _rounds_view_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild a per-round view (the `toolRounds` shape the reconstructors
    consume) from the SEGMENT structure — sourcing every field from the
    segments, not from a passed-in rounds list.

    This is what makes the reconstruction genuinely *segment-driven* rather
    than a `derive_tool_rounds` tautology: it reads `tool_use.id/name/input/
    result`, pairs each batch's `deliverable:false` text segment as
    `assistantContent` and its `thinking` segment (+signature) — exactly the
    fields `_reconstruct_tool_call_messages` / `inject_tool_history` need. The
    ONLY field not present in a thin (persisted) segment is Gemini's
    `extraContent`; it is pulled from the rehydrated `_round` mirror when
    present (so callers rehydrate first).

    Batch prose is attached to the FIRST tool_use of each llmRound, matching
    the "first-seen assistantContent in batch" rule of the reconstructors.
    """
    # Pre-scan: per-batch prose + thinking (from the non-terminal text/thinking
    # segments assemble_segments emits once per llmRound batch).
    batch_text: dict[Any, str] = {}
    batch_think: dict[Any, str] = {}
    batch_sig: dict[Any, str] = {}
    for s in segments:
        if s.get('terminal'):
            continue
        lr = s.get('llmRound')
        st = s.get('type')
        if st == SEG_TEXT and not s.get('deliverable'):
            batch_text.setdefault(lr, s.get('text', ''))
        elif st == SEG_THINKING:
            batch_think.setdefault(lr, s.get('text', ''))
            if s.get('signature'):
                batch_sig.setdefault(lr, s['signature'])

    rounds: list[dict[str, Any]] = []
    seen_prose_batches: set = set()
    for s in segments:
        if s.get('type') != SEG_TOOL_USE:
            continue
        lr = s.get('llmRound')
        result = s.get('result') or {}
        r: dict[str, Any] = {
            'toolCallId': s.get('id', ''),
            'toolName': s.get('name', ''),
            'toolArgs': s.get('input', ''),
            'toolContent': result.get('content'),
            'status': result.get('status'),
            'llmRound': lr,
        }
        # Attach the batch prose/thinking to the FIRST tool_use of the batch.
        if lr not in seen_prose_batches:
            seen_prose_batches.add(lr)
            if batch_text.get(lr):
                r['assistantContent'] = batch_text[lr]
            if batch_think.get(lr):
                r['thinking'] = batch_think[lr]
            if batch_sig.get(lr):
                r['thinkingSignature'] = batch_sig[lr]
        # extraContent (Gemini thought_signature) is thin-stripped — recover it
        # from the rehydrated origin round if present.
        origin = s.get('_round') or {}
        if origin.get('extraContent'):
            r['extraContent'] = origin['extraContent']
        rounds.append(r)
    return rounds


def reconstruct_tool_messages_from_segments(segments: list[dict[str, Any]]):
    """Segment-driven equivalent of `_reconstruct_tool_call_messages(rounds)`.

    Rebuilds the per-round view from the segment structure
    (`_rounds_view_from_segments`) then delegates to the vetted reconstructor,
    so the emitted assistant(tool_calls)+tool message shape is byte-identical
    to the toolRounds-fed path — proving segments can drive the exact wire
    messages. Returns the message list, or None (→ caller uses the legacy
    fallback) when any round lacks the required identity fields.
    """
    from lib.tasks_pkg.conv_message_builder import _reconstruct_tool_call_messages
    rounds = _rounds_view_from_segments(segments)
    return _reconstruct_tool_call_messages(rounds)


def tool_history_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the `cfg['toolHistory']` shape (what `inject_tool_history`
    consumes on Continue) from the segment structure — one entry per llmRound
    batch: `{assistantContent, thinking, thinkingSignature, toolCalls[], toolResults[]}`.

    Lets a Continue rebuild be driven from persisted segments byte-identically
    to the frontend-supplied toolHistory (the step-4 parity gate).
    """
    rounds = _rounds_view_from_segments(segments)
    history: list[dict[str, Any]] = []
    by_batch: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for r in rounds:
        lr = r.get('llmRound')
        if lr not in by_batch:
            entry: dict[str, Any] = {'toolCalls': [], 'toolResults': []}
            if r.get('assistantContent'):
                entry['assistantContent'] = r['assistantContent']
            if r.get('thinking'):
                entry['thinking'] = r['thinking']
            if r.get('thinkingSignature'):
                entry['thinkingSignature'] = r['thinkingSignature']
            by_batch[lr] = entry
            order.append(lr)
        tc: dict[str, Any] = {'id': r['toolCallId'], 'name': r['toolName'],
                              'arguments': r.get('toolArgs') or '{}'}
        if r.get('extraContent'):
            tc['extraContent'] = r['extraContent']
        by_batch[lr]['toolCalls'].append(tc)
        by_batch[lr]['toolResults'].append(
            {'tool_call_id': r['toolCallId'], 'content': r.get('toolContent') or ''})
    for lr in order:
        history.append(by_batch[lr])
    return history


def resume_prefill_from_segments(segments: list[dict[str, Any]] | None,
                                 model: str,
                                 finish_reason: str | None = None) -> str | None:
    """Extract the resumable assistant-prefill string for a Continue turn.

    Returns the terminal deliverable text (the tail the model was mid-writing)
    IFF the provider tolerates a trailing ``role='assistant'`` prefill AND the
    turn ended in a resumable state. Otherwise ``None`` → the caller keeps the
    universal contentPrefix-seed behaviour (Claude / clean stop).

    Why the terminal deliverable segment is the correct prefill for BOTH
    interruption shapes: segments are assembled from the LIVE task where
    ``task['content']`` holds ONLY the terminal round's prose (the accumulator
    is zeroed after each tool batch by ``_discard_pretool_prose``). So the
    terminal deliverable is exactly the trailing tail after the last completed
    tool batch (case 2), and for a no-tool turn it is the whole in-progress
    answer (case 3). The pre-tool prose of earlier batches is replayed
    separately via ``inject_tool_history`` — so prefilling the terminal
    deliverable never double-counts it.

    Args:
        segments: the persisted (thin) segment list, or None.
        model: target model id — gated via ``model_supports_assistant_prefill``
            (False for Claude → prefill removed / rejected; the fail-closed
            gate).
        finish_reason: authoritative finish reason from the message dict. Used
            when the persisted segment was assembled at a partial checkpoint
            (status='running', no finishReason yet) so its ``resumable`` flag
            was not stamped. When provided and resumable, it overrides.

    Returns:
        The prefill string, or ``None`` when prefill is unavailable/unwanted.
    """
    if not segments:
        return None
    if not model_supports_assistant_prefill(model):
        return None  # Claude — fail closed (Messages API rejects the prefill)
    fr_resumable = (finish_reason or '') in RESUMABLE_FINISH_REASONS
    for s in segments:
        if s.get('type') == SEG_TEXT and s.get('terminal') and s.get('deliverable'):
            text = s.get('text') or ''
            if not text:
                return None
            if s.get('resumable') or fr_resumable:
                return text
            return None
    return None


def apply_edited_deliverable(segments: list[dict[str, Any]] | None,
                             new_content: str) -> list[dict[str, Any]] | None:
    """Realign a persisted segment list with a user-edited deliverable string.

    When a finished assistant/critic/VU turn is edited in place (the chatInner
    "Edit → Save" affordance PATCHes ``content``), the stored ``segments`` list
    still carries the PRE-EDIT terminal deliverable text segment. Because the
    segment list is the authoritative render/wire SoT (``deliverable_text`` /
    ``derive_content`` read it first), leaving it stale plants a second source
    of truth that resurfaces the old answer on any segment-driven read.

    This rewrites ONLY the terminal deliverable ``text`` segment to ``new_content``
    (the exact inverse projection of ``derive_content``); every other segment
    — thinking, per-round narration, tool_use — is untouched, so the tool
    timeline and reasoning history survive the edit intact. Returns a NEW list
    (inputs not mutated), or ``None`` when there is nothing to realign so the
    caller can skip the write:

      * ``segments`` is empty/None (nothing to keep consistent), or
      * no terminal deliverable segment exists AND ``new_content`` is empty
        (a no-op — don't synthesize a segment for an empty edit).

    When a segment list exists but has no terminal deliverable (e.g. a turn
    that ended purely on tools) and ``new_content`` is non-empty, a terminal
    deliverable segment is appended so the edited answer becomes part of the
    SoT.
    """
    if not segments:
        return None
    out = [dict(s) for s in segments]
    for s in out:
        if (s.get('type') == SEG_TEXT and s.get('terminal')
                and s.get('deliverable')):
            if s.get('text', '') == new_content:
                return None  # already consistent — no write needed
            s['text'] = new_content
            return out
    # No terminal deliverable segment present.
    if not new_content:
        return None
    out.append({'type': SEG_TEXT, 'text': new_content,
                'deliverable': True, 'terminal': True})
    return out


def deliverable_text(task: dict[str, Any]) -> str:
    """The narration-free deliverable answer for a headless/compat consumer.

    THE single source of truth for "what text is the answer" on the compat
    surfaces (sync + streaming, OpenAI + Anthropic). Prefers the segment model
    (`derive_content` over `task['segments']`, i.e. concat of `deliverable:true`
    text — inter-round narration excluded by construction); falls back to
    `task['content']` when segments are absent (e.g. an in-flight task whose
    segments haven't been assembled yet at persist time). Both yield the same
    clean deliverable — `task['content']` is already narration-free post
    `_discard_pretool_prose` — so the fallback is safe, not lossy.
    """
    segs = task.get('segments')
    if segs:
        return derive_content(segs)
    return task.get('content') or ''


def derive_tool_rounds(segments: list[dict[str, Any]]) -> list:
    """Project the ordered tool-round list from the segment list.

    Byte-identical to ``_merge_tool_rounds(task)`` by construction — each
    ``tool_use`` segment mirrors its origin round under ``_round`` and this
    returns them in segment order (which is merged order).
    """
    return [s['_round'] for s in segments
            if s.get('type') == SEG_TOOL_USE and '_round' in s]


def segments_to_json(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the PERSISTABLE ("thin") form of the segment list.

    Strips the ``_round`` mirror off every ``tool_use`` segment. ``_round``
    embeds the ENTIRE origin round dict (assistantContent / toolArgs / thinking
    / results / …), which is already persisted verbatim in the sibling
    ``task_results.tool_rounds`` column and ``last_msg['toolRounds']``. Keeping
    it inside ``segments`` too would double the largest payload AND create a
    second source of truth that can drift from the ``toolRounds`` column.

    The thin form keeps everything a reader needs WITHOUT ``toolRounds``:
    ``thinking`` / ``text`` (with ``deliverable``) segments are complete, and a
    ``tool_use`` keeps ``id`` / ``name`` / ``input`` / ``llmRound`` / ``result``
    (the nested ``{content,status}``) — enough for the compat surfaces (step 3)
    to render block-by-block. The full round is recoverable via
    ``rehydrate_segments`` when ``derive_tool_rounds`` is needed (step 4).

    Returns NEW segment dicts (shallow copies); the input is not mutated.
    """
    out: list[dict[str, Any]] = []
    for s in segments:
        if s.get('type') == SEG_TOOL_USE and '_round' in s:
            s = {k: v for k, v in s.items() if k != '_round'}
        out.append(s)
    return out


def rehydrate_segments(thin_segments: list[dict[str, Any]],
                       tool_rounds: list) -> list[dict[str, Any]]:
    """Re-attach the ``_round`` mirror to a thin (persisted) segment list.

    The inverse of ``segments_to_json``: walks the ``tool_use`` segments in
    order and re-zips each with the correspondingly-ordered entry of
    ``tool_rounds`` (assembly emits exactly one ``tool_use`` per merged round,
    in merged order — so the k-th ``tool_use`` segment maps to the k-th round).
    After rehydration ``derive_tool_rounds`` is byte-identical to
    ``_merge_tool_rounds`` again, proving the strip is LOSSLESS given
    ``tool_rounds`` was co-persisted.

    Non-``tool_use`` segments pass through unchanged. If the counts disagree
    (should never happen for a co-persisted pair) the surplus ``tool_use``
    segments are left thin — a reader that needs ``_round`` will simply skip
    them in ``derive_tool_rounds`` rather than crash.

    Returns NEW segment dicts; inputs are not mutated.
    """
    out: list[dict[str, Any]] = []
    tu_idx = 0
    for s in thin_segments:
        if s.get('type') == SEG_TOOL_USE:
            if tu_idx < len(tool_rounds):
                s = {**s, '_round': tool_rounds[tu_idx]}
            tu_idx += 1
        out.append(s)
    return out


__all__ = [
    'assemble_segments',
    'derive_content',
    'derive_thinking',
    'derive_tool_rounds',
    'deliverable_text',
    'apply_edited_deliverable',
    'resume_prefill_from_segments',
    'reconstruct_tool_messages_from_segments',
    'tool_history_from_segments',
    'segments_to_json',
    'rehydrate_segments',
    'RESUMABLE_FINISH_REASONS',
    'SEG_THINKING',
    'SEG_TEXT',
    'SEG_TOOL_USE',
]
