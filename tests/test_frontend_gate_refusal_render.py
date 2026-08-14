"""Render-contract guard for the write-gate REFUSAL card (tool_rounds.js).

The shared-worktree guards (read-before-edit + write-freshness) refuse a
write tool call with a raw developer badge token — 'stale', 'read first',
'partial: …', 'ref failed' — that used to render VERBATIM on the card,
leaving users guessing. The renderer now upgrades them: a localized amber
badge (`.ptool-badge-warn.ptool-badge-gate`, blink off — a terminal
interception, not a transient warning) carrying the reason as tooltip,
plus an explanation card (`.ptool-gate-note`) naming the file(s) and the
automatic next step (re-read + re-issue, no user action).

Drives the REAL ``renderToolRoundsHTML`` under jsdom (same harness
discipline as tests/test_frontend_tool_rounds_render.py — the swarm
panel target loads first, mirroring bundle order) and pins:

  1. structured meta.refusal (new rounds) → badge + notice per kind;
  2. LEGACY badge-only rounds (persisted history, no meta.refusal) →
     same upgrade via the badge-string fallback;
  3. non-write tools whose badge happens to equal a refusal token stay
     RAW (no misfire), and an ordinary failed write keeps its plain red
     'failed' badge;
  4. NEUTER: with the three notice-injection splices amputated, the
     notice checks for ALL THREE write blocks go red — the injections
     are load-bearing, not decoration.

A Python-side static guard closes the i18n loop: every ``tool.gate*``
key referenced by tool_rounds.js must exist in i18n.js with both zh and
en — a missing key renders the raw key name, the exact bug class this
feature removes.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_gate_refusal_render.py
"""
from __future__ import annotations

import os
import json
import re
import tempfile

import pytest

from tests._jsdom import HERE, JS_DIR, node_deps_available, run_harness

pytestmark = pytest.mark.unit

TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
    /* The shared stub echoes the KEY; override so _t(k, enDefault) returns
     * the English copy — assertions then pin the real fallback text AND
     * the {placeholder} interpolation, not just the key name. */
    t: (k, d) => d,
  },
});
function frag(html) { const d = document.createElement('div'); d.innerHTML = html; return d; }

if (typeof renderToolRoundsHTML !== 'function') {
  console.log('FAIL entry_exposed renderToolRoundsHTML missing');
  report();
  return;
}
check('entry_exposed', true);

// ── 1. full stale refusal (structured meta.refusal) on apply_diffs ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'apply_diffs', status: 'done',
      query: 'Patch JOURNAL.md (2 edits)',
      toolArgs: JSON.stringify({ edits: [
        { path: 'JOURNAL.md', search: 'a', replace: 'b', description: 'one' },
        { path: 'JOURNAL.md', search: 'c', replace: 'd', description: 'two' },
      ] }),
      results: [{ toolName: 'apply_diffs', badge: 'stale', writeOk: false,
        refusal: { kind: 'stale', paths: ['JOURNAL.md'] },
        editSummaries: [
          { path: 'JOURNAL.md', description: 'one', status: 'fail', detail: '' },
          { path: 'JOURNAL.md', description: 'two', status: 'fail', detail: '' },
        ] }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('stale_badge_warn_class', !!(badge && badge.classList.contains('ptool-badge-warn')));
  check('stale_badge_gate_class', !!(badge && badge.classList.contains('ptool-badge-gate')));
  check('stale_badge_localized', !!(badge && badge.textContent === 'changed on disk'));
  check('stale_badge_not_raw_token', !!(badge && badge.textContent.trim() !== 'stale'));
  check('stale_badge_tooltip', !!(badge && (badge.getAttribute('title') || '').includes('Write blocked')));
  const note = d.querySelector('.ptool-gate-note');
  check('stale_notice_present', !!note);
  check('stale_notice_title', !!(note && note.querySelector('.ptool-gate-note-title')
    .textContent.includes('Write blocked — file changed on disk')));
  check('stale_notice_names_file', !!(note && note.querySelector('.ptool-gate-note-path')
    && note.querySelector('.ptool-gate-note-path').textContent === 'JOURNAL.md'));
  check('stale_notice_remedy', !!(note && note.querySelector('.ptool-gate-note-text')
    .textContent.includes('re-read the file and re-issue')));
}

// ── 2. LEGACY badge-only stale round (persisted history) on write_file ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'write_file', status: 'done',
      query: 'Write JOURNAL.md',
      toolArgs: JSON.stringify({ path: 'JOURNAL.md', content: '# hi\n' }),
      results: [{ toolName: 'write_file', badge: 'stale', writeOk: false }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('legacy_badge_upgraded', !!(badge && badge.textContent === 'changed on disk'));
  check('legacy_notice_present', !!d.querySelector('.ptool-gate-note'));
  const text = d.querySelector('.ptool-gate-note-text');
  check('legacy_notice_generic_target', !!(text && text.textContent.includes('The target file')));
}

// ── 3. read_first refusal on single apply_diff ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'apply_diff', status: 'done',
      query: 'Patch a.py',
      toolArgs: JSON.stringify({ path: 'a.py', search: 'x', replace: 'y' }),
      results: [{ toolName: 'apply_diff', badge: 'read first', writeOk: false,
        refusal: { kind: 'read_first', paths: ['a.py'] } }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('readfirst_badge', !!(badge && badge.textContent === 'must read first'));
  const note = d.querySelector('.ptool-gate-note');
  check('readfirst_notice_title', !!(note && note.querySelector('.ptool-gate-note-title')
    .textContent.includes('not read in this conversation')));
  check('readfirst_notice_mentions_read_files', !!(note &&
    note.querySelector('.ptool-gate-note-text').textContent.includes('read_files')));
}

// ── 4. partial_stale interpolates skipped/proceeded counts ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'apply_diffs', status: 'done',
      query: 'Patch a.py, b.py (2 edits)',
      toolArgs: JSON.stringify({ edits: [
        { path: 'a.py', search: 'x', replace: 'y', description: 'ok one' },
        { path: 'b.py', search: 'x', replace: 'y', description: 'ok two' },
      ] }),
      results: [{ toolName: 'apply_diffs', badge: 'partial: stale', writeOk: true,
        refusal: { kind: 'partial_stale', paths: ['c.py'], skipped: 1, proceeded: 2 },
        editSummaries: [
          { path: 'a.py', description: 'ok one', status: 'ok', detail: '' },
          { path: 'b.py', description: 'ok two', status: 'ok', detail: '' },
        ] }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('partial_badge', !!(badge && badge.textContent === 'partial · changed'));
  const note = d.querySelector('.ptool-gate-note');
  check('partial_title_count', !!(note && note.querySelector('.ptool-gate-note-title')
    .textContent.includes('1 edit(s) blocked')));
  check('partial_text_proceeded', !!(note && note.querySelector('.ptool-gate-note-text')
    .textContent.includes('the other 2 edit(s) ran normally')));
  check('partial_names_skipped_path', !!(note && note.querySelector('.ptool-gate-note-path')
    && note.querySelector('.ptool-gate-note-path').textContent === 'c.py'));
}

// ── 5. content_ref refusal on write_file ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'write_file', status: 'done',
      query: 'Write out.md',
      toolArgs: JSON.stringify({ path: 'out.md', content: 'x' }),
      results: [{ toolName: 'write_file', badge: 'ref failed', writeOk: false,
        refusal: { kind: 'content_ref' } }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('contentref_badge', !!(badge && badge.textContent === 'content ref failed'));
  const note = d.querySelector('.ptool-gate-note');
  check('contentref_notice', !!(note && note.querySelector('.ptool-gate-note-title')
    .textContent.includes('content reference failed')));
}

// ── 6. a NON-write tool whose badge collides with a refusal token stays raw ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'list_dir', status: 'done', query: 'list_dir .',
      results: [{ toolName: 'list_dir', badge: 'stale' }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('nonwrite_badge_raw', !!(badge && badge.textContent.trim() === 'stale'));
  check('nonwrite_no_notice', !d.querySelector('.ptool-gate-note'));
}

// ── 7. an ordinary failed apply_diff keeps its plain red 'failed' badge ──
{
  const html = renderToolRoundsHTML([
    { roundNum: 1, toolName: 'apply_diff', status: 'done', query: 'Patch a.py',
      toolArgs: JSON.stringify({ path: 'a.py', search: 'x', replace: 'y' }),
      results: [{ toolName: 'apply_diff', badge: 'failed', writeOk: false }] },
  ], false);
  const d = frag(html);
  const badge = d.querySelector('.ptool-badge');
  check('ordinary_fail_badge_raw', !!(badge && badge.textContent.trim() === 'failed'));
  check('ordinary_fail_err_class', !!(badge && badge.classList.contains('ptool-badge-err')));
  check('ordinary_fail_no_notice', !d.querySelector('.ptool-gate-note'));
}

report();
"""


def test_gate_refusal_render():
    run_harness(
        target_js=TOOL_ROUNDS,
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')],
        min_pass=27,
        label='gate refusal render',
    )


def _run_raw(target_js: str) -> str:
    """Run the harness without asserting — NEUTER arm inspects FAIL lines
    on a deliberately-broken scratch copy."""
    import subprocess

    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        harness_path = fh.name
        fh.write(_BODY)
    try:
        proc = subprocess.run(
            ['node', harness_path, target_js,
             os.path.normpath(os.path.join(HERE, '..')),
             os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(HERE, '_jsdom_harness.js')},
        )
    finally:
        try:
            os.remove(harness_path)
        except OSError:
            pass
    return proc.stdout or ''


_NEUTER_ANCHOR = 'const gateNoticeHtml = _renderGateNotice(_refusalInfo(round, meta));'


def test_NEUTER_notice_injection_is_load_bearing():
    """NEUTER: blank all THREE notice-injection splices (write_file /
    single-diff / batch blocks share the same one-liner) — the notice
    checks for every block shape MUST go red while the badge checks
    (driven by _computeToolBadgeHtml, untouched) stay green."""
    src = open(TOOL_ROUNDS, encoding='utf-8').read()
    assert src.count(_NEUTER_ANCHOR) == 3, (
        f'expected exactly 3 notice-injection sites, found '
        f'{src.count(_NEUTER_ANCHOR)} — the guard no longer matches the '
        f'code; re-check the refactor.')
    neutered = src.replace(_NEUTER_ANCHOR, 'const gateNoticeHtml = "";')
    assert neutered != src
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        scratch = fh.name
        fh.write(neutered)
    try:
        output = _run_raw(scratch)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    for name in ('stale_notice_present',      # batch block
                 'legacy_notice_present',     # write_file block
                 'readfirst_notice_title',    # single-diff block
                 'partial_title_count'):
        assert f'FAIL {name}' in output, (
            f'NEUTER ineffective: {name} did not go red without the '
            f'injection — it would pass on notice-less code too:\n{output}')
    # The badge upgrade lives in _computeToolBadgeHtml (not amputated) —
    # it must still pass, proving the neuter hit only the notice seam.
    assert 'PASS stale_badge_localized' in output, output


def test_gate_i18n_keys_defined():
    """Every tool.gate* key referenced in tool_rounds.js must exist in
    i18n.js with BOTH zh and en — a missing key renders the raw key
    name, the exact bug class this feature removes."""
    js = open(TOOL_ROUNDS, encoding='utf-8').read()
    referenced = set(re.findall(r'"(tool\.gate[A-Za-z]+)"', js))
    assert referenced, 'no tool.gate* keys referenced — guard lost its anchor'
    locale_dir = os.path.join(os.path.dirname(HERE), 'frontend', 'src', 'i18n', 'locales')
    with open(os.path.join(locale_dir, 'zh.json'), encoding='utf-8') as fh:
        zh = json.load(fh)
    with open(os.path.join(locale_dir, 'en.json'), encoding='utf-8') as fh:
        en = json.load(fh)
    missing = []
    for key in sorted(referenced):
        if key not in zh or key not in en:
            missing.append(key)
    assert not missing, (
        f'tool.gate* keys referenced but not fully defined in i18n.js '
        f'(need zh + en): {missing}')


if __name__ == '__main__':
    for fn in (test_gate_refusal_render,
               test_NEUTER_notice_injection_is_load_bearing,
               test_gate_i18n_keys_defined):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:400])
