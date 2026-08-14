#!/usr/bin/env python3
"""Phase-A ratchet — prompt-cache stability across the backend-authoritative
message-mutation points (committed-dict projection + ghost reconcile).

WHY
---
The "context mechanism" became backend-authoritative: ``_sync_result_to_conversation``
rewrites the tail assistant at turn end (``task['_committedMsg']``) and
``reconcile_conversation_messages`` deletes ghost messages. Prompt caching
(``lib/llm/cache.py`` + ``lib/tasks_pkg/cache_tracking.py``) assumes an
APPEND-ONLY history: the Anthropic tail breakpoint hashes nearly the whole
byte prefix, so any mutation/deletion of a message BEFORE it busts the cache.

The cache-relevant transition is NOT the tail rewrite itself — it is the moment
that settled assistant scrolls from tail into the cached prefix on the NEXT
round (a new user turn is appended above it). If the result-merge added
anything the WIRE actually sends (content / thinking / tool_calls / tool_result)
beyond frontend-only metadata (usage / finishReason / model / _taskId), the
round-(N+1) prefix would differ from the round-N tail → guaranteed miss.

This suite makes "the rewrite happens to stay wire-invariant" an ENFORCED,
wire-fingerprint-verified invariant, and gives ``detect_cache_break`` a NAMED
cause for a backend history rewrite BEFORE the parked GET-path reconcile is
allowed onto the hot path.

Parts (each double-neutered with byte-identical restore):
  SEAM — ``notify_history_rewrite`` NAMES the cause (not "(compacted)") and,
         unlike ``notify_compaction``, does NOT silence detection nor flip
         ``_wire_proven_identical`` to a false "PROVEN server-side".
  A    — pure reconcile prefix-neutrality: ``diff_canonical`` of the pre-tail
         region is empty across a real ``reconcile_conversation_messages``.
  B    — scroll-into-prefix: the settled assistant's wire fingerprint is
         IDENTICAL between round-N-tail and round-(N+1)-prefix, driving the
         REAL ``_sync_result_to_conversation`` against a temp DB, and PROVING
         usage/finishReason are wire-invisible (not assuming it).
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
#  SEAM — history_rewrite break cause
# ═══════════════════════════════════════════════════════════════════════════

def test_history_rewrite_named_not_compacted():
    """A count-drop round preceded by notify_history_rewrite is labeled a
    backend history rewrite, NOT '(compacted)'."""
    from lib.tasks_pkg.cache_tracking import (
        _cache_states, detect_cache_break, notify_history_rewrite,
    )
    _cache_states.clear()
    conv = 'hr-name'
    m1 = [{'role': 'system', 'content': 'sys'},
          {'role': 'user', 'content': 'u1'},
          {'role': 'assistant', 'content': 'a1'},
          {'role': 'user', 'content': 'u2'}]
    detect_cache_break(conv, m1, None, 'claude-opus-4',
                       usage={'cache_creation_input_tokens': 88000,
                              'cache_read_input_tokens': 51000})
    # Backend deletes a message (count drops) and signals a history rewrite.
    notify_history_rewrite(conv)
    m2 = [{'role': 'system', 'content': 'sys'},
          {'role': 'user', 'content': 'u2'}]
    r2 = detect_cache_break(conv, m2, None, 'claude-opus-4',
                            usage={'cache_creation_input_tokens': 89000,
                                   'cache_read_input_tokens': 0})
    assert r2 is not None, 'expected a break on the count-drop round'
    blob = _json.dumps(r2)
    assert 'history rewrite' in blob, (
        f'count drop was NOT labeled a history rewrite: {r2}')
    assert '(compacted)' not in blob, (
        f'history rewrite was mislabeled as compaction: {r2}')
    _ok('backend history rewrite is NAMED, not mislabeled "(compacted)"')


def test_resolve_break_cause_names_history_rewrite_branch():
    """Directly exercise the _resolve_break_cause history_rewrite branch
    (surface #2 — the narrow path where a rewrite is the WINNING cause: no
    client_changes, no prefix_mutation_break, no TTL). Without wire fp and with
    no count change this is the ONLY signal, and it must name the backend
    rewrite instead of falling through to a server-side guess."""
    from lib.tasks_pkg.cache_tracking import _resolve_break_cause
    named = _resolve_break_cause(
        client_changes={}, prefix_mutation_break=False, elapsed=1.0,
        cache_read=0, prefix_mutated=False, prefix_culprits=None,
        wire_proven_identical=False, history_rewrite=True)
    assert 'history rewrite' in named.lower() or 'reconcile' in named.lower(), (
        f'history_rewrite branch did not name the backend rewrite: {named!r}')
    assert 'server-side' not in named.lower(), (
        f'history rewrite laundered into a server-side guess: {named!r}')
    # Same inputs WITHOUT the flag → falls through to the server-side guess.
    unnamed = _resolve_break_cause(
        client_changes={}, prefix_mutation_break=False, elapsed=1.0,
        cache_read=0, prefix_mutated=False, prefix_culprits=None,
        wire_proven_identical=False, history_rewrite=False)
    assert 'history rewrite' not in unnamed.lower(), (
        f'history-rewrite wording leaked when flag was False: {unnamed!r}')
    _ok('_resolve_break_cause NAMES the history_rewrite branch (surface #2)')



def test_byte_identical_verdict_has_no_contention_falsehood():
    """★ INFRA-TRUTH GUARD. The byte-identical ('NOT a client-side change')
    verdict is what renders into the cost panel. It must NOT assert the
    debunked 'contention in this key's shared cache pool' model: the gateway
    only FORWARDS (never evicts) and there is no per-key cache capacity limit
    (owner-confirmed; A/B-verified no cross-conversation contention). The honest
    cause set is 'an upstream per-request gateway miss or a TTL boundary'. This
    pins the wording so the debunked eviction/contention model can't silently
    return to the user-facing string.
    """
    from lib.tasks_pkg.cache_tracking import _resolve_break_cause
    # Both byte-identical sub-branches: whole-prefix (cache_read small) and
    # partial (cache_read large). Neither may mention contention/shared pool.
    for _cr in (0, 250000):
        verdict = _resolve_break_cause(
            client_changes={}, prefix_mutation_break=False, elapsed=20.0,
            cache_read=_cr, prefix_mutated=False, prefix_culprits=None,
            wire_proven_identical=True, history_rewrite=False)
        low = verdict.lower()
        assert 'contention' not in low, (
            f'byte-identical verdict re-introduced the contention falsehood: '
            f'{verdict!r}')
        assert 'shared cache pool' not in low and 'shared pool' not in low, (
            f'byte-identical verdict re-introduced the shared-pool model: '
            f'{verdict!r}')
        # The correct, honest cause set must still be named.
        assert 'gateway miss' in low or 'ttl' in low, (
            f'byte-identical verdict lost the honest cause set: {verdict!r}')
        # And it must keep the correct 'NOT a client-side change' framing.
        assert 'client-side' in low, (
            f'byte-identical verdict lost the client-side framing: {verdict!r}')
    _ok('byte-identical verdict names gateway-miss/TTL, never shared-pool '
        'contention (infra-truth guard)')


def test_history_rewrite_does_not_flip_proven_server_side():
    """★ THE LANDMINE. A reconcile that MUTATES a scrolled-in prefix message,
    with notify_history_rewrite set, must STILL surface prefix_mutation and must
    NOT be laundered into a false 'server-side — PROVEN' verdict. Contrast with
    notify_compaction on identical inputs, which DOES silence the wire diff and
    flip proven→True."""
    from lib.tasks_pkg.cache_tracking import (
        _cache_states, detect_cache_break, notify_history_rewrite,
        notify_compaction,
    )
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash,
    )
    # A prefix tool_result whose bytes the backend rewrites between rounds.
    wire1 = [{'role': 'system', 'content': 'sys'},
             {'role': 'user', 'content': 'hi'},
             {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ORIGINAL'}]
    wire2 = [{'role': 'system', 'content': 'sys'},
             {'role': 'user', 'content': 'hi'},
             {'role': 'tool', 'tool_call_id': 'c1', 'content': 'REWRITTEN'},
             {'role': 'user', 'content': 'next'}]
    fp1, st1 = canonical_messages(wire1), static_prefix_hash(wire1)
    fp2, st2 = canonical_messages(wire2), static_prefix_hash(wire2)
    seed = [{'role': 'system', 'content': 'sys'}]
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp1, '_wire_static': st1}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp2, '_wire_static': st2}

    # ── history_rewrite arm: wire diff RUNS → prefix_mutation, NOT proven ──
    _cache_states.clear()
    detect_cache_break('hr-mut', seed, None, 'claude-opus-4', usage=dict(u1))
    notify_history_rewrite('hr-mut')
    r_hr = detect_cache_break('hr-mut', seed, None, 'claude-opus-4', usage=dict(u2))
    assert r_hr is not None
    blob_hr = _json.dumps(r_hr)
    assert 'prefix_mutation' in r_hr, (
        f'history_rewrite silenced the real mutation: {r_hr}')
    assert 'PROVEN' not in blob_hr, (
        f'history_rewrite falsely flipped to PROVEN server-side: {r_hr}')
    assert 'server_side' not in r_hr, (
        f'a real client mutation was laundered to server_side: {r_hr}')

    # ── contrast: notify_compaction on identical bytes DOES silence + flip ──
    _cache_states.clear()
    detect_cache_break('cp-mut', seed, None, 'claude-opus-4', usage=dict(u1))
    notify_compaction('cp-mut')
    r_cp = detect_cache_break('cp-mut', seed, None, 'claude-opus-4', usage=dict(u2))
    # notify_compaction blanket-suppresses; the mutation does NOT surface.
    assert not (r_cp and 'prefix_mutation' in r_cp), (
        'sanity check failed: notify_compaction should suppress prefix_mutation')
    _ok('notify_history_rewrite keeps the wire diff live (prefix_mutation, '
        'never false-PROVEN) — the opposite of notify_compaction')



class _WarnCapture:
    """Attach a handler to the cache_tracking logger and record WARNING text."""

    def __init__(self):
        import logging
        self._logging = logging
        self.records: list[str] = []
        self._logger = logging.getLogger('lib.tasks_pkg.cache_tracking')
        self._handler = logging.Handler()
        self._handler.emit = lambda rec: self.records.append(rec.getMessage())

    def __enter__(self):
        self._prev_level = self._logger.level
        self._logger.setLevel(self._logging.WARNING)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *a):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)

    def has(self, substr: str) -> bool:
        return any(substr in m for m in self.records)


def _hash_path_prefix_mutation(conv, notify_fn):
    """Drive the HASH-based prefix-mutation path (the exact site of the
    anonymous 'PREFIX MUTATION DETECTED …without compaction' warning) across
    two rounds, mutating an IN-PREFIX message between them (no wire fp, so the
    hash detector — not the wire diff — is what fires). ``notify_fn`` is called
    on the conv between rounds (or None). Returns (result_dict, WarnCapture)."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    _cache_states.clear()
    # count 5 → prefix_count = 5 - EDITABLE_TAIL_COUNT(2) = 3 → prefix=[sys,u1,a1]
    m1 = [{'role': 'system', 'content': 'sys'},
          {'role': 'user', 'content': 'u1'},
          {'role': 'assistant', 'content': 'ORIGINAL prefix answer'},
          {'role': 'user', 'content': 'u2'},
          {'role': 'assistant', 'content': 'a2'}]
    detect_cache_break(conv, m1, None, 'claude-opus-4',
                       usage={'cache_creation_input_tokens': 50000,
                              'cache_read_input_tokens': 40000})
    if notify_fn is not None:
        notify_fn(conv)
    # a1 (index 2, INSIDE the [0:3] prefix) is rewritten → prefix bytes change.
    m2 = [{'role': 'system', 'content': 'sys'},
          {'role': 'user', 'content': 'u1'},
          {'role': 'assistant', 'content': 'REWRITTEN prefix answer'},
          {'role': 'user', 'content': 'u2'},
          {'role': 'assistant', 'content': 'a2'}]
    with _WarnCapture() as cap:
        r = detect_cache_break(conv, m2, None, 'claude-opus-4',
                               usage={'cache_creation_input_tokens': 50000,
                                      'cache_read_input_tokens': 0})
    return r, cap


def test_profile_splice_labels_without_suppressing_the_rebill():
    """★ P1 — the profile-splice cost-visibility invariant.

    The per-turn user-profile / detail block is spliced INTO messages[0]'s
    carrier, which sits inside the cached prefix after the first tool round —
    a genuine prefix mutation that RE-BILLS the whole body uncached. The
    splice signals ``notify_history_rewrite`` (NOT ``notify_compaction``).

    This must produce the exact two-part distinction:
      (a) the ANONYMOUS false-positive 'PREFIX MUTATION DETECTED …without
          compaction' alarm is SUPPRESSED (the cause is now named), and
      (b) the real cache-break is STILL detected + attributed
          ('prefix_mutation' surfaces, total_breaks increments) — unlike
          ``notify_compaction`` which blanket-suppresses BOTH, laundering a
          real re-bill into invisibility.

    Three arms on the identical in-prefix mutation:
      CONTROL (no notify) → anonymous alarm FIRES + break surfaces (proves the
                            warning path is live; my gate is what silences it).
      history_rewrite     → anonymous alarm SILENT, break STILL surfaces.
      compaction          → anonymous alarm SILENT, break SUPPRESSED (masked).
    """
    from lib.tasks_pkg.cache_tracking import (
        _cache_states, _state_key, notify_history_rewrite, notify_compaction,
    )
    _ANON = 'PREFIX MUTATION DETECTED'

    # ── CONTROL: no notify → the anonymous alarm MUST fire, break MUST surface.
    r_ctrl, cap_ctrl = _hash_path_prefix_mutation('p1-ctrl', None)
    assert cap_ctrl.has(_ANON), (
        'control: the anonymous PREFIX MUTATION DETECTED alarm should fire when '
        'no cause is signalled (else the test proves nothing)')
    assert r_ctrl and 'prefix_mutation' in r_ctrl, (
        f'control: a real in-prefix re-bill must be detected: {r_ctrl}')

    # ── history_rewrite (what the profile splice now does): (a) silent, (b) surfaces.
    r_hr, cap_hr = _hash_path_prefix_mutation('p1-hr', notify_history_rewrite)
    assert not cap_hr.has(_ANON), (
        f'history_rewrite: the false anonymous alarm should be SUPPRESSED — '
        f'the cause is named. Captured: {cap_hr.records}')
    assert r_hr and 'prefix_mutation' in r_hr, (
        f'history_rewrite: the real re-bill must STILL be detected + attributed '
        f'(the whole point vs notify_compaction): {r_hr}')
    # total_breaks incremented → the cost is counted in the session metrics.
    assert _cache_states[_state_key('p1-hr')].total_breaks >= 1, (
        'history_rewrite: the break was not counted in session metrics')

    # ── compaction (the OLD masking behavior): (a) silent, (b) ALSO masked.
    r_cp, cap_cp = _hash_path_prefix_mutation('p1-cp', notify_compaction)
    assert not cap_cp.has(_ANON), 'compaction: anonymous alarm suppressed (expected)'
    assert not (r_cp and 'prefix_mutation' in r_cp), (
        f'compaction blanket-suppresses detection — proving WHY the profile '
        f'splice must NOT use it (the re-bill would be invisible): {r_cp}')
    assert _cache_states[_state_key('p1-cp')].total_breaks == 0, (
        'compaction: the real break was masked out of the metrics (the bug)')

    _ok('profile splice via notify_history_rewrite: silences the false alarm '
        'YET keeps the re-bill detected + counted (notify_compaction masks both)')


# ═══════════════════════════════════════════════════════════════════════════
#  PART A — reconcile prefix-neutrality (pure)
# ═══════════════════════════════════════════════════════════════════════════

def _reconcile_prefix_diff(messages):
    """Return the wire-fp culprits in the PRE-TAIL region across a real
    reconcile. Empty list == the cached prefix is byte-identical."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical
    before = canonical_messages(messages)
    out, changed = reconcile_conversation_messages(messages)
    after = canonical_messages(out)
    # Align the pre-tail region by the shorter length (reconcile may drop the
    # tail); the prefix that survives must be byte-identical.
    n = min(len(before), len(after)) - 1
    if n < 0:
        n = 0
    return diff_canonical(before[:n], after[:n]), changed


def test_reconcile_leaves_settled_prefix_byte_identical():
    """A real reconcile that stamps/deletes the TAIL leaves the settled prefix
    wire-identical → no cache bust for the surviving prefix."""
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': 'settled answer',
         'toolRounds': [{'status': 'done', 'toolContent': 'x'}]},
        {'role': 'user', 'content': 'U2'},
        # trailing thinking-only ghost → reconcile stamps interrupted (tail).
        {'role': 'assistant', 'content': '', 'thinking': 'partial reasoning'},
    ]
    culprits, changed = _reconcile_prefix_diff(msgs)
    assert changed, 'reconcile should have stamped the trailing ghost'
    assert culprits == [], (
        f'reconcile mutated the cached prefix (cache bust): {culprits}')
    _ok('reconcile leaves the settled prefix wire-byte-identical (Part A)')


def test_reconcile_buried_ghost_sweep_prefix_identical():
    """A buried-ghost sweep removes a bodyless mid-list assistant; the OTHER
    surviving prefix messages stay wire-identical (the removed one is gone, not
    mutated)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': 'real answer 1',
         'toolRounds': [{'status': 'done'}]},
        # buried empty ghost (no content/thinking/error/real round) → swept.
        {'role': 'assistant', 'content': '', 'thinking': ''},
        {'role': 'user', 'content': 'U2'},
        {'role': 'assistant', 'content': 'real answer 2',
         'toolRounds': [{'status': 'done'}]},
    ]
    before = canonical_messages(msgs)
    out, changed = reconcile_conversation_messages(msgs)
    after = canonical_messages(out)
    assert changed and len(out) == len(msgs) - 1, 'expected one ghost swept'
    # Every surviving message must appear byte-identical in the output — align
    # by stable key, not index (the sweep shifts indices).
    before_by_key = {e['key']: e for e in before}
    for e in after:
        if e['key'] in before_by_key:
            assert diff_canonical([before_by_key[e['key']]], [e]) == [], (
                f'a surviving message was mutated by the sweep: {e["key"]}')
    _ok('buried-ghost sweep removes (never mutates) — survivors wire-identical')


def test_reconcile_skips_ghost_inside_cache_prefix():
    """★ PHASE-B GATE. On a WARM, LARGE list where a buried ghost sits DEEP in
    the cached prefix (not the tail), the prefix guard must SKIP it — deleting
    an in-prefix message shifts the prefix bytes and busts the whole cache.

    Without the guard the ghost is swept (list shortens, prefix bytes shift);
    with the guard the in-prefix ghost survives untouched and only ghosts
    OUTSIDE the prefix are eligible. This is what makes moving reconcile onto
    the hot path safe."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical

    # A long list: a buried ghost at index 3 (deep in the prefix) + a settled
    # tail. Simulate a warm cache whose immutable prefix covers indices 0..7.
    msgs = [{'role': 'system', 'content': 'sys'}]
    for i in range(4):
        msgs.append({'role': 'user', 'content': f'U{i}'})
        if i == 1:
            # buried empty ghost DEEP in the prefix (index 3)
            msgs.append({'role': 'assistant', 'content': '', 'thinking': ''})
        else:
            msgs.append({'role': 'assistant', 'content': f'A{i}',
                         'toolRounds': [{'status': 'done'}]})
    msgs.append({'role': 'user', 'content': 'tail-U'})
    ghost_idx = next(i for i, m in enumerate(msgs)
                     if m.get('role') == 'assistant'
                     and not (m.get('content') or '').strip())
    prefix_count = ghost_idx + 3  # the ghost is well inside the immutable prefix

    before = canonical_messages(msgs)
    out, changed = reconcile_conversation_messages(
        msgs, cache_prefix_count=prefix_count)
    after = canonical_messages(out)

    # The in-prefix ghost SURVIVED (not swept) → prefix length preserved.
    assert len(out) == len(msgs), (
        f'in-prefix ghost was swept despite the guard: {len(msgs)} → {len(out)}')
    # And the whole immutable prefix is byte-identical.
    assert diff_canonical(before[:prefix_count], after[:prefix_count]) == [], (
        'reconcile mutated the immutable cache prefix')
    _ok('reconcile SKIPS a buried ghost inside the cache prefix (Phase-B gate)')


def test_editable_tail_count_is_single_sourced():
    """The immutable-prefix bound is ONE constant. get_cache_prefix_count must
    equal message_count - EDITABLE_TAIL_COUNT on a warm conv — so 'what the
    cache assumes stable' and 'what a writer may mutate' cannot diverge."""
    from lib.tasks_pkg.cache_tracking import (
        _cache_states, _state_key, get_cache_prefix_count,
        EDITABLE_TAIL_COUNT, CacheState,
    )
    _cache_states.clear()
    conv = 'etc-single-source'
    st = CacheState()
    st.message_count = 20
    st.last_cache_read_tokens = 50000  # warm
    _cache_states[_state_key(conv)] = st
    assert get_cache_prefix_count(conv) == 20 - EDITABLE_TAIL_COUNT, (
        'get_cache_prefix_count diverged from the single-sourced bound')
    _ok('EDITABLE_TAIL_COUNT single-sources the immutable-prefix bound')


# ═══════════════════════════════════════════════════════════════════════════
#  PART B — scroll-into-prefix wire identity (real DB)
# ═══════════════════════════════════════════════════════════════════════════

def _seed_conv(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'cache-stability-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _committed_tail_via_real_sync(conv_id, content, thinking, meta_fields):
    """Drive the REAL _sync_result_to_conversation and return the committed
    tail dict it stamped."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (
        create_task, _sync_result_to_conversation, build_result_meta,
        _conv_latest_task, _conv_latest_task_lock,
    )
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = content
        task['thinking'] = thinking
        for k, v in meta_fields.items():
            task[k] = v
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']
        _sync_result_to_conversation(task, build_result_meta(task))
        return task.get('_committedMsg')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


def test_settled_tail_wire_identical_when_scrolled_into_prefix():
    """★ THE INVARIANT. The settled assistant committed at round N must have a
    wire fingerprint IDENTICAL to how it appears at round N+1 once a new user
    turn is appended above it (scroll into prefix). If the result-merge added a
    wire-visible field, the round-N+1 prefix would differ → cache bust."""
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical

    _content = 'The final settled answer.'
    _thinking = 'chain of thought'
    committed = _committed_tail_via_real_sync(
        'cv-cache-scroll',
        content=_content, thinking=_thinking,
        meta_fields={'finishReason': 'stop', 'model': 'test-model',
                     'usage': {'input_tokens': 10, 'output_tokens': 20}},
    )
    assert committed is not None, '_committedMsg not stamped'

    # The wire bytes the model actually SAW while streaming the answer: an
    # assistant turn carrying only content + thinking (terminal metadata does
    # not exist until commit). This is what the settled assistant MUST still
    # fingerprint as once it scrolls into the cached prefix next round — else a
    # commit-time wire-visible field would bust the cache.
    streamed_tail = {'role': 'assistant', 'content': _content,
                     'thinking': _thinking}
    # NOT tautological: `committed` came from the REAL sync (content + thinking
    # + finishReason + usage + model + _taskId), `streamed_tail` has only the
    # wire-visible pair. If the sync injected any wire-visible drift, this diff
    # is non-empty.
    culprits = diff_canonical(canonical_messages([streamed_tail]),
                              canonical_messages([committed]))
    assert culprits == [], (
        f'the committed tail carries wire-visible drift vs the streamed answer '
        f'— it would bust cache when scrolled into the prefix: {culprits}')
    # And the committed dict really DID pick up the terminal metadata (proving
    # the diff above is empty because those fields are wire-invisible, not
    # because the sync did nothing).
    assert committed.get('finishReason') == 'stop' and committed.get('usage'), (
        'sanity: the real sync did not merge terminal metadata onto the tail')
    _ok('committed tail is wire-identical to the streamed answer despite '
        'carrying terminal metadata (Part B, non-tautological)')


def test_usage_and_finishreason_are_wire_invisible():
    """PROVE (not assume) that the terminal metadata the merge adds
    (usage/finishReason/model/_taskId) does NOT change the wire fingerprint —
    that is WHY the tail rewrite is cache-neutral."""
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical
    base = {'role': 'assistant', 'content': 'answer', 'thinking': 'reasoning'}
    enriched = dict(base)
    enriched.update({
        'finishReason': 'stop',
        'usage': {'input_tokens': 10, 'output_tokens': 20},
        'model': 'test-model',
        '_taskId': 'abc123',
        'toolSummary': 'did stuff',
        'apiRounds': 3,
    })
    culprits = diff_canonical(canonical_messages([base]),
                              canonical_messages([enriched]))
    assert culprits == [], (
        f'terminal metadata is wire-VISIBLE — it would bust cache: {culprits}')
    # And a REAL content change IS visible (the canonicaliser is not blind).
    changed = dict(base, content='DIFFERENT answer')
    culprits2 = diff_canonical(canonical_messages([base]),
                               canonical_messages([changed]))
    assert culprits2 != [], 'sanity: a real content change must be wire-visible'
    _ok('usage/finishReason/model/_taskId are wire-invisible; content is visible')


def main():
    print()
    print(_color('═══ Phase-A cache-prefix-stability ratchet ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_cache_prefix_stability.__main__')
    tests = [
        test_history_rewrite_named_not_compacted,
        test_resolve_break_cause_names_history_rewrite_branch,
        test_byte_identical_verdict_has_no_contention_falsehood,
        test_history_rewrite_does_not_flip_proven_server_side,
        test_profile_splice_labels_without_suppressing_the_rebill,
        test_reconcile_leaves_settled_prefix_byte_identical,
        test_reconcile_buried_ghost_sweep_prefix_identical,
        test_reconcile_skips_ghost_inside_cache_prefix,
        test_editable_tail_count_is_single_sourced,
        test_settled_tail_wire_identical_when_scrolled_into_prefix,
        test_usage_and_finishreason_are_wire_invisible,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} RATCHET TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
