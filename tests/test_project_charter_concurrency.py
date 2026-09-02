"""tests/test_project_charter_concurrency.py — the charter's lost-update root cause.

## What this pins

``project_charter`` stores its committed decisions as ONE JSON array in a single
column, so ``commit_charter`` is a read-modify-write: read the whole list →
append in memory → write the whole list back. Two commits that interleave inside
that window therefore CLOBBER each other — and, because the write is an
unconditional upsert, the loser is told ``ok=True``. The decision it just
"committed" is gone, and a charter decision is prompt-injected shared intent, so
a silently dropped one misleads every sibling conversation.

Measured before the fix (this is the shipped behaviour, reproduced in
``test_NC_*`` below):

    sibling appends SIB inside the window → I append MINE
    → final decisions == ['D0', 'MINE']      # SIB swallowed
    → my return value == {'ok': True, ...}   # and nothing said so

## Why the tests interleave rather than run in sequence

A SEQUENTIAL probe (sibling commits fully, THEN I commit) passes even on the
broken code, because my read happens after their write — it never enters the
window. An earlier analysis used exactly that shape and concluded "append needs
no lock", which is the opposite of the truth. Every test here therefore drives a
real interleave by hooking ``read_charter`` to fire the sibling's commit INSIDE
my read-modify-write window. If someone later "simplifies" these into sequential
calls they will pass against broken code — that is the trap this note exists to
prevent.

## The two halves

  • **append vs append** — the sibling's decision must survive my append.
  • **append vs content** — the sibling's NEW north star must survive my append.
    This is the half a naive "just retry the whole commit" fix gets wrong: it
    replays the caller's stale ``content`` over the sibling's edit, trading the
    decisions lost-update for a north-star lost-update.

``content`` and ``add_decision`` are mutually exclusive precisely so that
"a pure append" is decidable from the arguments, which is what makes replaying
one safe. The mixed call used to be accepted (the route's gate is
``content is None and not add_decision``), so the safety of a replay rested on
caller habit rather than on the signature.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')


@pytest.fixture(autouse=True)
def _clean(chat_sidecar, monkeypatch):
    import tests._seed as seed
    seed.clear_records('project_charter')
    seed.clear_events()
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _interleave(pc, sibling_commit):
    """Return a ``read_charter`` stand-in that fires ONE sibling commit inside
    the caller's read-modify-write window.

    The hook restores the real reader before running the sibling so the sibling
    itself is a normal, un-hooked commit, then re-arms — otherwise the sibling
    would recurse into its own interleave.
    """
    real = pc.read_charter
    state = {'fired': False}

    def hooked(path, *, user_id):
        rec = real(path, user_id=user_id)
        if not state['fired']:
            state['fired'] = True
            pc.read_charter = real
            sibling_commit()
            pc.read_charter = hooked
        return rec

    return hooked, real


# ════════════════════════════════════════════════════════════════════
#  Half 1 — append vs append: the sibling's decision must survive
# ════════════════════════════════════════════════════════════════════

def test_concurrent_appends_both_survive(flask_app):
    """Two decisions committed across an interleave → BOTH are in the charter.

    The assertion is on the DATA, not on the return code: the shipped bug
    returned ok=True while dropping a decision, so a test that only checked
    ``res['ok']`` would have passed against it.
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-append')
    with flask_app.app_context():
        pc.commit_charter(p, add_decision='D0', summary='D0',
                          updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)

        def sibling():
            pc.commit_charter(p, add_decision='SIBLING', summary='SIBLING',
                              updated_by_conv='sibB', user_id=TEST_OWNER_USER_ID)

        hooked, real = _interleave(pc, sibling)
        pc.read_charter = hooked
        try:
            res = pc.commit_charter(p, add_decision='MINE', summary='MINE',
                                    updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        finally:
            pc.read_charter = real
        texts = [d['text'] for d in pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['decisions']]

    assert res.get('ok'), res
    assert 'SIBLING' in texts, (
        "the sibling's decision was swallowed by my append — a charter "
        'decision is injected shared intent, so losing one misleads every '
        f'sibling conversation. got {texts}')
    assert 'MINE' in texts, f'my own append must land too, got {texts}'
    assert texts == ['D0', 'SIBLING', 'MINE'], \
        f'both appends must be present, in commit order; got {texts}'


def test_concurrent_append_reports_the_truth(flask_app):
    """A commit that could NOT be applied must not report success.

    Complement to the test above: the failure mode being fixed is not just
    "data lost", it is "data lost while the caller is told it landed". If a
    future change bounds the retry and gives up, it must say so.
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-truth')
    with flask_app.app_context():
        pc.commit_charter(p, add_decision='D0', summary='D0',
                          updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)

        def sibling():
            pc.commit_charter(p, add_decision='SIB', summary='SIB',
                              updated_by_conv='sibB', user_id=TEST_OWNER_USER_ID)

        hooked, real = _interleave(pc, sibling)
        pc.read_charter = hooked
        try:
            res = pc.commit_charter(p, add_decision='MINE', summary='MINE',
                                    updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        finally:
            pc.read_charter = real
        texts = [d['text'] for d in pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['decisions']]

    assert res.get('ok') == ('MINE' in texts), (
        'ok=True MUST mean the decision is actually in the charter — '
        f'res={res} texts={texts}')


# ════════════════════════════════════════════════════════════════════
#  Half 2 — append vs content: the sibling's north star must survive
#
#  This is the half a "retry the whole commit" fix breaks: replaying the
#  caller's stale `content` overwrites the north star the sibling just set.
# ════════════════════════════════════════════════════════════════════

def test_append_never_reverts_a_concurrent_north_star_edit(flask_app):
    """A sibling rewrites the north star inside my window → my decision append
    lands AND the north star stays the sibling's new text.

    An append carries no opinion about ``content``, so it must never write one
    back. The shipped read-modify-write wrote the WHOLE row from a stale read,
    which is exactly how an append reverts someone else's goal edit.
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-northstar')
    with flask_app.app_context():
        pc.commit_charter(p, content='NORTH STAR v1', updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)

        def sibling():
            pc.commit_charter(p, content='NORTH STAR v2 (sibling)',
                              updated_by_conv='sibB', user_id=TEST_OWNER_USER_ID)

        hooked, real = _interleave(pc, sibling)
        pc.read_charter = hooked
        try:
            res = pc.commit_charter(p, add_decision='MINE', summary='MINE',
                                    updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        finally:
            pc.read_charter = real
        rec = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)

    assert res.get('ok'), res
    assert rec['content'] == 'NORTH STAR v2 (sibling)', (
        'a pure decision append must not revert a concurrent north-star edit; '
        f"got {rec['content']!r}")
    assert [d['text'] for d in rec['decisions']] == ['MINE'], \
        f"my append must still land; got {rec['decisions']}"


# ════════════════════════════════════════════════════════════════════
#  The mutual-exclusion rule that makes "replay one append" SAFE
# ════════════════════════════════════════════════════════════════════

def test_content_and_add_decision_are_mutually_exclusive(flask_app):
    """A mixed call is REFUSED, so "is this a pure append?" is decidable from
    the arguments alone.

    Without this, replay safety rests on caller habit: a mixed call replayed
    after a version skew would push a stale ``content`` over a concurrent edit.
    Making the combination unrepresentable is what turns the replay from
    "usually fine" into "cannot be wrong".
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-mutex')
    with flask_app.app_context():
        res = pc.commit_charter(p, content='NS', add_decision='D',
                                summary='D', updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        rec = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)

    assert res.get('ok') is False, \
        'content + add_decision in one call must be refused'
    assert res.get('error') == 'invalid_combination', res
    assert not rec.get('exists'), \
        'a refused combination must write NOTHING (no partial charter)'


def test_route_refuses_the_mixed_combination(flask_app, flask_client):
    """The REST surface enforces the same rule (400), so an external client
    cannot reach the unsafe shape the library now refuses."""
    import json as _json
    p = os.path.abspath('/tmp/charter-cc-mutex-route')
    r = flask_client.post('/api/v1/project/charter/commit',
                          json={'path': p, 'content': 'NS',
                                'add_decision': 'D', 'summary': 'D'})
    assert r.status_code == 400, r.get_data(as_text=True)
    body = _json.loads(r.get_data(as_text=True))
    assert body.get('ok') is False
    assert 'add_decision' in _json.dumps(body), body


# ════════════════════════════════════════════════════════════════════
#  expected_version: scoped by OPERATION, not by call site
# ════════════════════════════════════════════════════════════════════

def test_pure_append_is_not_refused_for_a_stale_version(flask_app):
    """A stale ``expected_version`` must NOT refuse a pure append.

    The Charter tab bakes the version it rendered into the button. Sibling
    agents self-commit decisions constantly, so by the time the human clicks,
    that version is routinely stale — and the click was refused with a 409 that
    the UI swallowed, i.e. the same "dead button" the user reported. An append
    commutes with any other append, so version skew is not a conflict for it:
    the CAS re-reads and replays instead of refusing.
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-append-stale')
    with flask_app.app_context():
        pc.commit_charter(p, add_decision='D0', summary='D0',
                          updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)
        rendered_version = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['version']
        pc.commit_charter(p, add_decision='SIBLING', summary='S',
                          updated_by_conv='sibB', user_id=TEST_OWNER_USER_ID)   # version moves on
        res = pc.commit_charter(p, add_decision='MINE', summary='M',
                                expected_version=rendered_version,
                                updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        texts = [d['text'] for d in pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['decisions']]

    assert res.get('ok'), \
        f'a pure append must survive version skew, not 409; got {res}'
    assert texts == ['D0', 'SIBLING', 'MINE'], \
        f'and neither decision may be lost; got {texts}'


def test_content_overwrite_still_refuses_a_stale_version(flask_app):
    """The complement: an OVERWRITE keeps the hard optimistic lock.

    Rewriting the north star from a stale base genuinely destroys the other
    edit — there is no commutative merge — so it must still be refused. Without
    this test, "stop 409-ing" could be over-applied to every operation and turn
    a visible conflict into a silent overwrite.
    """
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-content-stale')
    with flask_app.app_context():
        pc.commit_charter(p, content='v1', updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)
        stale = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['version'] - 1
        res = pc.commit_charter(p, content='clobber',
                                expected_version=stale,
                                updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        rec = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)

    assert res.get('ok') is False and res.get('error') == 'version_conflict', res
    assert rec['content'] == 'v1', 'the rejected overwrite must not land'


def test_decision_cap_still_holds_under_replay(flask_app):
    """The append path keeps its bounded-size invariant when it goes through
    the CAS/replay branch (a replay must not double-append or bypass the cap)."""
    import lib.conversations.project_charter as pc
    p = os.path.abspath('/tmp/charter-cc-cap')
    cap = pc._MAX_DECISIONS
    with flask_app.app_context():
        pc.commit_charter(p, add_decision='D0', summary='D0',
                          updated_by_conv='seed', user_id=TEST_OWNER_USER_ID)

        def sibling():
            pc.commit_charter(p, add_decision='SIB', summary='S',
                              updated_by_conv='sibB', user_id=TEST_OWNER_USER_ID)

        hooked, real = _interleave(pc, sibling)
        pc.read_charter = hooked
        try:
            pc.commit_charter(p, add_decision='MINE', summary='M',
                              updated_by_conv='human', user_id=TEST_OWNER_USER_ID)
        finally:
            pc.read_charter = real
        decisions = pc.read_charter(p, user_id=TEST_OWNER_USER_ID)['decisions']

    texts = [d['text'] for d in decisions]
    assert len(decisions) <= cap
    assert texts.count('MINE') == 1, f'a replay must not double-append; {texts}'
    assert texts.count('SIB') == 1, f'nor duplicate the sibling; {texts}'
