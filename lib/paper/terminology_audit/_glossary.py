"""Glossary-table parsing for the terminology audit.

Holds the markdown-table row regex (``_ROW_RE``) and the separator-cell regex
(``_SEP_CELL_RE``), plus ``_parse_glossary`` which turns a Core-Terminology
table body into ``{term: definition}``. Stateless (regex consts + pure fn).
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

# A glossary table row: ``| Term | Definition | Why it matters |``. The first
# cell is the term, the second the definition. Header/separator rows are skipped
# by requiring the term cell to be a plausible term (not "Term"/"术语"/dashes).
_ROW_RE = re.compile(r'^\|([^|\n]+)\|([^|\n]+)\|(.*)$', re.MULTILINE)
_SEP_CELL_RE = re.compile(r'^[\s:\-]+$')


def _parse_glossary(glossary_body: str) -> dict[str, str]:
    """Parse the glossary table → ``{term: definition}``.

    The term cell is normalised (stripped of markdown bold/emphasis). Header and
    separator rows are dropped. The returned keys are the exact term strings; a
    caller matches usage against these case-insensitively.
    """
    rows: dict[str, str] = {}
    for m in _ROW_RE.finditer(glossary_body):
        term_cell = m.group(1).strip()
        def_cell = m.group(2).strip()
        if not term_cell or _SEP_CELL_RE.match(term_cell):
            continue
        term = re.sub(r'[*_`]', '', term_cell).strip()
        if not term or term.lower() in ('term', '术语', 'field', '字段'):
            continue
        rows[term] = def_cell
    return rows
