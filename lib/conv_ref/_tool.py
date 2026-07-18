"""Conversation reference — tool-dispatch entrypoint.

``execute_conv_ref_tool`` routes a tool call name to ``list_conversations``
or ``get_conversation`` and returns a formatted result string.
"""

from lib.conv_ref._detail import get_conversation
from lib.conv_ref._query import list_conversations
from lib.log import get_logger

logger = get_logger(__name__)


def execute_conv_ref_tool(fn_name, fn_args, current_conv_id=None,
                          project_path=None):
    """Execute a conversation reference tool and return the result string.

    Args:
        fn_name: 'list_conversations' or 'get_conversation'
        fn_args: dict of arguments
        current_conv_id: the ID of the current conversation (to prevent self-reference)
        project_path: the current task's project path, used to scope
            ``list_conversations`` to sibling conversations of the same project.

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
            )

        elif fn_name == 'get_conversation':
            conv_id = fn_args.get('conversation_id', '')
            if not conv_id:
                return "Error: conversation_id is required."
            include_details = fn_args.get('include_tool_details', True)
            raw = fn_args.get('raw', False)
            return get_conversation(
                conversation_id=conv_id,
                include_tool_details=include_details,
                current_conv_id=current_conv_id,
                raw=raw,
            )

        else:
            return f"Error: Unknown conversation reference tool '{fn_name}'"

    except Exception as e:
        logger.warning("Error executing conv_ref tool %s: %s", fn_name, e, exc_info=True)
        return f"Error executing {fn_name}: {str(e)}"
