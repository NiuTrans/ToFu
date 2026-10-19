"""Source-level citation/reference audit for one frozen build snapshot.

The TeX log only flags undefined citations AFTER bibtex has run, and never
sees duplicate .bib keys or oversized unused pools. This audit reads the
same frozen sources the compiler consumed (bounded), cross-references
``\\cite``/``\\nocite`` keys with .bib entries and ``\\ref`` keys with
``\\label`` definitions, and emits contract diagnostics with file:line
positions. It is advisory: a scan failure never changes the build verdict,
and keys the engine log already reported are not repeated.
"""
from __future__ import annotations
from .contract import limit as limit

import re
from pathlib import Path

from lib.paper.bib import parse_entries

from .errors import BuildError
from .sources import read_source

_AUDIT_BUDGET_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 24

_CITE = re.compile(
    r'\\(?:cite|citep|citet|citealt|citealp|citeauthor|citeyear|citeyearpar'
    r'|parencite|textcite|autocite|footcite|smartcite|supercite|nocite)'
    r'\s*(?:\[[^\]]{0,200}\])*\s*\{([^}]{1,2000})\}')
_REF = re.compile(
    r'\\(?:ref|eqref|autoref|cref|Cref|vref|vpageref|pageref|nameref)'
    r'\s*(?:\[[^\]]{0,200}\])?\s*\{([^}]{1,512})\}')
_LABEL = re.compile(r'\\label\s*\{([^}]{1,512})\}')
_LOG_CITATION = re.compile(r"Citation `([^']+)'")
_LOG_REFERENCE = re.compile(r"Reference `([^']+)'")
_STRIP_COMMENTS = re.compile(r'(?<!\\)%[^\n]*')


def _keys(raw: str) -> list[str]:
    return [key.strip() for key in raw.split(',') if key.strip() and len(key.strip()) <= 256]


def _row(severity: str, code: str, message: str, file: str, line: int | None) -> dict:
    return {'severity': severity, 'code': code, 'message': message[:2000],
            'file': file[:512], 'line': line}


def audit_citations(snapshot_root: Path, files: tuple[dict, ...] | list[dict],
                    existing: list[dict] | None = None) -> list[dict]:
    """Audit ``\\cite``/``\\ref`` against .bib entries/``\\label`` in frozen sources.

    Returns at most ``_MAX_ROWS`` diagnostics. ``existing`` log diagnostics
    supply already-reported keys so the same problem is never told twice.
    """
    tex_paths = [row['path'] for row in files
                 if row['path'].lower().endswith(('.tex', '.ltx'))]
    bib_paths = [row['path'] for row in files if row['path'].lower().endswith('.bib')]
    if not tex_paths and not bib_paths:
        return []
    reported = set()
    for row in existing or []:
        for pattern in (_LOG_CITATION, _LOG_REFERENCE):
            match = pattern.search(row.get('message') or '')
            if match:
                reported.add(match.group(1))
    budget = {'bytes': 0}

    def read(path: str) -> str:
        if budget['bytes'] > _AUDIT_BUDGET_BYTES:
            raise BuildError('audit_limit', 'Citation audit byte budget reached')
        data = read_source(snapshot_root, snapshot_root / path,
                           limit=_AUDIT_BUDGET_BYTES - budget['bytes'] + 1)
        budget['bytes'] += len(data)
        return data.decode('utf-8', errors='replace')

    rows: list[dict] = []
    cites: dict[str, tuple[str, int]] = {}
    refs: dict[str, tuple[str, int]] = {}
    labels: set[str] = set()
    bib_keys: dict[str, tuple[str, int]] = {}
    duplicates: list[tuple[str, str, int]] = []

    for path in tex_paths:
        text = _STRIP_COMMENTS.sub('', read(path))
        for match in _CITE.finditer(text):
            for key in _keys(match.group(1)):
                cites.setdefault(key, (path, text.count('\n', 0, match.start()) + 1))
        for match in _REF.finditer(text):
            for key in _keys(match.group(1)):
                refs.setdefault(key, (path, text.count('\n', 0, match.start()) + 1))
        labels.update(_LABEL.findall(text))

    for path in bib_paths:
        entries, repeated = parse_entries(read(path))
        for entry in entries:
            bib_keys.setdefault(entry.key, (path, entry.line))
        for key in repeated:
            first = bib_keys.get(key, (path, 0))
            duplicates.append((key, first[0], first[1]))

    if not cites and not refs and not bib_paths:
        return []
    if bib_paths:
        for key, (path, line) in sorted(cites.items()):
            if key not in bib_keys and key != '*' and key not in reported:
                rows.append(_row('warning', 'undefined_citation',
                                 "Citation '%s' has no entry in any .bib file" % key,
                                 path, line))
        for key, path, line in duplicates[:8]:
            rows.append(_row('warning', 'duplicate_bib_key',
                             "BibTeX key '%s' is defined more than once" % key, path, line or None))
        unused = [] if '*' in cites else [key for key in bib_keys if key not in cites]
        if unused:
            rows.append(_row('warning', 'unused_bib_entries',
                             '%d of %d .bib entries are never cited' % (len(unused), len(bib_keys)),
                             '', None))
    elif cites:
        rows.append(_row('warning', 'missing_bibliography',
                         'Sources cite %d key(s) but no .bib file is part of the project'
                         % len(cites), '', None))
    for key, (path, line) in sorted(refs.items()):
        if key not in labels and key not in reported:
            rows.append(_row('warning', 'undefined_reference',
                             "Reference '%s' has no matching \\label" % key, path, line))
    return rows[:_MAX_ROWS]
