"""tests/test_project_dispatch.py — Pillar #5 brain-driven dispatch.

The spine that closes "无需人手": the board's open epics get STARTED without a
human. Two load-bearing properties:

  • **select_dispatchable is genuinely-pickable-only.** It must EXCLUDE an epic
    with an unfinished dependency AND an epic with a live (unexpired) claim;
    it must INCLUDE an open epic whose deps are all done. (Built on read_board,
    so an EXPIRED claim already reads open → reuses the one deadlock path.)
  • **Claim-on-dispatch = idempotency guard.** dispatch_epic claims the epic,
    so a second dispatch pass sees it claimed and does NOT re-select it — no
    concurrent double-dispatch.

Two MANDATORY source-level negative controls:
  • NC-1: no-op the dependency/claim filter in select_dispatchable → an
    unfinished-dep / live-claimed epic leaks into the candidate set → the
    exclusion test FAILS.
  • NC-2: no-op the claim-on-dispatch → after a dispatch, the epic is still
    open → a second select re-selects it → the idempotency test FAILS.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.execute('DELETE FROM message_queue')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _set_lease(flask_app, project_path, task_id, lease_ms):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=? WHERE id=? AND project_path=?',
                   (lease_ms, task_id, project_path))
        db.commit()


def _queue_kinds(flask_app, conv_id):
    from lib.message_queue import get_queue
    with flask_app.app_context():
        return [q['kind'] for q in get_queue(conv_id)]


# ════════════════════════════════════════════════════════════════════
#  select_dispatchable
# ════════════════════════════════════════════════════════════════════

def test_select_includes_open_with_deps_done(flask_app):
    # Pure selection check: mark the dependency done via a DIRECT status write
    # (NOT complete_task, which would auto-dispatch the dependent via the
    # on_epic_completed trigger and claim it before we can observe it open).
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        dep = post_task('/d/ok', 'cA', 'dependency')['id']
        epic = post_task('/d/ok', 'cA', 'the work', depends_on=[dep])['id']
        db = get_thread_db(DOMAIN_CHAT)
        db.execute("UPDATE project_tasks SET status='done' WHERE id=?", (dep,))
        db.commit()
        cands = select_dispatchable('/d/ok')
    ids = [c['id'] for c in cands]
    assert epic in ids, 'an open epic with all deps done must be dispatchable'


def test_select_excludes_unfinished_dependency(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        dep = post_task('/d/dep', 'cA', 'dependency')['id']   # NOT completed
        epic = post_task('/d/dep', 'cA', 'the work', depends_on=[dep])['id']
        cands = select_dispatchable('/d/dep')
    ids = [c['id'] for c in cands]
    assert epic not in ids, 'epic with an unfinished dependency must be excluded'
    # the dependency itself (no deps) IS dispatchable
    assert dep in ids


def test_select_excludes_live_claim(flask_app):
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        epic = post_task('/d/claim', 'cA', 'work')['id']
        claim_task('/d/claim', 'cB', epic)   # live claim
        cands = select_dispatchable('/d/claim')
    assert epic not in [c['id'] for c in cands], \
        'a live-claimed epic must NOT be dispatchable'


def test_select_includes_after_lease_expired(flask_app):
    """An EXPIRED claim reads open (via read_board) → becomes dispatchable
    again — reuses the single deadlock path, no second one."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        epic = post_task('/d/exp', 'cA', 'work')['id']
        claim_task('/d/exp', 'cB', epic)
    _set_lease(flask_app, '/d/exp', epic, 1)  # expired
    with flask_app.app_context():
        cands = select_dispatchable('/d/exp')
    assert epic in [c['id'] for c in cands], \
        'an expired-claim epic must be dispatchable again'


# ════════════════════════════════════════════════════════════════════
#  dispatch_epic — claim-on-dispatch idempotency
# ════════════════════════════════════════════════════════════════════

def test_dispatch_enqueues_workflow_kickoff_and_claims(flask_app):
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_dispatch import (
        BRAIN_DISPATCH_MARKER, dispatch_epic, select_dispatchable,
    )
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        epic_id = post_task('/d/disp', 'cA', 'work')['id']
        epic = select_dispatchable('/d/disp')[0]
        res = dispatch_epic('/d/disp', epic, 'cTARGET')
        assert res['ok'] and res['queueId']
        board = read_board('/d/disp')
        # The epic is now claimed by the target → NOT re-selectable.
        again = select_dispatchable('/d/disp')
    # claimed under the target conv
    t = board['tasks'][0]
    assert t['status'] == 'claimed' and t['owner_conv_id'] == 'cTARGET'
    # a workflow_step kickoff is queued, carrying the brain-dispatch marker
    assert KIND_WORKFLOW in _queue_kinds(flask_app, 'cTARGET')
    assert BRAIN_DISPATCH_MARKER  # marker constant is exported
    # idempotency: a second pass must NOT re-select the just-dispatched epic
    assert epic_id not in [c['id'] for c in again], \
        'claim-on-dispatch must prevent re-selecting the same epic'


def test_dispatch_refused_when_claimed_by_other(flask_app):
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_dispatch import dispatch_epic
    with flask_app.app_context():
        epic_id = post_task('/d/dr', 'cA', 'work')['id']
        claim_task('/d/dr', 'cOTHER', epic_id)   # someone else holds it live
        res = dispatch_epic('/d/dr', {'id': epic_id, 'title': 'work'}, 'cTARGET')
    assert res['ok'] is False, 'must not dispatch an epic held live by another conv'
    assert _queue_kinds(flask_app, 'cTARGET') == [], 'no kickoff enqueued on refusal'


# ════════════════════════════════════════════════════════════════════
#  on_epic_completed trigger
# ════════════════════════════════════════════════════════════════════

def test_on_complete_dispatches_unblocked_dependent(flask_app):
    """Completing a dependency makes its dependent dispatchable; the trigger
    kicks the dependent off to its poster conversation."""
    from lib.conversations.project_board import complete_task, post_task, read_board
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        dep = post_task('/d/trig', 'cPOSTER', 'dependency')['id']
        dependent = post_task('/d/trig', 'cPOSTER', 'dependent work', depends_on=[dep])['id']
        # Completing the dependency fires on_epic_completed internally.
        complete_task('/d/trig', 'cPOSTER', dep)
        board = read_board('/d/trig')
    # the dependent is now claimed (dispatched) under its poster conv
    dependent_row = [t for t in board['tasks'] if t['id'] == dependent][0]
    assert dependent_row['status'] == 'claimed'
    assert dependent_row['owner_conv_id'] == 'cPOSTER'
    assert KIND_WORKFLOW in _queue_kinds(flask_app, 'cPOSTER')


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════
#  sweep_dispatch — the heartbeat (idempotency + busy guard)
# ════════════════════════════════════════════════════════════════════

def test_sweep_dispatches_cold_start_first_epic(flask_app):
    """The heartbeat starts the FIRST epic on a fresh board — no completion
    has happened, no human typing. The completion trigger could never do this."""
    from lib.conversations.project_board import read_board
    from lib.conversations.project_dispatch import sweep_dispatch
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        from lib.conversations.project_board import post_task
        post_task('/s/cold', 'cPOSTER', 'first epic')
        n = sweep_dispatch('/s/cold')
        board = read_board('/s/cold')
    assert n == 1, 'cold-start first epic must be dispatched by the sweep'
    assert board['tasks'][0]['status'] == 'claimed'
    assert KIND_WORKFLOW in _queue_kinds(flask_app, 'cPOSTER')


def test_two_sweeps_produce_exactly_one_kickoff(flask_app):
    """Idempotency: the sweep runs repeatedly over the same board. A
    dispatched epic (now claimed) must NOT be re-dispatched on the next sweep
    → exactly ONE kickoff total.

    DRAIN the kickoff queue between sweeps so the ``_epic_already_queued``
    busy-guard CANNOT be what suppresses the second dispatch — this isolates
    the CLAIM-on-dispatch as the load-bearing idempotency guard (so NC-3,
    which disables the claim, genuinely bites this test)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import sweep_dispatch
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import KIND_WORKFLOW, get_queue
    with flask_app.app_context():
        post_task('/s/idem', 'cPOSTER', 'epic')
        n1 = sweep_dispatch('/s/idem')
        q_after1 = [x for x in get_queue('cPOSTER') if x['kind'] == KIND_WORKFLOW]
        # Drain the queued kickoff (as if it had been dispatched/consumed) so
        # the second sweep's idempotency rests SOLELY on the claim.
        get_thread_db(DOMAIN_CHAT).execute(
            'DELETE FROM message_queue WHERE conv_id=?', ('cPOSTER',))
        get_thread_db(DOMAIN_CHAT).commit()
        n2 = sweep_dispatch('/s/idem')   # second sweep, queue drained
        q_after2 = [x for x in get_queue('cPOSTER') if x['kind'] == KIND_WORKFLOW]
    assert n1 == 1, 'first sweep dispatches the epic'
    assert len(q_after1) == 1, 'first sweep enqueues exactly one kickoff'
    assert n2 == 0, 'second sweep must NOT re-dispatch the claimed epic (claim guard)'
    assert len(q_after2) == 0, 'no second kickoff after the queue was drained'


def test_sweep_skips_busy_conv(flask_app, monkeypatch):
    """An epic whose target conv has a live task is skipped (no stacked
    kickoff into a busy conversation)."""
    import lib.conversations.project_dispatch as pd
    from lib.conversations.project_board import post_task
    # Make the poster conversation look busy.
    monkeypatch.setattr(pd, '_conv_has_live_task',
                        lambda conv_id: conv_id == 'cBUSY')
    with flask_app.app_context():
        post_task('/s/busy', 'cBUSY', 'epic')
        n = pd.sweep_dispatch('/s/busy')
    assert n == 0, 'must not dispatch into a busy target conversation'
    assert _queue_kinds(flask_app, 'cBUSY') == [], 'no kickoff stacked on a busy conv'


def test_sweep_capped(flask_app):
    """A single sweep can't flood — capped at max_per_sweep."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import sweep_dispatch
    with flask_app.app_context():
        for i in range(5):
            post_task('/s/cap', 'cP', f'epic {i}')
        n = sweep_dispatch('/s/cap', max_per_sweep=2)
    assert n == 2, f'sweep must cap at max_per_sweep=2, dispatched {n}'


from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC1_dropping_filters_leaks_candidates(flask_app):
    """NC-1: no-op the open-status + dependency filters → an unfinished-dep
    epic AND a live-claimed epic leak into the candidate set."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import claim_task, post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM project_tasks WHERE project_path='/nc1d'")
            get_thread_db(DOMAIN_CHAT).commit()
            dep = post_task('/nc1d', 'cA', 'dep')['id']
            blocked = post_task('/nc1d', 'cA', 'blocked', depends_on=[dep])['id']
            claimed = post_task('/nc1d', 'cA', 'claimed')['id']
            claim_task('/nc1d', 'cB', claimed)
            cands = [c['id'] for c in pd.select_dispatchable('/nc1d')]
        # With the filters no-opped, BOTH the unfinished-dep epic and the
        # live-claimed epic leak in.
        assert blocked in cands and claimed in cands, \
            'NC-1: dropping the filters must leak the excluded epics'

    _patch_restore(
        _DISPATCH_SRC,
        ("        if t['status'] != 'open':\n"
         "            continue\n"
         "        # ── block-cooldown filter: an epic that hit a genuine external gate was\n"
         "        #    stamped blocked_until = now + an escalating cooldown by block_task.\n"
         "        #    While that window is live, SKIP it — this is what stops the ~30-min\n"
         "        #    lease-expiry re-dispatch churn (a billed agent turn each cycle to\n"
         "        #    re-discover the same unmet dep). At-READ-time expiry: once the\n"
         "        #    window lapses the epic is pickable again (a resolved dep IS\n"
         "        #    retried), with NO reaper and NO human un-block gate. ──\n"
         "        if int(t.get('blocked_until') or 0) > now_ms:\n"
         "            continue\n"
         "        # ── pending-question filter: an epic blocked WITH a structured human\n"
         "        #    question waits for the ANSWER, not for time — re-dispatching\n"
         "        #    before the human answers can only re-discover the same gate (the\n"
         "        #    billed-turn loop this redesign exists to kill). answer_task\n"
         "        #    clears the question + cooldown, so an ANSWERED epic falls through\n"
         "        #    to normal pick-up (and carries its answer into the kickoff). ──\n"
         "        if t.get('block_question') and not (t.get('human_answer') or '').strip():\n"
         "            continue\n"
         "        # ── dependency filter: every dependency must be DONE. An epic with an\n"
         "        #    unfinished (or unknown) dependency is NOT yet pickable. ──\n"
         "        deps = t.get('depends_on') or []\n"
         "        if any(d not in done_ids for d in deps):\n"
         "            continue"),
        "        pass  # NC-1 (filters disabled)",
        run,
    )


def test_NC2_no_claim_on_dispatch_allows_redispatch(flask_app):
    """NC-2: no-op the claim-on-dispatch → after dispatch the epic stays open
    → a second select re-selects it (double-dispatch)."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM project_tasks WHERE project_path='/nc2d'")
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM message_queue WHERE conv_id='cT'")
            get_thread_db(DOMAIN_CHAT).commit()
            epic_id = post_task('/nc2d', 'cA', 'work')['id']
            epic = pd.select_dispatchable('/nc2d')[0]
            pd.dispatch_epic('/nc2d', epic, 'cT')
            again = [c['id'] for c in pd.select_dispatchable('/nc2d')]
        # With no claim written, the epic is still open → re-selected.
        assert epic_id in again, \
            'NC-2: without claim-on-dispatch the epic is re-dispatchable'

    _patch_restore(
        _DISPATCH_SRC,
        ("        claim = claim_task(project_path, target_conv_id, task_id,\n"
         "                           dispatched=True)\n"
         "        if not claim.get('ok'):\n"
         "            return {'ok': False, 'error': claim.get('error', 'claim_failed')}"),
        "        claim = {'ok': True}  # NC-2 (claim-on-dispatch disabled)",
        run,
    )


def test_NC3_no_claim_breaks_two_sweep_idempotency(flask_app):
    """NC-3 (sweep): no-op the claim-on-dispatch → the epic stays open after
    the first sweep → the SECOND sweep re-dispatches it → two kickoffs (the
    double-dispatch the idempotency guard prevents)."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import post_task
        from lib.message_queue import KIND_WORKFLOW, get_queue
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_tasks WHERE project_path='/nc3s'")
            db.execute("DELETE FROM message_queue WHERE conv_id='cNC3'")
            db.commit()
            from lib.database import DOMAIN_CHAT, get_thread_db
            post_task('/nc3s', 'cNC3', 'epic')
            pd.sweep_dispatch('/nc3s')
            # Drain the queue so ONLY the claim can prevent re-dispatch.
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM message_queue WHERE conv_id='cNC3'")
            get_thread_db(DOMAIN_CHAT).commit()
            n2 = pd.sweep_dispatch('/nc3s')
            kickoffs = [x for x in get_queue('cNC3') if x['kind'] == KIND_WORKFLOW]
        # With the claim disabled + queue drained, the epic is still open →
        # the second sweep re-dispatches it → a duplicate kickoff.
        assert n2 == 1 and len(kickoffs) == 1, \
            'NC-3: without claim-on-dispatch the second sweep re-dispatches (double-dispatch)'

    _patch_restore(
        _DISPATCH_SRC,
        ("        claim = claim_task(project_path, target_conv_id, task_id,\n"
         "                           dispatched=True)\n"
         "        if not claim.get('ok'):\n"
         "            return {'ok': False, 'error': claim.get('error', 'claim_failed')}"),
        "        claim = {'ok': True}  # NC-3 (claim disabled)",
        run,
    )


def test_NC4_no_busy_guard_stacks_duplicate(flask_app, monkeypatch):
    """NC-4 (sweep): no-op the busy guard → a sweep dispatches into a busy
    conv (stacking a kickoff it should have skipped)."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        # Force the target conv to look busy; with the guard disabled the
        # sweep dispatches anyway.
        monkeypatch.setattr(pd, '_conv_has_live_task', lambda conv_id: True)
        from lib.conversations.project_board import post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_tasks WHERE project_path='/nc4s'")
            db.execute("DELETE FROM message_queue WHERE conv_id='cNC4'")
            db.commit()
            post_task('/nc4s', 'cNC4', 'epic')
            n = pd.sweep_dispatch('/nc4s')
        # With the busy guard no-opped, the busy conv gets a kickoff anyway.
        assert n == 1, 'NC-4: with the busy guard disabled, a busy conv is dispatched into'

    _patch_restore(
        _DISPATCH_SRC,
        "            if _conv_has_live_task(target) or _epic_already_queued(target, epic.get('id', '')):\n                continue",
        "            if False:  # NC-4 (busy guard disabled)\n                continue",
        run,
    )


