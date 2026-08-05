"""tests/test_translate_noop_verdict.py — the auto-translate "already in
target language" no-op must reach the client as a FIRST-CLASS terminal
verdict (epic pt_3ea0e045).

Measured incident (2026-08-01, conv ms91b45tva0sym): the deliverable was
already Chinese, so the server-side safety net correctly skipped the
translation — and committed NOTHING, and said NOTHING on the wire. The
client's auto-translate watchdog (armed by finishStream on every frozen-ON
settle) therefore polled the FULL conversation every ~6s × 90s per arm
looking for a translatedContent that would never exist: 3 arms × 13 GETs ×
24 MB ≈ 5 minutes of storm behind one finished turn (plus the "翻译中…"
pending state surviving until each arm's budget expired).

The fix has three load-bearing parts, each pinned here:
  1. Server: the no-op skip PERSISTS ``_translateDone: true`` (no
     translatedContent — the canonical "settled, show original" tri-state)
     on the message row AND pushes a terminal ``noop`` translate frame.
  2. Client push handler: a ``noop`` frame settles the message silently
     (``_translateDone = true``, pending/status/partial cleared) — so the
     watchdog disarms and finishStream's translatable-tail finder skips the
     message on every later click-open.
  3. Client DB-recovery: the watchdog's own probe adopts the persisted
     ``_translateDone`` marker, so a dropped push frame still self-heals on
     the FIRST tick instead of burning the full 90s budget.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_translate_noop_verdict.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


# ═════════════════════════════════════════════════════════════════════
#  1. Server: the no-op skip persists the marker + pushes the verdict
# ═════════════════════════════════════════════════════════════════════

class _FakeDB:
    """Two-query fake: the safety net first reads (messages, settings), then
    reads rev for the CAS write. Records every UPDATE."""

    def __init__(self, messages, settings, rev=41):
        self._row = (json.dumps(messages), json.dumps(settings))
        self._rev = rev
        self.updates = []          # (sql, params) of every UPDATE attempted
        self._pending = None

    def execute(self, sql, params=()):
        self._pending = sql
        if sql.lstrip().upper().startswith('UPDATE'):
            self.updates.append((sql, params))
        return self

    def commit(self):
        return None

    def fetchone(self):
        sql = (self._pending or '').lower()
        if 'select rev' in sql:
            return (self._rev,)
        return self._row


def _run_noop_path(monkeypatch):
    """Drive the real already-target skip; return every observable."""
    import lib.tasks_pkg.auto_translate as at

    pushes = []
    claims = []
    mirrors = []
    monkeypatch.setattr('lib.push.push_event',
                        lambda channel, task_id, frame: pushes.append(
                            (channel, task_id, frame)))
    monkeypatch.setattr('lib.translate.claim_inflight',
                        lambda *a, **kw: claims.append((a, kw)) or True)
    monkeypatch.setattr('lib.database.messages_rows.mirror_write_and_commit',
                        lambda *a, **kw: mirrors.append((a, kw)), raising=False)

    zh = '这段中文回复已经是目标语言，不需要任何翻译。'
    msg = {'role': 'assistant', 'content': zh, '_msgId': 'm-noop-1'}
    db = _FakeDB(messages=[msg], settings={'autoTranslate': True})

    at._maybe_auto_translate_assistant('conv-noop', zh, 0, db=db, task=None)
    return pushes, claims, mirrors, db


def test_noop_persists_translate_done_marker(monkeypatch):
    _pushes, _claims, mirrors, db = _run_noop_path(monkeypatch)

    marker_writes = [
        params for sql, params in db.updates
        if '"_translateDone": true' in (params[0] if params else '')
    ]
    assert len(marker_writes) == 1, (
        'the no-op verdict must PERSIST _translateDone:true on the message row '
        '(CAS) — reloads and the watchdog DB probe read this row'
    )
    # The marker must be a bare settle — never a fabricated translation.
    payload = json.loads(marker_writes[0][0])
    assert payload[0].get('_translateDone') is True
    assert 'translatedContent' not in payload[0], (
        'a no-op must not invent translatedContent (the original IS the target)'
    )
    # Phase-5 dual-write mirror fires for the single edited message.
    assert mirrors, 'the single-message mirror write should accompany the CAS'


def test_noop_pushes_terminal_frame_and_never_claims(monkeypatch):
    pushes, claims, _mirrors, _db = _run_noop_path(monkeypatch)

    noop_frames = [f for _c, _t, f in pushes if f.get('noop') is True]
    assert len(noop_frames) == 1, (
        'exactly one terminal noop frame must be pushed — the live client '
        'settles the pending state immediately instead of arming the watchdog'
    )
    f = noop_frames[0]
    assert f.get('type') == 'done' and f.get('status') == 'done'
    assert f.get('reason') == 'already_target'
    assert f.get('convId') == 'conv-noop'
    assert f.get('msgIdx') == 0
    assert f.get('msgId') == 'm-noop-1'
    assert f.get('field') == 'translatedContent'
    assert claims == [], (
        'a no-op must never claim the in-flight guard / spawn a translate '
        'thread (the whole point is that no translation work exists)'
    )


def test_noop_marker_is_idempotent(monkeypatch):
    """The narration-backfill + endpoint rescan re-enter the same skip — the
    marker write must be idempotent (same settled state, no error)."""
    _p1, _c1, _m1, db1 = _run_noop_path(monkeypatch)
    # Second entry: the row now already carries the marker.
    pushes2 = []
    monkeypatch.setattr('lib.push.push_event',
                        lambda channel, task_id, frame: pushes2.append(frame))
    zh = '这段中文回复已经是目标语言，不需要任何翻译。'
    msg = {'role': 'assistant', 'content': zh, '_msgId': 'm-noop-1',
           '_translateDone': True}
    db2 = _FakeDB(messages=[msg], settings={'autoTranslate': True})
    import lib.tasks_pkg.auto_translate as at
    at._maybe_auto_translate_assistant('conv-noop', zh, 0, db=db2, task=None)
    assert any(f.get('noop') is True for f in pushes2), (
        're-entry must re-emit the verdict (a tab that missed the first frame '
        'still settles) at zero marginal cost'
    )


# ═════════════════════════════════════════════════════════════════════
#  2. Client: noop frame settles; the watchdog DB probe adopts the marker
# ═══════════════════════════════════════════════════════════════════════════

_BODY_NOOP = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

let _pushHandler = null;
const _emitCalls = [];

const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],  // translation.js
  globals: {
    activeConvId: 'cN',
    activeStreams: new Map(),
    conversations: [
      { id: 'cN', activeTaskId: null, messages: [
        // The already-target deliverable, stuck in the server-pending state
        // (as finishStream / a running frame would have left it).
        { role: 'assistant', content: '这段中文已经是目标语言。', _msgId: 'mN1',
          _translateDone: false, _translateStatus: 'Translating…',
          _translateStatusKind: 'started', _translatePartial: '这段中文' },
      ] },
    ],
    pushSubscribe: (channel, taskId, fn) => { if (channel === 'translate') _pushHandler = fn; },
    saveConversations: () => {},
    emitMessageChanged: (convId, idx, msg, detail) => {
      _emitCalls.push({ convId, idx, kind: (detail && detail.kind) || 'full',
                        msgId: msg && msg._msgId });
    },
    ConvCache: { put: () => {} },
    _patchMessageOnServer: () => {},
    _armAutoTranslateWatchdog: () => {},
    stripNoTranslateTags: (s) => s,
    Api: { conversations: { get: async () => null } },
  },
});

(async () => {
check('push_subscriber_registered', typeof _pushHandler === 'function');
const convN = conversations[0], msgN = convN.messages[0];

// ── Guard 1 — the noop verdict settles the pending state silently. ──
_pushHandler({ status: 'done', noop: true, reason: 'already_target',
               convId: 'cN', msgId: 'mN1', field: 'translatedContent',
               model: 'skipped' });
check('noop_sets_translate_done', msgN._translateDone === true);
check('noop_clears_pending_status', msgN._translateStatus === undefined
      && msgN._translateStatusKind === undefined);
check('noop_clears_partial', msgN._translatePartial === undefined);
check('noop_no_translation_invented', msgN.translatedContent === undefined);
check('noop_repaints_full_once',
      _emitCalls.filter(c => c.msgId === 'mN1' && c.kind === 'full').length === 1);

// ── NEUTER — drop the noop flag (the pre-fix frame shape: status done with
//    no translated): the handler must IGNORE it, exactly as before the fix —
//    proving the noop branch (and the widened gate) is load-bearing rather
//    than incidental. ──
msgN._translateDone = false;
_pushHandler({ status: 'done', convId: 'cN', msgId: 'mN1',
               field: 'translatedContent', model: 'skipped' });
check('NEUTER_done_without_translation_still_ignored', msgN._translateDone === false);

// ── Guard 2 — the watchdog DB probe adopts the persisted marker. ──
// _tryRecoverFromServer is a top-level declaration in translation.js → global.
msgN._translateDone = false;
msgN._translateStatus = 'Translating…';
Api.conversations.get = async () => ({
  messages: [{ role: 'assistant', content: '这段中文已经是目标语言。',
               _msgId: 'mN1', _translateDone: true }],
});
const _recovered = await _tryRecoverFromServer('cN', 0, msgN, 'translatedContent');
check('probe_adopts_marker', _recovered === true);
check('probe_settles_message', msgN._translateDone === true
      && msgN._translateStatus === undefined);

// ── NEUTER 2 — the marker absent from the DB row: the probe finds nothing
//    (pre-fix behaviour: full budget burned). ──
msgN._translateDone = false;
Api.conversations.get = async () => ({
  messages: [{ role: 'assistant', content: '这段中文已经是目标语言。',
               _msgId: 'mN1' }],
});
const _recovered2 = await _tryRecoverFromServer('cN', 0, msgN, 'translatedContent');
check('NEUTER_probe_without_marker_finds_nothing', _recovered2 === false
      && msgN._translateDone === false);

report();
})().catch(e => { console.error('[harness]', e && e.stack || e); process.exit(1); });
"""


def test_client_noop_frame_and_probe_adoption():
    run_harness(
        target_js=os.path.join(JS_DIR, 'translation.js'),
        body_js=_BODY_NOOP,
        min_pass=10,
        label='translate-noop-verdict',
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
