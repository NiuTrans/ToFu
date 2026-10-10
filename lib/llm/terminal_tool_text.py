"""Detect provider terminal tool text without CLI or executor dependencies.

Terminal recovery consumes the classification and owns its retry budget. Text
never authorizes a tool call or replay; native calls and ordinary quoted prose
remain unchanged. Entry point: classify_longcat_tool_text.
"""
from __future__ import annotations

import re

_LONGCAT_PACKET_START = re.compile(r'^<longcat_(?:tool_call|arg_key)(?:\s|>|$)')


def classify_longcat_tool_text(
    content: object, *, profile: str = 'auto', model: str = '',
    has_native_tool_calls: bool = False,
) -> str:
    """Classify packet-looking terminal text for bounded native reissue.

    Only leading, unquoted protocol markers qualify. XML in prose, quotes or
    code fences is ordinary content. Truncated/ambiguous packets are invalid,
    never repaired here. Native calls take precedence: mixed text must not
    cause an already-dispatched side effect to be reissued. This function has
    no executor, parser, permission bypass, or independent recovery counter.
    """
    model_family = re.sub(r'[^a-z0-9]', '', str(model).lower())
    enabled = profile == 'longcat_flash' or (
        profile == 'auto' and 'longcat' in model_family)
    if not enabled or has_native_tool_calls:
        return ''
    if not isinstance(content, str):
        return ''
    if _LONGCAT_PACKET_START.match(content.lstrip()):
        return 'invalid_protocol'
    # Observed r12 truncation: prose followed by a raw argument pair and a
    # closing call marker. Only a terminal suffix outside quoting/fences is
    # evidence; arbitrary mentions of these tags remain ordinary prose.
    suffix = re.search(
        r'<longcat_arg_key>[^<>]+</longcat_arg_key>\s*'
        r'<longcat_arg_value>[\s\S]+</longcat_arg_value>\s*'
        r'</longcat_tool_call>\s*$', content)
    if suffix:
        prefix = content[:suffix.start()]
        line_prefix = prefix.rsplit('\n', 1)[-1]
        # Quoting is checked before the packet, not inside its argument value.
        outside_fence = prefix.count('```') % 2 == 0 and prefix.count('~~~') % 2 == 0
        outside_inline = line_prefix.count('`') % 2 == 0
        if (outside_fence and outside_inline
                and not line_prefix.lstrip().startswith(('>', '\"', "'"))):
            return 'invalid_protocol'
    return ''
