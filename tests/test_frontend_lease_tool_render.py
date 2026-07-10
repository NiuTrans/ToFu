"""tests/test_frontend_lease_tool_render.py — path-lease tools render as first-class conv-meta cards.

Regression pin for the reported bug: ``project_claim_path`` / ``project_release_path``
(the Project-Brain path-LEASE pair) were added to the backend (routed through
``_handle_board_tool``, in ``BOARD_TOOL_NAMES``) but never registered in the
frontend tool-round renderer — absent from ``_CONV_META_TOOLS``, ``_TOOL_DISPLAY``,
``_convMetaHeadLabel`` / ``_convMetaPurpose``, and not matching the
``project_board_`` icon prefix. So they fell through to the GENERIC grey chip
labelled "Project claim path" / "Project release path" with no card body — the
"renders so strangely, no idea what it's for" symptom.

This pins the fix:
  • both tools are conv-meta tools (``_isRoundConvMeta`` true);
  • each gets a FRIENDLY localized header (not the raw ``project_claim_path``
    display string) + a "why this ran" purpose caption;
  • the plain outcome string renders as the Markdown body (leases carry no
    epic ``boardTransition``, by design — see the backend half below).

Front half loads the REAL shipped ``ui/tool_rounds.js`` under jsdom and calls
the REAL ``_renderUnifiedToolLine`` (the same entry the transcript uses). The
NEUTER byte-reverts the ``_CONV_META_TOOLS`` membership and asserts the checks
flip to FAIL (the tools drop back to the generic chip) — then restores the file
byte-identical.

Back half asserts the backend contract the frontend depends on: the lease
tools produce a ``hold`` / ``release`` badge (NOT the ugly full name) and
attach NO ``boardTransition`` meta (which would otherwise suppress the accurate
outcome prose in ``_structuredConvMetaBody``).

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_TR_SRC = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
// renderMarkdown is the body path for a lease card (no structured meta) — mark
// its output so we can assert the outcome prose is actually rendered.
global.renderMarkdown = (s) => 'MD-DUMP:' + String(s);
global.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Both lease tools are conv-meta tools (route to the structured card).
check('claim_is_conv_meta', _isRoundConvMeta({ toolName: 'project_claim_path' }));
check('release_is_conv_meta', _isRoundConvMeta({ toolName: 'project_release_path' }));

// ── project_claim_path → friendly header + purpose caption + outcome body ──
const claimRound = {
  status: 'done', toolName: 'project_claim_path', query: 'project_claim_path',
  toolContent: 'Path(s) held. Siblings now see a "Held — do NOT edit" notice.',
  toolRounds: [],
  results: [{ source: 'Board', snippet: 'held' }],
};
const cHtml = _renderUnifiedToolLine(claimRound, false);
// friendly header, NOT the raw display string
check('claim_head_friendly', cHtml.includes('Reserved files for editing'));
check('claim_head_not_raw', !cHtml.includes('>project_claim_path<')
                            && !cHtml.includes('Project claim path'));
check('claim_why_caption', cHtml.includes('ptool-convmeta-why')
                           && cHtml.includes('hold off editing'));
// the outcome prose renders as the body (no structured meta ⇒ MD dump path)
check('claim_body_prose', cHtml.includes('MD-DUMP:') && cHtml.includes('Held'));
// board family icon (same as project_board_read: two-rect kanban rects)
check('claim_board_icon', cHtml.includes('<rect x="14" y="3"'));
// action card ⇒ default-OPEN (not a routine read)
check('claim_open', cHtml.includes('ptool-convmeta-block" open'));

// ── project_release_path → friendly header + purpose caption ──
const relRound = {
  status: 'done', toolName: 'project_release_path', query: 'project_release_path',
  toolContent: 'Released. The "Held" notice is cleared for siblings.',
  toolRounds: [],
  results: [{ source: 'Board', snippet: 'released' }],
};
const rHtml = _renderUnifiedToolLine(relRound, false);
check('release_head_friendly', rHtml.includes('Released a file reservation'));
check('release_head_not_raw', !rHtml.includes('Project release path'));
check('release_why_caption', rHtml.includes('ptool-convmeta-why')
                             && rHtml.includes('reservation'));
check('release_body_prose', rHtml.includes('MD-DUMP:') && rHtml.includes('Released'));
check('release_open', rHtml.includes('ptool-convmeta-block" open'));

console.log(out.join('\n'));
"""

_MUST_PASS = (
    'claim_is_conv_meta', 'release_is_conv_meta',
    'claim_head_friendly', 'claim_head_not_raw', 'claim_why_caption',
    'claim_body_prose', 'claim_board_icon', 'claim_open',
    'release_head_friendly', 'release_head_not_raw', 'release_why_caption',
    'release_body_prose', 'release_open',
)


def _run(src_path):
    harness = os.path.join(HERE, '_lease_tool_render_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_lease_tools_render_as_conv_meta_cards():
    output = _run(_TR_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'lease-tool render failures:\n' + output
    for must in _MUST_PASS:
        assert ('PASS ' + must) in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_lease_tools_in_conv_meta_set_is_load_bearing():
    """Byte-revert the ``_CONV_META_TOOLS`` membership → the lease tools stop
    routing to the structured card (both ``*_is_conv_meta`` + every friendly
    header/caption check FAIL, i.e. they fall back to the generic grey chip),
    while the shipped file is restored byte-identical."""
    with open(_TR_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ('  "project_message", "project_intervene",\n'
              '  "project_claim_path", "project_release_path",\n')
    replacement = '  "project_message", "project_intervene",\n'
    assert anchor in original, 'NC anchor (lease tools in _CONV_META_TOOLS) not found'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = os.path.join(HERE, '_lease_tool_render_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        # With the tools out of the set, they are no longer conv-meta and lose
        # every card affordance — the exact "generic grey chip" regression.
        for m in ('claim_is_conv_meta', 'release_is_conv_meta',
                  'claim_head_friendly', 'release_head_friendly',
                  'claim_why_caption', 'release_why_caption'):
            assert ('FAIL ' + m) in output, \
                f'NC: expected {m} to FAIL with lease tools removed from the set:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_TR_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped tool_rounds.js must be byte-identical'


# ── Backend contract the frontend depends on ──────────────────────────────

def test_lease_tool_backend_meta_no_transition_and_lease_verb(flask_app):
    """The lease tools route through ``_handle_board_tool``; assert they (a)
    produce a ``hold`` / ``release`` badge (NOT the ugly full name a naive
    ``project_board_`` strip would leave) and (b) attach NO ``boardTransition``
    meta — a lease has no epic to describe, and a transition would SUPPRESS the
    accurate outcome prose in ``_structuredConvMetaBody``."""
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.misc import _handle_board_tool

    # sanity: both route to the board handler
    assert tool_registry.lookup('project_claim_path', {}) is _handle_board_tool
    assert tool_registry.lookup('project_release_path', {}) is _handle_board_tool

    captured = {}

    import lib.tasks_pkg.handlers.misc as misc

    def _fake_finalize(task, rn, round_entry, results, **kw):
        captured['results'] = results
        round_entry['results'] = results

    with flask_app.app_context():
        # patch finalize on BOTH the adapter (used by simple_call) so we grab
        # the built meta without needing the full task/SSE machinery.
        import lib.tasks_pkg.handlers._adapter as adapter
        orig = adapter._finalize_tool_round
        adapter._finalize_tool_round = _fake_finalize
        try:
            task = {'id': 'ttest0001', 'convId': 'cA', 'messages': []}
            round_entry = {}
            _handle_board_tool(
                task, {}, 'project_claim_path', 'tc1',
                {'resource': 'static/styles.css'}, 1, round_entry,
                {}, '/lease/test', True)
        finally:
            adapter._finalize_tool_round = orig

    meta = captured['results'][0]
    assert meta.get('badge') == 'hold', \
        f"lease claim badge must be 'hold', got {meta.get('badge')!r}"
    assert 'boardTransition' not in meta, \
        'a lease op must NOT attach a boardTransition (it has no epic; it ' \
        'would suppress the outcome prose body)'
    assert 'boardSnapshot' not in meta


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
