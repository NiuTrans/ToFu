"""Structure-aware document chunking for the local knowledge base."""

from __future__ import annotations

import re

_TARGET_CHARS = 1800
_MAX_CHARS = 2600
_OVERLAP_CHARS = 240
_HEADING_RE = re.compile(r'^\s{0,3}(#{1,6})\s+(.+?)\s*$')
_NUMBERED_HEADING_RE = re.compile(
    r'^(?:(?:[一二三四五六七八九十百]+|\d+(?:\.\d+)*)[、.．]|【[^】]+】)')
_SHEET_RE = re.compile(r'^\s*##\s+(?:Sheet|Worksheet)\s*:\s*(.+?)\s*$', re.I)
_TABLE_SEPARATOR_RE = re.compile(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$')


def _clean(text: str) -> str:
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\x00', '')
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    return text.strip()


def _sections(text: str) -> list[tuple[str, list[tuple[int, str]]]]:
    """Split on headings while retaining source line numbers."""
    out: list[tuple[str, list[tuple[int, str]]]] = []
    title = ''
    buf: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        match = _HEADING_RE.match(line)
        heading = match.group(2).strip() if match else _rich_heading(line)
        if heading and buf:
            out.append((title, buf))
            buf = []
        if heading:
            title = re.sub(r'\*{1,2}', '', heading).strip()
        buf.append((line_no, line))
    if buf:
        out.append((title, buf))
    return out


def _rich_heading(line: str) -> str:
    """Recognize standalone headings emitted as bold text by PDF parsers.

    ``pymupdf4llm`` commonly preserves a PDF heading as ``**三、 Zoom**``
    instead of introducing a Markdown ``#``.  Treating those lines as normal
    prose made an entire handbook one giant section and detached a match from
    the item it described.  Keep this deliberately conservative: only short,
    fully-bold numbered/bracket headings or bold labels with a dash qualify.
    """
    stripped = (line or '').strip()
    if (not stripped.startswith('**') or len(stripped) > 220
            or stripped.count('**') < 2):
        return ''
    candidate = re.sub(r'\*{2}', '', stripped).strip()
    if not candidate or '返回主页面' in candidate:
        return ''
    if _NUMBERED_HEADING_RE.match(candidate):
        return candidate
    if (' — ' in candidate or ' – ' in candidate) and len(candidate) <= 140:
        return candidate
    return ''


def _split_long_line(line: str) -> list[str]:
    if len(line) <= _MAX_CHARS:
        return [line]
    pieces = re.split(r'(?<=[。！？.!?；;])\s*', line)
    out: list[str] = []
    current = ''
    for piece in pieces:
        if not piece:
            continue
        if current and len(current) + len(piece) > _MAX_CHARS:
            out.append(current)
            current = current[-_OVERLAP_CHARS:] + piece
        else:
            current += piece
    if current:
        out.append(current)
    return out


def chunk_document(text: str) -> list[dict]:
    """Return ordered, overlapping chunks with section/location metadata.

    Markdown headings (including generated ``Sheet:`` headings) are hard
    boundaries. Table rows stay line-aligned, and a table header is repeated
    when a large table continues in the next chunk so retrieved rows retain
    their column meaning.
    """
    text = _clean(text)
    if not text:
        return []

    chunks: list[dict] = []
    ordinal = 0
    for section, numbered_lines in _sections(text):
        expanded: list[tuple[int, str]] = []
        for line_no, line in numbered_lines:
            expanded.extend((line_no, p) for p in _split_long_line(line))

        start = 0
        table_header: list[str] = []
        while start < len(expanded):
            end = start
            size = 0
            lines: list[str] = []
            while end < len(expanded):
                line = expanded[end][1]
                added = len(line) + 1
                if lines and size + added > _TARGET_CHARS:
                    break
                lines.append(line)
                size += added
                end += 1
                if size >= _MAX_CHARS:
                    break

            # A tiny trailing fragment is more useful merged with its parent.
            if end < len(expanded) and len(expanded) - end <= 2 and size < _MAX_CHARS:
                for _line_no, line in expanded[end:]:
                    if size + len(line) + 1 > _MAX_CHARS:
                        break
                    lines.append(line)
                    size += len(line) + 1
                    end += 1

            # Remember Markdown table headers and carry them into continuation
            # chunks. Generated spreadsheet text is therefore self-describing
            # even when a match lands thousands of rows below the header.
            for i in range(max(0, len(lines) - 1)):
                if lines[i].lstrip().startswith('|') and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
                    table_header = [lines[i], lines[i + 1]]
            if start > 0 and table_header and lines and lines[0].lstrip().startswith('|'):
                if lines[:2] != table_header:
                    lines = table_header + lines

            content = '\n'.join(lines).strip()
            if content:
                first_line = expanded[start][0]
                last_line = expanded[max(start, end - 1)][0]
                sheet_match = _SHEET_RE.match(numbered_lines[0][1]) if numbered_lines else None
                chunks.append({
                    'ordinal': ordinal,
                    'section': section,
                    'location': (
                        f'sheet {sheet_match.group(1)}, lines {first_line}-{last_line}'
                        if sheet_match else f'lines {first_line}-{last_line}'
                    ),
                    'content': content,
                })
                ordinal += 1

            if end >= len(expanded):
                break
            overlap = 0
            next_start = end
            while next_start > start + 1 and overlap < _OVERLAP_CHARS:
                next_start -= 1
                overlap += len(expanded[next_start][1]) + 1
            start = max(start + 1, next_start)
    return chunks
