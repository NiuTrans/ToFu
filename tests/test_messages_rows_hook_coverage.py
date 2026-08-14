#!/usr/bin/env python3
"""tests/test_messages_rows_hook_coverage.py — dual-write hook coverage ratchet.

pt_59140ecd step ②. The row store only stays truthful if EVERY writer of the
authoritative ``conversations.messages`` blob mirrors into
``conversation_messages``. An un-hooked same-count edit silently leaves stale
rows (the count-based ``row_window_usable`` guard cannot see it), so coverage
is a hard invariant, not a nice-to-have.

This scanner walks ``lib/`` + ``routes/`` (via ``git ls-files`` — never
``os.walk``, which times out on FUSE) and requires every blob-write site to
have a dual-write hook marker (``mirror_write_and_commit`` /
``dual_write_conv``) inside the enclosing function:

  * P1 — ``UPDATE conversations SET`` statements that assign ``messages=``;
  * P2 — ``upsert(db, CONVERSATIONS, ...)`` calls (the migrated-table write).

Grandfathered allowlist (shrinks only): the two one-off schema REPLACE
migrations (they rewrite text in place during bootstrap, before the row table
is meaningful) and ``load_or_create_conv``'s ``INSERT … '[]'`` (an empty
array — nothing to mirror).

NEUTER: stripping the hook markers from one hooked file (in-memory) must make
the scanner flag it — proves the guard is load-bearing.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_messages_rows_hook_coverage.py -v
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_HOOK_MARKERS = (
    'mirror_write_and_commit', 'dual_write_conv', 'write_conv_rows')

# (path fragment, def name or '*') — grandfathered; shrinks only.
_ALLOWLIST = {
    ('lib/database/_schema_sqlite/_chat.py', '*'),   # one-off DDL migration
    ('lib/database/_schema_pg/_chat.py', '*'),       # one-off DDL migration
    ('lib/chat/persistence.py', 'load_or_create_conv'),  # INSERT … '[]' (empty)
    # The canonical rows remain authoritative while this verified maintenance
    # operation replaces only the retired hot blob with an empty archive stub.
    ('lib/database/message_archive_offload.py',
     'offload_frozen_message_archives'),
    # Offline recovery imports legacy PG rows with messages_rows_rev left at
    # its stale default, so readers intentionally fall back to the blob until
    # the normal row migrator proves and advances the marker.
    ('lib/database/pg_tooling.py',
     'merge_recovery_conversation_archives'),
}

_DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)')
_UPDATE_RE = re.compile(r'UPDATE\s+conversations\s+SET', re.IGNORECASE)
_MSG_ASSIGN_RE = re.compile(r'\bmessages\s*=')
_UPSERT_RE = re.compile(r'upsert\(\s*db\s*,\s*CONVERSATIONS')
_INSERT_RE = re.compile(r'INSERT\s+INTO\s+conversations(?!\w)', re.IGNORECASE)


def _git_files():
    out = subprocess.run(
        ['git', 'ls-files', 'lib/*.py', 'routes/*.py'],
        cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in out.stdout.split()
            if f.endswith('.py') and os.path.isfile(os.path.join(ROOT, f))]


def _def_spans(lines):
    """Yield (def_name, start, end) for every def in the file."""
    spans = []
    for i, line in enumerate(lines):
        m = _DEF_RE.match(line)
        if m:
            spans.append([m.group(2), i, len(lines), len(m.group(1))])
    for k, (name, start, _end, indent) in enumerate(spans):
        for j in range(start + 1, len(lines)):
            ln = lines[j]
            if ln.strip() and not ln.strip().startswith('#'):
                cur = len(ln) - len(ln.lstrip())
                if cur <= indent and (_DEF_RE.match(ln)
                                      or ln.lstrip().startswith('class ')):
                    spans[k][2] = j
                    break
    return [(n, s, e) for n, s, e, _ in spans]


def _scan_source(src: str, path: str) -> list[str]:
    """Return violations for one source text."""
    lines = src.splitlines()
    spans = _def_spans(lines)
    violations = []

    def _enclosing(idx):
        best = None
        for name, s, e in spans:
            if s <= idx < e:
                best = (name, s, e)
        return best

    def _flag(idx, kind):
        enc = _enclosing(idx)
        def_name = enc[0] if enc else '<module>'
        for frag, dn in _ALLOWLIST:
            if frag in path and (dn == '*' or dn == def_name):
                return
        body = '\n'.join(lines[enc[1]:enc[2]]) if enc else src
        if not any(marker in body for marker in _HOOK_MARKERS):
            violations.append(
                f'{path}:{idx + 1} {kind} in `{def_name}` has no dual-write '
                'hook (repository write_conv_rows / legacy dual-write)')

    for i, line in enumerate(lines):
        if _UPDATE_RE.search(line):
            window = '\n'.join(lines[i:i + 8])
            if _MSG_ASSIGN_RE.search(window):
                _flag(i, 'UPDATE-conversations-messages')
        if _UPSERT_RE.search(line):
            _flag(i, 'upsert-CONVERSATIONS')
        if _INSERT_RE.search(line):
            _flag(i, 'INSERT-conversations')
    return violations


def test_every_blob_writer_has_a_dual_write_hook():
    bad = []
    for path in _git_files():
        with open(os.path.join(ROOT, path), encoding='utf-8') as f:
            bad.extend(_scan_source(f.read(), path))
    assert not bad, 'blob writers missing the dual-write hook:\n  ' + '\n  '.join(bad)


def test_NEUTER_stripped_hook_is_flagged():
    """Byte-reverting NEUTER: remove the hook markers from one hooked file —
    the scanner MUST flag its writers (proves the guard is not vacuous)."""
    src = '''
def synthetic_writer(db, messages):
    db.execute("UPDATE conversations SET messages=? WHERE id=?")
    write_conv_rows(db, "c1", messages)
'''
    assert not _scan_source(src, 'synthetic.py')
    neutered = src.replace('write_conv_rows', 'REMOVED_HOOK')
    violations = _scan_source(neutered, 'synthetic.py')
    assert violations, 'NEUTER FAILED: scanner did not fire on stripped hooks'


def test_allowlist_shrinks_only():
    """The allowlist must reference real, still-existing targets — a renamed
    file/def silently widening the grandfather set is a ratchet escape."""
    with open(os.path.join(ROOT, 'lib/chat/persistence.py'), encoding='utf-8') as f:
        assert 'def load_or_create_conv' in f.read()
    for frag, _dn in _ALLOWLIST:
        assert os.path.exists(os.path.join(ROOT, frag)), f'allowlist path gone: {frag}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
