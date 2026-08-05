#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3.5 — DOM-apply single-seam: parity guards + ratchet.

Docs: docs/RENDER_CONTRACT_PHASE3_5_PLAN.md. Step 2 (this commit) landed
``ConvView.apply`` + the translation_render.js convergence + the streaming_ui
/health_stream_timer census join + the boot-time ConvView hard check, so the
two former RED anchors are now STANDING GREEN guards:

1. ``test_convview_exposes_single_apply_entry`` — **standing guard** (was RED
   anchor ①). ``ConvView.apply(convId, idx, msg)`` is THE single public
   DOM-apply entry; every CONTENT-DERIVED write converges onto it. This test
   keeps it from being renamed/removed.

2. ``test_live_vs_cold_narration_byte_parity`` — **standing parity guard**
   (was RED anchor ②, JSDOM). For the SAME narration fact, the LIVE
   translation preview painted into the streaming bubble
   (``_renderStreamingTranslatePreview``) is byte-identical to the COLD
   settled render's narration slot (``_applyPartialByRoundToSettled`` over a
   slot built to the exact tool_rounds.js:_renderSegNarrationHTML contract).
   Step 2 made them equal by changing the LIVE side to the settled class
   contract (`md-content seg-narration`, dropping the live-only
   `stream-seg-narration` marker) — a side-pin check asserts the live node
   carries exactly the settled class list, so the anchor can't rot in
   either direction. NEUTER: an injected byte difference must be detected.

3. ``test_raw_dom_write_ratchet`` — **monotonic-decrease ratchet** over the
   15 non-seam files. A single-pass tokenizer strips comments + strings and
   counts ``innerHTML=`` / ``outerHTML=`` / ``insertAdjacentHTML(`` /
   ``appendChild(`` / ``.remove()``; each file must stay ≤ its baseline.
   streaming_ui.js (49) + core/health_stream_timer.js (10) joined in step 2
   after a repo-wide census proved they write inside #chatInner (the census
   also EXEMPTED ui/turn_nav.js — sidebar #turnNav + detached-builder — and
   ui/finish_info.js — zero #chatInner writes, popover attaches to body;
   see plan §2.15). conv_view.js is excluded — it IS the seam.
   NEUTER: poisoning a file's source with one extra raw op must increment
   the count.

4. ``test_ratchet_baseline_matches_plan_total`` — the baselines sum to the
   plan's §2.14 tally (207 non-seam raw ops after step 2).

5. ``test_boot_hard_check_convview_present`` — the step-4 precondition,
   landed early in step 2: main.js init carries the boot-time ConvView hard
   check (loud banner + console.error when the bundler dropped conv_view.js),
   AND conv_view.js precedes main.js in lib/js_bundler._BUNDLE_FILES so the
   check can actually observe the absence.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_dom_seam_convergence.py
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import run_harness, JS_DIR, ROOT

pytestmark = pytest.mark.unit

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')
TRANSLATION_RENDER = os.path.join(JS_DIR, 'ui', 'translation_render.js')
MAIN_JS = os.path.join(JS_DIR, 'main.js')
BUNDLER = os.path.join(ROOT, 'lib', 'js_bundler.py')


# ════════════════════════════════════════════════════════════════════
# 1. ConvView.apply existence guard (standing; was RED anchor ①)
# ════════════════════════════════════════════════════════════════════

def test_convview_exposes_single_apply_entry():
    """ConvView.apply(convId, idx, msg) — THE single public DOM-apply entry.

    Was RED anchor ① in the step-1 commit; flipped GREEN when step 2 landed
    the method (renderMessage + identity sweep + fingerprint refresh). Kept
    as a standing guard so the seam cannot be renamed or removed while the
    §2 table's CONTENT-DERIVED sites still converge onto it.
    """
    with open(CONV_VIEW, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'\bapply\s*:\s*function\b', src), (
        'ConvView.apply is gone — the single DOM-apply seam '
        '(docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §5 step 2) was renamed or '
        'removed. Restore it, or migrate every converged call site in the '
        'same commit.')


# ════════════════════════════════════════════════════════════════════
# 2. Live-vs-cold narration byte parity (standing; was RED anchor ②, JSDOM)
# ════════════════════════════════════════════════════════════════════

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="streaming-msg" data-msg-id="m1">' +
          '<div class="message-body" id="streaming-body">' +
            '<div class="panel-body">' +
              '<div class="ptool-turn" data-llm-round="L0"></div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="message" id="msg-0" data-msg-id="m1">' +
          '<div class="message-body">' +
            '<div class="seg-timeline" data-seg-timeline="1">' +
              '<div class="md-content seg-narration" data-seg-round="L0"></div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '</div></div></body>',
  targets: [process.argv[2]],   // the REAL static/js/ui/translation_render.js
  globals: {
    activeConvId: 'c1',
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
  },
});

const ZH = '第一轮旁白';
const BY_ROUND = { 0: ZH };

// ── LIVE path: the real streaming translation preview painter. ──
const liveOk = window._renderStreamingTranslatePreview('c1', 'm1', ZH, BY_ROUND);
check('live_preview_painted', liveOk === true);
/* The live zh node now carries the SETTLED class list (no stream- marker) —
 * locate it by exclusion from the English sibling, exactly like the real
 * query sites (translation_render.js + streaming_ui.js). */
const liveNarr = document.querySelector(
  '#streaming-msg .seg-narration[data-seg-round="L0"]' +
  ':not(.stream-seg-en-narration)');
check('live_narration_node_exists', !!liveNarr);

// ── COLD path: the real settled-slot painter over a tool_rounds-contract slot. ──
const coldOk = window._applyPartialByRoundToSettled('c1', 0, BY_ROUND);
check('cold_settled_painted', coldOk === true);
const coldNarr = document.querySelector(
  '#msg-0 .seg-narration[data-seg-round="L0"]');
check('cold_narration_node_exists', !!coldNarr);

// ── THE ANCHOR: one narration fact ⇒ one byte-identical DOM fragment. ──
const liveOuter = liveNarr ? liveNarr.outerHTML : '<missing-live>';
const coldOuter = coldNarr ? coldNarr.outerHTML : '<missing-cold>';
console.error('LIVE  : ' + liveOuter);
console.error('COLD  : ' + coldOuter);
check('ANCHOR_live_cold_narration_byte_identical', liveOuter === coldOuter);

// ── Side-pin: the LIVE painter changed to the settled contract
//    (`md-content seg-narration`, tool_rounds.js:_renderSegNarrationHTML) —
//    never the reverse. Trips if a future edit re-adds a live-only marker
//    class OR the settled renderer's class list drifts. ──
check('live_class_is_the_settled_contract',
      !!liveNarr && liveNarr.className === 'md-content seg-narration');

// ── NEUTER: the comparator must detect an injected byte difference. ──
const coldNarr2 = document.querySelector('#msg-0 .seg-narration[data-seg-round="L0"]');
if (coldNarr2) coldNarr2.innerHTML = coldNarr2.innerHTML + 'X';
const drifted = document.querySelector('#msg-0 .seg-narration[data-seg-round="L0"]');
check('NEUTER_injected_byte_difference_detected',
      !!drifted && drifted.outerHTML !== coldOuter);

report();
"""


def test_live_vs_cold_narration_byte_parity():
    """One narration fact, one DOM fragment — live preview == cold settled render.

    Was RED anchor ② in the step-1 commit (live side carried an extra
    `stream-seg-narration` class). Step 2 changed the LIVE painter to the
    settled class contract — visuals unchanged because the live panel carries
    `seg-timeline`, so `.seg-timeline .seg-narration` (styles.css:6096,
    values identical to the now-inert `.stream-seg-narration` block at :6158)
    applies verbatim. Both sides of the comparison are produced by REAL
    production code (translation_render.js); only the settled slot's initial
    markup is hand-built to the tool_rounds.js contract.
    """
    output = run_harness(
        target_js=TRANSLATION_RENDER,
        body_js=_BODY,
        min_pass=7,
        label='dom-seam-narration-parity',
    )
    assert 'PASS live_preview_painted' in output, output
    assert 'PASS cold_settled_painted' in output, output
    assert 'PASS live_class_is_the_settled_contract' in output, output
    assert 'PASS NEUTER_injected_byte_difference_detected' in output, output
    assert 'PASS ANCHOR_live_cold_narration_byte_identical' in output, (
        'LIVE/COLD PARITY BROKE — the live translation preview and the cold '
        'settled render emit different bytes for the same narration fact '
        '(see stderr for the two fragments). One narration fact must project '
        'to one DOM fragment (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §3).\n'
        + output)


# ════════════════════════════════════════════════════════════════════
# 3. Raw DOM-write ratchet (monotonic decrease)
# ════════════════════════════════════════════════════════════════════

_RAW_PATS = [
    r'\b(?:inner|outer)HTML\s*=',
    r'\binsertAdjacentHTML\s*\(',
    r'\bappendChild\s*\(',
    r'\.remove\s*\(\s*\)',
]

# Baselines measured 2026-07-24 with the single-pass tokenizer (v3) in
# _scan_raw_dom_ops (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §4). A file's
# count may only go DOWN as its CONTENT-DERIVED sites converge onto
# ConvView.apply; any NEW raw write trips the ratchet.
# SEAM SIDE (excluded, pinned — the allowed writers): conv_view.js (8, the
# public seam) and ui/chat_render.js (8, the seam's reconcile ENGINE after
# the step-4 SEAM-2 fold — its raw writes are the projection implementation).
# turn_nav.js / finish_info.js are exempt per plan §2.15 (sidebar +
# detached-builder / zero #chatInner writes — census-verified).
# Rebalance 2026-07-28 (fallback-banner commit): streaming_ui.js 50 → 52 —
# the two new ops are STRUCT-ONLY zone writes, the §7-sanctioned pattern:
# ``body.insertAdjacentHTML('afterbegin', '<div data-zone="fallback"></div>')``
# (one fixed zone container, created once) + the fingerprint-gated zone paint
# ``fbZone.innerHTML = renderModelFallbackBannerHtml(msg)`` (zone-inner only,
# never a message-CONTENT reconcile). Per the ratchet rule, another file was
# lowered instead: health_stream_timer.js 10 → 8 (its actual count after the
# step-2 census join — the slack was measured, not assumed). Sum stays 158.
_RATCHET_BASELINE = {
    'static/js/ui/streaming_ui.js': 52,
    'static/js/ui/streaming_render.js': 20,
    'static/js/ui/translation_render.js': 14,
    'static/js/image-gen.js': 13,
    'static/js/main/main_send_pipeline.js': 12,
    'static/js/core/health_stream_timer.js': 8,
    'static/js/main/main_conv_lifecycle.js': 10,
    'static/js/ui/sse_pipeline.js': 10,
    'static/js/ui/stream_lifecycle.js': 5,
    'static/js/image-gen-batch.js': 5,
    'static/js/main/main_translating_bubble.js': 3,
    'static/js/core/conversations.js': 3,
    'static/js/main/main_regen_continue.js': 2,
    'static/js/ui/edit_message.js': 1,
}


def _scan_raw_dom_ops(src: str) -> int:
    """Count raw DOM-mutation ops in JS source via a single-pass tokenizer.

    A regex-strip pass gets confused by real JS — a quote inside a template
    literal / regex flips the string state and silently swallows the rest of
    the file (the first 2026-07-24 audit undercounted main_send_pipeline.js
    23→1 that way, leaving the ratchet blind to 22 real writes). This scanner
    walks the source once, tracking // and /* */ comments and the three
    string delimiters with backslash escapes, so only CODE is matched.
    v3 (step 4): strings are replaced with a NON-EMPTY placeholder (`""`)
    instead of vanishing — otherwise `classList.remove('cv-off')` collapsed
    to `.remove()` and was counted as a DOM detach (false positive). Known
    limitation: regex literals are not tokenized (a quote inside a regex can
    still flip state); the baselines were measured with this same scanner,
    so the ratchet is self-consistent.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == '//':
            j = src.find('\n', i)
            i = n if j < 0 else j
        elif two == '/*':
            j = src.find('*/', i + 2)
            i = n if j < 0 else j + 2
        elif src[i] in ('"', "'", '`'):
            q = src[i]
            i += 1
            while i < n:
                if src[i] == '\\':
                    i += 2
                elif src[i] == q:
                    i += 1
                    break
                else:
                    i += 1
            out.append('""')
        else:
            out.append(src[i])
            i += 1
    code = ''.join(out)
    return sum(len(re.findall(p, code)) for p in _RAW_PATS)


def test_raw_dom_write_ratchet():
    """No file may gain raw DOM writes beyond its Phase-3.5 baseline."""
    violations = []
    for rel, baseline in _RATCHET_BASELINE.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            count = _scan_raw_dom_ops(f.read())
        if count > baseline:
            violations.append(
                f'{rel}: {count} raw DOM ops > baseline {baseline} '
                f'(+{count - baseline})')
    assert not violations, (
        'RAW-WRITE RATCHET TRIPPED — new direct #chatInner mutation(s) '
        'appeared. Route content writes through ConvView.apply '
        '(docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §2); if the new write is '
        'genuinely STRUCT-ONLY / PENDING-PLACEHOLDER, justify it in the plan '
        'and lower another file instead:\n  ' + '\n  '.join(violations))


def test_ratchet_baseline_matches_plan_total():
    """The baselines sum to the plan's §2.14 tally (158 non-seam raw ops
    after step 4's sweep + §7's sanctioned status-zone appendChild in the
    live projection engine)."""
    assert sum(_RATCHET_BASELINE.values()) == 158, (
        f'baseline sum {sum(_RATCHET_BASELINE.values())} != 158 — the plan '
        '§2.14 tally and this ratchet drifted apart; update both together')


def test_NEUTER_ratchet_detects_injected_raw_op():
    """NEUTER: poisoning a file's source with one extra raw op must increment
    the count — proves the audit is load-bearing, not decorative."""
    rel = 'static/js/main/main_send_pipeline.js'   # mid-table baseline (23)
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        clean = f.read()
    baseline = _scan_raw_dom_ops(clean)
    poisoned = clean + '\nvar x = document.getElementById("chatInner");\nx.innerHTML = "y";\n'
    after = _scan_raw_dom_ops(poisoned)
    assert after == baseline + 1, (
        f'NEUTER FAILED: injected innerHTML= moved count {baseline} → {after}, '
        'expected +1; the ratchet scan is blind to a real raw write')


# ════════════════════════════════════════════════════════════════════
# 4. Boot-time ConvView hard check (step-4 precondition, landed in step 2)
# ════════════════════════════════════════════════════════════════════

def test_boot_hard_check_convview_present():
    """main.js init must loudly fail at boot when ConvView is missing.

    The bundler's silent-no-op failure mode (CLAUDE.md §3.2.1: a <script> tag
    stripped from index.html but never added to _BUNDLE_FILES) used to mean
    `typeof window.ConvView === 'undefined'` at runtime with per-call silent
    degradation. The boot check turns that into an explicit startup failure
    (console.error + fixed banner) BEFORE any render runs. Two static facts
    are pinned here: the check exists in main.js's init IIFE, and conv_view.js
    precedes main.js in _BUNDLE_FILES (otherwise the check runs before the
    seam's slot and can never observe its absence correctly).
    """
    with open(MAIN_JS, encoding='utf-8') as f:
        main_src = f.read()
    assert 'MISSING at boot' in main_src and 'window.ConvView' in main_src, (
        'boot-time ConvView hard check is gone from main.js — the §5 step-4 '
        'precondition (loud startup failure instead of silent per-call '
        'degradation) must stay; see docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §5')
    init_pos = main_src.find('(function init()')
    check_pos = main_src.find('MISSING at boot')
    assert 0 <= init_pos < check_pos, (
        'the ConvView boot check must run INSIDE main.js\'s init IIFE')

    with open(BUNDLER, encoding='utf-8') as f:
        bundler_src = f.read()
    m = re.search(r'_BUNDLE_FILES\s*(?::\s*list\[str\])?\s*=\s*\[(.*?)\]',
                  bundler_src, re.DOTALL)
    assert m, 'could not locate _BUNDLE_FILES in lib/js_bundler.py'
    entries = re.findall(r"'([^']+\.js)'", m.group(1))
    assert 'conv_view.js' in entries, (
        'conv_view.js missing from _BUNDLE_FILES — the boot check would fire '
        'on every page load (and rightly so)')
    assert entries.index('conv_view.js') < entries.index('main.js'), (
        'conv_view.js must precede main.js in _BUNDLE_FILES so the boot '
        'check observes the seam\'s absence, not a load-order artifact')


if __name__ == '__main__':
    for fn in (test_convview_exposes_single_apply_entry,
               test_raw_dom_write_ratchet,
               test_ratchet_baseline_matches_plan_total,
               test_NEUTER_ratchet_detects_injected_raw_op,
               test_boot_hard_check_convview_present):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:300])
    try:
        test_live_vs_cold_narration_byte_parity()
        print('  PASS test_live_vs_cold_narration_byte_parity')
    except Exception as e:  # noqa: BLE001
        print('  RED  test_live_vs_cold_narration_byte_parity ::', str(e)[:300])
