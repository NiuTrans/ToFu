# HOT_PATH
"""Advanced-host STRUCTURAL example — ``drop_superseded_turns``.

Deletes whole evictable turns whose assistant messages are pure mechanical
tool activity (no natural-language synthesis).  Runs only under the
advanced host (Stage B), selected via
``task['config']['compaction']['advanced_steps']``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)

logger = get_logger(__name__)


# Tools whose tool_call args carry a file path (used to decide whether an
# old turn's work has been superseded by later edits to the same files).
_PATH_TOOLS = frozenset({
    'read_files', 'read_file', 'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})


@register_step('drop_superseded_turns', kind=STEP_KIND_STRUCTURAL)
def drop_superseded_turns(ctx: CompactionContext) -> int:
    """STRUCTURAL example: delete whole evictable turns whose assistant
    messages are pure mechanical tool activity (no natural-language
    synthesis).

    A *turn* = ``[user request, ...assistant/tool work]``.  Whole-turn
    eviction necessarily drops the turn's user request too — that is the
    nature of structural turn-drop (it is why Layer 2 *summarizes* before
    dropping).  This standalone variant is therefore lossy by design and
    is opt-in: it targets cold turns that were pure exploration
    (assistant emitted only tool calls, no prose decision/explanation),
    on the theory that such turns carry no reasoning the model still
    needs.  Methods that must preserve the request should pair this with
    a summary (see :func:`summarize_oldest_turn`).

    The :class:`MessageEditor` guarantees we never drop the in-flight
    turn or a turn overlapping the cache prefix, and whole-turn deletion
    can't orphan a tool_call↔tool pair.  Real structural methods
    (relevance-based eviction, etc.) plug in the same way.
    """
    editor = ctx.edit
    if editor is None:  # not granted (wrong host) — defensive no-op
        return 0

    def _turn_is_pure_tool_activity(turn) -> bool:
        msgs = ctx.messages
        saw_tool = False
        for i in turn.indices:
            m = msgs[i]
            role = m.get('role')
            if role == 'tool':
                saw_tool = True
            elif role == 'assistant':
                # Any natural-language assistant content → keep the turn
                # (it holds a decision/explanation worth preserving). The
                # user request is part of the turn and is dropped with it.
                c = m.get('content')
                if isinstance(c, str) and c.strip():
                    return False
                if isinstance(c, list) and any(
                        isinstance(b, dict) and b.get('type') == 'text'
                        and b.get('text', '').strip() for b in c):
                    return False
        return saw_tool

    victims = [t for t in editor.evictable_turns()
               if _turn_is_pure_tool_activity(t)]
    if not victims:
        return 0
    return editor.drop_turns(victims)
