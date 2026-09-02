"""Empty-conv purge must spare conversations carrying a project mount.

Incident (2026-08-21, owner report: "switching conversations loses the
project bar's per-conversation state"): attach a project to a BRAND-NEW
conversation, switch away before creating any Turn, and the conversation
— together with its project bar state — silently vanished, forcing the
user to re-attach via the project modal on every new chat. The local settings
draft existed, but the catalog sweeper deleted its shell before the first Turn.

Root cause: the local empty-draft sweeper had no project-state exception:

  * ``_purgeEmptyConvs()`` — runs at the top of every ``loadConversation``
    switch and ``newChat``. Its keep-predicate (Turns / active /
    server-known-count / needs-load) had no clause for per-conv STATE: a
    zero-Turn conversation is "empty" even when the user deliberately attached a
    project. The attachment exists only in the local draft until the first
    authoritative Turn, so the purge was unrecoverable.

Fix: a conv with ``projectPath`` is never "empty" in either sweeper.
Pinned here by driving the REAL extracted ``_purgeEmptyConvs`` under node,
plus a poisoned-NEUTER control that removes the clause and proves the positive
assertions are load-bearing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
RUNTIME = runtime_section_path('main.js')

# The keep-clause added by the fix (exact source text, reused by the poison).
_KEEP_CLAUSE = '\n      || !!c.projectPath;'
def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


_PRELUDE = r'''
var conversations = [];
var activeConvId = null;
var runtimeScope = {
  ConversationTurnRead: {
    ordered: function (conversation) {
      return Array.from({ length: conversation.turnCount || 0 }, function (_, i) {
        return { turnId: conversation.id + '-turn-' + i };
      });
    },
  },
};
// The real fn console.warns on purge; keep stdout JSON-only.
console.warn = function () {};
function mk(id, extra) {
  var c = { id: id, title: id };
  for (var k in extra) c[k] = extra[k];
  return c;
}
'''

_DRIVER = r'''
var convMounted  = mk('convMounted',  { projectPath: '/repo/chatui',
                                        projectPaths: ['/repo/chatui'] });
var convEmpty    = mk('convEmpty',    {});
var convTurned   = mk('convTurned',   { turnCount: 1 });
var convActive   = mk('convActive',   {});
var convServer   = mk('convServer',   { _serverTurnCount: 3 });
conversations = [convMounted, convEmpty, convTurned, convActive, convServer];
activeConvId = 'convActive';
_purgeEmptyConvs();
process.stdout.write(JSON.stringify({
  survivors: conversations.map(function (c) { return c.id; }),
}));
'''


def _run(*, poison: bool = False) -> dict:
    """Eval the REAL ``_purgeEmptyConvs`` under node. ``poison`` removes the
    projectPath keep-clause, resurrecting the reported bug."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    src = _read(RUNTIME)
    fn = _extract_fn(src, '_purgeEmptyConvs')
    if poison:
        assert _KEEP_CLAUSE in fn, 'poison did not apply (clause missing)'
        fn = fn.replace(_KEEP_CLAUSE, '')
    harness = _PRELUDE + fn + _DRIVER
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True,
                             timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return json.loads(out.stdout)
    finally:
        os.unlink(tmp)


def test_purge_spares_project_mounted_empty_conv():
    """The reported bug: a 0-message conv with a project attached must
    survive the switch-time purge; a truly-empty non-active conv is still
    purged (the sweeper's legitimate job is unchanged)."""
    survivors = _run()['survivors']
    assert 'convMounted' in survivors, (
        'project-mounted empty conv purged — the reported state loss')
    assert 'convTurned' in survivors
    assert 'convActive' in survivors
    assert 'convServer' in survivors
    assert 'convEmpty' not in survivors, (
        'truly-empty non-active conv must still be purged')


def test_NEUTER_without_clause_mounted_conv_is_purged():
    """POISONED-NEUTER: strip the projectPath clause → the mounted conv is
    purged again. Proves the positive test exercises the real clause."""
    survivors = _run(poison=True)['survivors']
    assert 'convMounted' not in survivors, 'poison did not resurrect the bug'
    assert 'convTurned' in survivors
    assert 'convEmpty' not in survivors


def test_source_pinned_invariants():
    """Ratchet: the sweeper reads Turn state and keeps project drafts."""
    src = _read(RUNTIME)
    purge = _extract_fn(src, '_purgeEmptyConvs')
    assert _KEEP_CLAUSE in purge, 'purge keep-predicate lost the mount clause'
    assert 'ConversationTurnRead?.ordered?.(c)' in purge
    assert 'c.messages' not in purge


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
