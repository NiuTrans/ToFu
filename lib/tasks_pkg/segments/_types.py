"""lib/tasks_pkg/segments/_types.py — the closed vocabulary constants.

These are the segment-type tags and the resumable-finish-reason set. They are
defined here ONCE and re-exported from the package facade so every consumer
shares the SAME constant object (tests import ``SEG_THINKING`` / ``SEG_TEXT`` /
``SEG_TOOL_USE`` / ``RESUMABLE_FINISH_REASONS`` by name).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# Segment type constants — the closed vocabulary (Anthropic content-blocks shape).
SEG_THINKING = 'thinking'
SEG_TEXT = 'text'
SEG_TOOL_USE = 'tool_use'
# The nested tool-result shape lives inside a tool_use segment under
# ``result`` ({content, status}); there is no standalone tool_result segment
# tag today. Provided as a named constant for symmetry / forward-compat so
# consumers that reference it by name resolve against this single object.
SEG_TOOL_RESULT = 'tool_result'

# Engine-authored intervention note (``role='user', _isMeta=True`` on the
# wire): the intent-stall nudge and the todo-continuation reminder. These
# messages re-drive the model mid-turn; the segment makes the intervention a
# first-class timeline entry at its true insertion position instead of a
# display-only sidecar chip or a transient status line. A distinct type —
# NOT a ``text`` segment — is load-bearing for wire purity: the replay
# projections (``_rounds_view_from_segments``) collect non-deliverable
# ``text`` segments as assistant batch prose, and the note was a USER-role
# message, so as ``text`` it would be re-sent as assistant content.
SEG_SYSTEM_NOTE = 'system_note'

# The closed ``noteKind`` vocabulary. Renderers key styling/labels off it.
NOTE_INTENT_STALL = 'intent-stall'
NOTE_TODO_CONTINUATION = 'todo-continuation'
NOTE_KINDS = frozenset({NOTE_INTENT_STALL, NOTE_TODO_CONTINUATION})

# ── Synthetic display-only tool rounds (agent_inbox lanes) ──
# The frontend surfaces async <swarm-update> / peer / user-steer injections as
# SYNTHETIC toolRounds entries (roundNum 9e6+, no toolCallId / toolContent) so
# they show up as in-timeline chips. They are DISPLAY-ONLY and must NEVER reach
# the wire: toolRounds is also the replay source, and a row lacking
# toolCallId/toolContent collapses the whole assistant turn into a lossy summary
# (breaking tool-turn continuation AND shifting prefix-cache bytes). Both
# reconstructors (_reconstruct_tool_call_messages, assemble_segments) skip any
# round for which is_synthetic_inbox_round() is True. Adding a new inbox lane =
# add its marker key here (single source of truth).
#
# ``_stallNudge`` is the intent-stall lane: when criterion A∧B∧C∧D fires, the
# orchestrator appends a ``role='user'`` nudge to the WIRE messages so the model
# is re-driven. That injection is structurally identical to a human steer — same
# role, same round-boundary placement — but it has NO human author, so it must
# never be mistaken for something the user said. It gets a display-only chip and
# is excluded from every replay projection, exactly like the other three.
SYNTHETIC_INBOX_MARKERS = ('_inboxInject', '_peerInject', '_userSteerInject',
                           '_stallNudge', '_programSynthetic')


def is_synthetic_inbox_round(round_dict) -> bool:
    """True iff ``round_dict`` is a frontend display-only inbox-inject row.

    Such rows carry a lane marker (``_inboxInject`` / ``_peerInject`` /
    ``_userSteerInject`` / ``_stallNudge``) and no real tool_call data. They must
    be excluded from every wire-replay projection so the bytes sent to the model
    are identical whether or not the chips are present.
    """
    if not isinstance(round_dict, dict):
        return False
    return any(round_dict.get(k) for k in SYNTHETIC_INBOX_MARKERS)


# ── Continuation-prose durable record ──
# Task field holding the ordered snapshots of interrupted final answers:
# prose-only rounds whose stop was vetoed by a continuation enforcer
# (todo-continuation / intent-stall nudge). The wire keeps those answers (an
# assistant row is appended before the meta nudge), but the three legacy
# channels cannot represent them: the round owns no tool batch to stamp
# ``assistantContent`` onto, and ``task['content']`` / ``task['thinking']``
# are zeroed by the NEXT tool round's ``_discard_pretool_prose`` — so the
# interrupted answer silently vanished from the assembled segments (the
# 2026-09-02 lost-report incident). ``assemble_segments`` interleaves these
# entries as non-deliverable prose so the durable display record is lossless.
CONTINUATION_PROSE_FIELD = '_continuation_prose'


# Task field holding the ordered engine-authored intervention notes (see
# ``SEG_SYSTEM_NOTE`` above). Same lifecycle as ``CONTINUATION_PROSE_FIELD``:
# recorded on the task dict at injection time, consumed by
# ``assemble_segments`` at the next checkpoint / final assembly.
INJECTED_NOTES_FIELD = '_injected_notes'


# Finish reasons under which the terminal deliverable text is a RESUMABLE
# prefix (a turn cut off mid-answer), not a settled answer. Continue can feed
# this tail back as an assistant prefill so a capable provider continues the
# SAME tokens rather than regenerating from scratch. ``length`` (model hit
# max_tokens) is the canonical Continue case; the three interrupt reasons cover
# a dropped transport / server crash / frontend stop. ``aborted`` is the MANUAL
# Stop case: the frontend stamps it optimistically on Stop click, and the
# partial answer is a perfectly valid prefill prefix — excluding it made a
# Stop→Continue on a no-tools turn fall back to a full regeneration that
# discarded the partial prose (the manual-stop lossless gap, epic
# ). An empty aborted turn is still correctly declined
# downstream (resume_prefill_from_segments returns None on empty text).
RESUMABLE_FINISH_REASONS = frozenset({
    'interrupted', 'server_offline', 'premature_close', 'length', 'aborted',
})
