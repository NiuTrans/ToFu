"""Conversation Reference — retrieve and format other conversations for cross-referencing.

Provides two tool implementations:
  - list_conversations: search/list conversations with optional keyword filter
  - get_conversation: retrieve full conversation content by ID
"""

import json

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger
from lib.utils import safe_json

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors routes/common.py


def _get_db():
    """Get a DB connection — works both inside Flask request context and background threads."""
    try:
        from flask import has_app_context
        if has_app_context():
            from lib.database import get_db
            return get_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug("Flask app context not available, using thread DB: %s", e, exc_info=True)
    return get_thread_db(DOMAIN_CHAT)


def list_conversations(keyword=None, limit=20):
    """List conversations, optionally filtered by keyword in title.

    Returns a formatted string with conversation metadata.
    """
    limit = min(max(1, int(limit or 20)), 50)
    db = _get_db()

    if keyword:
        rows = db.execute(
            '''SELECT id, title, created_at, updated_at,
                      json_array_length(messages) as msg_count
               FROM conversations
               WHERE user_id=? AND title LIKE ?
               ORDER BY updated_at DESC LIMIT ?''',
            (DEFAULT_USER_ID, f'%{keyword}%', limit)
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT id, title, created_at, updated_at,
                      json_array_length(messages) as msg_count
               FROM conversations
               WHERE user_id=?
               ORDER BY updated_at DESC LIMIT ?''',
            (DEFAULT_USER_ID, limit)
        ).fetchall()

    if not rows:
        if keyword:
            return f"No conversations found matching '{keyword}'. Try a different keyword or omit the keyword to list all recent conversations."
        return "No conversations found."

    lines = [f"Found {len(rows)} conversation(s):\n"]
    for r in rows:
        title = r['title'] or '(untitled)'
        msg_count = r['msg_count'] or 0
        conv_id = r['id']
        updated = r['updated_at'] or r['created_at'] or 0

        # Format timestamp
        if updated:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(updated / 1000, tz=timezone.utc)
                time_str = dt.strftime('%Y-%m-%d %H:%M UTC')
            except (ValueError, OSError):
                logger.debug('Failed to parse timestamp %s for conversation', updated, exc_info=True)
                time_str = str(updated)
        else:
            time_str = 'unknown'

        lines.append(f"• [{conv_id}] \"{title}\" — {msg_count} messages, updated {time_str}")

    lines.append("\nUse get_conversation(conversation_id=\"<id>\") to retrieve full content.")
    return '\n'.join(lines)


def get_conversation(conversation_id, include_tool_details=True, current_conv_id=None):
    """Retrieve and format the full content of a conversation.

    Args:
        conversation_id: ID of the conversation to fetch
        include_tool_details: whether to include full tool arguments/results
        current_conv_id: the current conversation's ID (to prevent self-reference loops)

    Returns a formatted string with all messages, tool calls, and results.
    """
    if current_conv_id and conversation_id == current_conv_id:
        return "Error: Cannot reference the current conversation — you are already in it. Use list_conversations to find other conversations."

    db = _get_db()
    row = db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE id=? AND user_id=?',
        (conversation_id, DEFAULT_USER_ID)
    ).fetchone()

    if not row:
        return f"Error: Conversation '{conversation_id}' not found. Use list_conversations to find valid conversation IDs."

    title = row['title'] or '(untitled)'
    messages = safe_json(row['messages'], default=[], label='conv-ref-messages')

    if not messages:
        return f"Conversation '{title}' [{conversation_id}] exists but has no messages."

    # Parse settings for model info
    settings = safe_json(row['settings'], default={}, label='conv-ref-settings')

    # Build formatted output
    parts = []
    parts.append(f"{'═' * 60}")
    parts.append(f"📋 Referenced Conversation: \"{title}\"")
    parts.append(f"   ID: {conversation_id}")
    if settings.get('preset'):
        parts.append(f"   Model preset: {settings['preset']}")
    parts.append(f"   Messages: {len(messages)}")
    parts.append(f"{'═' * 60}")
    parts.append("")

    for i, msg in enumerate(messages):
        role = msg.get('role', 'unknown')
        content = msg.get('content', '')

        if role == 'user':
            parts.append(f"── User Message #{i+1} {'─' * 40}")
            # Handle text content
            text = _extract_text(content)
            if text:
                parts.append(text)

            # Note any images/PDFs
            if msg.get('images'):
                parts.append(f"  [Contains {len(msg['images'])} image(s)]")
            if msg.get('pdfTexts'):
                for pdf in msg['pdfTexts']:
                    parts.append(f"  [PDF: {pdf.get('name', 'unknown')} — {pdf.get('pages', '?')} pages]")
                    if include_tool_details and pdf.get('text'):
                        # Truncate very long PDFs
                        pdf_text = pdf['text']
                        if len(pdf_text) > 5000:
                            pdf_text = pdf_text[:5000] + f"\n... [truncated, {len(pdf['text'])} chars total]"
                        parts.append(f"  PDF Content:\n{pdf_text}")

        elif role == 'assistant':
            parts.append(f"── Assistant Response #{i+1} {'─' * 36}")

            # Content
            if content:
                parts.append(content)

            # Thinking/reasoning
            if msg.get('thinking') and include_tool_details:
                thinking = msg['thinking']
                if len(thinking) > 3000:
                    thinking = thinking[:3000] + f"\n... [thinking truncated, {len(msg['thinking'])} chars total]"
                parts.append(f"\n  [Thinking]: {thinking}")

            # Tool rounds (searchRounds)
            search_rounds = msg.get('searchRounds', [])
            if search_rounds:
                parts.append(_format_tool_rounds(search_rounds, include_tool_details))

        parts.append("")  # blank line between messages

    # Trim trailing whitespace
    result = '\n'.join(parts).rstrip()

    # Safety: cap total length to avoid flooding context
    MAX_CHARS = 80000
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + f"\n\n... [output truncated at {MAX_CHARS} chars — conversation has more content]"

    return result


def _extract_text(content):
    """Extract text from a message content field (string or multimodal array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text':
                    texts.append(part.get('text', ''))
                elif part.get('type') == 'image_url':
                    texts.append('[image]')
            elif isinstance(part, str):
                texts.append(part)
        return '\n'.join(texts)
    return str(content) if content else ''


def _format_tool_rounds(rounds, include_details=True):
    """Format tool call rounds from searchRounds data."""
    if not rounds:
        return ""

    parts = ["\n  📦 Tool Calls:"]
    for j, rnd in enumerate(rounds):
        tool_name = rnd.get('toolName', rnd.get('tool_name', 'unknown'))
        status = rnd.get('status', 'done')

        # Build call signature
        call_desc = f"    {j+1}. {tool_name}"

        # Add key arguments based on tool type
        query = rnd.get('query', '')
        if query:
            call_desc += f"({_truncate(query, 120)})"

        call_desc += f"  [{status}]"
        parts.append(call_desc)

        if include_details:
            # Show arguments if present
            args = rnd.get('args', rnd.get('arguments', {}))
            if args and isinstance(args, dict):
                for key, val in args.items():
                    val_str = str(val)
                    if len(val_str) > 500:
                        val_str = val_str[:500] + '...'
                    parts.append(f"       {key}: {val_str}")

            # Show results
            results = rnd.get('results', rnd.get('result', []))
            if results:
                if isinstance(results, list):
                    for res in results:
                        res_text = _extract_result_text(res)
                        if res_text:
                            if len(res_text) > 3000:
                                res_text = res_text[:3000] + f'\n       ... [result truncated, {len(res_text)} chars total]'
                            parts.append(f"       → {res_text}")
                elif isinstance(results, str):
                    if len(results) > 3000:
                        results = results[:3000] + '\n       ... [result truncated]'
                    parts.append(f"       → {results}")

    return '\n'.join(parts)


def _extract_result_text(result):
    """Extract readable text from a tool result entry."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # Common patterns in searchRounds results
        if 'text' in result:
            return result['text']
        if 'content' in result:
            return result['content']
        if 'title' in result and 'snippet' in result:
            return f"{result['title']}: {result['snippet']}"
        if 'title' in result and 'url' in result:
            return f"{result['title']} — {result['url']}"
        # Fallback: compact JSON
        try:
            return json.dumps(result, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):
            logger.debug('JSON serialization failed for tool result, falling back to str()', exc_info=True)
            return str(result)[:2000]
    return str(result)[:2000] if result else ''


def _truncate(text, max_len=120):
    """Truncate text with ellipsis."""
    text = str(text).replace('\n', ' ').strip()
    if len(text) > max_len:
        return text[:max_len] + '...'
    return text


def execute_conv_ref_tool(fn_name, fn_args, current_conv_id=None):
    """Execute a conversation reference tool and return the result string.

    Args:
        fn_name: 'list_conversations' or 'get_conversation'
        fn_args: dict of arguments
        current_conv_id: the ID of the current conversation (to prevent self-reference)

    Returns:
        str: formatted result
    """
    try:
        if fn_name == 'list_conversations':
            keyword = fn_args.get('keyword', None)
            limit = fn_args.get('limit', 20)
            return list_conversations(keyword=keyword, limit=limit)

        elif fn_name == 'get_conversation':
            conv_id = fn_args.get('conversation_id', '')
            if not conv_id:
                return "Error: conversation_id is required."
            include_details = fn_args.get('include_tool_details', True)
            return get_conversation(
                conversation_id=conv_id,
                include_tool_details=include_details,
                current_conv_id=current_conv_id
            )

        else:
            return f"Error: Unknown conversation reference tool '{fn_name}'"

    except Exception as e:
        logger.warning("Error executing conv_ref tool %s: %s", fn_name, e, exc_info=True)
        return f"Error executing {fn_name}: {str(e)}"
