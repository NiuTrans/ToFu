"""Source-bound citation audit pages without a second task or evidence store.

audit_document uses the citation owner's parser/network boundaries and safe
source reader. Cursors select a page in one exact document snapshot; they never
certify earlier pages. A second snapshot read refuses edits during verification.
The caller retains ordinary tool receipts and follows next_cursor/retry_cursor.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import citations
from .bib import parse_entries
from .builds.errors import BuildError

_CURSOR = re.compile(r'c1:([0-9a-f]{64}):(0|[1-9][0-9]{0,9})\Z')


def _snapshot(base, main_file):
    paths = citations._walk_sources(base, ('.bib', '.tex', '.ltx'), require_complete=True)
    sources = {}
    identities = []
    size = 0
    for path in sorted(paths):
        cap = citations.MAX_BIB_BYTES if path.lower().endswith('.bib') else citations._MAX_TEX_BYTES
        try:
            data = citations._read_source_bytes(base, path, cap)
        except OSError as exc:
            raise BuildError('source_path', 'Cannot read citation source %s' % path) from exc
        size += len(data)
        if size > citations._MAX_SOURCE_BYTES:
            raise BuildError('source_limit', 'Citation sources exceed the total byte budget')
        sources[path] = data.decode('utf-8', errors='replace')
        identities.append([path, hashlib.sha256(data).hexdigest()])
    identity = [1, os.path.realpath(base), main_file, identities]
    source_digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return sources, source_digest


def audit_document(base, *, main_file='', max_entries=citations.VERIFY_MAX_ENTRIES, cursor=''):
    """Verify one bounded page and return explicit local/snapshot coverage."""
    if type(max_entries) is not int or not 1 <= max_entries <= citations.VERIFY_MAX_ENTRIES:
        raise BuildError('citation_query', 'Invalid citation verification page size')
    if not isinstance(cursor, str) or (cursor and (len(cursor) > 80 or not _CURSOR.fullmatch(cursor))):
        raise BuildError('citation_cursor', 'Invalid citation continuation cursor')
    sources, source_digest = _snapshot(base, main_file)
    start = 0
    if cursor:
        match = _CURSOR.fullmatch(cursor)
        if match.group(1) != source_digest:
            raise BuildError('citation_source_changed', 'Citation sources changed; restart the audit without a cursor')
        start = int(match.group(2))
    bib_files = [path for path in sources if path.lower().endswith('.bib')]
    tex_files = [path for path in sources if path.lower().endswith(('.tex', '.ltx'))]
    entries = []
    duplicates = set()
    bib_keys = set()
    for path in bib_files:
        heads, repeated = parse_entries(sources[path], max_entries=citations.MAX_BIB_ENTRIES + 1)
        if len(heads) > citations.MAX_BIB_ENTRIES:
            raise BuildError('bib_limit', 'Bibliography %s exceeds the entry budget' % path)
        for entry in heads:
            if entry.key in bib_keys:
                duplicates.add(entry.key)
            bib_keys.add(entry.key)
        duplicates.update(repeated)
        entries.extend(citations.collect_entries(sources[path], source_file=path))
    if start and start >= len(entries):
        raise BuildError('citation_cursor', 'Citation cursor is past the bibliography')
    cites = set()
    for path in tex_files:
        text = citations._STRIP_COMMENTS.sub('', sources[path])
        for match in citations._CITE.finditer(text):
            cites.update(key.strip() for key in match.group(1).split(',') if 0 < len(key.strip()) <= 256)
    batch = entries[start:start + max_entries]
    rows = citations.verify_entries(batch, max_entries=max_entries)
    # Budget-skipped suffix entries were never attempted. Resume at the first
    # such row; backend failures were attempted and retain an explicit retry.
    attempted = next((n for n, row in enumerate(rows) if row.get('reason') in ('deadline', 'entry_limit')), len(rows))
    rows = [dict(row, source_file=entry['source_file'], line=entry['line'])
            for entry, row in zip(batch[:attempted], rows[:attempted])]
    end = start + attempted
    current_cursor = f'c1:{source_digest}:{start}'
    skipped = any(row['status'] == 'skipped' for row in rows)
    bibliography = citations._bibliography_status_from_sources(
        ((path, sources[path]) for path in tex_files), bib_files[0] if bib_files else '')
    if _snapshot(base, main_file)[1] != source_digest:
        raise BuildError('citation_source_changed', 'Citation sources changed during verification; restart the audit')
    return {
        'bib_files': bib_files, 'tex_files': tex_files, 'entries_total': len(entries),
        'verifications': rows, 'duplicates': sorted(duplicates),
        'undefined': sorted(cites - bib_keys - {'*'}),
        'unused': [] if '*' in cites else sorted(bib_keys - cites),
        'bibliography': bibliography, 'source_digest': source_digest,
        'cursor': current_cursor,
        'next_cursor': f'c1:{source_digest}:{end}' if end < len(entries) else None,
        'retry_cursor': current_cursor if skipped else None,
        'coverage': {'start': start, 'end': end, 'remaining': len(entries) - end,
                     'complete': start == 0 and end == len(entries) and not skipped},
    }
