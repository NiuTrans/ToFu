#!/usr/bin/env python3
"""Comprehensive functional tests for ``lib/tasks_pkg/autopilot_state.py``.

Complements ``test_autopilot_state_extraction_wire_parity.py`` (which locks
structural identity) with per-function logic coverage.

Gaps this file closes vs the pre-slice-1 baseline:

  * ``_extract_objective`` — already covered by test_autopilot_verify.py
    for the 3 happy paths; here we add the edge cases (empty list, non-dict
    entries, assistant/system-role skips, empty content, empty multimodal,
    mixed VU + real user).
  * ``_extract_objective_from_db`` — was UNCOVERED. All 4 branches:
    empty conv_id, load-raise, empty raw, real derivation.
  * ``_get_or_persist_objective`` — was UNCOVERED. All 5 branches:
    empty conv_id, existing pin (early-return, no write), fresh derive+persist,
    no-objective-to-pin (return derived, don't write), conv-row absent (fallback).
  * ``_update_objective_from_receipt`` — NEW. All 6 branches: empty args,
    no-pin (never mints), identical pin (skip write), changed pin (overwrite),
    conv-row absent (silent), exception swallowed (compaction stays green).
  * ``_get_or_persist_run_id`` — was UNCOVERED. All 4 branches:
    empty conv_id (ephemeral), existing pin (no write), fresh mint+persist,
    conv-row absent (ephemeral).
  * ``_clear_run_id`` — was UNCOVERED. Idempotent-nothing-to-clear, all-four-keys
    cleared but ``autopilotObjective`` pin RETAINED (Hole A), empty conv_id noop.
  * ``_resolve_recent_run_id`` — was UNCOVERED. All 4 paths:
    empty conv_id, pinned in settings, fallback to tail-scan of messages,
    no run at all.
  * ``_resolve_run_anchor_turn_id`` — was UNCOVERED. All 5 boundary rules:
    empty conv_id/run_id, stamped VU turn only, extended across unstamped
    followups, stopped by next VU stamp, stopped by real (non-VU) user turn.
  * ``_record_vu_turn_and_check_budget`` — extra coverage: no conv_id (early
    return), progress ledger delta arithmetic (fresh, negative-clamp, None-
    fail-open), ledger cap eviction.
  * Module constants — ``_VU_HISTORY_CAP=6`` / ``_PROGRESS_LEDGER_CAP=8``
    values load-bearing; guarded here.

Every test targets the ORIGINAL module attributes (``autopilot_state.X``) so a
future rename or move flips them; the wire-parity file covers the facade
mirror separately.

Pure unit — no live DB, no network. The fake settings store mirrors the
``update_conversation_settings`` re-read/mutate/skip-on-False/return contract.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

import pytest

import lib.tasks_pkg.autopilot_state as ap_state


# ══════════════════════════════════════════════════════════
#  Fake settings store — mirrors update_conversation_settings
# ══════════════════════════════════════════════════════════

class _FakeSettingsStore:
    """Mirrors the real update_conversation_settings contract:

      * conv row absent → returns None (caller treats as skipped).
      * mutate() runs against a dict that is the persistent row.
      * mutate returns False → do NOT persist (real store snapshots-before
        and restores on False; here we ROLLBACK to snapshot).
      * any other return (incl None) → persist. Returns the settings dict.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.write_count = 0
        self.skip_count = 0
        self.absent_count = 0

    def ensure(self, conv_id: str, **initial):
        self.rows.setdefault(conv_id, {}).update(initial)

    def update(self, conv_id, mutate, *, user_id=1, db=None, notify=True):
        if conv_id not in self.rows:
            self.absent_count += 1
            return None
        settings = self.rows[conv_id]
        import copy
        snapshot = copy.deepcopy(settings)
        ret = mutate(settings)
        if ret is False:
            # Rollback to snapshot (real store re-reads from DB on next call).
            self.rows[conv_id] = snapshot
            self.skip_count += 1
            return settings
        self.write_count += 1
        return settings


@pytest.fixture
def store(monkeypatch):
    """Install a fake update_conversation_settings on lib.conversations.

    All autopilot_state functions do ``from lib.conversations import
    update_conversation_settings`` inside their body, so we patch the module
    attribute and the fresh import will resolve to the fake.
    """
    import lib.conversations as conv_pkg
    s = _FakeSettingsStore()
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', s.update)
    return s


# ══════════════════════════════════════════════════════════
#  Module constants
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_vu_history_cap_is_6():
    """The bounded VU nudge history is load-bearing for detect_stuck's
    window=3 — cap must be ≥ window (3) but small enough to keep settings
    blob compact. 6 was the deliberate choice; guard it."""
    assert ap_state._VU_HISTORY_CAP == 6


@pytest.mark.unit
def test_progress_ledger_cap_is_8():
    """The bounded progress ledger is load-bearing for
    detect_diminishing_returns' window≤4 — cap must be ≥ window*2 to keep
    two evaluation windows in scope during the current turn. 8 was the
    deliberate choice; guard it."""
    assert ap_state._PROGRESS_LEDGER_CAP == 8


# ══════════════════════════════════════════════════════════
#  _extract_objective — edge cases beyond test_autopilot_verify.py
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_extract_objective_empty_returns_empty_string():
    assert ap_state._extract_objective([]) == ''
    assert ap_state._extract_objective(None) == ''


@pytest.mark.unit
def test_extract_objective_skips_non_dict_entries():
    """A malformed message list with non-dict entries must be tolerated."""
    msgs = ['not-a-dict', 42, None, {'role': 'user', 'content': 'real ask'}]
    assert ap_state._extract_objective(msgs) == 'real ask'


@pytest.mark.unit
def test_extract_objective_skips_assistant_and_system_roles():
    msgs = [
        {'role': 'system', 'content': 'system prompt'},
        {'role': 'assistant', 'content': 'an earlier assistant reply'},
        {'role': 'tool', 'content': 'tool result'},
        {'role': 'user', 'content': 'the human ask'},
    ]
    assert ap_state._extract_objective(msgs) == 'the human ask'


@pytest.mark.unit
def test_extract_objective_skips_empty_content_finds_next():
    """An empty-content user turn is not the objective; look further."""
    msgs = [
        {'role': 'user', 'content': ''},
        {'role': 'user', 'content': '   '},  # whitespace-only stripped to ''
        {'role': 'user', 'content': 'the real one'},
    ]
    assert ap_state._extract_objective(msgs) == 'the real one'


@pytest.mark.unit
def test_extract_objective_multimodal_no_text_parts():
    """A user turn whose content is a list of ONLY non-text blocks (e.g. all
    image blocks) has no text to extract; fall through to the next user."""
    msgs = [
        {'role': 'user', 'content': [{'type': 'image', 'src': 'x'}]},
        {'role': 'user', 'content': 'text ask'},
    ]
    assert ap_state._extract_objective(msgs) == 'text ask'


@pytest.mark.unit
def test_extract_objective_multimodal_concatenates_text_parts():
    msgs = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'analyze'},
        {'type': 'image', 'src': 'x'},
        {'type': 'text', 'text': 'this chart'},
    ]}]
    assert ap_state._extract_objective(msgs) == 'analyze this chart'


@pytest.mark.unit
def test_extract_objective_ignores_content_non_str_non_list():
    """Content that is a bare int / dict / None is treated as empty."""
    msgs = [
        {'role': 'user', 'content': 42},
        {'role': 'user', 'content': None},
        {'role': 'user', 'content': {'text': 'not a list'}},
        {'role': 'user', 'content': 'actual ask'},
    ]
    assert ap_state._extract_objective(msgs) == 'actual ask'


@pytest.mark.unit
def test_extract_objective_all_synthetic_returns_empty():
    """A conversation entirely composed of injected/synthetic user turns has
    no human ask → ''."""
    msgs = [
        {'role': 'user', '_isMeta': True, 'content': 'CLAUDE.md'},
        {'role': 'user', '_isVuDirective': True, 'content': 'directive'},
        {'role': 'user', '_isVirtualUser': True, 'content': 'vu turn'},
    ]
    assert ap_state._extract_objective(msgs) == ''


# ══════════════════════════════════════════════════════════
#  _extract_objective_from_db — all 4 branches
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_extract_objective_from_db_empty_conv_id():
    assert ap_state._extract_objective_from_db('', user_id=1) == ''
    assert ap_state._extract_objective_from_db(None, user_id=1) == ''  # type: ignore[arg-type]


@pytest.mark.unit
def test_extract_objective_from_db_load_raise_swallowed(monkeypatch):
    """Any exception in _load_messages_from_db → '' (best-effort)."""
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    def _boom(_cid, *, user_id):
        del user_id
        raise RuntimeError('DB unavailable')
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db', _boom)
    assert ap_state._extract_objective_from_db('cv-boom', user_id=1) == ''


@pytest.mark.unit
def test_extract_objective_from_db_empty_raw(monkeypatch):
    """Empty messages returned → ''."""
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: [])
    assert ap_state._extract_objective_from_db('cv-empty', user_id=1) == ''


@pytest.mark.unit
def test_extract_objective_from_db_delegates_to_pure_extract(monkeypatch):
    """A real DB row is round-tripped through _extract_objective — same
    skip rules apply (DB is truth, but injected synthetic turns COULD in
    theory exist if the caller persisted them, so verify by skipping)."""
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: [
                            {'role': 'user', '_isVirtualUser': True,
                             'content': 'VU synthetic'},
                            {'role': 'user', 'content': 'the persisted ask'},
                        ])
    assert ap_state._extract_objective_from_db('cv-real', user_id=1) == 'the persisted ask'


# ══════════════════════════════════════════════════════════
#  _get_or_persist_objective — all 5 branches
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_get_or_persist_objective_empty_conv_id_derives_locally():
    """Empty conv_id → no persistence, just derive from live messages."""
    msgs = [{'role': 'user', 'content': 'local derive'}]
    assert ap_state._get_or_persist_objective('', msgs, user_id=1) == 'local derive'


@pytest.mark.unit
def test_get_or_persist_objective_existing_pin_no_write(store, monkeypatch):
    """Existing autopilotObjective → skip the write, return the pin."""
    store.ensure('cv1', autopilotObjective='pinned value')
    # DB read should NOT be reached — pin wins.
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: (_ for _ in ()).throw(RuntimeError(
                            'DB should not be read when pin exists')))
    result = ap_state._get_or_persist_objective(
        'cv1', [{'role': 'user', 'content': 'live ask'}], user_id=1)
    assert result == 'pinned value'
    # Verify no write (skip counter incremented, write did not).
    assert store.write_count == 0
    assert store.skip_count == 1


@pytest.mark.unit
def test_get_or_persist_objective_fresh_derives_from_db_and_persists(
        store, monkeypatch):
    """No pin → derive from DB (truth) → persist. Live messages are the
    FALLBACK, not the primary source (they carry injected boilerplate)."""
    store.ensure('cv2')  # no pin
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: [
                            {'role': 'user', 'content': 'db truth'}])
    live_with_injection = [
        {'role': 'user', '_isMeta': True, 'content': 'CLAUDE.md carrier'},
        {'role': 'user', 'content': 'db truth (mirrored on live but with boilerplate wrapper)'},
    ]
    result = ap_state._get_or_persist_objective('cv2', live_with_injection, user_id=1)
    assert result == 'db truth', 'objective must come from DB, not live'
    assert store.rows['cv2']['autopilotObjective'] == 'db truth'
    assert store.write_count == 1


@pytest.mark.unit
def test_get_or_persist_objective_falls_back_to_live_when_db_empty(
        store, monkeypatch):
    """No pin, DB read returns nothing → fall back to live messages."""
    store.ensure('cv3')
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: [])
    result = ap_state._get_or_persist_objective(
        'cv3', [{'role': 'user', 'content': 'live only'}], user_id=1)
    assert result == 'live only'
    assert store.rows['cv3']['autopilotObjective'] == 'live only'


@pytest.mark.unit
def test_get_or_persist_objective_no_objective_skips_write(
        store, monkeypatch):
    """DB empty + live has NO real user ask (all synthetic) → return ''
    WITHOUT writing an empty pin."""
    store.ensure('cv4')
    from lib.tasks_pkg.conv_message_builder import _load as conv_message_load
    monkeypatch.setattr(conv_message_load, '_load_messages_from_db',
                        lambda _cid, *, user_id: [])
    result = ap_state._get_or_persist_objective(
        'cv4', [{'role': 'user', '_isMeta': True, 'content': 'meta'}], user_id=1)
    assert result == ''
    assert 'autopilotObjective' not in store.rows['cv4']
    assert store.write_count == 0


@pytest.mark.unit
def test_get_or_persist_objective_conv_row_absent_falls_back(monkeypatch):
    """When the conv row is missing (update returns None), fall back to
    deriving from live messages WITHOUT persisting."""
    import lib.conversations as conv_pkg
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings',
                        lambda *a, **k: None)
    result = ap_state._get_or_persist_objective(
        'ghost', [{'role': 'user', 'content': 'ephemeral ask'}], user_id=1)
    assert result == 'ephemeral ask'


@pytest.mark.unit
def test_get_or_persist_objective_exception_falls_back(monkeypatch):
    """Any exception during resolve → warning log + fallback derive."""
    import lib.conversations as conv_pkg
    def _boom(*a, **k):
        raise RuntimeError('settings store on fire')
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    result = ap_state._get_or_persist_objective(
        'cv-err', [{'role': 'user', 'content': 'fallback ask'}], user_id=1)
    assert result == 'fallback ask'


# ══════════════════════════════════════════════════════════
#  _update_objective_from_receipt — all 6 branches
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_repin_empty_args_noop(store):
    """Empty conv_id or empty objective → no write, returns False."""
    assert ap_state._update_objective_from_receipt('', 'goal', user_id=1) is False
    assert ap_state._update_objective_from_receipt('cv1', '', user_id=1) is False
    assert ap_state._update_objective_from_receipt('cv1', '   ', user_id=1) is False
    assert store.write_count == 0


@pytest.mark.unit
def test_repin_without_existing_pin_never_mints(store):
    """No autopilotObjective pin → skip. Re-pinning must not create pins for
    conversations autopilot never ran in (minting is _get_or_persist's job)."""
    store.ensure('cv1', unrelatedKey='x')
    assert ap_state._update_objective_from_receipt(
        'cv1', 'new goal', user_id=1) is False
    assert store.rows['cv1'] == {'unrelatedKey': 'x'}
    assert store.skip_count == 1 and store.write_count == 0


@pytest.mark.unit
def test_repin_identical_objective_skips_write(store):
    store.ensure('cv2', autopilotObjective='same goal')
    assert ap_state._update_objective_from_receipt(
        'cv2', 'same goal', user_id=1) is False
    assert store.rows['cv2']['autopilotObjective'] == 'same goal'
    assert store.skip_count == 1 and store.write_count == 0


@pytest.mark.unit
def test_repin_changed_objective_overwrites_pin(store):
    """The goal-replacement path: an L2 receipt's model-authored Objective
    becomes the VU's new north star."""
    store.ensure('cv3', autopilotObjective='Build the CSV exporter.')
    assert ap_state._update_objective_from_receipt(
        'cv3', 'Rewrite the report as a press release.', user_id=1) is True
    assert store.rows['cv3']['autopilotObjective'] == (
        'Rewrite the report as a press release.')
    assert store.write_count == 1


@pytest.mark.unit
def test_repin_conv_row_absent_silent(monkeypatch):
    import lib.conversations as conv_pkg
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings',
                        lambda *a, **k: None)
    assert ap_state._update_objective_from_receipt('gone', 'goal', user_id=1) is False


@pytest.mark.unit
def test_repin_exception_swallowed(monkeypatch):
    """Compaction must never fail because run bookkeeping couldn't update."""
    import lib.conversations as conv_pkg
    def _boom(*a, **k):
        raise RuntimeError('settings store on fire')
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    assert ap_state._update_objective_from_receipt(
        'cv-err', 'goal', user_id=1) is False


# ══════════════════════════════════════════════════════════
#  _get_or_persist_run_id — all 4 branches
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_get_or_persist_run_id_empty_conv_id_ephemeral():
    """Empty conv_id → mint a fresh 'ar-...' id that is not persisted."""
    r = ap_state._get_or_persist_run_id('', user_id=1)
    assert r.startswith('ar-')
    assert len(r) == 15  # 'ar-' + 12 hex chars


@pytest.mark.unit
def test_get_or_persist_run_id_existing_pin_no_write(store):
    """Existing autopilotRunId → return the pin, no write."""
    store.ensure('cvR1', autopilotRunId='ar-preexisting')
    result = ap_state._get_or_persist_run_id('cvR1', user_id=1)
    assert result == 'ar-preexisting'
    assert store.skip_count == 1
    assert store.write_count == 0


@pytest.mark.unit
def test_get_or_persist_run_id_fresh_mint_and_persist(store):
    """No pin → mint 'ar-<12hex>' and persist under autopilotRunId."""
    store.ensure('cvR2')
    result = ap_state._get_or_persist_run_id('cvR2', user_id=1)
    assert result.startswith('ar-')
    assert len(result) == 15
    assert store.rows['cvR2']['autopilotRunId'] == result
    assert store.write_count == 1


@pytest.mark.unit
def test_get_or_persist_run_id_conv_row_absent_ephemeral(monkeypatch):
    """conv row missing → ephemeral id, not persisted."""
    import lib.conversations as conv_pkg
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings',
                        lambda *a, **k: None)
    result = ap_state._get_or_persist_run_id('ghost-R', user_id=1)
    assert result.startswith('ar-')
    assert len(result) == 15


@pytest.mark.unit
def test_get_or_persist_run_id_exception_ephemeral(monkeypatch):
    """Exception during resolve → warning + ephemeral id."""
    import lib.conversations as conv_pkg
    def _boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    result = ap_state._get_or_persist_run_id('cv-R-err', user_id=1)
    assert result.startswith('ar-')


@pytest.mark.unit
def test_get_or_persist_run_id_mints_are_unique(store):
    """Two fresh mints for different convs produce different ids (guard
    against a shadow-bug where the uuid slice collapsed to a constant)."""
    store.ensure('cvR-a')
    store.ensure('cvR-b')
    a = ap_state._get_or_persist_run_id('cvR-a', user_id=1)
    b = ap_state._get_or_persist_run_id('cvR-b', user_id=1)
    assert a != b


# ══════════════════════════════════════════════════════════
#  _clear_run_id — Hole A + empty + all 4 keys
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_clear_run_id_empty_conv_id_noop(monkeypatch):
    """Empty conv_id → silent no-op (no raise)."""
    import lib.conversations as conv_pkg
    calls = []
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings',
                        lambda *a, **k: calls.append((a, k)))
    ap_state._clear_run_id('', user_id=1)
    ap_state._clear_run_id(None, user_id=1)  # type: ignore[arg-type]
    assert calls == [], 'empty ids must return before touching persistence'


@pytest.mark.unit
def test_clear_run_id_removes_all_four_run_scoped_keys(store):
    """All four run-scoped keys popped in one atomic write."""
    store.ensure('cvC1',
                 autopilotRunId='ar-1',
                 autopilotTurnCount=5,
                 autopilotVuHistory=['a', 'b'],
                 autopilotProgress=[{'resolved_delta': 1}],
                 autopilotObjective='the goal',
                 unrelatedKey='keep me')
    ap_state._clear_run_id('cvC1', user_id=1)
    for k in ('autopilotRunId', 'autopilotTurnCount',
              'autopilotVuHistory', 'autopilotProgress'):
        assert k not in store.rows['cvC1'], f'{k} should be cleared'
    # ★ Hole A — objective pin retained.
    assert store.rows['cvC1']['autopilotObjective'] == 'the goal'
    # Unrelated keys untouched.
    assert store.rows['cvC1']['unrelatedKey'] == 'keep me'
    assert store.write_count == 1


@pytest.mark.unit
def test_clear_run_id_nothing_to_clear_skips_write(store):
    """If no run-scoped keys are present, skip the write entirely."""
    store.ensure('cvC2', autopilotObjective='ok', unrelatedKey='x')
    ap_state._clear_run_id('cvC2', user_id=1)
    assert store.write_count == 0
    assert store.skip_count == 1
    # Nothing lost.
    assert store.rows['cvC2']['autopilotObjective'] == 'ok'
    assert store.rows['cvC2']['unrelatedKey'] == 'x'


@pytest.mark.unit
def test_clear_run_id_conv_row_absent_silent(monkeypatch):
    """conv row missing → silent (best-effort)."""
    import lib.conversations as conv_pkg
    calls = []
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings',
                        lambda *a, **k: calls.append((a, k)))
    result = ap_state._clear_run_id('ghost-C', user_id=1)
    assert result is None
    assert len(calls) == 1 and calls[0][0][0] == 'ghost-C'


@pytest.mark.unit
def test_clear_run_id_exception_swallowed(monkeypatch):
    """Any exception is swallowed at debug level (Hole-A contract:
    never wedge conclude on a settings glitch)."""
    import lib.conversations as conv_pkg
    def _boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    ap_state._clear_run_id('cv-C-err', user_id=1)  # no raise


# ══════════════════════════════════════════════════════════
#  _resolve_recent_run_id — all 4 paths
# ══════════════════════════════════════════════════════════

@pytest.fixture
def fake_snapshot(monkeypatch):
    """Install the owner-scoped snapshot boundary with a canned projection."""
    import json
    import lib.conversations.repository as repository
    from lib.conversations.repository import ConversationSnapshot

    installed = {'row': None}

    def _install(row):
        installed['row'] = row

    def _load(_conv_id, **_kwargs):
        row = installed['row']
        if row is None:
            return None
        if len(row) == 1:
            raw_settings, raw_messages = '{}', row[0]
        else:
            raw_settings, raw_messages = row[0], row[1]
        try:
            messages = json.loads(raw_messages or '[]')
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError('invalid test transcript') from exc
        return ConversationSnapshot(
            metadata={'settings': raw_settings},
            messages=messages,
        )

    monkeypatch.setattr(repository, 'get_conversation', _load)

    return _install


@pytest.mark.unit
def test_resolve_recent_run_id_empty_conv_id():
    assert ap_state._resolve_recent_run_id('', user_id=1) == ''


@pytest.mark.unit
def test_resolve_recent_run_id_pinned_wins(fake_snapshot):
    """A pinned settings.autopilotRunId beats the message-tail scan."""
    import json
    fake_snapshot((
        json.dumps({'autopilotRunId': 'ar-pinned'}),
        json.dumps([{'_autopilotRunId': 'ar-tail-stamp',
                     'role': 'user', '_isVirtualUser': True}]),
    ))
    assert ap_state._resolve_recent_run_id('cvA', user_id=1) == 'ar-pinned'


@pytest.mark.unit
def test_resolve_recent_run_id_falls_back_to_tail_scan(fake_snapshot):
    """No pin → scan messages TAIL-FIRST for the newest _autopilotRunId
    stamp (a run whose pin was already cleared)."""
    import json
    fake_snapshot((
        json.dumps({}),  # no pin
        json.dumps([
            {'_autopilotRunId': 'ar-old', 'role': 'user', '_isVirtualUser': True},
            {'role': 'assistant', 'content': 'reply'},
            {'_autopilotRunId': 'ar-new', 'role': 'user', '_isVirtualUser': True},
            {'role': 'assistant', 'content': 'later reply'},
        ]),
    ))
    assert ap_state._resolve_recent_run_id('cvB', user_id=1) == 'ar-new'


@pytest.mark.unit
def test_resolve_recent_run_id_no_run_at_all(fake_snapshot):
    """Empty pin AND no _autopilotRunId stamp anywhere → ''."""
    import json
    fake_snapshot((
        json.dumps({}),
        json.dumps([
            {'role': 'user', 'content': 'ordinary ask'},
            {'role': 'assistant', 'content': 'ordinary reply'},
        ]),
    ))
    assert ap_state._resolve_recent_run_id('cvC', user_id=1) == ''


@pytest.mark.unit
def test_resolve_recent_run_id_conv_missing(fake_snapshot):
    """fetchone → None → ''."""
    fake_snapshot(None)
    assert ap_state._resolve_recent_run_id('cv-missing', user_id=1) == ''


@pytest.mark.unit
def test_resolve_recent_run_id_bad_settings_json_falls_through(fake_snapshot):
    """Corrupt settings JSON → treat as empty settings → tail-scan."""
    import json
    fake_snapshot(('not-json', json.dumps([
        {'_autopilotRunId': 'ar-fallback', 'role': 'user', '_isVirtualUser': True}])))
    assert ap_state._resolve_recent_run_id('cvBad', user_id=1) == 'ar-fallback'


@pytest.mark.unit
def test_resolve_recent_run_id_bad_messages_json_returns_empty(fake_snapshot):
    """Corrupt messages JSON + no pin → '' (silent, defensive)."""
    import json
    fake_snapshot((json.dumps({}), 'not-json'))
    assert ap_state._resolve_recent_run_id('cvBadM', user_id=1) == ''


@pytest.mark.unit
def test_resolve_recent_run_id_pinned_trimmed(fake_snapshot):
    """Whitespace on pinned id is stripped."""
    import json
    fake_snapshot((json.dumps({'autopilotRunId': '  ar-padded  '}),
             json.dumps([])))
    assert ap_state._resolve_recent_run_id('cvT', user_id=1) == 'ar-padded'


# ══════════════════════════════════════════════════════════
#  _resolve_run_anchor_turn_id — all 5 boundary rules
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_resolve_anchor_empty_conv_or_run_id():
    assert ap_state._resolve_run_anchor_turn_id('', 'ar-x', user_id=1) == ''
    assert ap_state._resolve_run_anchor_turn_id('cv', '', user_id=1) == ''


@pytest.mark.unit
def test_resolve_anchor_stamped_vu_turn_alone(fake_snapshot):
    """A run's VU turn with NO subsequent messages → the VU turn IS the boundary."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', 'content': 'human ask', '_turnId': 'm-user'},
        {'role': 'assistant', 'content': 'reply', '_turnId': 'm-asst'},
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-target', '_turnId': 'm-vu'},
    ]),))
    assert ap_state._resolve_run_anchor_turn_id('cvA', 'ar-target', user_id=1) == 'm-vu'


@pytest.mark.unit
def test_resolve_anchor_extends_over_unstamped_followups(fake_snapshot):
    """The VU turn extends FORWARD across unstamped agent follow-ups —
    the boundary is the LAST unstamped follow-up (assistant reply)."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-r', '_turnId': 'm-vu'},
        {'role': 'assistant', 'content': 'first followup', '_turnId': 'm-a1'},
        {'role': 'assistant', 'content': 'second followup', '_turnId': 'm-a2'},
    ]),))
    assert ap_state._resolve_run_anchor_turn_id('cvB', 'ar-r', user_id=1) == 'm-a2'


@pytest.mark.unit
def test_resolve_anchor_stops_at_next_run_stamp(fake_snapshot):
    """A NEXT run's VU turn (stamped with a different run id) is the STOP
    boundary — anchor is the last unstamped turn BEFORE it."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-A', '_turnId': 'm-vu-A'},
        {'role': 'assistant', 'content': 'A followup', '_turnId': 'm-A-fu'},
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-B', '_turnId': 'm-vu-B'},  # next run — STOP
    ]),))
    # Anchor for run A must NOT drift into run B.
    assert ap_state._resolve_run_anchor_turn_id('cvC', 'ar-A', user_id=1) == 'm-A-fu'


@pytest.mark.unit
def test_resolve_anchor_stops_at_real_user_turn(fake_snapshot):
    """A real (non-VU) human user turn is the STOP boundary — the run
    ends before the human intervenes."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-r', '_turnId': 'm-vu'},
        {'role': 'assistant', 'content': 'reply', '_turnId': 'm-asst'},
        {'role': 'user', 'content': 'human interrupt', '_turnId': 'm-user'},
    ]),))
    assert ap_state._resolve_run_anchor_turn_id('cvD', 'ar-r', user_id=1) == 'm-asst'


@pytest.mark.unit
def test_resolve_anchor_no_stamped_turn_returns_empty(fake_snapshot):
    """A run id that never appeared → ''."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', 'content': 'no autopilot here', '_turnId': 'm-u'},
    ]),))
    assert ap_state._resolve_run_anchor_turn_id('cvE', 'ar-nonexistent', user_id=1) == ''


@pytest.mark.unit
def test_resolve_anchor_boundary_lacks_turn_id_returns_empty(fake_snapshot):
    """A boundary turn that has NO stable _turnId → '' (backend must not
    invent placement)."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-r'},  # no _turnId
    ]),))
    assert ap_state._resolve_run_anchor_turn_id('cvF', 'ar-r', user_id=1) == ''


@pytest.mark.unit
def test_resolve_anchor_conv_missing_returns_empty(fake_snapshot):
    fake_snapshot(None)
    assert ap_state._resolve_run_anchor_turn_id('cv-missing', 'ar-x', user_id=1) == ''


@pytest.mark.unit
def test_resolve_anchor_last_stamp_wins_on_repeated_run(fake_snapshot):
    """A run id that appears TWICE (edit-branch pathology): the code uses
    the LAST occurrence (its stamped_idx is overwritten in the loop)."""
    import json
    fake_snapshot((json.dumps([
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-dup', '_turnId': 'm-first'},
        {'role': 'user', '_isVirtualUser': True,
         '_autopilotRunId': 'ar-dup', '_turnId': 'm-second'},
        {'role': 'assistant', 'content': 'tail', '_turnId': 'm-tail'},
    ]),))
    # Boundary extends from the LAST 'ar-dup' occurrence over the trailing
    # unstamped assistant → m-tail.
    assert ap_state._resolve_run_anchor_turn_id('cvDup', 'ar-dup', user_id=1) == 'm-tail'


# ══════════════════════════════════════════════════════════
#  _record_vu_turn_and_check_budget — extra edge cases
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_record_vu_turn_empty_conv_id_early_return():
    """Empty conv_id → fail-open no-stop with turn=0."""
    assert ap_state._record_vu_turn_and_check_budget('', 'text', user_id=1) == {
        'stop': False, 'reason': '', 'turn': 0}


@pytest.mark.unit
def test_record_vu_turn_progress_ledger_delta_arithmetic(store, monkeypatch):
    """Progress ledger delta = cum_resolved_now - prev_cum. Fresh entry
    (no prior cum) → delta = resolved (or None). Non-negative clamp."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    store.ensure('cvP1', autopilotRunId='ar-p1')

    # Turn 1: baseline, no prior → delta = 3 (== resolved).
    r1 = ap_state._record_vu_turn_and_check_budget(
        'cvP1', 'first turn [PROGRESS: resolved=3 remaining=1]',
        targets=['a.py'], user_id=1)
    ledger = store.rows['cvP1']['autopilotProgress']
    assert ledger[-1]['resolved_delta'] == 3
    assert ledger[-1]['cum_resolved'] == 3
    assert ledger[-1]['targets'] == ['a.py']
    assert r1['stop'] is False

    # Turn 2: cum unchanged (still 3) → delta = 0.
    ap_state._record_vu_turn_and_check_budget(
        'cvP1', 'no new progress [PROGRESS: resolved=3 remaining=1]',
        targets=['a.py'], user_id=1)
    ledger = store.rows['cvP1']['autopilotProgress']
    assert ledger[-1]['resolved_delta'] == 0
    assert ledger[-1]['cum_resolved'] == 3

    # Turn 3: NEGATIVE delta (resolved went backwards) → clamp to 0.
    ap_state._record_vu_turn_and_check_budget(
        'cvP1', 'oops backwards [PROGRESS: resolved=1 remaining=3]',
        targets=['a.py'], user_id=1)
    ledger = store.rows['cvP1']['autopilotProgress']
    assert ledger[-1]['resolved_delta'] == 0  # clamped from -2
    assert ledger[-1]['cum_resolved'] == 1

    # Turn 4: no [PROGRESS] line → delta None, cum stays at prev cum (1).
    ap_state._record_vu_turn_and_check_budget(
        'cvP1', 'no progress marker here', targets=['a.py'], user_id=1)
    ledger = store.rows['cvP1']['autopilotProgress']
    assert ledger[-1]['resolved_delta'] is None
    assert ledger[-1]['cum_resolved'] == 1


@pytest.mark.unit
def test_record_vu_turn_progress_ledger_cap_evicts_oldest(store, monkeypatch):
    """Ledger caps at _PROGRESS_LEDGER_CAP=8; older entries drop off the head."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '0')  # disable no-progress guard
    store.ensure('cvP2', autopilotRunId='ar-p2')

    # 10 turns → ledger holds only the last 8.
    for i in range(10):
        ap_state._record_vu_turn_and_check_budget(
            'cvP2',
            f'turn {i} [PROGRESS: resolved={i} remaining=0]',
            targets=[f'f{i}.py'], user_id=1)
    ledger = store.rows['cvP2']['autopilotProgress']
    assert len(ledger) == ap_state._PROGRESS_LEDGER_CAP == 8
    # The last 8 entries are turns 2..9; verify by cum_resolved.
    assert ledger[0]['cum_resolved'] == 2
    assert ledger[-1]['cum_resolved'] == 9


@pytest.mark.unit
def test_record_vu_turn_history_cap_evicts_oldest(store, monkeypatch):
    """History caps at _VU_HISTORY_CAP=6; oldest entries drop off the head."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    store.ensure('cvH1', autopilotRunId='ar-h1')
    varied = [
        'alpha turn one text distinct',
        'beta turn two text distinct',
        'gamma turn three text distinct',
        'delta turn four text distinct',
        'epsilon turn five text distinct',
        'zeta turn six text distinct',
        'eta turn seven text distinct',
        'theta turn eight text distinct',
    ]
    for t in varied:
        ap_state._record_vu_turn_and_check_budget('cvH1', t, user_id=1)
    hist = store.rows['cvH1']['autopilotVuHistory']
    assert len(hist) == ap_state._VU_HISTORY_CAP == 6
    assert hist[0].startswith('gamma')  # first two evicted
    assert hist[-1].startswith('theta')


@pytest.mark.unit
def test_record_vu_turn_exception_fail_open(store, monkeypatch):
    """If the settings update raises unexpectedly, the guard fails OPEN
    (returns no-stop rather than wedging the loop)."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '3')
    import lib.conversations as conv_pkg
    def _boom(*a, **k):
        raise RuntimeError('settings on fire')
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', _boom)
    assert ap_state._record_vu_turn_and_check_budget('cv-err', 'text', user_id=1) == {
        'stop': False, 'reason': '', 'turn': 0}


@pytest.mark.unit
def test_record_vu_turn_deduplicates_targets(store, monkeypatch):
    """Targets are stored as a deduplicated SORTED list — the churn signal
    is presence, not count."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '0')
    store.ensure('cvT1', autopilotRunId='ar-t1')
    ap_state._record_vu_turn_and_check_budget(
        'cvT1', 'text', targets=['b.py', 'a.py', 'b.py', 'a.py', ''], user_id=1)
    ledger = store.rows['cvT1']['autopilotProgress']
    assert ledger[-1]['targets'] == ['a.py', 'b.py']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
