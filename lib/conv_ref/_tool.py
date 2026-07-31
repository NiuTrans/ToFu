"""Conversation reference — tool-dispatch entrypoint.

``execute_conv_ref_tool`` routes a tool call name to ``list_conversations``
or ``get_conversation`` and returns a formatted result string.
"""

from lib.conv_ref._detail import get_conversation
from lib.conv_ref._query import list_conversations
from lib.log import get_logger

logger = get_logger(__name__)


def raw_requested(fn_args):
    """Whether a ``get_conversation`` TOOL call returns the raw DB record.

    The tool surface defaults to RAW (owner-directed): a read whose purpose is
    debugging must show the record, not a prose retelling of it. The prose
    transcript remains reachable with an explicit ``raw=false``, and the
    library default (:func:`lib.conv_ref.get_conversation`) is untouched — the
    ``@``-mention injection path and the human export both still want prose.

    This is the ONE place the tool-surface default is decided. Both the
    executor and the display handler that badges the card ``RAW``
    (``lib/tasks_pkg/handlers/misc/_brain.py``) resolve the mode here; while
    each side read ``fn_args.get('raw')`` for itself, flipping the default on
    one of them would leave the card describing a mode that was never run.

    A model may emit the flag as a JSON string, so ``"false"`` / ``"0"`` are
    honoured as the opt-out they clearly mean.
    """
    val = fn_args.get('raw')
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return bool(val)


def execute_conv_ref_tool(fn_name, fn_args, current_conv_id=None,
                          project_path=None, user_id=None):
    """Execute a conversation reference tool and return the result string.

    Args:
        fn_name: 'list_conversations' or 'get_conversation'
        fn_args: dict of arguments
        current_conv_id: the ID of the current conversation (to prevent self-reference)
        project_path: the current task's project path, used to scope
            ``list_conversations`` to sibling conversations of the same project.
        user_id: the OWNING principal whose conversations are readable. The
            caller resolves it (``task_user_id(task)`` on a background task
            thread, ``_request_user_id()`` on a request thread); ``None``
            falls back to the single-user default.

    Returns:
        str: formatted result
    """
    try:
        if fn_name == 'list_conversations':
            keyword = fn_args.get('keyword', None)
            limit = fn_args.get('limit', 20)
            scope = fn_args.get('scope', 'auto')
            return list_conversations(
                keyword=keyword, limit=limit, scope=scope,
                project_path=project_path, current_conv_id=current_conv_id,
                user_id=user_id,
            )

        elif fn_name == 'get_conversation':
            conv_id = fn_args.get('conversation_id', '')
            if not conv_id:
                return "Error: conversation_id is required."
            include_details = fn_args.get('include_tool_details', True)
            raw = raw_requested(fn_args)
            return get_conversation(
                conversation_id=conv_id,
                include_tool_details=include_details,
                current_conv_id=current_conv_id,
                raw=raw,
                user_id=user_id,
            )

        else:
            return f"Error: Unknown conversation reference tool '{fn_name}'"

    except Exception as e:
        logger.warning("Error executing conv_ref tool %s: %s", fn_name, e, exc_info=True)
        return f"Error executing {fn_name}: {str(e)}"
