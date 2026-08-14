"""Tests for lib/tasks_pkg/killed_recovery.py — making interruptedReason ACTIONABLE.

The acceptance criterion: a turn tagged interruptedReason='killed' is
automatically re-dispatched exactly ONCE per boot (and stops after the cap),
while a 'manual' turn triggers NO re-dispatch, and a restart STORM stands the
whole mechanism down.

Two layers:
  * decide()/next_attempt() are PURE (no DB, no dispatch) — tested directly.
  * run_killed_recovery() is tested against an in-memory fake DB + a monkey-
    patched _redispatch_conv, so it needs no real schema/LLM (the reliable
    form here — see the autopilot-resume test gotcha memory).

Run standalone: ``python3 tests/test_killed_recovery.py`` (the env pytest
transitively imports a broken napari/vispy GL plugin at collection).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.tasks_pkg.killed_recovery as kr


def _killed_tail(user_key='u1'):
    return [
        {'role': 'user', 'content': 'Q', '_msgId': user_key},
        {'role': 'assistant', 'content': 'partial…', 'finishReason': 'interrupted',
         'interruptedReason': 'killed'},
    ]


# ── PURE decide()/next_attempt() ──────────────────────────────────────

def test_killed_tail_decides_redispatch():
    v = kr.decide(_killed_tail(), {}, storm=False)
    assert v['action'] == 'redispatch', v
    assert v['attempts'] == 1
    assert v['tag'] == 'killed'
    print('OK killed → redispatch (attempt 1)')


def test_manual_tail_is_skipped():
    msgs = _killed_tail()
    msgs[-1]['interruptedReason'] = 'manual'
    v = kr.decide(msgs, {}, storm=False)
    assert v['action'] == 'skip', v
    print('OK manual → skip (no re-dispatch)')


def test_completed_tail_is_skipped():
    msgs = _killed_tail()
    msgs[-1].pop('interruptedReason')
    msgs[-1]['finishReason'] = 'stop'
    v = kr.decide(msgs, {}, storm=False)
    assert v['action'] == 'skip'
    print('OK completed → skip')


def test_storm_holds_without_burning_an_attempt():
    v = kr.decide(_killed_tail(), {}, storm=True)
    assert v['action'] == 'storm_hold', v
    assert v['attempts'] == 0  # storm must NOT consume an attempt
    print('OK storm → hold, no attempt burned')


def test_attempt_counter_caps_then_exhausts():
    """Same user turn, attempts accumulate; past the cap → exhausted."""
    settings = {}
    msgs = _killed_tail('turnX')
    seen = []
    for _ in range(kr.KILLED_RECOVERY_MAX_ATTEMPTS + 2):
        v = kr.decide(msgs, settings, storm=False)
        seen.append(v['action'])
        # persist the patch exactly as run_killed_recovery does
        if v['settings_patch'] is not None:
            settings['_killedRecovery'] = v['settings_patch']
    redispatch_n = seen.count('redispatch')
    assert redispatch_n == kr.KILLED_RECOVERY_MAX_ATTEMPTS, (seen, redispatch_n)
    assert seen[-1] == 'exhausted', seen
    print('OK cap: %d redispatch then exhausted (%s)' % (redispatch_n, seen))


def test_new_user_turn_resets_the_counter():
    """A DIFFERENT user turn starts a fresh attempt budget (not stuck exhausted)."""
    settings = {'_killedRecovery': {'key': 'old', 'attempts': 99}}
    v = kr.decide(_killed_tail('brand-new'), settings, storm=False)
    assert v['action'] == 'redispatch'
    assert v['attempts'] == 1, v
    print('OK new user turn resets counter')


def test_counter_keyed_on_user_not_assistant():
    """The counter must survive assistant re-creation — key is the USER turn."""
    # Two different assistant messages, SAME user turn key → count increments.
    s = {}
    a1, p1 = kr.next_attempt(s, 'sameuser')
    s['_killedRecovery'] = p1
    a2, p2 = kr.next_attempt(s, 'sameuser')
    assert (a1, a2) == (1, 2), (a1, a2)
    print('OK counter keyed on user turn (1→2)')


# ── run_killed_recovery() with a fake DB + patched dispatch ───────────

class _FakeRow(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


class _FakeDB:
    def __init__(self, convs):
        # convs: {conv_id: {'settings': dict, 'messages': list}}
        self._convs = convs
        self.commits = 0

    def execute(self, sql, params=()):
        s = sql.strip().upper()
        if s.startswith('SELECT'):
            cid = params[0]
            c = self._convs.get(cid)
            self._last = None
            if c is not None:
                if s.startswith('SELECT REV'):
                    self._last = _FakeRow(rev=c.get('rev', 0))
                else:
                    self._last = _FakeRow(
                        settings=json.dumps(c['settings']),
                        messages=json.dumps(c['messages']))
            return self
        if s.startswith('UPDATE'):
            assignments = sql.upper().split(' SET ', 1)[1].split(' WHERE ', 1)[0]
            columns = [part.strip().split('=', 1)[0].strip().lower()
                       for part in assignments.split(',')]
            cid = params[len(columns)]
            c = self._convs.get(cid, {})
            for column, value in zip(columns, params):
                if column == 'settings':
                    c['settings'] = json.loads(value)
                elif column == 'messages':
                    c['messages'] = json.loads(value)
                    c['rev'] = c.get('rev', 0) + 1
            self._convs[cid] = c
            return self
        return self

    def fetchone(self):
        return self._last

    def commit(self):
        self.commits += 1

    def begin(self):
        pass

    def rollback(self):
        pass


def _patch(db, dispatched):
    kr.get_thread_db = lambda *_a, **_k: db  # type: ignore
    kr._redispatch_conv = lambda conv_id: (dispatched.append(conv_id) or f'task-{conv_id}')  # type: ignore
    kr._conv_has_live_task = lambda conv_id: False  # type: ignore
    # Keep these legacy single-conversation tests isolated from live task
    # objects left in the process-wide manager by the preceding integration
    # module. Concurrency behaviour has dedicated tests below.
    kr._count_live_killed_carriers = lambda: 0  # type: ignore


def test_run_killed_redispatches_exactly_once():
    convs = {'c1': {'settings': {}, 'messages': _killed_tail('c1u')}}
    db = _FakeDB(convs)
    dispatched = []
    _patch(db, dispatched)
    summary = kr.run_killed_recovery(['c1'], storm=False)
    assert dispatched == ['c1'], dispatched
    assert summary['redispatched'] == 1, summary
    # attempt counter persisted
    assert convs['c1']['settings']['_killedRecovery']['attempts'] == 1
    print('OK run: killed conv re-dispatched exactly once')


def test_run_manual_redispatches_none():
    msgs = _killed_tail('c2u')
    msgs[-1]['interruptedReason'] = 'manual'
    convs = {'c2': {'settings': {}, 'messages': msgs}}
    db = _FakeDB(convs)
    dispatched = []
    _patch(db, dispatched)
    summary = kr.run_killed_recovery(['c2'], storm=False)
    assert dispatched == [], dispatched
    assert summary['redispatched'] == 0 and summary['skipped'] == 1, summary
    print('OK run: manual conv → zero re-dispatch')


def test_run_storm_redispatches_none():
    convs = {'c3': {'settings': {}, 'messages': _killed_tail('c3u')}}
    db = _FakeDB(convs)
    dispatched = []
    _patch(db, dispatched)
    summary = kr.run_killed_recovery(['c3'], storm=True)
    assert dispatched == [], dispatched
    assert summary['storm_held'] == 1 and summary['redispatched'] == 0, summary
    # NO attempt burned during a storm
    assert '_killedRecovery' not in convs['c3']['settings']
    print('OK run: storm → zero re-dispatch, no attempt burned')


def test_run_caps_after_max_then_exhausts():
    convs = {'c4': {'settings': {}, 'messages': _killed_tail('c4u')}}
    db = _FakeDB(convs)
    dispatched = []
    _patch(db, dispatched)
    # Re-run recovery repeatedly (simulating re-kill each attempt).
    for _ in range(kr.KILLED_RECOVERY_MAX_ATTEMPTS + 2):
        # a re-dispatch would re-tag the tail 'killed' on the next kill; keep it
        convs['c4']['messages'][-1]['interruptedReason'] = 'killed'
        kr.run_killed_recovery(['c4'], storm=False)
    assert len(dispatched) == kr.KILLED_RECOVERY_MAX_ATTEMPTS, dispatched
    assert convs['c4']['messages'][-1]['interruptedReason'] == 'killed_exhausted'
    print('OK run: capped at %d re-dispatches, then killed_exhausted'
          % kr.KILLED_RECOVERY_MAX_ATTEMPTS)


# ── REGRESSION: the killed re-dispatch config must not FATAL on maxTokens=None ──
# The first shipped killed-recovery build re-dispatched all 6 killed turns and
# every one FATALed with "'<' not supported between instances of 'int' and
# 'NoneType'": resolve_conv_config with no server_defaults returns
# maxTokens=None, which flows to build_body → _clamp_max_tokens → min(None,int).

def test_clamp_max_tokens_tolerates_none():
    from lib.model_info import _clamp_max_tokens
    # Must NOT raise (was: TypeError). Any positive fallback is acceptable.
    r = _clamp_max_tokens('aws.claude-opus-4.8', None)
    assert isinstance(r, int) and r > 0, r
    # A normal value is still clamped as before.
    assert _clamp_max_tokens('aws.claude-opus-4.8', 128000) == 128000
    print('OK _clamp_max_tokens(None) no longer crashes')


def test_model_config_coerces_none_max_tokens():
    from lib.tasks_pkg.model_config import _resolve_model_config
    # Present-but-None (the resolve_conv_config output) must coerce to a number.
    mc = _resolve_model_config({'model': 'aws.claude-opus-4.8', 'maxTokens': None}, 't1')
    assert mc['max_tokens'] == 128000, mc['max_tokens']
    # Absent still defaults.
    assert _resolve_model_config({'model': 'gpt-4o'}, 't2')['max_tokens'] == 128000
    print('OK _resolve_model_config coerces None/absent maxTokens → 128000')


def test_redispatch_config_has_valid_max_tokens():
    """The recovery-built config must NEVER carry maxTokens=None (root cause).

    resolve_conv_config is now hardened at the source: a missing/None value
    resolves to 128000 REGARDLESS of whether server_defaults were supplied. The
    killed-turn recovery path calls it with no overrides AND no server_defaults
    (the exact shape that used to emit None and FATAL the turn), so THAT call
    is the load-bearing case here.
    """
    from lib.conv_config import resolve_conv_config
    # With server_defaults present — still a valid int.
    cfg = resolve_conv_config(conv_settings={'model': 'aws.claude-opus-4.8'},
                              server_defaults={'serverModel': 'gpt-4o',
                                               'maxTokens': 64000}, is_active=False)
    assert cfg['maxTokens'] == 64000, cfg['maxTokens']
    assert cfg['model'] == 'aws.claude-opus-4.8'
    # The historical crash path: NO overrides + NO server_defaults. This used
    # to yield None (which then fataled _clamp_max_tokens); it must now resolve
    # to the 128000 default at the SOURCE, so no None ever leaves the resolver.
    recovered = resolve_conv_config(conv_settings={'model': 'm'}, is_active=False)
    assert recovered['maxTokens'] == 128000, recovered['maxTokens']
    # And that value no longer fatals the historical crash line.
    from lib.model_info import _clamp_max_tokens
    assert isinstance(_clamp_max_tokens('m', recovered['maxTokens']), int)
    print('OK recovery config carries valid maxTokens (128000, never None)')


# ── Durable scan: tail-only, so a mid-history killed tag doesn't re-fire ──

def test_durable_scan_matches_tail_only():
    import time as _t
    now_ms = int(_t.time() * 1000)
    # conv A: killed on the TAIL → should be picked up.
    a = json.dumps(_killed_tail('au'))
    # conv B: a killed tag MID-history but a completed tail → must NOT match.
    b = json.dumps([
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'x', 'interruptedReason': 'killed'},
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'done', 'finishReason': 'stop'},
    ])
    convs = {
        'A': {'settings': {}, 'messages': json.loads(a), '_row_messages': a,
              'updated_at': now_ms},
        'B': {'settings': {}, 'messages': json.loads(b), '_row_messages': b,
              'updated_at': now_ms},
    }

    class _ScanDB:
        def execute(self, sql, params=()):
            low = ' '.join(sql.lower().split())
            if ' where id=? and user_id=?' in low:
                cid = params[0]
                value = convs.get(cid)
                self._rows = ([] if value is None else [_FakeRow(
                    id=cid, user_id=1, messages=value['_row_messages'],
                    rev=0, msg_count=len(value['messages']),
                    messages_rows_rev=-1)])
            else:
                self._rows = [_FakeRow(id=k, user_id=1)
                              for k in convs]
            return self
        def fetchall(self):
            return self._rows
        def fetchone(self):
            return self._rows[0] if self._rows else None
    kr.get_thread_db = lambda *_a, **_k: _ScanDB()  # type: ignore
    found = kr.list_killed_turn_convs()
    assert found == ['A'], found  # B excluded: killed tag is not on its tail
    print('OK durable scan: tail-only (mid-history killed tag ignored)')


# ── Point 2: internal-FATAL vs real-model-error distinction ───────────

def test_internal_fatal_is_recoverable_model_error_is_not():
    # internal / generic = recovery-internal fault (model never reached) → recoverable
    assert kr.is_recovery_internal_fatal({'kind': 'internal'}) is True
    assert kr.is_recovery_internal_fatal({'kind': 'generic'}) is True
    # a non-envelope error → treat as internal (fail toward re-recovery)
    assert kr.is_recovery_internal_fatal(None) is True
    assert kr.is_recovery_internal_fatal('boom') is True
    # real model outcomes → NOT recoverable (completed turn, stays terminal)
    for k in ('ratelimit', 'quota', 'permission', 'prompt_too_long',
              'content_filter', 'model_limit'):
        assert kr.is_recovery_internal_fatal({'kind': k}) is False, k
    print('OK internal/generic → recoverable; model errors → terminal')


def test_restamp_rearms_below_cap_but_exhausts_at_cap():
    # Below cap: re-stamp back to 'killed' (retry on calm boot), clear error.
    msgs = _killed_tail('ru1')
    msgs[-1]['interruptedReason'] = None
    msgs[-1]['finishReason'] = 'error'
    msgs[-1]['error'] = {'kind': 'internal'}
    convs = {'rc1': {'settings': {'_killedRecovery': {'key': 'ru1', 'attempts': 1}},
                     'messages': msgs}}
    db = _FakeDB(convs)
    kr.get_thread_db = lambda *_a, **_k: db  # type: ignore
    ok = kr.restamp_killed_after_internal_fatal({'convId': 'rc1', 'id': 't1'})
    assert ok is True
    assert convs['rc1']['messages'][-1]['interruptedReason'] == 'killed'
    assert 'error' not in convs['rc1']['messages'][-1]
    print('OK re-stamp below cap → killed (recoverable)')

    # At cap: degrade to killed_exhausted, do NOT re-arm.
    msgs2 = _killed_tail('ru2')
    convs2 = {'rc2': {'settings': {'_killedRecovery': {'key': 'ru2',
                      'attempts': kr.KILLED_RECOVERY_MAX_ATTEMPTS}},
                      'messages': msgs2}}
    db2 = _FakeDB(convs2)
    kr.get_thread_db = lambda *_a, **_k: db2  # type: ignore
    ok2 = kr.restamp_killed_after_internal_fatal({'convId': 'rc2', 'id': 't2'})
    assert ok2 is True
    assert convs2['rc2']['messages'][-1]['interruptedReason'] == 'killed_exhausted'
    print('OK re-stamp at cap → killed_exhausted (no re-arm)')


# ── Concurrency cap + gentle boot ramp + drain daemon + weight ordering ──

def _killed_conv(user_key, content):
    """A killed-tail conv whose context weight scales with ``content`` length."""
    return {'settings': {}, 'messages': [
        {'role': 'user', 'content': 'Q', '_msgId': user_key},
        {'role': 'assistant', 'content': content, 'finishReason': 'interrupted',
         'interruptedReason': 'killed'},
    ]}


def _patch_bounded(db, dispatched, *, live=0, capture_deferred=None):
    """Patch dispatch primitives + the concurrency counter for bounded tests."""
    kr.get_thread_db = lambda *_a, **_k: db  # type: ignore
    kr._redispatch_conv = lambda conv_id: (dispatched.append(conv_id) or f'task-{conv_id}')  # type: ignore
    kr._conv_has_live_task = lambda conv_id: False  # type: ignore
    kr._count_live_killed_carriers = lambda: live  # type: ignore
    if capture_deferred is not None:
        # No-op the daemon so the INLINE decision is tested in isolation.
        kr._spawn_drain_daemon = lambda deferred, **_k: (capture_deferred.extend(deferred) or None)  # type: ignore


def test_boot_ramp_dispatches_one_inline_and_defers_the_rest():
    """Cap=2, inline-boot=1: exactly ONE carrier fires at boot, rest deferred."""
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_INLINE_BOOT_DISPATCH = 1
    convs = {'light': _killed_conv('lu', 'x'),
             'mid': _killed_conv('mu', 'x' * 500),
             'heavy': _killed_conv('hu', 'x' * 5000)}
    db = _FakeDB(convs)
    dispatched, deferred_seen = [], []
    _patch_bounded(db, dispatched, live=0, capture_deferred=deferred_seen)
    summary = kr.run_killed_recovery(['heavy', 'light', 'mid'], storm=False)
    assert summary['redispatched'] == 1, summary
    assert summary['deferred'] == 2, summary
    assert dispatched == ['light'], dispatched          # lightest went inline
    assert deferred_seen == ['mid', 'heavy'], deferred_seen  # heaviest deferred LAST
    print('OK boot ramp: 1 inline (lightest), 2 deferred (heaviest last)')


def test_deferred_convs_burn_no_attempt_until_dispatched():
    """A deferred conv must NOT have its attempt counter advanced at classify."""
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_INLINE_BOOT_DISPATCH = 1
    convs = {'a': _killed_conv('au', 'x'),
             'b': _killed_conv('bu', 'x' * 100),
             'c': _killed_conv('cu', 'x' * 200)}
    db = _FakeDB(convs)
    dispatched, deferred_seen = [], []
    _patch_bounded(db, dispatched, live=0, capture_deferred=deferred_seen)
    kr.run_killed_recovery(['a', 'b', 'c'], storm=False)
    # Inline conv 'a' burned its attempt; deferred 'b','c' must NOT have yet.
    assert convs['a']['settings'].get('_killedRecovery', {}).get('attempts') == 1
    assert '_killedRecovery' not in convs['b']['settings'], convs['b']['settings']
    assert '_killedRecovery' not in convs['c']['settings'], convs['c']['settings']
    print('OK deferred convs burn no attempt until dispatched')


def test_drain_dispatches_remainder_as_slots_free():
    """The drain daemon fires the deferred queue when carriers are under cap."""
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_DRAIN_POLL_SECS = 0.02
    convs = {'b': _killed_conv('bu', 'x' * 100),
             'c': _killed_conv('cu', 'x' * 200)}
    db = _FakeDB(convs)
    dispatched = []
    _patch_bounded(db, dispatched, live=0)   # under cap → drains immediately
    kr._drain_deferred(['b', 'c'], storm=False)   # run synchronously
    assert sorted(dispatched) == ['b', 'c'], dispatched
    # Each drained conv burned exactly one attempt (dispatched for real).
    assert convs['b']['settings']['_killedRecovery']['attempts'] == 1
    assert convs['c']['settings']['_killedRecovery']['attempts'] == 1
    print('OK drain: remainder dispatched as slots free')


def test_drain_waits_while_carriers_at_cap():
    """When live carriers == cap the drain must NOT dispatch (waits for a slot)."""
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_DRAIN_POLL_SECS = 0.02
    convs = {'b': _killed_conv('bu', 'x' * 100)}
    db = _FakeDB(convs)
    dispatched = []
    _patch_bounded(db, dispatched, live=2)   # at cap → no slot
    import threading
    queue = ['b']                              # shared ref → clear it to stop the loop
    th = threading.Thread(target=kr._drain_deferred,
                          kwargs={'deferred': queue, 'storm': False}, daemon=True)
    th.start()
    th.join(timeout=0.3)                      # give it a few poll cycles
    assert dispatched == [], dispatched        # nothing dispatched while at cap
    assert th.is_alive()                       # still waiting (queue not drained)
    # Terminate the thread cleanly so it can't leak into a later test: emptying
    # the shared queue makes the `while deferred:` loop condition false on its
    # next poll (poll interval was set to 0.02s above).
    queue.clear()
    th.join(timeout=0.5)
    assert not th.is_alive(), 'drain thread did not exit after queue cleared'
    print('OK drain: holds off while carriers at cap, then exits when drained')


def test_storm_holds_all_and_spawns_no_drain():
    """A restart storm stands the whole mechanism down — no inline, no deferred."""
    convs = {'a': _killed_conv('au', 'x'), 'b': _killed_conv('bu', 'x' * 100)}
    db = _FakeDB(convs)
    dispatched, deferred_seen = [], []
    _patch_bounded(db, dispatched, live=0, capture_deferred=deferred_seen)
    summary = kr.run_killed_recovery(['a', 'b'], storm=True)
    assert summary['storm_held'] == 2, summary
    assert summary['redispatched'] == 0 and summary['deferred'] == 0, summary
    assert dispatched == [] and deferred_seen == [], (dispatched, deferred_seen)
    # No attempt burned on either.
    assert '_killedRecovery' not in convs['a']['settings']
    assert '_killedRecovery' not in convs['b']['settings']
    print('OK storm: holds all, no drain spawned, no attempt burned')


# ── SHUTDOWN GATE: the drain daemon must stop on shutdown ─────────────
# The FIRST line of the crash cascade was the drain daemon (`_drain_deferred`)
# calling get_thread_db / _dispatch_one AFTER shutdown began, while PG was being
# stopped: `[killed-recovery-drain] drain dispatch failed … FATAL: the database
# system is shutting down`. quiesce only marks EXISTING running tasks; it does
# NOT stop the daemon from spawning a FRESH carrier + touching PG. A stop_event
# threaded through gates every dispatch/DB touch.

def test_drain_stops_immediately_when_shutdown_set():
    """A pre-set stop_event → the drain dispatches NOTHING, touches no DB."""
    import threading
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_DRAIN_POLL_SECS = 0.02
    convs = {'b': _killed_conv('bu', 'x' * 100), 'c': _killed_conv('cu', 'x' * 200)}
    db = _FakeDB(convs)
    dispatched = []
    _patch_bounded(db, dispatched, live=0)   # under cap → WOULD drain if not gated
    # Trip a DB tripwire so ANY get_thread_db call after shutdown fails the test.
    touched = {'db': 0}
    _orig_getdb = kr.get_thread_db
    def _tripwire_db(*a, **k):
        touched['db'] += 1
        return _orig_getdb(*a, **k)
    kr.get_thread_db = _tripwire_db  # type: ignore

    stop = threading.Event()
    stop.set()                               # shutdown already requested
    kr._drain_deferred(['b', 'c'], storm=False, stop_event=stop)
    assert dispatched == [], f'gated drain must dispatch nothing, got {dispatched}'
    assert touched['db'] == 0, f'gated drain must not touch the DB, got {touched["db"]}'
    # No attempt burned on either conv (never dispatched).
    assert '_killedRecovery' not in convs['b']['settings']
    assert '_killedRecovery' not in convs['c']['settings']
    print('OK drain: shutdown set → zero dispatch, zero DB, no attempt burned')


def test_NC_neutered_gate_dispatches_despite_shutdown():
    """NC (neuter the gate): with _stop_requested forced to always-False the
    drain dispatches EVEN WITH stop_event set — reproducing the cascade. Proves
    the gate check is what stops it, not something incidental. Restores after."""
    import threading
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_DRAIN_POLL_SECS = 0.02
    convs = {'b': _killed_conv('bu', 'x' * 100), 'c': _killed_conv('cu', 'x' * 200)}
    db = _FakeDB(convs)
    dispatched = []
    _patch_bounded(db, dispatched, live=0)
    _orig = kr._stop_requested
    kr._stop_requested = lambda _ev: False   # neuter the gate (byte-revert equiv)
    try:
        stop = threading.Event(); stop.set()   # shutdown IS requested…
        kr._drain_deferred(['b', 'c'], storm=False, stop_event=stop)
        assert sorted(dispatched) == ['b', 'c'], dispatched  # …but neutered → drains
    finally:
        kr._stop_requested = _orig
    # And with the REAL gate restored + shutdown set, it dispatches nothing.
    dispatched2 = []
    _patch_bounded(_FakeDB({'b': _killed_conv('bu', 'x' * 100)}), dispatched2, live=0)
    stop2 = threading.Event(); stop2.set()
    kr._drain_deferred(['b'], storm=False, stop_event=stop2)
    assert dispatched2 == [], dispatched2
    print('OK NC: neutered gate drains despite shutdown; real gate stops it')


def test_run_killed_recovery_skips_inline_when_shutdown_set():
    """run_killed_recovery with a set stop_event → no inline dispatch, no daemon."""
    import threading
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_INLINE_BOOT_DISPATCH = 1
    convs = {'a': _killed_conv('au', 'x'), 'b': _killed_conv('bu', 'x' * 100)}
    db = _FakeDB(convs)
    dispatched, deferred_seen = [], []
    _patch_bounded(db, dispatched, live=0, capture_deferred=deferred_seen)
    stop = threading.Event(); stop.set()
    summary = kr.run_killed_recovery(['a', 'b'], storm=False, stop_event=stop)
    assert dispatched == [], dispatched
    assert deferred_seen == [], deferred_seen
    assert summary['redispatched'] == 0, summary
    print('OK run_killed_recovery: shutdown set → nothing dispatched, no daemon')


def test_drain_stops_mid_queue_when_shutdown_arrives():
    """stop_event set AFTER the first dispatch → the drain stops before the 2nd."""
    import threading
    kr.KILLED_RECOVERY_MAX_CONCURRENT = 2
    kr.KILLED_RECOVERY_DRAIN_POLL_SECS = 0.02
    convs = {'b': _killed_conv('bu', 'x' * 100), 'c': _killed_conv('cu', 'x' * 200)}
    db = _FakeDB(convs)
    dispatched = []
    stop = threading.Event()
    kr.get_thread_db = lambda *_a, **_k: db  # type: ignore
    kr._conv_has_live_task = lambda conv_id: False  # type: ignore
    kr._count_live_killed_carriers = lambda: 0  # type: ignore
    # After the first real dispatch, trip shutdown so the loop's next check stops.
    def _redispatch(conv_id):
        dispatched.append(conv_id)
        stop.set()
        return f'task-{conv_id}'
    kr._redispatch_conv = _redispatch  # type: ignore
    kr._drain_deferred(['b', 'c'], storm=False, stop_event=stop)
    assert dispatched == ['b'], f'must stop after first dispatch, got {dispatched}'
    print('OK drain: shutdown mid-queue → stops before the next dispatch')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_killed_recovery.__main__')
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        # restore patched module globals between run_* tests
        import importlib
        importlib.reload(kr)
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            print('FAIL %s: %s' % (fn.__name__, e))
            traceback.print_exc()
    print('\n%d/%d passed' % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
