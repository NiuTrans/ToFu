"""Single-message execution activity loading.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_message_activity_loading.py -v
"""

from __future__ import annotations

import os
import time

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

CONV_WINDOW = os.path.join(JS_DIR, 'conv_window.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
TRANSLATION_MODEL = os.path.join(JS_DIR, 'core', 'translation_model.js')
TRANSLATION_INDICATOR = os.path.join(JS_DIR, 'ui', 'translation_indicator.js')
TURN_SETTLEMENT = os.path.join(JS_DIR, 'core', 'turn_settlement.js')


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert

    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id,
        'user_id': 1,
        'title': 'activity',
        'messages': json_dumps_pg(messages),
        'msg_count': len(messages),
        'created_at': now,
        'updated_at': now,
        'settings': '{}',
    }, insert_cols=[
        'id', 'user_id', 'title', 'messages', 'msg_count',
        'created_at', 'updated_at', 'settings',
    ], retry=True)
    db.commit()


def _cleanup(db, *conv_ids):
    for conv_id in conv_ids:
        db.execute(
            'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def _deferred_payload(result):
    assert result.helper.__name__ == 'api_ok'
    assert result.args and isinstance(result.args[0], dict)
    return result.args[0]


def test_backend_returns_only_target_message_activity_by_stable_id():
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db
    from routes.conversations import _get_message_activity_blocking

    init_db()
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'activity-one-{time.time_ns()}'
    target_rounds = [{'roundNum': 7, 'status': 'done', 'payload': 'target'}]
    target_segments = [{'type': 'tool', 'text': 'target narration'}]
    messages = [
        {'role': 'assistant', '_msgId': 'other', 'content': 'other',
         'toolRounds': [{'payload': 'must-not-leak'}]},
        {'role': 'assistant', '_msgId': 'target', 'content': 'answer',
         'toolRounds': target_rounds, 'segments': target_segments,
         'apiRounds': [{'usage': {'prompt_tokens': 3}}]},
    ]
    _seed(db, conv_id, messages)
    try:
        payload = _deferred_payload(
            _get_message_activity_blocking(db, conv_id, 'target'))
        assert payload['msgId'] == 'target'
        assert payload['idx'] == 1
        assert payload['activity']['toolRounds'] == target_rounds
        assert payload['activity']['segments'] == target_segments
        assert 'messages' not in payload
        assert 'content' not in payload['activity']
        assert 'other' not in repr(payload)
    finally:
        _cleanup(db, conv_id)


def test_backend_missing_id_is_explicit_404_without_index_fallback():
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db
    from routes.conversations import _get_message_activity_blocking

    init_db()
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'activity-missing-{time.time_ns()}'
    _seed(db, conv_id, [
        {'role': 'assistant', '_msgId': 'present',
         'toolRounds': [{'roundNum': 1}]},
    ])
    try:
        result = _get_message_activity_blocking(db, conv_id, 'missing')
        assert result.helper.__name__ == 'api_not_found'
        assert result.args == ('message_id_not_found',)
    finally:
        _cleanup(db, conv_id)


def test_backend_duplicate_id_is_explicit_409():
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db
    from routes.conversations import _get_message_activity_blocking

    init_db()
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'activity-duplicate-{time.time_ns()}'
    _seed(db, conv_id, [
        {'role': 'assistant', '_msgId': 'dup',
         'toolRounds': [{'roundNum': 1}]},
        {'role': 'assistant', '_msgId': 'dup',
         'toolRounds': [{'roundNum': 2}]},
    ])
    try:
        result = _get_message_activity_blocking(db, conv_id, 'dup')
        assert result.helper.__name__ == 'api_conflict'
        assert result.args == ('duplicate_message_id',)
        assert result.kwargs == {'matchCount': 2}
    finally:
        _cleanup(db, conv_id)


_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let conv = null;
let callCount = 0;
let applyCalls = [];
let pendingResolve = null;
let mode = 'pending';
const originalMessages = [];

function responseFor(msgId) {
  return {
    ok: true,
    msgId,
    idx: 1,
    activity: {
      toolRounds: [{
        roundNum: 1, status: 'done', toolName: 'run_command',
        toolCallId: 'tc1', toolContent: 'command output',
      }],
      segments: [{ type: 'tool', text: 'worked' }],
      apiRounds: [{ usage: { prompt_tokens: 3 } }],
    },
  };
}

const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2], process.argv[4], process.argv[5], process.argv[6],
            process.argv[7], process.argv[8], process.argv[9]],
  globals: {
    activeConvId: 'c1',
    conversations: [],
    activeStreams: new Map(),
    getActiveConv: () => conv,
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    renderToolRoundsHTML: () => '<div class="ptool-panel">TOOLS</div>',
    renderSegmentTimelineHTML: () => '',
    renderMcpLoginHintHtml: () => '',
    renderTurnProvenanceHtml: () => '',
    renderFileChangesBar: () => '',
    renderErrorEnvelope: () => '',
    renderBranchZone: () => '',
    renderTurnCtxNote: () => '',
    renderPreferenceLearnedHtml: () => '',
    renderFinishInfo: () => '',
    _buildSwarmInboxChipsHTML: () => '',
    _injectAnchoredBranches: () => '',
    _prefetchConvCosts: () => '',
    _prefetchConvFileChanges: () => '',
    _stampFreshness: () => '',
    buildTurnNav: () => '',
    calcCostCny: () => 0,
    _fmtAbsoluteDateTime: () => '',
    stripNoTranslateTags: (s) => String(s || ''),
    renderMarkdown: (s) => '<p>' + String(s || '') + '</p>',
    _USER_AVATAR_SVG: '<svg></svg>',
    _TOFU_WORKER_SVG: '<svg></svg>',
    _TOFU_PLANNER_SVG: '<svg></svg>',
    _TOFU_CRITIC_SVG: '<svg></svg>',
    BASE_PATH: '',
    _INITIAL_RENDER: 20,
    Api: { conversations: {
      messageActivity: async (convId, msgId) => {
        callCount++;
        if (mode === 'fail') throw new Error('offline');
        if (mode === 'success') return responseFor(msgId);
        return await new Promise((resolve) => { pendingResolve = resolve; });
      },
      get: async () => null,
    }},
  },
});

global.ConvView = window.ConvView = {
  apply: (convId, idx, msg, opts) => {
    applyCalls.push({ convId, idx, msgId: msg._msgId, append: opts && opts.append });
    const old = document.querySelector('[data-msg-id="' + msg._msgId + '"]');
    const holder = document.createElement('div');
    holder.innerHTML = renderMessage(msg, idx);
    if (old) old.replaceWith(holder.firstElementChild);
    return true;
  },
  replaceAll: () => { throw new Error('whole conversation repaint forbidden'); },
};

function trimmedTail() {
  return {
    role: 'assistant', _msgId: 'a1', content: '', thinking: '',
    toolRounds: [], finishReason: 'error', error: 'API HTTP 401',
    model: 'kimi-k3', _trimmed: true, _trimmedToolRoundCount: 9,
  };
}

function reset() {
  callCount = 0;
  applyCalls = [];
  pendingResolve = null;
  const tail = trimmedTail();
  conv = { id: 'c1', activeTaskId: null, messages: [
    { role: 'user', _msgId: 'u1', content: 'ask' }, tail,
  ]};
  global.conversations = window.conversations = [conv];
  const inner = document.getElementById('chatInner');
  inner.innerHTML = renderMessage(conv.messages[0], 0) + renderMessage(tail, 1);
  originalMessages.length = 0;
  originalMessages.push(...conv.messages);
}

function snapshotControls() {
  const el = document.querySelector('[data-msg-id="a1"]');
  const action = el.querySelector('.message-actions');
  const cont = el.querySelector('.msg-continue-btn');
  return {
    actionCount: action ? action.querySelectorAll('button').length : 0,
    continueTitle: cont ? cont.getAttribute('title') : '',
  };
}

(async () => {
  reset();
  let before = snapshotControls();
  const first = loadMessageActivity('c1', 'a1');
  const second = loadMessageActivity('c1', 'a1');
  let loading = snapshotControls();
  const loadingButton = document.querySelector('.message-activity-loader');
  check('loading_dedupes_request', callCount === 1 && first === second);
  check('loading_is_accessible_and_retry_safe',
        loadingButton && loadingButton.tagName === 'BUTTON'
        && loadingButton.disabled && loadingButton.getAttribute('aria-busy') === 'true'
        && !loadingButton.hasAttribute('onclick'));
  check('loading_preserves_action_bar_and_continue',
        loading.actionCount === before.actionCount
        && loading.continueTitle === before.continueTitle
        && loading.continueTitle.indexOf('Continue generating') !== -1);
  pendingResolve(responseFor('a1'));
  check('success_resolves_true', await first === true);
  let after = snapshotControls();
  check('success_merges_only_target_message',
        conv.messages.length === 2 && conv.messages[0] === originalMessages[0]
        && conv.messages[1] === originalMessages[1]
        && conv.messages[1].toolRounds.length === 1
        && !conv.messages[1]._trimmed
        && !conv.messages[0].toolRounds);
  check('success_uses_local_apply_only',
        applyCalls.length >= 2
        && applyCalls.every((c) => c.msgId === 'a1' && c.idx === 1 && c.append === false));
  check('success_preserves_action_bar_and_continue',
        after.actionCount === before.actionCount
        && after.continueTitle === before.continueTitle);

  reset();
  before = snapshotControls();
  mode = 'fail';
  check('failure_returns_false', await loadMessageActivity('c1', 'a1') === false);
  let failed = snapshotControls();
  const retryButton = document.querySelector('.message-activity-loader');
  check('failure_keeps_retry_button',
        retryButton && retryButton.classList.contains('is-error')
        && !retryButton.disabled && !retryButton.hasAttribute('onclick'));
  check('failure_preserves_message_and_controls',
        conv.messages.length === 2 && conv.messages[1]._trimmed
        && failed.actionCount === before.actionCount
        && failed.continueTitle === before.continueTitle);
  mode = 'success';
  check('retry_succeeds', await loadMessageActivity('c1', 'a1') === true);
  check('retry_removes_loader_after_merge',
        !document.querySelector('.message-activity-loader')
        && conv.messages[1].toolRounds.length === 1);

  report();
})().catch((e) => {
  console.log('FAIL harness_threw ' + ((e && e.stack) || e));
  report();
});
"""


def test_frontend_single_message_loading_retry_and_controls():
    run_harness(
        target_js=CONV_WINDOW,
        body_js=_HARNESS,
        extra_targets=[
            CHAT_RENDER,
            ESCAPE_HTML,
            SAFE_HTML,
            TRANSLATION_MODEL,
            TRANSLATION_INDICATOR,
            TURN_SETTLEMENT,
        ],
        expect_pass=12,
        label='message-activity-loading',
    )
