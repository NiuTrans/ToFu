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


def _paging_int(value, name, *, zero_means_default=False):
    """Coerce one paging arg to an integer cursor; ``(value, error)``.

    Models emit JSON numbers as strings, so ``'40'`` is honoured like ``40``.
    Everything unusable (``'abc'``, ``-5``, booleans, floats with a fraction)
    returns an ``Error:`` string. ``before=0`` is the one compatibility
    sentinel: it has no valid exclusive-cursor meaning and is normalized to
    omitted/default instead of spending a model repair round. The provider
    schema still advertises a positive minimum so new calls do not learn it.
    """
    if value is None:
        return None, None
    bad = (f'Error: {name} must be a positive integer, got {value!r}. '
           f'Omit {name} for the default window.')
    if isinstance(value, bool):
        return None, bad
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None, bad
        n = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            return None, bad
        n = int(s)
    else:
        return None, bad
    if n == 0 and zero_means_default:
        return None, None
    if n < 1:
        return None, bad
    return n, None


def execute_conv_ref_tool(fn_name, fn_args, current_conv_id=None,
                          project_path=None, *, user_id):
    """Execute a conversation reference tool and return the result string.

    Args:
        fn_name: 'list_conversations' or 'get_conversation'
        fn_args: dict of arguments
        current_conv_id: the ID of the current conversation (to prevent self-reference)
        project_path: the current task's project path, used to scope
            ``list_conversations`` to sibling conversations of the same project.
        user_id: the owning principal whose conversations are readable. The
            caller resolves it at its request or task boundary.

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
            # Paging args are validated before forwarding. The sole tolerant
            # repair is before=0 -> omitted: zero cannot identify a page and
            # production traces show it is emitted as the default-page
            # sentinel. Negative/malformed cursors and limit=0 still fail.
            limit, err = _paging_int(fn_args.get('limit'), 'limit')
            if err:
                return err
            before, err = _paging_int(
                fn_args.get('before'), 'before', zero_means_default=True,
            )
            if err:
                return err
            return get_conversation(
                conversation_id=conv_id,
                include_tool_details=include_details,
                current_conv_id=current_conv_id,
                raw=raw,
                user_id=user_id,
                limit=limit,
                before=before,
            )

        else:
            return f"Error: Unknown conversation reference tool '{fn_name}'"

    except Exception as e:
        logger.warning("Error executing conv_ref tool %s: %s", fn_name, e, exc_info=True)
        return f"Error executing {fn_name}: {str(e)}"
