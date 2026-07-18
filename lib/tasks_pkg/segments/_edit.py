"""lib/tasks_pkg/segments/_edit.py — realign segments with an edited deliverable.

``apply_edited_deliverable`` is the exact inverse projection of
``derive_content``: it rewrites ONLY the terminal deliverable ``text`` segment,
leaving thinking / per-round narration / tool_use untouched, so the segment
list (the authoritative render/wire SoT) never plants a stale second copy of
the answer after an in-place "Edit → Save".

Pure function; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.segments._types import SEG_TEXT

logger = get_logger(__name__)


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
