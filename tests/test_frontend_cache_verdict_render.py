"""jsdom test for the cost-popover cache-verdict surface (2026-07 wire fp).

Loads the REAL shipped static/js/ui/finish_info.js under node and drives the
cache-verdict helpers directly, asserting:

  • The backend cause strings (upstream cache eviction / … UNPROVEN, and the
    legacy server-side … PROVEN rows) translate to Chinese with NO leftover
    English on the zh path (the order-sensitivity trap: a short alias must not
    eat a longer sentence).
  • _cacheBreakState classifies eviction / unproven / culprit correctly (and
    folds the legacy PROVEN wording into 'eviction').
  • _cacheBreakCulprits extracts the "[changed: key.field]" list so the popover
    can show WHICH message broke cache.

Negative controls (prove the checks bite):
  • NC1: a mis-ordered phrase table (short alias before the full sentence)
    leaves English behind → the leftover-English assertion FAILS.
  • NC2: dropping the culprit-extraction regex → the culprit assertion FAILS.

Skips cleanly when node isn't installed. No jsdom needed — finish_info.js's
verdict helpers are pure string/dict functions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

FINISH_INFO = runtime_section_path('ui/finish_info.js')
FINISH_INFO_RICH = runtime_section_path('ui/finish_info_rich.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The harness slices the shipped finish_info.js to just the cache-verdict
# helpers (the file's top ~1050 lines contain _CACHE_CAUSE_PHRASES,
# _translateCacheCause, _cacheBreakState, _cacheBreakCulprits) and evals them
# with stubbed t()/escapeHtml/_i18nLang, then asserts behaviour. ``NC`` (argv[3])
# selects a negative-control mutation applied to the source before eval.
_HARNESS = r"""
const fs = require('fs');
const SRC_PATH = process.argv[2];
const NC = process.argv[3] || '';
let src = fs.readFileSync(SRC_PATH, 'utf8');

// Keep only up to (and including) _cacheBreakCulprits — the later render code
// pulls in DOM/globals we don't need for the pure verdict helpers.
const _cut = src.indexOf('function _cacheBreakReason');
// _cacheBreakState / _cacheBreakCulprits are defined AFTER _cacheBreakReason,
// so slice to the end of _cacheBreakCulprits instead.
const _endMarker = 'return m ? m[1].trim() : \'\';\n}';
const _endIdx = src.indexOf(_endMarker);
if (_endIdx !== -1) src = src.slice(0, _endIdx + _endMarker.length);

// ── Negative-control mutations ──
if (NC === 'order') {
  // Move the short 'PROVEN' alias to the FRONT of the phrase table so it eats
  // the prefix of the full PROVEN sentence → leftover English remains.
  src = src.replace(
    'const _CACHE_CAUSE_PHRASES = [',
    "const _CACHE_CAUSE_PHRASES = [\n  ['PROVEN', '已实证'],");
}
if (NC === 'culprit') {
  // Break the culprit extraction regex → returns '' always.
  src = src.replace(
    "const m = txt.match(/\\[changed:\\s*([^\\]]+)\\]/);",
    "const m = null;");
}

// Stubs the sliced code closes over.
let _i18nLang = 'zh';
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.t = (k, o) => k;  // verdict helpers don't need real labels here

eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── 1. New PROVEN / UNPROVEN strings translate with NO leftover English ──
const _proven = 'server-side cache miss — PROVEN: the wire bytes were byte-identical to the previous round (only the body past the static prefix was not read back)';
const _unproven = 'prefix not reused — likely server-side miss or TTL expiry (UNPROVEN — no wire fingerprint)';
const zhProven = _translateCacheCause(_proven);
const zhUnproven = _translateCacheCause(_unproven);
// "byte-identical", "wire fingerprint", "server-side" etc must all be gone.
const _leftover = (s) => /[A-Za-z]{4,}/.test(s.replace(/PROVEN|UNPROVEN/, ''));
check('proven_no_leftover_english', !_leftover(zhProven));
check('unproven_no_leftover_english', !_leftover(zhUnproven));
check('proven_is_chinese', zhProven.indexOf('已实证') !== -1);
check('unproven_is_chinese', zhUnproven.indexOf('未证实') !== -1);
// The CURRENT byte-identical wording (no over-claim) fully Sinicizes too.
const _evict = 'prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change. The whole cached prefix was not reused upstream: most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once). (Most misses in this system are instead client-side and are named per-field above; this is not that class.)';
const zhEvict = _translateCacheCause(_evict);
check('byteident_no_leftover_english', !_leftover(zhEvict));
check('byteident_is_chinese', zhEvict.indexOf('前缀未被读回') !== -1);

// English UI path: returned verbatim (no Sinicization).
_i18nLang = 'en';
check('english_verbatim', _translateCacheCause(_proven) === _proven);
_i18nLang = 'zh';

// ── 2. State classification ──
// The CURRENT dominant real-traffic verdict: a byte-identical wire proves the
// miss is NOT our client-side change → its OWN 'upstream' state (our side
// cleared), NOT the apologetic 'unproven' guess.
const _byteIdent = 'prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change. The whole cached prefix was not reused upstream: most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once). (Most misses in this system are instead client-side and are named per-field above; this is not that class.)';
check('state_byteident_is_upstream', _cacheBreakState({ server_side: _byteIdent }) === 'upstream');
// A TRUE no-fingerprint fallback (non-Claude / capture failure) stays 'unproven'.
check('state_nofp_is_unproven',
  _cacheBreakState({ server_side: 'prefix not reused — likely server-side miss or TTL expiry (UNPROVEN — no wire fingerprint)' }) === 'unproven');
// LEGACY persisted rows: the old 'upstream cache eviction' verdict + the older
// 'server-side … PROVEN' wording must still fold into 'eviction' so history
// renders consistently (never the reassuring teal).
const _legacyEvict = 'upstream cache eviction — bytes were byte-identical to the previous round, so this is NOT a client change and NOT a random server failure: the whole cached prefix was evicted from the shared cache pool on this key before read';
check('state_legacy_eviction', _cacheBreakState({ server_side: _legacyEvict }) === 'eviction');
check('state_legacy_proven_is_eviction', _cacheBreakState({ server_side: _proven }) === 'eviction');
check('state_unproven', _cacheBreakState({ server_side: _unproven }) === 'unproven');
check('state_culprit_prefix_mutation',
  _cacheBreakState({ prefix_mutation: 'cached prefix bytes changed between turns [changed: user:ab.content]' }) === 'culprit');
check('state_culprit_client_change',
  _cacheBreakState({ system_prompt: 'changed' }) === 'culprit');
check('state_empty', _cacheBreakState({}) === '');
// ROUND-1 boundary re-bill (2026-07): a new-turn round-1 miss the detector now
// counts. It MUST get its OWN 'boundary' state — NOT '' (which would render no
// badge and look benign, the user-facing half of "too optimistic"), and NOT
// laundered to upstream/unproven.
check('state_turn_boundary_rebill',
  _cacheBreakState({ turn_boundary_rebill: 'new-turn round-1 boundary re-bill: the previous turn left a warm ~262000-token cached prefix, but this turn read back only 79000 (collapsed toward the static floor) — the cached prefix was not reused across the turn boundary and was re-billed uncached.' }) === 'boundary');

// ── 3. Culprit extraction (WHICH message broke cache) ──
const _cb = { prefix_mutation: 'cached prefix bytes changed between turns (non-idempotent history edit) — the whole body was re-billed uncached [changed: user:ab12.content, tool_result(c1).tool_result]' };
const culp = _cacheBreakCulprits(_cb);
check('culprit_extracted', culp.indexOf('user:ab12.content') !== -1
  && culp.indexOf('tool_result(c1).tool_result') !== -1);
check('culprit_empty_when_none', _cacheBreakCulprits({ server_side: _proven }) === '');

console.log(out.join('\n'));
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_cache_verdict_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, FINISH_INFO, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cache_verdict_translates_and_culprit_renders():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'cache-verdict render failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_phrase_order_leaves_english():
    """NC1: short alias before full sentence → leftover English detected."""
    output = _run('order')
    # The leftover-English assertions must now FAIL (proving they bite).
    assert 'FAIL proven_no_leftover_english' in output, (
        'NC1 did not bite — mis-ordered phrase table should leave English:\n'
        + output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_dropped_culprit_regex_fails():
    """NC2: break the culprit regex → the culprit assertion fails."""
    output = _run('culprit')
    assert 'FAIL culprit_extracted' in output, (
        'NC2 did not bite — dropping the culprit regex should fail extraction:\n'
        + output)


# ── Full-render DOM proof: drive the REAL renderFinishInfo and assert the
#    state badge + named culprit actually appear in the popover HTML. ──
_RENDER_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8')
  + '\n;\n' + fs.readFileSync(process.argv[3], 'utf8');
const NC = process.argv[4] || '';

let _i18nLang = 'zh';
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// t(): render {culprits}/{reason} placeholders so we can see the culprit text.
global.t = (k, o) => {
  o = o || {};
  if (k === 'finishInfo.cbCulpritLabel') return 'CULPRIT> ' + (o.culprits || '');
  if (k === 'finishInfo.cacheBreakLabel') return 'MISS: ' + (o.reason || '');
  if (k === 'finishInfo.cbState.culprit') return 'OUR-EDIT-BADGE';
  if (k === 'finishInfo.cbState.eviction') return 'EVICTION-BADGE';
  if (k === 'finishInfo.cbState.upstream') return 'UPSTREAM-BADGE';
  if (k === 'finishInfo.cbState.proven') return 'PROVEN-BADGE';
  if (k === 'finishInfo.cbState.unproven') return 'UNPROVEN-BADGE';
  if (k === 'finishInfo.cbState.boundary') return 'BOUNDARY-BADGE';
  if (k && k.indexOf('{') === -1 && o && Object.keys(o).length) {
    let s = k; for (const kk in o) s = s.replace('{'+kk+'}', o[kk]); return s;
  }
  return k;
};
global.formatCny = (n) => '¥' + Number(n||0).toFixed(4);
global.calcCostCny = () => 0.01;
global.calcCost = () => 0.01;
global.window = {}; global.document = {};

eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The per-round break line renders inside _buildCostPopover, and only when
// numRounds > 1 (the per-round table). Build a 2-round ctx: round 2 carries a
// prefix_mutation with a NAMED culprit list.
const _round = (n, cb) => ({
  round: n,
  usage: { prompt_tokens: 100, completion_tokens: 10,
           cache_creation_input_tokens: 50000, cache_read_input_tokens: 40000,
           _dispatch: { model: 'aws.claude-opus-4.8' } },
  cacheBreak: cb,
});
const _ctx = (cb) => ({
  costInfo: { inputTokens: 100, totalInputTokens: 100, outputTokens: 10,
              costCny: 0.01, cacheReadTokens: 40000, cacheWriteTokens: 50000 },
  rounds: [_round(1, undefined), _round(2, cb)],
  numRounds: 2, u: {}, inp: 100, out: 10, cw: 50000, cr: 40000, thk: 0,
  mid: 'aws.claude-opus-4.8', pid: '', taskId: 't1', toolRounds: [],
});

const html = _buildCostPopover(_ctx({ prefix_mutation:
  'cached prefix bytes changed between turns (non-idempotent history edit) — the whole body was re-billed uncached [changed: user:ab12.content, tool_result(c1).tool_result]' }));
check('render_nonempty', typeof html === 'string' && html.length > 0);
check('render_has_culprit_line', html.indexOf('cp-break-culprit') !== -1);
check('render_shows_culprit_text',
  html.indexOf('user:ab12.content') !== -1
  && html.indexOf('tool_result(c1).tool_result') !== -1);
check('render_has_culprit_state_class', html.indexOf('cp-break-culprit') !== -1);
check('render_has_our_edit_badge', html.indexOf('OUR-EDIT-BADGE') !== -1);

// A byte-identical round (read not reused): the CURRENT dominant real-traffic
// verdict. It proves the miss is NOT our client change → its OWN 'upstream'
// badge, NOT the apologetic 'unproven' one, and NO culprit line (not a client
// byte change). This is the exact "疑似服务端（未证实）excuse" the user rejected.
const html2 = _buildCostPopover(_ctx({ server_side:
  'prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change. The whole cached prefix was not reused upstream: most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once). (Most misses in this system are instead client-side and are named per-field above; this is not that class.)' }));
check('byteident_has_upstream_badge', html2.indexOf('UPSTREAM-BADGE') !== -1);
check('byteident_not_unproven_badge', html2.indexOf('UNPROVEN-BADGE') === -1);
check('byteident_no_culprit_line', html2.indexOf('cp-break-culprit') === -1);
check('byteident_state_class', html2.indexOf('cp-break-upstream') !== -1);
check('byteident_not_unproven_class', html2.indexOf('cp-break-unproven') === -1);

// A round-1 turn-boundary re-bill renders its OWN badge + own class, and the
// verbatim cause text — NOT a bare no-badge line (the too-optimistic look) and
// NOT the reassuring teal 'upstream cleared' badge.
const html3 = _buildCostPopover(_ctx({ turn_boundary_rebill:
  'new-turn round-1 boundary re-bill: the previous turn left a warm ~262000-token cached prefix, but this turn read back only 79000 (collapsed toward the static floor) — the cached prefix was not reused across the turn boundary and was re-billed uncached. Counted here so round-1 is no longer a stats blind spot (likely a TTL-window boundary miss; see the tail-TTL ticket).' }));
check('boundary_has_badge', html3.indexOf('BOUNDARY-BADGE') !== -1);
check('boundary_state_class', html3.indexOf('cp-break-boundary') !== -1);
check('boundary_not_upstream_badge', html3.indexOf('UPSTREAM-BADGE') === -1);
check('boundary_shows_cause', html3.indexOf('boundary re-bill') !== -1);

console.log(out.join('\n'));
"""


def _run_render() -> str:
    harness = os.path.join(HERE, '_cache_verdict_render_harness.js')
    with open(harness, 'w') as f:
        f.write(_RENDER_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, FINISH_INFO, FINISH_INFO_RICH],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_render_surfaces_culprit_and_state():
    """The REAL renderFinishInfo emits the state badge + named culprit line for
    a client-caused miss, and a badge-but-no-culprit for a PROVEN server miss."""
    output = _run_render()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'render DOM failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS, got:\n{output}'
    print(output)


# ═══════════════════════════════════════════════════════════════════════
# DERIVED DRIFT GUARD — the ROOT fix for the mixed zh/en verdict bug.
#
# The bug: the backend (lib/tasks_pkg/cache_tracking/_detect.py:_resolve_break_
# cause) is the single source of the verdict wording; the frontend
# _CACHE_CAUSE_PHRASES table translates it by substring. When the backend
# evolved its wording (dropped the "shared cache pool" clause, added the
# "The routing was also identical …" sentence, uppercased "Only …") the table
# was NOT updated, so those sentences shipped as raw English in a Chinese UI.
# The OLD guard hardcoded the (already-stale) strings, so it stayed green — a
# false green.
#
# THIS guard eliminates the hardcoding: it DERIVES the real cause strings by
# calling _resolve_break_cause across every prose-producing branch, then feeds
# each through the REAL shipped _translateCacheCause under a zh UI and asserts
# NO untranslated English prose remains. If EITHER side changes wording without
# the other following, this turns red on the next run — the drift can no longer
# hide.
# ═══════════════════════════════════════════════════════════════════════


def _derive_backend_causes():
    """Return [(label, cause_str)] for every prose-producing branch of the
    backend verdict fn — derived by CALLING it, never hardcoded."""
    from lib.tasks_pkg.cache_tracking._detect import _resolve_break_cause as R

    def cause(**kw):
        base = dict(client_changes={}, prefix_mutation_break=False,
                    elapsed=10, cache_read=5000, prefix_mutated=False)
        base.update(kw)
        return R(**base)

    return [
        # Byte-identical upstream-miss verdict (the dominant real-traffic case),
        # in all four shapes the fn emits.
        ('upstream_cached_nsverified',
         cause(wire_proven_identical=True, namespace_verified_same=True)),
        ('upstream_whole_nsverified',
         cause(wire_proven_identical=True, cache_read=0,
               namespace_verified_same=True)),
        ('upstream_cached_no_ns',
         cause(wire_proven_identical=True, namespace_verified_same=False)),
        ('upstream_whole_no_ns',
         cause(wire_proven_identical=True, cache_read=0,
               namespace_verified_same=False)),
        # Cache-namespace switch (client-caused cold miss).
        ('namespace_switch',
         cause(wire_proven_identical=True,
               namespace_switch=['<ns>key', '<ns>endpoint'])),
        # Client-side culprits.
        ('ttl_flip', cause(prefix_culprits=['<ttl-flip>'])),
        ('breakpoint_lost', cause(prefix_culprits=['<breakpoint-lost>'])),
        ('bytes_region', cause(prefix_culprits=['<bytes>system'])),
        ('bytes_generic', cause(prefix_culprits=['<bytes>user:ab.content'])),
        ('prefix_mutation',
         cause(prefix_mutation_break=True, cache_read=0,
               prefix_culprits=['user:ab.content'])),
        ('history_rewrite',
         cause(history_rewrite=True, prefix_culprits=['user:ab.content'])),
        # TTL expiry + the no-wire-fingerprint fallbacks (honestly hedged).
        ('ttl_expiry', cause(elapsed=400)),
        ('unproven_read', cause(cache_read=5000)),
        ('unproven_noprefix', cause(cache_read=0, prefix_mutated=False)),
    ]


# Node harness: slice finish_info.js to the verdict helpers, then translate a
# JSON list of backend cause strings (argv[2]=file) under a zh UI and report,
# per string, whether untranslated English PROSE survived. "Prose" = 3+
# consecutive space-separated ASCII words — this catches a whole English
# sentence (the bug) while tolerating retained technical identifiers
# ("API key", "anthropic-beta", "reasoning_details", "endpoint", "TTL", …)
# that the zh translations intentionally keep.
_DERIVE_HARNESS = r"""
const fs = require('fs');
const SRC_PATH = process.argv[2];
const CAUSES = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const NC = process.argv[4] || '';
let src = fs.readFileSync(SRC_PATH, 'utf8');

const _endMarker = 'return m ? m[1].trim() : \'\';\n}';
const _endIdx = src.indexOf(_endMarker);
if (_endIdx !== -1) src = src.slice(0, _endIdx + _endMarker.length);

// ── Negative control: neuter ONE full-sentence entry so its backend string no
//    longer matches → that sentence stays English → leftover prose detected.
//    Proves the derived guard actually bites on real drift.
if (NC === 'drift') {
  src = src.replace(
    "['The routing was also identical (key + anthropic-beta + endpoint all match last round), so this is not a client cache-namespace switch either.',",
    "['__DRIFT_NEUTERED_KEY_THAT_NEVER_MATCHES__',");
}

let _i18nLang = 'zh';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k, o) => k;
eval(src);

// 3+ consecutive space-separated ASCII words = untranslated English prose.
const _prose = /[A-Za-z][A-Za-z'\u2019-]*\s+[A-Za-z][A-Za-z'\u2019-]*\s+[A-Za-z][A-Za-z'\u2019-]*/;
const out = [];
for (const [label, en] of CAUSES) {
  const zh = _translateCacheCause(en);
  const leftover = _prose.exec(zh);
  out.push((leftover ? 'FAIL ' : 'PASS ') + label
    + (leftover ? '  <<leftover: "' + leftover[0] + '">>' : ''));
}
console.log(out.join('\n'));
"""


def _run_derive(causes, nc: str = '') -> str:
    import json
    harness = os.path.join(HERE, f'_cache_verdict_derive_{nc or "main"}.js')
    causes_file = os.path.join(HERE, f'_cache_verdict_causes_{nc or "main"}.json')
    with open(harness, 'w') as f:
        f.write(_DERIVE_HARNESS)
    with open(causes_file, 'w') as f:
        json.dump(causes, f)
    try:
        proc = subprocess.run(
            ['node', harness, FINISH_INFO, causes_file, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (harness, causes_file):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_backend_verdicts_all_translate_no_english_drift():
    """Every prose string _resolve_break_cause can emit today fully Sinicizes
    through the shipped _CACHE_CAUSE_PHRASES table — no leftover English prose.

    Derived, not hardcoded: the cause strings come from CALLING the backend fn,
    so if either side changes wording without the other, this turns red."""
    causes = _derive_backend_causes()
    assert len(causes) >= 12, f'expected the full branch set, got {len(causes)}'
    output = _run_derive(causes)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'backend verdict wording has drifted from the frontend translation '
        'table — these branches leave English prose on the zh UI:\n' + output)
    assert output.count('PASS') == len(causes), (
        f'expected {len(causes)} PASS, got:\n{output}')
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_derived_drift_leaves_english():
    """NC3: neuter one full-sentence phrase entry → the derived guard detects
    the untranslated sentence. Proves the drift guard bites."""
    causes = _derive_backend_causes()
    output = _run_derive(causes, 'drift')
    assert 'FAIL' in output, (
        'NC3 did not bite — neutering a phrase entry should leave English '
        'prose on at least one derived verdict:\n' + output)


if __name__ == '__main__':
    print(_run())
