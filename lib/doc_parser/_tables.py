"""Shared, format-neutral table normalization for document extraction.

Office, OpenDocument and delimited-text readers all eventually produce rows.
Keeping header inference here prevents each extractor from blindly assuming
that physical row one is the header -- a poor assumption for reports with a
title, notes, merged cells, or several independent table blocks.
"""

from __future__ import annotations

from datetime import date, datetime, time
import math
import re
from typing import Iterable, Sequence


_TEXT_RE = re.compile(r'[A-Za-z\u3400-\u9fff]')


def cell_text(value: object) -> str:
    """Return one stable, Markdown-safe cell value."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, datetime):
        return value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    return re.sub(r'\s+', ' ', str(value)).strip().replace('|', '\\|')


def _trim_rows(rows: Iterable[Sequence[object]]) -> list[list[str]]:
    cleaned: list[list[str]] = []
    for row in rows:
        values = [cell_text(value) for value in row]
        while values and not values[-1]:
            values.pop()
        if any(values):
            cleaned.append(values)
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    return [row + [''] * (width - len(row)) for row in cleaned]


def infer_header_row(rows: Sequence[Sequence[object]]) -> int:
    """Infer a likely header among the first rows of a ragged table.

    The heuristic is deliberately format-neutral.  It rewards dense, unique,
    short textual labels followed by a similarly shaped row, and rejects the
    common single-cell report title.  If evidence is weak, physical row one is
    retained so headerless data is never dropped.
    """
    cleaned = _trim_rows(rows)
    if not cleaned:
        return 0
    width = max(len(row) for row in cleaned)
    best_index = 0
    best_score = float('-inf')
    # Reports often carry a sizeable cover/preamble above the real table.
    # Fifty rows is still bounded while avoiding the old "header must be near
    # row one" assumption for exported HR/finance workbooks.
    for index, row in enumerate(cleaned[:min(50, len(cleaned))]):
        values = [value for value in row if value]
        populated = len(values)
        if not populated:
            continue
        density = populated / max(1, width)
        textual = sum(bool(_TEXT_RE.search(value)) for value in values)
        text_ratio = textual / populated
        unique_ratio = len({value.casefold() for value in values}) / populated
        short_ratio = sum(len(value) <= 48 for value in values) / populated
        next_density = 0.0
        if index + 1 < len(cleaned):
            next_density = sum(bool(value) for value in cleaned[index + 1]) / max(1, width)
        following_row_support = (
            0.4
            if (index + 1 < len(cleaned)
                and next_density >= max(0.5, density * 0.75))
            else 0.0
        )
        score = (
            density * 2.2 + text_ratio * 2.0 + unique_ratio * 0.7
            + short_ratio * 0.5 + min(density, next_density) * 1.4
            + following_row_support
            - index * 0.06
        )
        if populated == 1 and width > 1:
            score -= 3.5
        if sum(len(value) for value in values) > 180:
            score -= 1.0
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _unique_headers(values: Sequence[str], width: int) -> list[str]:
    out: list[str] = []
    counts: dict[str, int] = {}
    for index in range(width):
        base = (values[index] if index < len(values) else '') or f'Column {index + 1}'
        key = base.casefold()
        counts[key] = counts.get(key, 0) + 1
        out.append(base if counts[key] == 1 else f'{base} ({counts[key]})')
    return out


def render_markdown_table(
    rows: Iterable[Sequence[object]],
    *,
    context_label: str = 'Table context',
) -> str:
    """Render ragged rows with an inferred, repeatable Markdown header."""
    cleaned = _trim_rows(rows)
    if not cleaned:
        return ''
    width = max(len(row) for row in cleaned)
    header_index = infer_header_row(cleaned)
    headers = _unique_headers(cleaned[header_index], width)
    parts: list[str] = []
    for row in cleaned[:header_index]:
        values = [value for value in row if value]
        if values:
            parts.append(f'{context_label}: ' + ' · '.join(values))
    parts.append('| ' + ' | '.join(headers) + ' |')
    parts.append('| ' + ' | '.join(['---'] * width) + ' |')
    for row in cleaned[header_index + 1:]:
        parts.append('| ' + ' | '.join(row[:width]) + ' |')
    return '\n'.join(parts)


__all__ = ['cell_text', 'infer_header_row', 'render_markdown_table']
