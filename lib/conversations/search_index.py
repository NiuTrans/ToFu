"""Pure text projection used by the turn-native conversation search index."""

import json

from lib.log import get_logger

logger = get_logger(__name__)
def build_search_text(messages):
    """Extract plain text from messages list for full-text search indexing.

    Concatenates all user/assistant content and thinking fields into a single
    string, separated by newlines.  Tool calls, metadata, and JSON structure
    are stripped — only human-readable text is kept.

    Args:
        messages: List of message dicts (or raw JSON string / None).

    Returns:
        Flattened plain-text string suitable for full-text search.
    """
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Conversations] Failed to parse messages JSON: %s', e)
            return ''
    if not isinstance(messages, list):
        return ''
    parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role', '')
        if role not in ('user', 'assistant'):
            continue
        content = msg.get('content', '')
        if isinstance(content, list):
            # Multi-part content (text + images)
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get('text', ''))
        elif isinstance(content, str) and content:
            parts.append(content)
        thinking = msg.get('thinking', '')
        if isinstance(thinking, str) and thinking:
            parts.append(thinking)
        # Translated content (from translate feature) — must be indexed so
        # users can search in the translated language (e.g. Chinese translation
        # of an English assistant reply).
        translated = msg.get('translatedContent', '')
        if isinstance(translated, str) and translated:
            parts.append(translated)
        # Original pre-translation text (auto-translate-user feature): when a
        # user message is auto-translated to English, `content` holds the
        # translation and the text the user actually typed lives in
        # `originalContent`. Index it too, or the user can't find their own
        # message by the words they wrote (the mirror of translatedContent).
        original = msg.get('originalContent', '')
        if isinstance(original, str) and original:
            parts.append(original)
    return '\n'.join(parts)
__all__ = ['build_search_text']
