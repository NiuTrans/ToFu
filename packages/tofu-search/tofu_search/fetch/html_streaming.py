"""Restore received React/Fizz boundary completions before text extraction.

Entry: restore_completed_streams. Uses the existing Python BeautifulSoup parser
and literal completion instructions, never a JS engine, fetch, or shared cache.
Only the transport container is removed; destination/child visibility remains
the responsibility of html_extract. Unknown stream forms retain the input.
"""

import json
import re
from collections import Counter

from tofu_search.fetch.utils import _get_bs4

_FUNCTION = re.compile(r'\$(?:RC|RV)\s*=\s*function\s*\([\w$,\s]*\)\s*\{')
_BATCH = re.compile(r'\$RB\s*=\s*\[\s*\]\s*;')
_CALL = re.compile(r'\$RC\s*\(\s*')
_BOUNDARY_ID = re.compile(r'(.*)B:[0-9a-f]+', re.DOTALL)
_INERT = frozenset({'template', 'noscript', 'textarea', 'title', 'svg', 'math'})
_OPEN_COMMENTS = {'$': '/$', '$?': '/$', '$~': '/$', '$!': '/$', '&': '/&'}
_JS_TYPES = frozenset({'', 'text/javascript', 'application/javascript'})
_SPACE = re.compile(r'\s*')


def _function_end(source, start):
    """Skip one assigned runtime function, not surrounding executable code.

    Balanced braces and quoted/comment text are sufficient for the emitted
    Fizz helpers. Templates and regex literals are deliberately unsupported.
    Recognizing a declaration does not evaluate any of its statements.
    """
    depth, index = 1, start
    while index < len(source):
        char = source[index]
        if char in "\"'":
            quote = char
            index += 1
            while index < len(source) and source[index] != quote:
                if source[index] == '\\':
                    index += 1
                elif source[index] in '\r\n':
                    return None
                index += 1
            if index == len(source):
                return None
        elif char == '/':
            if source.startswith('//', index):
                end = source.find('\n', index + 2)
                if end == -1:
                    return None
                index = end
            elif source.startswith('/*', index):
                end = source.find('*/', index + 2)
                if end == -1:
                    return None
                index = end + 1
            else:
                return None
        elif char == '`':
            return None
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _completion_calls(source):
    """Accept only Fizz declarations followed by literal, direct $RC calls.

    Reject the whole script on unknown statements, expressions or arguments.
    Thus examples in strings, callbacks, conditions, JSON and comments cannot
    masquerade as an instruction. IDs are JSON strings, as emitted by React.
    """
    calls, index = [], 0
    decoder = json.JSONDecoder()
    while index < len(source):
        index = _SPACE.match(source, index).end()
        if index == len(source):
            break
        function = _FUNCTION.match(source, index) if not calls else None
        batch = _BATCH.match(source, index) if not calls else None
        if function:
            index = _function_end(source, function.end())
            if index is None:
                return []
            index = _SPACE.match(source, index).end()
            if not source.startswith(';', index):
                return []
            index += 1
            continue
        if batch:
            index = batch.end()
            continue
        match = _CALL.match(source, index)
        if not match:
            return []
        index = match.end()
        values = []
        for separator in (',', ')'):
            try:
                value, index = decoder.raw_decode(source, index)
            except ValueError:
                return []
            if not isinstance(value, str):
                return []
            values.append(value)
            index = _SPACE.match(source, index).end()
            if not source.startswith(separator, index):
                return []
            index += 1
            index = _SPACE.match(source, index).end()
        boundary, segment = values
        identifier = _BOUNDARY_ID.fullmatch(boundary)
        if not identifier or not re.fullmatch(
                re.escape(identifier[1]) + r'S:[0-9a-f]+', segment):
            return []
        calls.append((boundary, segment))
        if index < len(source):
            if source[index] != ';':
                return []
            index += 1
    return calls


def restore_completed_streams(html):
    """Return ordinary HTML unchanged; move only uniquely declared completions.

    Completion order follows inline scripts, not the order segments arrived.
    All work is local to the already byte-bounded response. A page with no
    recognized completion is returned byte-for-byte without serialization.
    """
    if '$RC' not in html or '<template' not in html or 'hidden' not in html:
        return html
    from bs4 import Comment, Tag

    soup = _get_bs4()(html, 'html.parser')
    positions, identifiers, instructions = {}, {}, []
    comment_stacks, comment_ends = {}, {}
    for position, node in enumerate(soup.descendants):
        if isinstance(node, Comment):
            positions[id(node)] = position
            # Pair same-parent markers once, including malformed streams. A
            # missing end must not make every pending boundary rescan the page.
            value = str(node)
            stack = comment_stacks.setdefault(id(node.parent), [])
            if value in _OPEN_COMMENTS:
                stack.append(node)
            elif value in {'/$', '/&'} and stack:
                start = stack.pop()
                if _OPEN_COMMENTS[str(start)] == value:
                    comment_ends[id(start)] = node
                else:
                    stack.clear()
        if not isinstance(node, Tag):
            continue
        positions[id(node)] = position
        identifier = node.get('id')
        if isinstance(identifier, str):
            identifiers[identifier] = None if identifier in identifiers else node
        if (node.name == 'script' and not node.has_attr('src')
                and str(node.get('type', '')).strip().lower() in _JS_TYPES
                and not any(p.name in _INERT for p in node.parents)):
            source = node.string or ''
            if '$RC' in source:
                instructions.extend((boundary, segment, node)
                                    for boundary, segment in _completion_calls(source))
    boundary_counts = Counter(b for b, _, _ in instructions)
    segment_counts = Counter(s for _, s, _ in instructions)
    changed = False
    for boundary_id, segment_id, script in instructions:
        if boundary_counts[boundary_id] != 1 or segment_counts[segment_id] != 1:
            continue
        template, segment = identifiers.get(boundary_id), identifiers.get(segment_id)
        if (template is None or segment is None or template.name != 'template'
                or template.contents or template.parent is None
                or segment.name != 'div' or not segment.has_attr('hidden')
                or set(segment.attrs) - {'hidden', 'id'} or segment.parent is None):
            continue
        # Neither future markup nor inert template contents are live DOM IDs.
        if (max(positions[id(template)], positions[id(segment)]) >= positions[id(script)]
                or any(p.name in _INERT for p in (*template.parents, *segment.parents))
                or any(p is segment or p is template for p in script.parents)
                or any(p is template for p in segment.parents)):
            continue
        start = template.previous_sibling
        if not isinstance(start, Comment) or str(start) not in {'$?', '$~'}:
            continue
        end = comment_ends.get(id(start))
        if (end is None or end.parent is not template.parent
                or positions[id(end)] >= positions[id(script)]
                or any(p is segment for p in end.parents)):
            continue
        # A segment cannot be one of the siblings about to be discarded.
        discarded, node = [], template
        while node is not end:
            discarded.append(node)
            node = node.next_sibling
        segment_ancestors = {id(p) for p in segment.parents}
        if any(n is segment or id(n) in segment_ancestors for n in discarded):
            continue
        segment.extract()
        for node in discarded:
            node.extract()
        for child in list(segment.contents):
            end.insert_before(child.extract())
        start.replace_with(Comment('$'))
        changed = True
    return str(soup) if changed else html
