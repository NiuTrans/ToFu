"""Golden test for ``core/translation_model.js`` — the canonical translation
model introduced in decoupling step 1 (strangler-fig).

WHY
---
Automatic translation is being separated from content display. Step 1 adds a
single authoritative ``msg.translation`` object, a pure ``displayContent(msg)``
resolver, and — because the ~80 existing legacy readers must keep working
during migration — a BIDIRECTIONAL projection between the canonical object and
the legacy per-message fields (``translatedContent`` / ``_translatedCache`` /
``_showingTranslation`` / ``_translate*``).

The load-bearing guarantee this test locks down is the ROUND-TRIP IDENTITY

    projectTranslation(msg, readTranslation(msg))  ≡  msg

for EVERY message shape the translation engine actually produces — normal user,
assistant-done, VU (``_isVirtualUser``), critic (``_isEndpointReview``),
streaming-partial (pending), terminal error, stale-<15% done, toggle-off, and
the pristine no-translation shape. That byte-identity is what lets later
increments delete each legacy reader without regressing any of them.

It also pins ``displayContent`` — the pure content-origin resolver that erases
the render-path "inversion" (normal user shows 源文; VU/critic shows content;
assistant shows content) with ZERO translation logic.

Runs the REAL shipped module under node/jsdom via the shared harness; skips
cleanly when node + jsdom aren't installed.
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],   // core/translation_model.js
  globals: {},
});

if (typeof readTranslation !== 'function' ||
    typeof projectTranslation !== 'function' ||
    typeof displayContent !== 'function') {
  check('module_exposed', false); report(); return;
}
check('module_exposed', true);

// Stable, key-order-independent structural equality: byte-identity of the
// message OBJECT means the same set of own keys with deep-equal values, not a
// particular JSON key order (projection re-adds keys in its own order).
function stable(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
  const keys = Object.keys(v).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
}
function clone(o) { return JSON.parse(JSON.stringify(o)); }

// ── The engine-produced message shapes (transcribed from translation.js
//    _applyTranslationDone / _applyTranslationStatus / _applyTranslationError /
//    _resetTranslationState). Each must survive the legacy↔canonical round trip
//    byte-identically. ──
const SHAPES = {
  // pristine normal user message (no display translation at all)
  user_plain: { role: 'user', content: 'English for model', originalContent: '中文源文', _msgId: 'mU' },
  // assistant with no translation yet
  assistant_idle: { role: 'assistant', content: 'A reply.', _msgId: 'mA', done: true },
  // assistant done (field=translatedContent)
  assistant_done: {
    role: 'assistant', content: 'A reply.', _msgId: 'mA', done: true,
    translatedContent: '一条回复。', _translatedCache: '一条回复。',
    _showingTranslation: true, _translateModel: 'gpt-x', _translateDone: true,
  },
  // VU (Autopilot) done — role=user, DISPLAY-translated (the inversion)
  vu_done: {
    role: 'user', _isVirtualUser: true, content: 'Model-language original', _msgId: 'mV',
    translatedContent: '模型语言的译文', _translatedCache: '模型语言的译文',
    _showingTranslation: true, _translateModel: 'gpt-x', _translateDone: true,
  },
  // critic done — role=user + _isEndpointReview
  critic_done: {
    role: 'user', _isEndpointReview: true, content: 'Critic verdict', _msgId: 'mC',
    translatedContent: '评审结论', _translatedCache: '评审结论',
    _showingTranslation: true, _translateModel: 'gpt-x', _translateDone: true,
  },
  // pending with a streaming partial + status
  pending_partial: {
    role: 'assistant', content: 'English body', _msgId: 'mP',
    _translateTaskId: 'task-1', _translatePartial: '部分译文…',
    _translateStatus: 'rate limited', _translateStatusKind: 'rate_limited',
  },
  // pending, task only (first tick, no partial yet)
  pending_taskonly: {
    role: 'assistant', content: 'English body', _msgId: 'mP2', _translateTaskId: 'task-2',
  },
  // terminal error (a partial may survive)
  error_with_partial: {
    role: 'assistant', content: 'English body', _msgId: 'mE',
    _translateDone: true, _translateError: 'Translation failed', _translatePartial: '半截…',
  },
  // terminal error, clean
  error_clean: {
    role: 'assistant', content: 'English body', _msgId: 'mE2',
    _translateDone: true, _translateError: 'timeout', _translateModel: 'gpt-x',
  },
  // done but toggle OFF (user hid the 译文)
  done_toggle_off: {
    role: 'assistant', content: 'A reply.', _msgId: 'mT',
    translatedContent: '译文', _translatedCache: '译文',
    _showingTranslation: false, _translateModel: 'gpt-x', _translateDone: true,
  },
  // stale-<15% done: short translation vs long source — still a done shape,
  // identity must hold (staleness is a downstream policy read, not a field)
  stale_done: {
    role: 'assistant', content: 'x'.repeat(4000), _msgId: 'mS',
    translatedContent: '短', _translatedCache: '短',
    _showingTranslation: true, _translateDone: true,
  },
};

for (const [name, shape] of Object.entries(SHAPES)) {
  const orig = clone(shape);
  const round = projectTranslation(clone(shape), readTranslation(clone(shape)));
  check('roundtrip_identity__' + name, stable(round) === stable(orig));
}

// Spot-check the derived status classification is what render will branch on.
check('status_user_plain_idle', readTranslation(SHAPES.user_plain).status === 'idle');
check('status_assistant_done', readTranslation(SHAPES.assistant_done).status === 'done');
check('status_vu_done', readTranslation(SHAPES.vu_done).status === 'done');
check('status_pending', readTranslation(SHAPES.pending_partial).status === 'pending');
check('status_error_wins_over_done', readTranslation(SHAPES.error_with_partial).status === 'error');
check('text_from_translatedContent', readTranslation(SHAPES.assistant_done).text === '一条回复。');
check('showing_false_preserved', readTranslation(SHAPES.done_toggle_off).showing === false);

// ── displayContent: the pure content-origin resolver (NO translation read) ──
// normal user → 源文 (originalContent), NOT markdown
{
  const d = displayContent(SHAPES.user_plain);
  check('display_user_shows_original', d.text === '中文源文' && d.isMarkdown === false && d.stripNoTranslate === true);
}
// VU → content (the model-language original), markdown
{
  const d = displayContent(SHAPES.vu_done);
  check('display_vu_shows_content_markdown', d.text === 'Model-language original' && d.isMarkdown === true);
}
// critic → content, markdown
{
  const d = displayContent(SHAPES.critic_done);
  check('display_critic_shows_content_markdown', d.text === 'Critic verdict' && d.isMarkdown === true);
}
// assistant → content, markdown
{
  const d = displayContent(SHAPES.assistant_done);
  check('display_assistant_shows_content_markdown', d.text === 'A reply.' && d.isMarkdown === true);
}
// user with no originalContent falls back to content
{
  const d = displayContent({ role: 'user', content: 'plain typed' });
  check('display_user_fallback_to_content', d.text === 'plain typed');
}

// ── NEUTER: break the projection (drop the done branch) and prove the golden
//    round-trip then FAILS — i.e. the projection is load-bearing, not vacuous. ──
{
  const _saved = projectTranslation;
  // A stub that never re-adds the done fields → assistant_done can't round-trip.
  projectTranslation = (msg) => { for (const k of ['translatedContent','_translatedCache','_showingTranslation','_translateModel','_translateDone']) delete msg[k]; return msg; };
  const orig = clone(SHAPES.assistant_done);
  const round = projectTranslation(clone(SHAPES.assistant_done), readTranslation(clone(SHAPES.assistant_done)));
  check('NC_broken_projection_breaks_roundtrip', stable(round) !== stable(orig));
  projectTranslation = _saved;
}

report();
"""


def test_frontend_translation_model():
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'translation_model.js'),
        body_js=_BODY,
        min_pass=25,
        label='translation-model-golden',
    )
