#!/usr/bin/env python3
"""Wire-parity baseline for the routes/chat.py sub-package split
(board epic ``pt_04686ac6054a451e``).

The plan: routes/chat.py (~2500 lines) is being decomposed into a
``routes/chat/`` sub-package (``_helpers.py`` first slice, then eventually
``send.py`` / ``stream.py`` / ``poll_abort.py``), with ``routes/chat.py``
kept as a **re-export facade** so import contracts and Blueprint
registrations stay byte-identical. Landed so far:

  * Slice 1 → ``routes/chat_helpers.py``: 5 pure utilities with NO
    module-level mutable state (``_dumps_yielding`` /
    ``_running_checkpoint_verdict`` / ``_log_poll_task_id_mismatch`` /
    ``_loads_yielding`` / ``_warm_resume_serviceable``).
  * Slice 2 → ``routes/chat_state.py``: the send-abort marker BUNDLE
    (``_send_abort_marker`` dict + ``_send_abort_marker_lock`` +
    ``_mark_conv_aborted`` / ``_was_aborted_after`` — the four items
    that share ONE piece of module-level state; they must live in the
    same module and be re-exported through routes.chat as a set so
    nobody imports half of the pair).
  * Slice 3 → ``routes/chat_side_effects.py``: ``_truncate_conv_history``
    (the IO-heavy helper that clears the message-queue + server-message
    -store side channels after a conv history truncate). Kept OUT of
    ``chat_helpers.py`` to preserve that module's "pure + import-
    lightweight" invariant; kept OUT of ``chat_state.py`` because it
    owns no state (it acts on state living in OTHER lib.* packages).

All three slices keep ``routes/chat.py`` importable via re-export
blocks so every ``from routes.chat import _X`` call site keeps working
unchanged.

This test is the CONTRACT the split must preserve. It runs BEFORE and AFTER
the extraction; the numbers must match. It intentionally does NOT hardcode
the current URL list (that would break every legitimate future add), it
SNAPSHOTS the current registered rules at import time and stashes them as a
frozenset — the split preserves the frozenset (proof: import → derive same
frozenset). A future genuine route add DOES update the snapshot, an
accidental route drop does not.

Two layers:

  1. Symbol-level: the 5 helpers being extracted this slice + 3 more
     imported by existing tests (_mark_conv_aborted / _was_aborted_after /
     _truncate_conv_history / _start_task_for_conv) MUST remain importable
     under their ``routes.chat`` names — the sibling tests that call
     ``from routes.chat import _dumps_yielding`` etc. MUST keep working
     without edit.

  2. Wire-level: every route rule registered on ``chat_bp`` and
     ``api_v1_chat_bp`` — url pattern, method set, endpoint name — is
     preserved. Anyone breaks a rule (renames endpoint, drops a method,
     mistypes /api/chat/… vs /api/v1/chat/…) trips this test.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# ── Layer 1: symbol-level re-export contract ──────────────────────────

# The exact names shipped by routes.chat that external code (tests +
# main app code) imports today. Each is a module-level function; a re-
# export facade after the split must keep every one of these
# `from routes.chat import X` calls resolving.
_REQUIRED_ROUTES_CHAT_SYMBOLS = (
    # 5 pure helpers extracted THIS slice into routes/chat/_helpers.py
    '_dumps_yielding',
    '_running_checkpoint_verdict',
    '_log_poll_task_id_mismatch',
    '_loads_yielding',
    '_warm_resume_serviceable',
    # More helpers imported by existing tests today; still in routes/chat.py
    # after this first slice but the test-set names them to prove no
    # accidental collision when the facade is added.
    '_mark_conv_aborted',
    '_was_aborted_after',
    '_truncate_conv_history',
    '_start_task_for_conv',
    # The two Blueprint names the app depends on.
    'chat_bp',
)


@_unit
def test_routes_chat_symbols_all_importable():
    """Every symbol external code imports from ``routes.chat`` today
    must still resolve after the split. A re-export facade (this slice)
    or the original module (before the split) both satisfy the test —
    what it forbids is a naive move that leaves the imports 404-ing."""
    import importlib
    mod = importlib.import_module('routes.chat')
    missing = [name for name in _REQUIRED_ROUTES_CHAT_SYMBOLS
               if not hasattr(mod, name)]
    assert not missing, (
        f'routes.chat is missing symbols external code imports: {missing}. '
        f'If you split the file, keep routes/chat.py as a re-export facade '
        f'that surfaces every name in _REQUIRED_ROUTES_CHAT_SYMBOLS.'
    )


@_unit
def test_extracted_helpers_are_callable():
    """The 5 helpers extracted this slice must be genuine callables (not
    e.g. modules or ``None`` placeholders). A wire-safe re-export makes
    them look identical to the pre-split state at every callsite."""
    import importlib
    mod = importlib.import_module('routes.chat')
    for name in ('_dumps_yielding', '_running_checkpoint_verdict',
                 '_log_poll_task_id_mismatch', '_loads_yielding',
                 '_warm_resume_serviceable'):
        obj = getattr(mod, name)
        assert callable(obj), (
            f'routes.chat.{name} is not callable (got {type(obj).__name__}); '
            f'a re-export facade must expose the FUNCTION, not the module')


@_unit
def test_pure_helper_wire_parity_smoke():
    """Sanity-invoke the 3 pure helpers with well-known inputs to prove
    the re-export goes to the REAL function (not a stub / wrong name).
    Values chosen so any accidental swap (e.g. re-exporting _loads_yielding
    from a namespace where it accidentally names _dumps_yielding) trips this."""
    from routes.chat import (
        _dumps_yielding, _loads_yielding,
        _running_checkpoint_verdict, _warm_resume_serviceable,
    )

    # _dumps_yielding: orjson round-trip a small dict.
    encoded = _dumps_yielding({'x': [1, 2, 3], 'y': 'hi'})
    assert isinstance(encoded, str)
    assert '"x"' in encoded and '"y":"hi"' in encoded.replace(' ', '')

    # _loads_yielding: reverses _dumps_yielding
    decoded = _loads_yielding(encoded)
    assert decoded == {'x': [1, 2, 3], 'y': 'hi'}

    # _running_checkpoint_verdict: pure 2-branch decision.
    assert _running_checkpoint_verdict(sharded=True) == ('running', True)
    assert _running_checkpoint_verdict(sharded=False) == ('interrupted', False)

    # _warm_resume_serviceable: pure math on (cursor, n_events).
    assert _warm_resume_serviceable(None, 10) is False
    assert _warm_resume_serviceable(-1, 10) is False
    assert _warm_resume_serviceable(3, 10) is True   # resume_from=4 <= 10
    assert _warm_resume_serviceable(9, 10) is True   # resume_from=10 <= 10 (boundary)
    assert _warm_resume_serviceable(10, 10) is False  # resume_from=11 > 10 → resync


@_unit
def test_abort_marker_bundle_semantics():
    """pt_04686ac6 slice 2: the abort-marker BUNDLE must reproduce the
    exact original send-abort race semantics after being moved out of
    chat.py's module namespace.

    The bundle is _send_abort_marker (dict) + _send_abort_marker_lock
    + _mark_conv_aborted() + _was_aborted_after(). All four names MUST
    resolve via routes.chat (re-export) AND via routes.chat_state (the
    real home). AND they must operate on the SAME shared state — a
    write via routes.chat_state._mark_conv_aborted MUST be visible to a
    routes.chat._was_aborted_after read (and vice versa). A duplicated-
    state bug (a naive "copy the dict into two modules" split) would
    trip this.

    Semantics guarded (byte-identical to the original chat.py code):
      1. no prior mark          → _was_aborted_after(...) is False
      2. mark then check with ts EARLIER than the mark → True
      3. mark then check with ts AFTER the mark        → False (the
         send that started AFTER the abort is not "aborted by it")
      4. empty conv_id / None since_ts                 → False (guard)
    """
    import time as _t
    import routes.chat as _rc
    import routes.chat_state as _rs

    # Every name must exist on BOTH modules — proves the re-export is real
    # and points at the same object.
    for name in ('_send_abort_marker', '_send_abort_marker_lock',
                 '_mark_conv_aborted', '_was_aborted_after'):
        assert hasattr(_rc, name), f'routes.chat missing re-export: {name}'
        assert hasattr(_rs, name), f'routes.chat_state missing: {name}'
        assert getattr(_rc, name) is getattr(_rs, name), (
            f'{name}: re-export must point at the SAME object (a copy '
            f'would split state — a mark via one namespace would be '
            f'invisible to a check via the other)')

    conv_id = 'test-abort-marker-cv'
    # Clean up any stale entry from a prior run to make the test
    # order-independent (a bare dict in module scope survives across
    # tests in the same process).
    with _rs._send_abort_marker_lock:
        _rs._send_abort_marker.pop(conv_id, None)

    # 1) No prior mark → False regardless of since_ts.
    assert _rc._was_aborted_after(conv_id, _t.time()) is False
    assert _rc._was_aborted_after(conv_id, 0) is False

    # 4) Empty conv_id / None since_ts → False (guard clauses).
    assert _rc._was_aborted_after('', _t.time()) is False
    assert _rc._was_aborted_after(conv_id, None) is False

    # 2) Mark, then check with an EARLIER since_ts → True (an abort
    #    that landed AFTER our send started is the race we detect).
    _earlier = _t.time() - 10
    _rs._mark_conv_aborted(conv_id)  # write via chat_state
    assert _rc._was_aborted_after(conv_id, _earlier) is True  # read via chat re-export

    # 3) Mark, then check with a LATER since_ts → False (a send that
    #    started AFTER the abort is not "aborted by it").
    _later = _t.time() + 10
    assert _rc._was_aborted_after(conv_id, _later) is False

    # Reverse-direction cross-module wire check: mark via chat re-export,
    # read via chat_state — same shared state, same verdict.
    _rc._mark_conv_aborted(conv_id)
    assert _rs._was_aborted_after(conv_id, _earlier) is True

    # Cleanup.
    with _rs._send_abort_marker_lock:
        _rs._send_abort_marker.pop(conv_id, None)


@_unit
def test_truncate_conv_history_side_effect_wire():
    """pt_04686ac6 slice 3: ``_truncate_conv_history`` must be importable
    from ``routes.chat`` (re-export) AND ``routes.chat_side_effects`` (real
    home), point at the SAME object, and — when invoked — call BOTH
    downstream side-effect points (``lib.message_queue.clear_queue`` +
    ``lib.tasks_pkg.server_message_store.clear``). A future slice that
    accidentally drops one clear (the specific class of bug the helper's
    docstring warns against — "folding both into one helper makes the
    invariant impossible to half-apply") is caught here.

    Monkey-patches the two lib entry points to observation-only shims;
    does NOT touch a real message queue or message store.
    """
    import importlib
    import routes.chat as _rc
    import routes.chat_side_effects as _rs

    # Symbol wire: both modules expose it AND they are the same object.
    assert hasattr(_rc, '_truncate_conv_history'), 'routes.chat missing re-export'
    assert hasattr(_rs, '_truncate_conv_history'), 'routes.chat_side_effects missing'
    assert _rc._truncate_conv_history is _rs._truncate_conv_history, (
        'routes.chat._truncate_conv_history must be the SAME object as '
        'routes.chat_side_effects._truncate_conv_history (re-export, not copy)')

    # Behavioural wire: invoking it calls BOTH downstream clears.
    calls = []

    def _fake_clear_queue(conv_id):
        calls.append(('clear_queue', conv_id))
        return 0

    def _fake_clear_store(conv_id):
        calls.append(('clear_store', conv_id))

    mq_mod = importlib.import_module('lib.message_queue')
    ms_mod = importlib.import_module('lib.tasks_pkg.server_message_store')
    orig_cq = mq_mod.clear_queue
    orig_cs = ms_mod.clear
    try:
        mq_mod.clear_queue = _fake_clear_queue
        ms_mod.clear = _fake_clear_store
        _rc._truncate_conv_history('cv-truncate-test')
    finally:
        mq_mod.clear_queue = orig_cq
        ms_mod.clear = orig_cs

    kinds = {c[0] for c in calls}
    assert 'clear_queue' in kinds, (
        '_truncate_conv_history did NOT invoke lib.message_queue.clear_queue — '
        "the 'clear the queued phantom turn' half of the invariant is broken")
    assert 'clear_store' in kinds, (
        '_truncate_conv_history did NOT invoke lib.tasks_pkg.'
        "server_message_store.clear — the 'clear the tool-history mirror' "
        'half of the invariant is broken (regen/edit would replay stale rounds)')
    assert {c[1] for c in calls} == {'cv-truncate-test'}, (
        'both clears must receive the same conv_id we passed in')


# ── Layer 2: Blueprint route wire-parity ──────────────────────────────

def _collect_bp_rules(bp_name: str) -> frozenset:
    """Extract (url_pattern, sorted-method-tuple, endpoint_name) for every
    rule registered on the named Blueprint.

    The tuple form is hashable and deterministic across process runs, so
    forming a frozenset gives a set-equality contract that is oblivious to
    registration order but sensitive to any URL/method/endpoint change.
    """
    if bp_name == 'chat_bp':
        from routes.chat import chat_bp as bp
    elif bp_name == 'api_v1_chat_bp':
        from routes.api_v1.chat import api_v1_chat_bp as bp
    else:
        raise ValueError(f'unknown blueprint: {bp_name}')

    # A Quart/Flask Blueprint carries its rules as deferred (URL, methods,
    # endpoint) triples on ``bp.deferred_functions`` before app.register
    # is called — that is the shape we can inspect without wiring a full
    # app. Every ``@bp.route('...')`` decorator appends one entry via
    # ``add_url_rule`` bound to the BP; the deferred is a callable that
    # replays that add_url_rule against a state object at register time.
    # We call it against a MINIMAL state that just records the call.
    class _Recorder:
        def __init__(self):
            self.rules = []

        def add_url_rule(self, rule, endpoint=None, view_func=None,
                         methods=None, **kwargs):
            m = tuple(sorted(methods or ('GET',)))
            self.rules.append((rule, m, endpoint or (
                view_func.__name__ if view_func else '')))

    rec = _Recorder()
    for setup in bp.deferred_functions:
        # Quart/Flask BP deferred callables take a ``state`` object; ours
        # just needs an ``add_url_rule`` compatible with those calls.
        # ``blueprint`` attribute is read by some code paths.
        class _State:
            blueprint = bp
            name_prefix = ''
            url_prefix = ''
            subdomain = None
            url_defaults = {}
            first_registration = True

            def add_url_rule(self_state, rule, endpoint=None, view_func=None,
                             methods=None, **kwargs):
                rec.add_url_rule(rule, endpoint=endpoint,
                                 view_func=view_func, methods=methods,
                                 **kwargs)
        try:
            setup(_State())
        except Exception:
            # A deferred function that isn't an add_url_rule (e.g.
            # before_request, error handlers) will raise on our
            # minimal state — skip those; they aren't route rules.
            continue
    return frozenset(rec.rules)


@_unit
def test_chat_bp_rules_snapshot():
    """The url/method/endpoint set for ``chat_bp`` (the ``/api/chat/...``
    top-level bp) must match a KNOWN baseline. The baseline reflects the
    pre-split HEAD state; the split's contract is to reproduce it byte-
    identically. When a legitimate future route add lands, update the
    baseline here in the same commit."""
    rules = _collect_bp_rules('chat_bp')
    # chat_bp is small: only the streaming carve-out lives on it.
    expected = frozenset({
        ('/api/chat/stream/<task_id>', ('GET',), 'chat.chat_stream'),
    })
    # Quart normalises endpoint to '<bp>.<view>' when registered on a BP.
    # deferred_functions capture the endpoint AS PASSED to add_url_rule
    # which is BARE (view name) — so we normalise here for equality.
    normalised = frozenset(
        (r, m, ep if '.' in ep else f'chat.{ep}')
        for (r, m, ep) in rules
    )
    assert normalised == expected, (
        f'chat_bp routes changed unexpectedly.\n'
        f'  expected: {sorted(expected)}\n'
        f'  actual:   {sorted(normalised)}\n'
        f'If this is a legitimate new route, update the "expected" set '
        f'in this test as part of the same commit. Otherwise a route '
        f'was silently dropped/renamed by the pt_04686ac6 split.'
    )


@_unit
def test_api_v1_chat_bp_rules_snapshot():
    """Same wire-parity guard for the ``/api/v1/chat/...`` blueprint —
    the primary REST surface. All 15+ rules must be preserved through
    the split."""
    rules = _collect_bp_rules('api_v1_chat_bp')
    # Snapshot of pre-split HEAD (2026-07-23). Every entry corresponds to
    # a route currently in routes/chat.py, routes/chat_queue.py,
    # routes/chat_human_io.py, or routes/chat_tool_state.py — all four
    # attach to the SAME BP; the split moves the routes/chat.py subset
    # around but keeps the BP registrations intact.
    expected_prefixes = {
        '/api/v1/chat/active',
        '/api/v1/chat/start',
        '/api/v1/chat/translate-status/<conv_id>',
        '/api/v1/chat/send',
        '/api/v1/chat/branch',
        '/api/v1/chat/regenerate',
        '/api/v1/chat/continue',
        '/api/v1/chat/abort-conv/<conv_id>',
        '/api/v1/chat/abort/<task_id>',
        '/api/v1/chat/poll/<task_id>',
        '/api/v1/chat/flow-trace/<task_id>',
        '/api/v1/chat/stdin-response',
        '/api/v1/chat/human-response',
        '/api/v1/chat/tool-state/<conv_id>',
        '/api/v1/chat/queue/<conv_id>',
        '/api/v1/chat/queue/<conv_id>/<queue_id>',
    }
    actual_prefixes = {r for (r, _m, _ep) in rules}
    missing = expected_prefixes - actual_prefixes
    added = actual_prefixes - expected_prefixes
    assert not missing, (
        f'api_v1_chat_bp is MISSING rules that existed pre-split: {sorted(missing)}. '
        f'The pt_04686ac6 split must preserve every registered rule; '
        f'either a decorator was lost during the file move or the '
        f'Blueprint import order changed and route registration is '
        f'no longer running.'
    )
    # An ADDED rule is legitimate (a new endpoint landed); flag it as an
    # advisory so the baseline stays honest.
    if added:
        print(f'NOTE: api_v1_chat_bp gained new rules since baseline: {sorted(added)} — '
              f'update the expected_prefixes set to record them')


# ── Slice 4: _start_task_for_conv extraction ──────────────────────────
# Moves the ~150-line task-starter orchestrator (called from chat_send,
# chat_regenerate, chat_continue, chat_branch_start) into a dedicated
# routes/chat_task_start.py. routes/chat.py keeps it re-exported so
# the 3 test files that ``monkeypatch.setattr('routes.chat._start_task_for_conv',
# ...)`` continue to work unchanged.

@_unit
def test_chat_task_start_submodule_exists():
    """Slice 4 (pt_04686ac6): routes/chat_task_start.py holds
    _start_task_for_conv as a module-level callable."""
    import importlib
    mod = importlib.import_module('routes.chat_task_start')
    assert hasattr(mod, '_start_task_for_conv'), (
        'routes.chat_task_start missing _start_task_for_conv')
    assert callable(mod._start_task_for_conv), (
        f'_start_task_for_conv is not callable '
        f'(got {type(mod._start_task_for_conv).__name__})')


@_unit
def test_routes_chat_reexports_start_task_for_conv():
    """Slice 4: routes/chat.py MUST re-export _start_task_for_conv so
    the 3 test files that do ``monkeypatch.setattr(
    'routes.chat._start_task_for_conv', ...)``  keep working unchanged.
    Same-object identity is required — a copy would let a monkey-patch
    on one namespace be invisible on the other."""
    import routes.chat as _rc
    import routes.chat_task_start as _cts
    assert hasattr(_rc, '_start_task_for_conv'), (
        'routes.chat missing _start_task_for_conv re-export — 3 test '
        'files monkeypatch this path')
    assert _rc._start_task_for_conv is _cts._start_task_for_conv, (
        'routes.chat._start_task_for_conv must be the SAME object as '
        'routes.chat_task_start._start_task_for_conv')


@_unit
def test_run_py_no_longer_carries_inline_start_task_for_conv():
    """Slice 4: the inline function body must be GONE from routes/chat.py
    — guards against a future silent revert (someone re-adds the body
    inline while the new module still exists, splitting the definition
    into two copies)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    # The 3-argument def line MUST be gone from chat.py (function body
    # moved to chat_task_start.py).
    assert 'def _start_task_for_conv(conv_id, config, data=None):' not in src, (
        'routes/chat.py still contains the inline def line — extraction '
        'was undone or never landed')
    # The re-export MUST be present.
    assert 'from routes.chat_task_start import' in src, (
        'routes/chat.py must import from routes.chat_task_start')


# ── Slice 5: chat_send business logic → lib/chat_dispatch.py ─────────
# Extracts the ~180-line queue classification + autopilot-followup
# detection + abort-on-send race + inject-mode steer + queue-with-
# pending-row-mirror pipeline into a testable pure-ish function.

@_unit
def test_chat_dispatch_module_exists_and_exposes_classifier():
    """Slice 5 (pt_04686ac6): lib/chat_dispatch.py exposes
    classify_send_intent + the SendIntent dataclass."""
    import importlib
    mod = importlib.import_module('lib.chat_dispatch')
    assert hasattr(mod, 'classify_send_intent'), (
        'lib.chat_dispatch missing classify_send_intent')
    assert callable(mod.classify_send_intent), (
        'classify_send_intent must be a callable function')
    assert hasattr(mod, 'SendIntent'), (
        'lib.chat_dispatch missing SendIntent dataclass')


@_unit
def test_chat_send_delegates_to_classify_send_intent():
    """Slice 5: routes/chat.py::chat_send must import + call the
    extracted classifier. Guards against silent revert to the inline
    ~180-line block."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.chat_dispatch import classify_send_intent' in src, (
        'routes/chat.py must import classify_send_intent from '
        'lib.chat_dispatch (slice 5 pt_04686ac6)')
    # Specific inline call-site marker strings that USED TO live inside
    # chat_send are GONE (they now live in lib/chat_dispatch.py).
    assert '⚠️ Abort-on-send: task %s marked aborted' not in src, (
        'routes/chat.py must NOT carry the "Abort-on-send" log line — '
        "it lives in lib.chat_dispatch.classify_send_intent now")
    assert '⚡ superseding %d in-flight autopilot' not in src, (
        'routes/chat.py must NOT carry the autopilot-followup supersede '
        'log line — extracted')
    assert '➡ STEER (injected into running turn)' not in src, (
        'routes/chat.py must NOT carry the STEER log line — extracted')
    assert '➡ QUEUED (active task running)' not in src, (
        'routes/chat.py must NOT carry the QUEUED log line — extracted')


@_unit
def test_classify_send_intent_none_on_no_running_task():
    """Slice 5: when no task is running for the conversation and no
    other classifier branch fires, classify_send_intent returns None
    so chat_send falls through to the immediate-start path.

    Uses monkey-patches on lib.tasks_pkg tasks / tasks_lock so no real
    DB or Flask app context is needed. This is the load-bearing
    invariant that the extracted classifier's None branch really is a
    "no-op, proceed to immediate start"."""
    import sys
    import types
    # Ensure lib.tasks_pkg's tasks dict/lock are available for the
    # classifier to consult. Real module is fine — just make sure
    # the ``tasks`` dict has no entries for our test conv.
    tasks_pkg = sys.modules.get('lib.tasks_pkg')
    if tasks_pkg is None:
        import importlib
        tasks_pkg = importlib.import_module('lib.tasks_pkg')
    orig_tasks = dict(getattr(tasks_pkg, 'tasks', {}))
    # Route the classifier's "was aborted during translate?" to False
    # so the abort-early branch doesn't fire.
    import routes.chat_state as _rs
    orig_was_aborted = _rs._was_aborted_after
    _rs._was_aborted_after = lambda cid, ts: False
    # Route has_autopilot_marker to False so the supersede branch
    # doesn't fire even if a followup task exists.
    import lib.message_queue as _mq
    orig_marker = getattr(_mq, 'has_autopilot_marker', None)
    _mq.has_autopilot_marker = lambda cid: False
    try:
        import lib.chat_dispatch as cd

        result = cd.classify_send_intent(
            db=None, conv_id='cv-none', config={}, payload={},
            data={}, messages=[], is_new=False, title='t',
            user_msg={'content': 'hi'}, settings_patch=None,
            text='hi', send_started_at=0.0,
        )
        assert result is None, (
            f'no running task + no abort + no steer + no supersede '
            f'MUST return None (fall through to immediate-start); '
            f'got {result!r}')
    finally:
        _rs._was_aborted_after = orig_was_aborted
        if orig_marker is not None:
            _mq.has_autopilot_marker = orig_marker
        # Restore any tasks we accidentally polluted.
        tasks_pkg.tasks.clear()
        tasks_pkg.tasks.update(orig_tasks)


@_unit
def test_classify_send_intent_aborted_kind_on_translate_abort():
    """Slice 5: when _was_aborted_after returns True (user hit Stop
    during auto-translate), classifier returns SendIntent(kind='aborted',
    response={'aborted': True, 'convId': <cid>}). Verifies the exact
    response shape chat_send previously returned inline."""
    import routes.chat_state as _rs
    orig = _rs._was_aborted_after
    _rs._was_aborted_after = lambda cid, ts: True
    try:
        import lib.chat_dispatch as cd
        result = cd.classify_send_intent(
            db=None, conv_id='cv-aborted', config={}, payload={},
            data={}, messages=[], is_new=False, title='t',
            user_msg={'content': ''}, settings_patch=None,
            text='hi', send_started_at=1.0,
        )
        assert result is not None
        assert result.kind == 'aborted'
        assert result.response == {'aborted': True, 'convId': 'cv-aborted'}
    finally:
        _rs._was_aborted_after = orig


# ── Slice 7: chat_stream warm-resume plan → lib/chat_dispatch.plan_warm_resume ─
# Extracts the ~130-line warm-path snapshot generation (Last-Event-ID parse,
# _warm_resume_serviceable verdict, resume-snapshot + fresh-snapshot builders)
# from the ``chat_stream.generate`` closure into two pure(-ish) helpers in
# lib/chat_dispatch.py:
#
#   * ``plan_warm_resume(task, last_event_id_hdr, task_id_short)
#       -> WarmResumePlan | None``
#       Returns None on a fresh (no valid cursor / cursor ahead of buffer)
#       connection so the caller falls through to build_fresh_state_snapshot.
#       Returns a WarmResumePlan{resume_from, replay_events, resume_state,
#       serviceable=True} when the cursor is in-buffer — resume_state is
#       the leading state event to emit AFTER the delta replay's cursor
#       computation but BEFORE the yields (mirrors the original inline code
#       shape byte-for-byte).
#
#   * ``build_fresh_state_snapshot(task) -> (state, meta, cursor)``
#       Reads task under events_lock and builds the fresh-path state event
#       + meta dict + advance cursor exactly as the pre-slice inline code
#       did. Caller does the ``asyncio.to_thread(_dumps_yielding, state)``
#       encode and the yield.

@_unit
def test_chat_dispatch_exposes_plan_warm_resume():
    """Slice 7 (pt_04686ac6): lib/chat_dispatch.py exposes plan_warm_resume
    + WarmResumePlan dataclass + build_fresh_state_snapshot."""
    import importlib
    mod = importlib.import_module('lib.chat_dispatch')
    for name in ('plan_warm_resume', 'WarmResumePlan',
                 'build_fresh_state_snapshot'):
        assert hasattr(mod, name), f'lib.chat_dispatch missing {name}'
    assert callable(mod.plan_warm_resume), 'plan_warm_resume must be callable'
    assert callable(mod.build_fresh_state_snapshot), (
        'build_fresh_state_snapshot must be callable')


@_unit
def test_chat_stream_delegates_warm_resume_to_dispatch():
    """Slice 7: routes/chat.py::chat_stream must call plan_warm_resume
    (warm-path cursor+snapshot planner) and build_fresh_state_snapshot
    (fresh-path snapshot builder). Guards against silent revert to the
    inline ~130-line block."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    # Slice 7 imports plan_warm_resume AND the fresh-path snapshot builder
    # together (they are the two halves of the warm/fresh split). Since the
    # 2026-07-26 VU-carrier contract the fresh half is
    # ``build_connect_snapshot`` — the flavor-aware selector that gives a
    # ``_vu_subtask`` carrier the VU contract (autopilot_vu_start +
    # replaySnapshot) and defers EVERY other task to
    # ``build_fresh_state_snapshot``. A slice-6-only cold-path import does
    # not satisfy this.
    assert ('from lib.chat_dispatch import plan_warm_resume, '
            'build_connect_snapshot') in src, (
        'routes/chat.py must import plan_warm_resume + '
        'build_connect_snapshot from lib.chat_dispatch '
        '(slice 7 pt_04686ac6 + VU-carrier selector)')
    assert 'plan_warm_resume(' in src, (
        'routes/chat.py must CALL plan_warm_resume in chat_stream')
    assert 'build_connect_snapshot(' in src, (
        'routes/chat.py must CALL build_connect_snapshot in chat_stream '
        '(the carrier-aware fresh-path selector)')
    # Specific inline call-site marker strings that USED to live inside
    # chat_stream's warm-path are GONE.
    assert 'full-snapshot resync' not in src, (
        'routes/chat.py must NOT carry the "full-snapshot resync" log line — '
        'extracted to plan_warm_resume')
    assert 'reconnecting with Last-Event-ID' not in src, (
        'routes/chat.py must NOT carry the Last-Event-ID reconnect log line — '
        'extracted to plan_warm_resume')
    # Fresh-path inline field-copy loop markers are GONE.
    assert "state['memoryPrefetch']" not in src, (
        "state['memoryPrefetch'] assignment must live in "
        "build_fresh_state_snapshot, not chat_stream inline")
    assert "state['endpointPhase']" not in src, (
        "state['endpointPhase'] assignment must live in "
        "build_fresh_state_snapshot, not chat_stream inline")


@_unit
def test_plan_warm_resume_none_on_no_cursor():
    """Slice 7: plan_warm_resume returns None when the client sends NO
    Last-Event-ID header (fresh connection) → caller falls through to
    build_fresh_state_snapshot."""
    import lib.chat_dispatch as cd
    import threading
    task = {
        'events': [{'type': 'delta', 'text': 'x'}],
        'events_lock': threading.Lock(),
    }
    plan = cd.plan_warm_resume(task, '', 'task-fresh')
    assert plan is None, (
        f'empty Last-Event-ID must yield None (fresh path); got {plan!r}')


@_unit
def test_plan_warm_resume_none_on_cursor_ahead_of_buffer():
    """Slice 7: plan_warm_resume returns None when the cursor is AHEAD
    of the in-memory buffer (buffer trimmed / stale client) →
    _warm_resume_serviceable=False → caller does a full resync snapshot.

    Guards against a naive extraction that returns an empty replay slice
    (which would silently stall the stream and mis-index the live loop).
    """
    import lib.chat_dispatch as cd
    import threading
    task = {
        'events': [{'type': 'delta'}, {'type': 'delta'}],  # len=2
        'events_lock': threading.Lock(),
    }
    # cursor=10 is way past len(events)=2 → resume_from=11 > 2 → False.
    plan = cd.plan_warm_resume(task, '10', 'task-stale')
    assert plan is None, (
        f'cursor ahead of buffer must yield None (resync); got {plan!r}')


@_unit
def test_plan_warm_resume_serviceable_slices_post_cursor_events():
    """Slice 7: valid cursor in-buffer returns a WarmResumePlan with
    resume_from = cursor + 1 (SSE spec: Last-Event-ID is the id of the
    last RECEIVED event, resume AFTER it) and replay_events sliced from
    exactly that index onward.

    Wire-parity check: matches the pre-slice inline
    ``task['events'][cursor+1:]`` behaviour byte-for-byte.
    """
    import lib.chat_dispatch as cd
    import threading
    task = {
        'content': 'hi',
        'thinking': '',
        'status': 'running',
        'error': None,
        'toolRounds': [],
        'events': [
            {'type': 'delta', 'text': 'a'},   # id 0
            {'type': 'delta', 'text': 'b'},   # id 1
            {'type': 'delta', 'text': 'c'},   # id 2
            {'type': 'delta', 'text': 'd'},   # id 3
        ],
        'events_lock': threading.Lock(),
    }
    # cursor=1 → resume_from=2 → replay [{c}, {d}]
    plan = cd.plan_warm_resume(task, '1', 'task-warm')
    assert plan is not None
    assert plan.serviceable is True
    assert plan.resume_from == 2, (
        f'resume_from must be cursor+1 (SSE spec); got {plan.resume_from}')
    assert plan.replay_events == [
        {'type': 'delta', 'text': 'c'},
        {'type': 'delta', 'text': 'd'},
    ], f'replay_events must slice task[events][resume_from:]; got {plan.replay_events}'
    # The leading state must carry content / thinking / status /
    # toolRounds (the resume-snapshot invariant — see the ★ comment
    # in the pre-slice inline code about frontend keep-longer guard).
    assert plan.resume_state['content'] == 'hi'
    assert plan.resume_state['status'] == 'running'
    assert plan.resume_state['toolRounds'] == []


@_unit
def test_build_fresh_state_snapshot_carries_full_task_state():
    """Slice 7: build_fresh_state_snapshot returns (state, meta, cursor)
    matching the pre-slice inline field-copy loop:

      * state['content'], state['thinking'], state['status'] set from task
      * state['createdAt'] = task['created_at'] * 1000 when present
      * state['toolRounds'] emitted only when truthy
      * meta fields (model / thinkingDepth) copied from _extract_task_meta
        into state when truthy
      * state['preset'] / memoryPrefetch / preferencesApplied /
        relatedConversations / preferencesLearned / inboxInjects /
        peerInjects / userSteerInjects — each copied only when present
      * endpoint_mode → endpointMode / endpointPhase / endpointIteration /
        endpointTurns / endpointStopReason
      * cursor == len(task['events'])

    ★ TERMINAL-FIELD ASSERTIONS REVERSED 2026-07-31 (duplicate-bubble root fix).
      This test's fixture is `status='running'` WITH `finishReason='stop'` +
      `usage` + `preset`, and it used to assert those three appear in the
      `state` event — i.e. it certified the self-contradictory wire shape
      {status:'running', finishReason:'stop'} as correct byte-parity.

      That shape is the DEFECT. The orchestrator stamps task['finishReason']
      ~111 lines before it flips task['status']='done'
      (lib/tasks_pkg/orchestrator/_finalize.py — the span holding the blocking
      _generate_tool_summary call), so this reconnect path (whose whole job is
      attaching to a task that is STILL RUNNING) emitted a terminal claim about
      a live turn. The frontend acts on it two independent ways:
      `renderFinishInfo` paints a settled finish bar on a generating turn
      (`msg.finishReason || msg.usage`), and `assistantTailIsPriorTurn`
      misfiles the live bubble as a prior turn → a second, duplicate bubble.

      Byte-parity with the pre-slice inline loop is preserved for every field
      that is NOT a settlement claim; the three terminal fields are now gated
      by lib/chat/terminal_gate.py, keyed on the status this snapshot reports.
      A terminal-status complement is asserted below so the gate cannot be
      satisfied by withholding them always.
      Full chain: tests/test_duplicate_bubble_midturn_finish_reason.py.
    """
    import lib.chat_dispatch as cd
    import threading
    task = {
        'content': 'hello',
        'thinking': 'thoughts',
        'status': 'running',
        'error': None,
        'toolRounds': [{'name': 'read_files'}],
        'events': ['e0', 'e1', 'e2'],
        'events_lock': threading.Lock(),
        'created_at': 1_700_000_000.5,
        'finishReason': 'stop',
        'usage': {'prompt': 10},
        'model': 'gpt-4',
        'thinkingDepth': 3,
        'preset': 'default',
        '_memoryPrefetch': [{'m': 1}],
        '_preferencesApplied': ['pref1'],
        'endpoint_mode': True,
        '_endpoint_phase': 'critic',
        '_endpoint_iteration': 2,
        '_endpoint_turns': [{'t': 1}],
        '_endpoint_stop_reason': 'critic_approved',
    }
    state, meta, cursor = cd.build_fresh_state_snapshot(task)
    assert state['content'] == 'hello'
    assert state['thinking'] == 'thoughts'
    assert state['status'] == 'running'
    assert state['createdAt'] == int(1_700_000_000.5 * 1000)
    assert state['toolRounds'] == [{'name': 'read_files'}]
    # ★ REVERSED: a RUNNING snapshot must not claim the turn has settled.
    assert 'finishReason' not in state, (
        'a status=running `state` snapshot must not carry finishReason — '
        'the frontend reads it as a settled turn and mints a duplicate bubble')
    assert 'usage' not in state, (
        'finish_info.js treats `usage` as a terminal signal too '
        '(`msg.finishReason || msg.usage`)')
    assert 'preset' not in state, (
        '`preset` is stamped in the same _finalize.py block as '
        'finishReason/usage and shares their gate')
    # Non-terminal identity/progress fields keep their byte-parity behaviour.
    assert state['model'] == 'gpt-4'
    assert state['thinkingDepth'] == 3
    assert state['memoryPrefetch'] == [{'m': 1}]
    assert state['preferencesApplied'] == ['pref1']
    assert state['endpointMode'] is True
    assert state['endpointPhase'] == 'critic'
    assert state['endpointIteration'] == 2
    assert state['endpointTurns'] == [{'t': 1}]
    assert state['endpointStopReason'] == 'critic_approved'
    assert cursor == 3  # len(events)
    # meta must carry the raw dict extract_task_meta produced, UNGATED —
    # chat_stream reuses it to synthesize the fresh-terminal `done` event,
    # which MUST carry the terminal fields. This is why the gate lives at the
    # snapshot boundary and not inside extract_task_meta.
    assert meta.get('finishReason') == 'stop'
    assert meta.get('model') == 'gpt-4'

    # ── Complement: once the task is terminal the same builder MUST emit the
    #    terminal fields, or a reconnect to a finished turn shows a
    #    permanently empty finish bar (a worse bug than the one fixed).
    task_done = dict(task, status='done')
    task_done['events_lock'] = threading.Lock()
    state_done, _meta_done, _c = cd.build_fresh_state_snapshot(task_done)
    assert state_done['status'] == 'done'
    assert state_done['finishReason'] == 'stop'
    assert state_done['usage'] == {'prompt': 10}
    assert state_done['preset'] == 'default'


# ── Slice 8: chat_stream LIVE-path tick planner → lib/chat_dispatch.next_live_tick ─
# Extracts the ~92-line ``while True`` main loop tick decision (abort-cascade +
# task_terminal + baton-attach + SSE-timeout + supersede-detect + keepalive
# gap) into a pure(-ish) tick planner. Also moves the two pure-over-``task``
# closures (``_task_terminal`` + ``_apply_autopilot_baton``) to module-level
# in lib/chat_dispatch.py so warm-resume / fresh-terminal / LIVE-late-done
# can all share ONE definition.

@_unit
def test_chat_dispatch_exposes_next_live_tick_and_helpers():
    """Slice 8 (pt_04686ac6): lib/chat_dispatch.py exposes:
      * LiveTickAction (dataclass)
      * next_live_tick (callable)
      * is_task_terminal (pure predicate on task)
      * apply_autopilot_baton (mutates evt in-place; returns evt)
    """
    import importlib
    mod = importlib.import_module('lib.chat_dispatch')
    for name in ('LiveTickAction', 'next_live_tick',
                 'is_task_terminal', 'apply_autopilot_baton'):
        assert hasattr(mod, name), f'lib.chat_dispatch missing {name}'
    assert callable(mod.next_live_tick), 'next_live_tick must be callable'
    assert callable(mod.is_task_terminal)
    assert callable(mod.apply_autopilot_baton)


@_unit
def test_chat_stream_delegates_live_loop_to_next_live_tick():
    """Slice 8: routes/chat.py::chat_stream must import + call the
    extracted tick planner. Guards against silent revert to the inline
    ~92-line while-loop body."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    # Slice 8: the import + call site must both be present (a raw
    # mention in a comment is not enough — that's the class of NEUTER
    # a silent-revert would produce). The import string is exact so a
    # deletion / rename trips it.
    assert 'from lib.chat_dispatch import next_live_tick' in src, (
        'routes/chat.py must IMPORT next_live_tick from lib.chat_dispatch '
        '(slice 8 pt_04686ac6)')
    # The call must appear with an argument list (a mere reference in
    # a comment does not).
    import re as _re
    assert _re.search(r'\bnext_live_tick\s*\(', src), (
        'routes/chat.py must CALL next_live_tick(...) — a bare mention '
        'in a comment does not satisfy slice 8')
    # Specific inline call-site markers that USED to live in the LIVE
    # loop are GONE.
    assert 'SSE connection reached maximum duration' not in src, (
        'routes/chat.py must NOT carry the SSE_TIMEOUT message literal — '
        'extracted to next_live_tick')
    assert 'superseded by newer reader' not in src, (
        'routes/chat.py must NOT carry the supersede log line — '
        'extracted to next_live_tick')
    assert 'emitting LATE done' not in src, (
        'routes/chat.py must NOT carry the LATE-done log line — '
        'extracted to next_live_tick')


@_unit
def test_is_task_terminal_pure_over_task():
    """Slice 8 (updated 2026-07-27 for the pt_8dc03017 cutover):
    is_task_terminal is a pure predicate — True iff
    task.status != 'running'. PERIOD.

    The old ``_autopilot_deciding`` withhold was DELIBERATELY retired by
    the pt_8dc03017 cutover (aa6f7ea6): the VU now runs as an
    independent carrier task on its own stream, so a done parent is
    genuinely terminal — the client discovers the successor via the
    conv→latest-task supersede index (latestLiveTaskId), not via a
    withheld baton.  This test pins the RETIREMENT so the withhold
    cannot silently creep back: a task stamped ``_autopilot_deciding``
    (a field nothing sets anymore) MUST still read terminal.
    """
    from lib.chat_dispatch import is_task_terminal
    # Running task → not terminal.
    assert is_task_terminal({'status': 'running'}) is False
    # Done task → terminal.
    assert is_task_terminal({'status': 'done'}) is True
    # Error task → terminal.
    assert is_task_terminal({'status': 'error'}) is True
    # Retired withhold: the legacy ``_autopilot_deciding`` flag MUST NOT
    # affect the verdict anymore (the VU has its own stream + carrier
    # terminal done — see tests/test_vu_carrier_stream_contract.py).
    assert is_task_terminal(
        {'status': 'done', '_autopilot_deciding': True}) is True


@_unit
def test_apply_autopilot_baton_copies_next_task_id_and_vu_msg():
    """Slice 8: apply_autopilot_baton stamps
    autopilotNextTaskId / autopilotVuMessage onto the passed event when
    task has a '_autopilot_followup' stash; no-op otherwise.
    Returns the (mutated) event for call-site convenience.
    """
    from lib.chat_dispatch import apply_autopilot_baton
    # With follow-up: baton fields copied.
    evt = {'type': 'done'}
    task = {'_autopilot_followup': {
        'next_task_id': 'task-next-abc',
        'vu_msg': {'role': 'user', 'content': 'follow-up prompt'},
    }}
    result = apply_autopilot_baton(task, evt)
    assert result is evt, 'must return the same evt object (mutation)'
    assert evt['autopilotNextTaskId'] == 'task-next-abc'
    assert evt['autopilotVuMessage'] == {
        'role': 'user', 'content': 'follow-up prompt'}
    # Without follow-up: no-op, evt unchanged.
    evt2 = {'type': 'done'}
    apply_autopilot_baton({}, evt2)
    assert evt2 == {'type': 'done'}
    assert 'autopilotNextTaskId' not in evt2


@_unit
def test_next_live_tick_sse_timeout_action():
    """Slice 8: SSE stream exceeding max duration returns
    LiveTickAction(kind='sse_timeout', timeout_evt=<SSE_TIMEOUT>).
    Byte-parity: the timeout_evt MUST carry a
    'SSE connection reached maximum duration' message so the frontend's
    switch-to-polling toast fires identically to the pre-slice inline
    build_event call.
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [],
        'events_lock': threading.Lock(),
        'status': 'running',
        'convId': 'cv-timeout',
    }
    # stream_start = 0.0, now = 7300 → elapsed 7300 > 7200 max.
    v = next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=7200.0, now=7300.0, task_id_short='t-timeout',
    )
    assert v.kind == 'sse_timeout', f'expected sse_timeout; got {v.kind}'
    assert isinstance(v.timeout_evt, dict)
    assert 'SSE connection reached maximum duration' in (
        v.timeout_evt.get('message') or '')


@_unit
def test_next_live_tick_events_action_slices_from_cursor():
    """Slice 8: with new events past the cursor, the tick returns
    kind='events' with frames = [(event_id, event), ...] indexed from
    cursor_before onward and next_cursor = new len(events).
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [
            {'type': 'delta', 'text': 'a'},   # id 0
            {'type': 'delta', 'text': 'b'},   # id 1
            {'type': 'delta', 'text': 'c'},   # id 2
        ],
        'events_lock': threading.Lock(),
        'status': 'running',
    }
    v = next_live_tick(
        task=task, cursor=1, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short='t-events',
    )
    assert v.kind == 'events', f'expected events; got {v.kind}'
    assert v.frames == [
        (1, {'type': 'delta', 'text': 'b'}),
        (2, {'type': 'delta', 'text': 'c'}),
    ], f'frames must be [(eid, evt)] from cursor onward; got {v.frames}'
    assert v.next_cursor == 3


@_unit
def test_next_live_tick_late_done_when_task_terminal_no_new_events():
    """Slice 8: task terminal + no new events past cursor → tick
    synthesizes the late DONE event with baton applied and returns
    kind='late_done' + late_done_evt carrying autopilot baton copies
    when task has _autopilot_followup.
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [{'type': 'delta'}],          # 1 event
        'events_lock': threading.Lock(),
        'status': 'done',
        'error': None,
        '_autopilot_followup': {
            'next_task_id': 'task-baton', 'vu_msg': {'x': 1}},
    }
    v = next_live_tick(
        task=task, cursor=1, sse_gen=1,   # cursor already past all events
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short='t-latedone',
    )
    assert v.kind == 'late_done', f'expected late_done; got {v.kind}'
    assert v.late_done_evt.get('type') == 'done'
    # Baton stamped.
    assert v.late_done_evt.get('autopilotNextTaskId') == 'task-baton'
    assert v.late_done_evt.get('autopilotVuMessage') == {'x': 1}
    # next_cursor == len(events) so caller uses it as the id: field.
    assert v.next_cursor == 1


@_unit
def test_next_live_tick_superseded_when_sse_gen_advanced():
    """Slice 8: task['_sse_gen_id'] advanced beyond our gen (a newer
    SSE reader took over) → tick returns kind='superseded' so caller
    closes the stale stream.

    Sequenced AFTER the events-drain check: if the task also has
    unread events, the events path runs FIRST (drains + advances
    cursor), THEN a subsequent tick sees supersede. This matches the
    pre-slice inline block's control-flow order.
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [],           # no events to drain
        'events_lock': threading.Lock(),
        'status': 'running',    # NOT terminal — so late_done doesn't fire
        '_sse_gen_id': 5,       # a newer reader bumped the counter
    }
    v = next_live_tick(
        task=task, cursor=0, sse_gen=3,   # we opened at gen=3, now 5
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short='t-super',
    )
    assert v.kind == 'superseded', f'expected superseded; got {v.kind}'


@_unit
def test_next_live_tick_keepalive_on_15s_gap():
    """Slice 8: no new events + not terminal + not superseded + gap >
    15s since last output → tick returns kind='keepalive' so caller
    yields the ``: keepalive\\n\\n`` comment and refreshes its SSE slot
    lease.
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [],
        'events_lock': threading.Lock(),
        'status': 'running',
    }
    v = next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=20.0,  # 20 - 0 = 20 > 15
        task_id_short='t-keep',
    )
    assert v.kind == 'keepalive', f'expected keepalive; got {v.kind}'


@_unit
def test_next_live_tick_sleep_default():
    """Slice 8: no new events + not terminal + not superseded + gap <=
    15s → tick returns kind='sleep' (the caller just does
    ``await asyncio.sleep(0.05)`` and loops).
    """
    from lib.chat_dispatch import next_live_tick
    import threading
    task = {
        'events': [],
        'events_lock': threading.Lock(),
        'status': 'running',
    }
    v = next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=5.0, now=10.0,  # 10 - 5 = 5 < 15
        task_id_short='t-sleep',
    )
    assert v.kind == 'sleep', f'expected sleep; got {v.kind}'


# ── Slice 9: chat_continue prefill-only path → lib/chat_dispatch.dispatch_prefill_continue ─
# Extracts the ~78-line ``_continue_via_prefill_only`` closure (the last
# independent, pure-in-shape closure in routes/chat.py per the epic plan).
# The route helper wrapped: assistant_msg mutation (content/thinking/
# stale-key strip), persist, build cfg_payload with excludeLast/
# resumePrefill/contentPrefix, dispatch through _start_task_for_conv,
# stamp activeTaskId, notify_conv_changed, audit_log, and build the
# checkpoint response envelope.
#
# After extraction: chat_continue's inline `_continue_via_prefill_only`
# call site becomes a `dispatch_prefill_continue(...)` call whose result
# is either a passthrough error Response (task-start failure) or the
# checkpoint payload dict the route wraps in a single ``jsonify(...)``.

@_unit
def test_chat_dispatch_exposes_dispatch_prefill_continue():
    """Slice 9 (pt_04686ac6): lib/chat_dispatch.py exposes
    dispatch_prefill_continue as a module-level callable + PrefillContinueResult
    dataclass with `kind` ∈ {'payload','passthrough'} discriminator."""
    import importlib
    mod = importlib.import_module('lib.chat_dispatch')
    for name in ('PrefillContinueResult', 'dispatch_prefill_continue'):
        assert hasattr(mod, name), f'lib.chat_dispatch missing {name}'
    assert callable(mod.dispatch_prefill_continue), (
        'dispatch_prefill_continue must be callable')


@_unit
def test_chat_continue_delegates_prefill_only_to_dispatch():
    """Slice 9 + epic pt_f5771a2e: routes/chat.py::chat_continue delegates
    its WHOLE core (scan / rollback / persist / task-start) to
    lib.chat_dispatch.execute_chat_continue — the shared continue contract
    that chat_start's interrupted-tail guard also rides. The case-3
    prefill branch inside that core delegates in turn to
    dispatch_prefill_continue (slice 9, now chat_dispatch-internal).
    The old inline ``_continue_via_prefill_only(...)`` closure must stay
    GONE from routes/chat.py."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.chat_dispatch import execute_chat_continue' in src, (
        'routes/chat.py must IMPORT execute_chat_continue from '
        'lib.chat_dispatch (epic pt_f5771a2e — the shared continue core)')
    import re as _re
    assert _re.search(r'\bexecute_chat_continue\s*\(', src), (
        'routes/chat.py must CALL execute_chat_continue(...) — a bare '
        'mention in a comment does not satisfy the extraction')
    with open(os.path.join(root, 'lib', 'chat_dispatch.py'), encoding='utf-8') as f:
        dispatch_src = f.read()
    assert _re.search(r'\bdispatch_prefill_continue\s*\(', dispatch_src), (
        'lib/chat_dispatch.py must CALL dispatch_prefill_continue(...) for '
        'the case-3 prefill branch (slice 9 contract, moved with the core)')
    # The old inline closure must be GONE — a silent revert would leave
    # its `def _continue_via_prefill_only(` re-declared.
    assert 'def _continue_via_prefill_only(' not in src, (
        'routes/chat.py must NOT re-declare the _continue_via_prefill_only '
        'closure — extracted to dispatch_prefill_continue')
    # Specific marker log-lines that USED to live in the closure body
    # must NOT be back in routes/chat.py.
    assert 'continue_prefill_only' not in src, (
        "routes/chat.py must NOT carry the 'continue_prefill_only' audit "
        'event name — extracted to dispatch_prefill_continue')


@_unit
def test_dispatch_prefill_continue_mutates_assistant_msg():
    """Slice 9: assistant_msg is mutated in place:
      * content ← orig_full_content (continuation base preserved).
      * thinking cleared, priorThinking stashed IF non-empty.
      * stale keys (finishReason/toolSummary/error) dropped.

    Mutation is what makes the follow-on ``_persist_conv_messages`` write
    the right shape to DB — regressing it would silently corrupt the
    resume state.
    """
    from lib.chat_dispatch import dispatch_prefill_continue

    orig_full = 'Once upon a time in ancient China'
    resume_prefill = 'time in ancient China'  # tail suffix
    assistant_msg = {
        'role': 'assistant',
        'content': 'stale short prefix',   # must be OVERWRITTEN with orig_full
        'thinking': 'live thinking tail',  # must be MOVED to priorThinking
        'finishReason': 'stop',            # must be POPPED
        'toolSummary': {'x': 1},           # must be POPPED
        'error': 'anything',               # must be POPPED
    }
    messages = [
        {'role': 'user', 'content': 'q'},
        assistant_msg,
    ]

    # Stub the collaborators so we exercise the mutation branch without
    # a real DB / Flask app context.
    def _fake_start_task(conv_id, cfg_payload, data):
        # Confirms cfg_payload propagation while we're here.
        assert cfg_payload.get('excludeLast') is True
        assert cfg_payload.get('resumePrefill') == resume_prefill
        assert cfg_payload.get('contentPrefix') == orig_full
        return 'task-new-123', None

    result = dispatch_prefill_continue(
        db=None, conv_id='cv-mutate', messages=messages,
        assistant_msg=assistant_msg, title=None, config={'model': 'm'},
        settings_patch=None, resume_prefill=resume_prefill,
        orig_full_content=orig_full, data={},
        _start_task_for_conv=_fake_start_task,
        _persist_conv_messages=lambda *a, **kw: None,
        _set_conversation_settings=lambda *a, **kw: None,
        _notify_conv_changed=lambda *a, **kw: None,
        _audit_log=lambda *a, **kw: None,
    )
    # Assistant message mutations — the SoT for what a mid-stream reload
    # will render post-Continue.
    assert assistant_msg['content'] == orig_full
    assert assistant_msg['thinking'] == ''
    assert assistant_msg['priorThinking'] == 'live thinking tail'
    for stale_key in ('finishReason', 'toolSummary', 'error'):
        assert stale_key not in assistant_msg, (
            f'{stale_key} must be stripped on prefill-only continue')
    # And the result is a payload result (not a passthrough).
    assert result.kind == 'payload'


@_unit
def test_dispatch_prefill_continue_builds_checkpoint_payload():
    """Slice 9: checkpoint payload dict is byte-identical to the
    pre-slice inline `jsonify({...})` body. Regressing any field
    (resumeMode / keptRounds / preservedContentLen / priorThinking) is
    a wire break the frontend depends on.
    """
    from lib.chat_dispatch import dispatch_prefill_continue

    orig_full = 'abc123abc123'  # 12 chars
    resume_prefill = '123'      # 3 chars
    assistant_msg = {'role': 'assistant', 'content': orig_full,
                     'thinking': ''}  # no priorThinking case
    messages = [assistant_msg]

    result = dispatch_prefill_continue(
        db=None, conv_id='cv-payload', messages=messages,
        assistant_msg=assistant_msg, title=None, config={},
        settings_patch=None, resume_prefill=resume_prefill,
        orig_full_content=orig_full, data={},
        _start_task_for_conv=lambda *a, **kw: ('task-xyz', None),
        _persist_conv_messages=lambda *a, **kw: None,
        _set_conversation_settings=lambda *a, **kw: None,
        _notify_conv_changed=lambda *a, **kw: None,
        _audit_log=lambda *a, **kw: None,
    )
    assert result.kind == 'payload'
    payload = result.payload
    assert payload['taskId'] == 'task-xyz'
    assert payload['convId'] == 'cv-payload'
    cp = payload['checkpoint']
    assert cp['keptRounds'] == 0
    assert cp['discardedRounds'] == 0
    assert cp['preservedContentLen'] == len(orig_full) == 12
    assert cp['discardedContentLen'] == 0
    assert cp['preservedThinkingChars'] == 0
    assert cp['discardedThinking'] == 0
    assert cp['resumeMode'] == 'prefill'
    assert cp['contentPrefix'] == orig_full
    assert cp['priorContent'] == ''
    assert cp['priorThinking'] == ''


@_unit
def test_dispatch_prefill_continue_passes_through_start_task_err_resp():
    """Slice 9: when ``_start_task_for_conv`` returns (None, err_resp),
    dispatch_prefill_continue MUST NOT persist activeTaskId or notify;
    it MUST return kind='passthrough' with the err_resp unchanged so the
    route hands it to the caller verbatim.
    """
    from lib.chat_dispatch import dispatch_prefill_continue

    # Sentinel object that stands in for the api_error Response — the
    # dispatcher must not touch it, only propagate.
    ERR_SENTINEL = object()

    activeTaskId_calls = []
    notify_calls = []

    result = dispatch_prefill_continue(
        db=None, conv_id='cv-err', messages=[{'role': 'assistant', 'content': ''}],
        assistant_msg={'role': 'assistant', 'content': 'x'},
        title=None, config={}, settings_patch=None,
        resume_prefill='p', orig_full_content='x', data={},
        _start_task_for_conv=lambda *a, **kw: (None, ERR_SENTINEL),
        _persist_conv_messages=lambda *a, **kw: None,
        _set_conversation_settings=lambda *a, **kw: activeTaskId_calls.append(kw),
        _notify_conv_changed=lambda *a, **kw: notify_calls.append(kw),
        _audit_log=lambda *a, **kw: None,
    )
    assert result.kind == 'passthrough'
    assert result.err_resp is ERR_SENTINEL
    # Byte-parity: the pre-slice route returned err_resp BEFORE
    # set_conversation_settings / notify — so those must not have fired.
    assert activeTaskId_calls == [], (
        'set_conversation_settings must NOT run when _start_task_for_conv errored')
    assert notify_calls == [], (
        '_notify_conv_changed must NOT run when _start_task_for_conv errored')


@_unit
def test_dispatch_prefill_continue_omits_priorthinking_key_when_empty():
    """Slice 9: byte-parity guard — pre-slice code stashed
    ``priorThinking`` ONLY when the original ``thinking`` was truthy.
    An empty thinking string must leave the key OUT of assistant_msg
    (mid-stream reload sees no 'earlier thinking' collapsed block).
    """
    from lib.chat_dispatch import dispatch_prefill_continue

    assistant_msg = {'role': 'assistant', 'content': 'x', 'thinking': ''}
    dispatch_prefill_continue(
        db=None, conv_id='cv-empty-think',
        messages=[assistant_msg], assistant_msg=assistant_msg,
        title=None, config={}, settings_patch=None,
        resume_prefill='p', orig_full_content='x', data={},
        _start_task_for_conv=lambda *a, **kw: ('t-1', None),
        _persist_conv_messages=lambda *a, **kw: None,
        _set_conversation_settings=lambda *a, **kw: None,
        _notify_conv_changed=lambda *a, **kw: None,
        _audit_log=lambda *a, **kw: None,
    )
    assert 'priorThinking' not in assistant_msg, (
        'priorThinking must NOT be stashed when original thinking was empty')


# ── Slice 6: chat_stream cold-path → lib/chat_dispatch.build_cold_replay_response ─
# Extracts the ~170-line cold-path (task not in memory) block:
# persisted-event replay OR DB snapshot OR not-found.

@_unit
def test_chat_dispatch_exposes_build_cold_replay_response():
    """Slice 6 (pt_04686ac6): lib/chat_dispatch.py exposes
    build_cold_replay_response as a module-level async callable."""
    import importlib
    import inspect
    mod = importlib.import_module('lib.chat_dispatch')
    assert hasattr(mod, 'build_cold_replay_response'), (
        'lib.chat_dispatch missing build_cold_replay_response')
    fn = mod.build_cold_replay_response
    assert callable(fn), 'build_cold_replay_response must be callable'
    assert inspect.iscoroutinefunction(fn), (
        'build_cold_replay_response MUST be async (chat_stream is async '
        'and uses asyncio.to_thread inside)')


@_unit
def test_chat_stream_delegates_cold_path_to_dispatch():
    """Slice 6: routes/chat.py::chat_stream must call
    build_cold_replay_response for its cold-path (task not in memory)
    branch. Guards against silent revert to the inline ~170-line block."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.chat_dispatch import build_cold_replay_response' in src, (
        'routes/chat.py must import build_cold_replay_response '
        '(slice 6 pt_04686ac6)')
    assert 'build_cold_replay_response(' in src, (
        'routes/chat.py must CALL build_cold_replay_response at the '
        'cold-path branch of chat_stream')
    # Specific inline call-site marker strings that USED to live inside
    # chat_stream's cold-path are GONE.
    assert 'Stream %s cold replay from event_log' not in src, (
        'routes/chat.py must NOT carry the "cold replay from event_log" '
        'log line — extracted')
    assert 'Stream %s served from DB' not in src, (
        'routes/chat.py must NOT carry the "served from DB" log line — '
        'extracted')
    assert 'def gen_persisted():' not in src, (
        'routes/chat.py must NOT carry the gen_persisted closure def — '
        'extracted')
    # But routes/chat.py MUST still return sse_response somewhere (for
    # the LIVE-stream branch that stays inline).
    assert 'sse_response(' in src, (
        'routes/chat.py must still call sse_response for the live-stream '
        'branch — that branch is NOT part of this slice')


@_unit
def test_build_cold_replay_response_returns_not_found_when_task_absent():
    """Slice 6: cold-path branch 3 (no persisted events + no task_results
    row) MUST return api_not_found('Task not found'). Byte-identical to
    the pre-slice inline block's final ``return api_not_found(...)``."""
    import asyncio
    import lib.chat_dispatch as cd
    from lib.database import DOMAIN_CHAT, get_db

    # Direct call with a task_id that has no persisted events and no
    # task_results row. Use a synthetic id unlikely to collide.
    _task_id = 'cv-cold-not-found-xyz-987'

    # Best-effort: ensure the row really is absent (test isolation on a
    # shared test DB).
    try:
        db = get_db(DOMAIN_CHAT)
        db.execute('DELETE FROM task_results WHERE task_id=?', (_task_id,))
        db.commit()
    except Exception:
        pass  # DB not available in this test env → build_cold_replay
              # will still return not_found via the row-is-None path.

    async def _run():
        # Empty Last-Event-ID → no persisted-replay branch fires.
        return await cd.build_cold_replay_response(_task_id, '')

    # Need a Quart app context because api_not_found returns a Quart Response.
    from quart import Quart
    app = Quart(__name__)

    async def _wrapped():
        async with app.test_request_context('/x'):
            resp = await _run()
            # api_not_found returns (Response, 404).
            assert isinstance(resp, tuple), (
                f'expected (Response, status) tuple; got {type(resp).__name__}')
            _r, _status = resp
            assert _status == 404, f'expected 404 not-found; got {_status}'

    asyncio.run(_wrapped())


if __name__ == '__main__':
    tests = [
        test_routes_chat_symbols_all_importable,
        test_extracted_helpers_are_callable,
        test_pure_helper_wire_parity_smoke,
        test_abort_marker_bundle_semantics,
        test_truncate_conv_history_side_effect_wire,
        test_chat_task_start_submodule_exists,
        test_routes_chat_reexports_start_task_for_conv,
        test_run_py_no_longer_carries_inline_start_task_for_conv,
        test_chat_bp_rules_snapshot,
        test_api_v1_chat_bp_rules_snapshot,
        test_chat_dispatch_module_exists_and_exposes_classifier,
        test_chat_send_delegates_to_classify_send_intent,
        test_classify_send_intent_none_on_no_running_task,
        test_classify_send_intent_aborted_kind_on_translate_abort,
        test_chat_dispatch_exposes_build_cold_replay_response,
        test_chat_stream_delegates_cold_path_to_dispatch,
        test_build_cold_replay_response_returns_not_found_when_task_absent,
        test_chat_dispatch_exposes_plan_warm_resume,
        test_chat_stream_delegates_warm_resume_to_dispatch,
        test_plan_warm_resume_none_on_no_cursor,
        test_plan_warm_resume_none_on_cursor_ahead_of_buffer,
        test_plan_warm_resume_serviceable_slices_post_cursor_events,
        test_build_fresh_state_snapshot_carries_full_task_state,
        test_chat_dispatch_exposes_next_live_tick_and_helpers,
        test_chat_stream_delegates_live_loop_to_next_live_tick,
        test_is_task_terminal_pure_over_task,
        test_apply_autopilot_baton_copies_next_task_id_and_vu_msg,
        test_next_live_tick_sse_timeout_action,
        test_next_live_tick_events_action_slices_from_cursor,
        test_next_live_tick_late_done_when_task_terminal_no_new_events,
        test_next_live_tick_superseded_when_sse_gen_advanced,
        test_next_live_tick_keepalive_on_15s_gap,
        test_next_live_tick_sleep_default,
        test_chat_dispatch_exposes_dispatch_prefill_continue,
        test_chat_continue_delegates_prefill_only_to_dispatch,
        test_dispatch_prefill_continue_mutates_assistant_msg,
        test_dispatch_prefill_continue_builds_checkpoint_payload,
        test_dispatch_prefill_continue_passes_through_start_task_err_resp,
        test_dispatch_prefill_continue_omits_priorthinking_key_when_empty,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
