"""Tests for the Pillar #7 WATCH lane (lib/conversations/project_watch.py).

Owner's non-negotiable coverage:
  (a) NEUTER proving each item's response genuinely reads LIVE pillar state
      (not a stub) — mirrors the empty-state neuter from the status lane.
  (b) the SOURCE guard: concern/question text must never reach the ambient
      prompt-injection path. NARROWED 2026-07-30 — a GOAL now deliberately DOES
      inject (its own [PROJECT GOALS] block), so a blanket "this module must not
      appear in ambient context" ban would forbid the shipped design.
  (c) a GOAL reaches agents by EXISTING (never via the charter), and the promote
      bridge REFUSES one; concern/question still route through charter commit.
  (d) append-only response trail + bounded retention + monotonic ordering.
  (e) the owner's acceptance criteria: the goal text is in the REAL injected
      prompt with charter.content EMPTY, and CHARTER_TOOLS ships no commit tool.

DB-free where possible; the store uses a real in-memory sqlite3 connection
(the module uses `?` placeholders + db.commit()). Pillar reads are lazily
imported, so we monkeypatch the SOURCE modules.
"""

import functools
import threading

import pytest

import lib.conversations.project_watch as pw
from tests._source_scan import strip_comments

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture(autouse=True)
def _bind_business_test_principal(monkeypatch):
    """Bind watch-lane behavior tests to the explicit fixture owner."""
    for name in (
        'add_watch_item',
        'address_watch_item',
        'delete_watch_item',
        'edit_watch_item',
        'list_watch_items',
        'answer_follow_up',
        'promote_watch_item',
        'render_goals_injection_block',
        'set_watch_status',
    ):
        original = getattr(pw, name)

        @functools.wraps(original)
        def call_as_test_user(*args, _original=original, **kwargs):
            kwargs.setdefault('user_id', 1)
            return _original(*args, **kwargs)

        monkeypatch.setattr(pw, name, call_as_test_user)


@pytest.fixture
def db(chat_sidecar):
    """Sidecar-backed watch lane (replaces the in-memory fake get_thread_db
    conn, which is inert under the sidecar branch).  Per-test isolation:
    events pruned + watch items deleted for every path this module uses."""
    import tests._seed as seed
    seed.clear_events()
    seed.clear_records('project_charter')
    seed.clear_watch('/proj/a', '/proj/b', '/proj/x', user_id=1)
    yield chat_sidecar


_NORTH_STAR = 'Ship the watch lane so the human sees drift over time.'
_EPIC_TITLE = 'Refactor the evaluation harness'


def test_background_watch_refresh_coalesces_latest_trigger(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    project_path = '/proj/background-watch'

    def blocking(path, trigger, *, user_id):
        calls.append((user_id, path, trigger))
        if len(calls) == 1:
            entered.set()
            release.wait(2)

    monkeypatch.setattr(pw, '_address_open_items_blocking', blocking)
    assert pw._schedule_background_address(
        project_path, 'first', user_id=TEST_OWNER_USER_ID)
    assert entered.wait(1)
    assert pw._schedule_background_address(
        project_path, 'latest', user_id=TEST_OWNER_USER_ID)

    release.set()
    assert pw._wait_for_background_watch(2)
    assert calls == [
        (TEST_OWNER_USER_ID, project_path, 'first'),
        (TEST_OWNER_USER_ID, project_path, 'latest'),
    ]
    snapshot = pw.background_watch_lane_snapshot()
    assert snapshot['capacity'] == pw._BACKGROUND_CAPACITY
    assert snapshot['trackedScopes'] == 0


def _wire_pillars(monkeypatch, *, done=5):
    """Monkeypatch the SOURCE modules collect_pillar_state lazily imports."""
    import lib.conversations.project_board as pb
    import lib.conversations.project_charter as pc
    import lib.presence.registry as pr
    import lib.conversations.project_feed as pf
    import lib.conversations.project_summary as ps
    monkeypatch.setattr(pb, 'read_board', lambda p, **_kwargs: {
        'tasks': [{'title': _EPIC_TITLE, 'status': 'claimed',
                   'owner_conv_id': 'convA', 'kind': 'epic'}],
        'open': 2, 'claimed': 1, 'done': done, 'blocked': 0})
    monkeypatch.setattr(pc, 'read_charter', lambda p, **_kwargs: {
        'exists': True, 'version': 8, 'content': _NORTH_STAR,
        'decisions': [{'text': 'No fan-out verb.'}]})
    monkeypatch.setattr(pc, 'pending_proposals', lambda p, **_kwargs: [])
    monkeypatch.setattr(pr, 'snapshot', lambda p: {'peers': [{'convId': 'convA'}]})
    monkeypatch.setattr(pf, 'read_project_feed', lambda p, **k: {'events': []})
    monkeypatch.setattr(ps, 'project_digest_entries', lambda p, **k: [])


class _DispatchSpy:
    def __init__(self, answer='On track; no drift.'):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return (self.answer, {})


def _wire_llm(monkeypatch, spy):
    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', spy)


# ════════════════════════════════════════════════════════════════════
#  Human CRUD
# ════════════════════════════════════════════════════════════════════

def test_add_lists_and_validates(db):
    r = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% rater agreement', user_id=TEST_OWNER_USER_ID)
    assert r['ok'] and r['item']['kind'] == 'goal'
    assert pw.add_watch_item('/proj/x', 'nonsense', 'x', user_id=TEST_OWNER_USER_ID)['error'] == 'invalid kind'
    assert pw.add_watch_item('/proj/x', 'goal', '   ', user_id=TEST_OWNER_USER_ID)['error'] == 'empty text'
    items = pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items']
    assert len(items) == 1 and items[0]['text'] == 'Reach 95% rater agreement'
    assert items[0]['responses'] == []


def test_edit_resolve_reopen_delete(db):
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Artifacts may desync', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    assert pw.edit_watch_item(item_id, text='Artifacts desync under load', user_id=TEST_OWNER_USER_ID)['ok']
    assert pw.set_watch_status(item_id, 'resolved', user_id=TEST_OWNER_USER_ID)['ok']
    assert pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['status'] == 'resolved'
    assert pw.set_watch_status(item_id, 'open', user_id=TEST_OWNER_USER_ID)['ok']
    assert pw.set_watch_status(item_id, 'bogus', user_id=TEST_OWNER_USER_ID)['error'] == 'invalid status'
    assert pw.delete_watch_item(item_id, user_id=TEST_OWNER_USER_ID)['ok']
    assert pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'] == []


# ════════════════════════════════════════════════════════════════════
#  (a) NEUTER — the response reads LIVE pillar state
# ════════════════════════════════════════════════════════════════════

def test_response_prompt_carries_live_pillar_state(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    resp = pw.address_watch_item(item_id, trigger='manual', force=True, user_id=TEST_OWNER_USER_ID)
    assert resp and resp['response'] == 'On track; no drift.'
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR in prompt, 'live north-star missing from watch-item prompt'
    assert _EPIC_TITLE in prompt, 'live in-flight epic missing from watch-item prompt'
    # The human's item text also rides into the prompt.
    assert 'Ship the watch lane' in prompt


def test_NC_response_on_empty_state_loses_live_values(db, monkeypatch):
    """NEUTER: stub the shared synthesis-source builder to ignore live state
    (as a stub generator would). The live north-star + epic then vanish from
    the prompt — proving the positive assertion is load-bearing on real pillar
    reads, not coincidence."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    import lib.conversations.project_status as pstat
    monkeypatch.setattr(pstat, '_build_synthesis_source',
                        lambda ps: 'STUBBED — no live state')
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.address_watch_item(item_id, trigger='manual', force=True, user_id=TEST_OWNER_USER_ID)
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR not in prompt
    assert _EPIC_TITLE not in prompt


# ════════════════════════════════════════════════════════════════════
#  (d) Append-only trail + retention + monotonic ordering + staleness gate
# ════════════════════════════════════════════════════════════════════

def test_staleness_gate_and_append_only_trail(db, monkeypatch):
    _wire_pillars(monkeypatch, done=5)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Watch drift', user_id=TEST_OWNER_USER_ID)['item']['item_id']

    r1 = pw.address_watch_item(item_id, trigger='manual', user_id=TEST_OWNER_USER_ID)
    assert r1['seq'] == 1 and len(spy.calls) == 1

    # Nothing changed → cached, NO second LLM call, no new response row.
    r2 = pw.address_watch_item(item_id, trigger='on_open', user_id=TEST_OWNER_USER_ID)
    assert r2['seq'] == 1 and len(spy.calls) == 1

    # Pillar state moves (an epic completes) → fresh response appended.
    _wire_pillars(monkeypatch, done=6)
    r3 = pw.address_watch_item(item_id, trigger='epic_completed', user_id=TEST_OWNER_USER_ID)
    assert r3['seq'] == 2 and len(spy.calls) == 2

    trail = pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['responses']
    assert [t['seq'] for t in trail] == [2, 1], 'trail not newest-first / monotonic'


def test_response_retention_prunes(db, monkeypatch):
    monkeypatch.setattr(pw, '_RESPONSES_KEEP', 3)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Retention', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    for done in range(6):
        _wire_pillars(monkeypatch, done=done)
        pw.address_watch_item(item_id, trigger='epic_completed', user_id=TEST_OWNER_USER_ID)
    trail = pw.list_watch_items('/proj/x', resp_limit=100, user_id=TEST_OWNER_USER_ID)['items'][0]['responses']
    assert len(trail) == 3
    assert [t['seq'] for t in trail] == [6, 5, 4]


def test_editing_text_forces_readdress(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'question', 'Q1?', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.address_watch_item(item_id, user_id=TEST_OWNER_USER_ID)
    assert len(spy.calls) == 1
    # Edit the text → fingerprint cleared → next address re-synthesizes even
    # though pillar state is unchanged.
    pw.edit_watch_item(item_id, text='Q1 revised?', user_id=TEST_OWNER_USER_ID)
    pw.address_watch_item(item_id, user_id=TEST_OWNER_USER_ID)
    assert len(spy.calls) == 2


# ════════════════════════════════════════════════════════════════════
#  (c) A goal is live by EXISTING; the promote bridge is concern/question only
# ════════════════════════════════════════════════════════════════════

def test_promote_refuses_a_goal(db, monkeypatch):
    """REVERSED IN PLACE, twice — the archaeology is the point.

    v1 asserted ``calls['add_decision'].startswith('[Goal')``: a goal was
    promoted as a committed DECISION. That was measurably broken — the decision
    list is injected tail-first through a 20-entry window and FIFO-capped at
    100, so a goal parked there silently stops reaching the model.

    v2 (2026-07-30 morning) asserted ``calls['content'] == <goal text>``: a goal
    REPLACED the charter's north-star column. Better, but still wrong for the
    owner's actual requirement — it made a goal's effectiveness conditional on a
    human remembering to promote it, and it created TWO copies of one sentence,
    which is what forced a diverged state, a replacement preview and a version
    gate into existence.

    v3 (now): a goal never travels to the charter at all. It is injected because
    it EXISTS (:func:`render_goals_injection_block`), so promotion is not a
    meaningful operation on one and is refused. Kept rather than deleted because
    a deleted test lets the next person re-introduce either earlier design."""
    calls = {}

    def _fake_commit(project_path, **kw):
        calls.update(kw)
        return {'ok': True, 'version': 9}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)

    item_id = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% agreement', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    res = pw.promote_watch_item(item_id, updated_by_conv='convH', expected_version=8, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False
    assert res['error'] == 'goal_not_promotable'
    # The charter must not have been touched in ANY form — neither the
    # north-star column nor the decision list.
    assert calls == {}, f'promoting a goal reached the charter: {calls!r}'
    # And the goal is nonetheless live, which is the whole point.
    assert pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID).count('Reach 95% agreement') == 1


def test_promote_concern_appends_decision_with_kind_and_summary(db, monkeypatch):
    """A concern/question is NOT a goal: it keeps the committed-decision path.

    Also pins the two fields this bridge used to omit — ``decision_kind`` and
    ``summary``. The per-turn injection renders ONLY the summary line
    (``_decision_headline``), so omitting it left every promoted concern showing
    as a first line clipped by the generic fallback."""
    calls = {}

    def _fake_commit(project_path, *, user_id, content=None, add_decision=None,
                     decision_kind='', summary='', updated_by_conv='',
                     expected_version=None, resolves_proposal=''):
        calls.update(content=content, add_decision=add_decision,
                     decision_kind=decision_kind, summary=summary)
        return {'ok': True, 'version': 4}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)

    item_id = pw.add_watch_item('/proj/x', 'concern',
                                'Artifacts may desync under load', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    assert pw.promote_watch_item(item_id, updated_by_conv='convH', user_id=TEST_OWNER_USER_ID)['ok']
    assert calls['content'] is None, 'a concern must never overwrite the north star'
    assert calls['add_decision'].startswith('[Concern')
    assert 'Artifacts may desync under load' in calls['add_decision']
    assert calls['decision_kind'] == 'invariant'
    assert calls['summary'] == 'Artifacts may desync under load'


def test_promote_propagates_charter_version_conflict(db, monkeypatch):
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: {'ok': False, 'error': 'version_conflict',
                                         'current_version': 12})
    item_id = pw.add_watch_item('/proj/x', 'concern', 'c', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    res = pw.promote_watch_item(item_id, expected_version=5, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False and res['error'] == 'version_conflict'
    assert pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['promotedAudit'] is False, \
        'a failed commit must not mark the item promoted'
    assert pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['promotionState'] == pw.PROMOTION_NONE


# ════════════════════════════════════════════════════════════════════
#  The [PROJECT GOALS] block — a goal's ONE route to agents
# ════════════════════════════════════════════════════════════════════

def test_goals_block_ships_open_goals_only(db):
    """kind + status are the WHOLE state model of a goal's prompt presence."""
    g1 = pw.add_watch_item('/proj/x', 'goal', 'Goal one', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.add_watch_item('/proj/x', 'goal', 'Goal two', user_id=TEST_OWNER_USER_ID)
    pw.add_watch_item('/proj/x', 'concern', 'A worry I am tracking', user_id=TEST_OWNER_USER_ID)
    pw.add_watch_item('/proj/x', 'question', 'An open question', user_id=TEST_OWNER_USER_ID)

    block = pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)
    assert '[PROJECT GOALS]' in block
    assert 'Goal one' in block and 'Goal two' in block
    # A concern/question is something the human TRACKS, not intent they DECLARE.
    # Injecting an unresolved worry as direction would steer on a question.
    assert 'A worry I am tracking' not in block
    assert 'An open question' not in block

    # Resolving is the withdrawal lever — the same one used for the cadence.
    pw.set_watch_status(g1, 'resolved', user_id=TEST_OWNER_USER_ID)
    block2 = pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)
    assert 'Goal one' not in block2 and 'Goal two' in block2

    # Deleting the card removes the goal outright (the card IS the goal).
    pw.delete_watch_item(g1, user_id=TEST_OWNER_USER_ID)
    assert 'Goal one' not in pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)


def test_goals_block_is_empty_when_no_open_goals(db):
    """Empty lane ⇒ ZERO prompt weight, the same contract as the charter block."""
    assert pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID) == ''
    pw.add_watch_item('/proj/x', 'concern', 'Only a concern here', user_id=TEST_OWNER_USER_ID)
    assert pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID) == ''
    gid = pw.add_watch_item('/proj/x', 'goal', 'A goal', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    assert pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID) != ''
    pw.set_watch_status(gid, 'resolved', user_id=TEST_OWNER_USER_ID)
    assert pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID) == ''


def test_goals_block_never_carries_the_brain_responses(db, monkeypatch):
    """The synthesized responses are the brain talking to the HUMAN.

    Feeding them back to agents would promote one summarizer's opinion into
    project direction, so the block carries the human's text only."""
    _wire_pillars(monkeypatch)
    _wire_llm(monkeypatch, _DispatchSpy(answer='BRAIN OPINION: drifting badly.'))
    gid = pw.add_watch_item('/proj/x', 'goal', 'Ship the lane', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.address_watch_item(gid, force=True, user_id=TEST_OWNER_USER_ID)
    trail = pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['responses']
    assert trail and 'BRAIN OPINION' in trail[0]['response'], 'fixture did not arm'
    assert 'BRAIN OPINION' not in pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)


def test_goals_block_is_bounded_and_says_what_it_dropped(db, monkeypatch):
    """Goals ride EVERY turn of EVERY sibling, so the lane must be bounded —
    and truncation must be VISIBLE, never a silently-shipped subset."""
    monkeypatch.setattr(pw, '_GOALS_BLOCK_MAX_CHARS', 120)
    for i in range(6):
        pw.add_watch_item('/proj/x', 'goal', f'Goal number {i} ' + 'x' * 40, user_id=TEST_OWNER_USER_ID)
    block = pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)
    assert len(block) < 700
    assert 'more goal(s) not shown' in block
    assert 'Goal number 0' in block, 'oldest-first: earliest goals are kept'


def test_multiple_goals_all_inject_no_uniqueness_rule(db):
    """A project may legitimately have several goals; all of them inject.

    The morning design forced single-goal-ness because ONE charter column had to
    hold the text. With no copy there is nothing to be unique about, so no
    constraint, no displacement, no diverged sibling."""
    pw.add_watch_item('/proj/x', 'goal', 'Goal A', user_id=TEST_OWNER_USER_ID)
    pw.add_watch_item('/proj/x', 'goal', 'Goal B', user_id=TEST_OWNER_USER_ID)
    block = pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID)
    assert 'Goal A' in block and 'Goal B' in block
    items = pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items']
    assert all(i['injected'] for i in items if i['kind'] == 'goal')
    # And none of them claims a charter promotion.
    assert all(i['promotionState'] == pw.PROMOTION_NONE for i in items)


def test_goals_block_is_project_scoped(db):
    pw.add_watch_item('/proj/a', 'goal', 'Goal for A', user_id=TEST_OWNER_USER_ID)
    pw.add_watch_item('/proj/b', 'goal', 'Goal for B', user_id=TEST_OWNER_USER_ID)
    a = pw.render_goals_injection_block('/proj/a', user_id=TEST_OWNER_USER_ID)
    assert 'Goal for A' in a and 'Goal for B' not in a


# ════════════════════════════════════════════════════════════════════
#  The COMPUTED promotion verdict — concern/question only, two states
# ════════════════════════════════════════════════════════════════════

def test_promotion_state_is_never_active_for_a_goal(db):
    """A goal has no promotion state: 'is it promoted' is not a question about
    it. Even when its text happens to equal the charter's north star (the human
    may well have typed the same sentence in both places), it must NOT be
    reported as promoted — that badge would imply the charter is why it reaches
    agents, and resolving the goal would then silently stop working as the
    withdrawal lever."""
    charter = {'exists': True, 'version': 3, 'content': 'Ship the lane',
               'decisions': [{'text': 'Ship the lane'}]}
    v = pw.promotion_state({'kind': 'goal', 'text': 'Ship the lane'}, charter)
    assert v['state'] == pw.PROMOTION_NONE
    assert v['divergedSide'] == '', 'divergedSide must stay an empty compat key'
    assert not hasattr(pw, 'PROMOTION_DIVERGED'), (
        'the diverged state was removed with the duplication that required it')


def test_promotion_state_for_concern_reads_the_decision_list(db):
    charter = {'exists': True, 'version': 2, 'content': 'North star',
               'decisions': [{'text': '[Concern — promoted by owner] Desync'}]}
    item = {'kind': 'concern', 'text': 'Desync'}
    assert pw.promotion_state(item, charter)['state'] == pw.PROMOTION_ACTIVE
    # A concern must NOT be satisfied by matching the north-star column.
    assert pw.promotion_state({'kind': 'concern', 'text': 'North star'},
                              charter)['state'] == pw.PROMOTION_NONE
    # Whitespace reflow still counts as the same text.
    assert pw.promotion_state({'kind': 'concern', 'text': ' Desync  '},
                              charter)['state'] == pw.PROMOTION_ACTIVE
    # A deleted charter reads as not-promoted, never as still-live.
    assert pw.promotion_state(item, {'exists': False, 'content': '',
                                     'decisions': []})['state'] == pw.PROMOTION_NONE


def test_delete_and_resolve_never_touch_the_charter(db, monkeypatch):
    """Removing/resolving a tracking CARD must not write to the charter."""
    calls = []
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: (calls.append(k) or {'ok': True, 'version': 1}))
    monkeypatch.setattr(pc, 'delete_charter',
                        lambda *a, **k: calls.append('delete'))
    item_id = pw.add_watch_item('/proj/x', 'goal', 'G', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.set_watch_status(item_id, 'resolved', user_id=TEST_OWNER_USER_ID)
    pw.delete_watch_item(item_id, user_id=TEST_OWNER_USER_ID)
    assert calls == [], 'resolve/delete must not write to the charter'


def test_goal_cap_is_independent_of_the_charter_content_cap(db):
    """REVERSED IN PLACE 2026-07-30. This asserted the two caps were EQUAL,
    because a goal was copied into charter.content and an unequal ceiling meant
    the copy-back could truncate. A goal is no longer copied anywhere, so the
    two are genuinely independent settings and coupling them would be a
    coincidence dressed as an invariant. What still matters is that the goal cap
    is a real, finite prompt-weight bound."""
    assert isinstance(pw._ITEM_TEXT_MAX, int) and pw._ITEM_TEXT_MAX > 0
    assert isinstance(pw._GOALS_BLOCK_MAX_CHARS, int)
    assert pw._GOALS_BLOCK_MAX_CHARS > 0


# ════════════════════════════════════════════════════════════════════
#  (b) HUMAN-FACING ONLY — not on the system-context injection path
# ════════════════════════════════════════════════════════════════════

def _injection_sources():
    """Return the canonical context-provider source."""
    from pathlib import Path
    path = Path('lib/tasks_pkg/context_composer/_providers.py')
    return {str(path): path.read_text(encoding='utf-8')}


def test_only_the_goals_renderer_is_on_the_injection_path():
    """NARROWED 2026-07-30 — the premise of the old blanket ban flipped.

    The owner's requirement is that a GOAL reaches the
    prompt directly ("goals are goals; they should work without being in the
    charter"). A ban that forbids the shipped design is not a guard, it is a
    tripwire on correct code.

    What still MUST hold — and is the part with teeth — is that the ONLY watch
    symbol the injection path may touch is the goals renderer. The
    concern/question readers must stay off it: those are things the human is
    TRACKING, and injecting an unresolved worry as if it were direction would
    steer the project on an open question."""
    sources = _injection_sources()
    for banned in ('address_watch_item', 'list_watch_items', 'add_watch_item',
                   'generate_item_response', 'promote_watch_item',
                   'promotion_state'):
        for rel, src in sources.items():
            assert banned not in strip_comments(src, lang='python'), (
                f'{rel} references {banned!r} — only render_goals_injection_block '
                f'may be reached from the prompt-injection path')


def test_the_goals_block_is_actually_wired_into_the_injection_path():
    """The complement: prove the renderer is genuinely CALLED there.

    Without this, the guard above is satisfied by a goals block that was never
    wired up at all — which is exactly the failure mode this whole epic began
    with (a Goals lane the human had filled in that reached no prompt). Asserted
    over the AST call/import sets, not a substring scan: a substring cannot tell
    a call from a mention, and this module's own comments name the symbol while
    explaining the design."""
    import ast
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inject = os.path.join(
        root, 'lib', 'tasks_pkg', 'context_composer', '_providers.py')
    with open(inject, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    called, imported = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
    assert 'render_goals_injection_block' in imported, (
        'Context Composer does not import the goals renderer — a goal would reach no '
        'prompt, the original defect this epic exists to fix')
    assert 'render_goals_injection_block' in called, (
        'Context Composer imports the goals renderer but never calls it')


def test_goal_text_reaches_the_real_prompt_with_an_empty_charter(db, monkeypatch):
    """The owner's headline acceptance criterion, end to end.

    Drives the REAL ``compose_task_context`` with the charter's ``content``
    column EMPTY, and asserts the goal text is nonetheless in the messages the
    model receives. That is the whole claim of this design: a goal works without
    being in the charter."""
    import lib.conversations.project_charter as pc
    # An EMPTY north star — and a decision present, so the charter block itself
    # still renders (proving the goal text does not arrive via that block).
    monkeypatch.setattr(pc, 'read_charter', lambda p, **_kwargs: {
        'exists': True, 'version': 5, 'content': '',
        'decisions': [{'text': 'Some binding rule.', 'summary': 'Some rule.'}]})
    monkeypatch.setattr(pc, 'pending_proposals', lambda p, **_kwargs: [])

    goal_text = 'Keep the evaluation harness reproducible across replicas.'
    pw.add_watch_item('/proj/x', 'goal', goal_text, user_id=TEST_OWNER_USER_ID)
    pw.add_watch_item('/proj/x', 'concern', 'CONCERN-MUST-NOT-INJECT', user_id=TEST_OWNER_USER_ID)

    from lib.tasks_pkg.context_composer import compose_task_context
    messages = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'hi'}]
    compose_task_context(
        messages, user_id=TEST_OWNER_USER_ID,
        project_path='/proj/x', project_enabled=True,
        memory_enabled=False, search_enabled=False, has_real_tools=True,
        conv_id='convGuard', task={'_userId': 1, 'config': {}})
    joined = '\n'.join(str(m.get('content', '')) for m in messages)

    assert '[PROJECT GOALS]' in joined, 'the goals block never reached the prompt'
    assert goal_text in joined, 'the goal text is not in the real injected prompt'
    # The charter block rendered too — so the goal did NOT ride in on it.
    assert '[PROJECT CHARTER]' in joined
    # And a concern stayed human-facing-only.
    assert 'CONCERN-MUST-NOT-INJECT' not in joined


def test_charter_block_does_not_claim_the_project_has_no_goals(db, monkeypatch):
    """With ``content`` empty the charter says "no north-star statement is set".

    It must NOT say the PROJECT has no goals — that would be a flat lie once the
    owner's goals ship in their own block, and it is the kind of confidently
    wrong context that makes a model argue with the user about their own
    intent."""
    import lib.conversations.project_charter as pc
    block = pc.render_charter_injection_block.__doc__ or ''
    assert block is not None  # keeps the import meaningful if the impl moves
    notice = pc._NO_GOAL_NOTICE
    assert 'Status & Focus' in notice, (
        'the empty-north-star notice must point at where goals actually live')
    assert 'no north-star goal is set for this project' not in notice.lower(), (
        'the notice still asserts the project has no goal')


def test_the_agent_toolset_cannot_write_the_charter():
    """Owner acceptance criterion: a charter always requires human review.

    Until 2026-07-30 ``CHARTER_TOOLS`` shipped ``project_charter_commit`` to the
    model while registry/_build.py's comment claimed it "is NEVER exposed as an
    agent tool" — so the code READ as safe and an agent could write shared intent
    unreviewed. This asserts the toolset, not the comment."""
    from lib.tools.conversation import CHARTER_TOOLS
    names = {t['function']['name'] for t in CHARTER_TOOLS}
    assert 'project_charter_read' in names
    assert 'project_charter_propose' in names, (
        'agents must keep a way to RAISE a binding rule')
    assert 'project_charter_commit' not in names, (
        'the charter is human-reviewed — no agent-facing commit tool')

    # The registry must not advertise it either (the phantom-tool trap).
    from lib.tools.registry._spec import _TOOL_SPECS
    specs = (list(_TOOL_SPECS.values()) if isinstance(_TOOL_SPECS, dict)
             else list(_TOOL_SPECS))
    conv_ref = [sp for sp in specs if getattr(sp, 'key', '') == 'conv_ref']
    assert conv_ref, 'conv_ref spec not found — update this guard to the new layout'
    for spec in conv_ref:
        assert 'project_charter_commit' not in spec.provides
        assert 'project_charter_commit' not in (spec.write_tools or frozenset())


def test_no_block_text_contains_another_blocks_marker(db, monkeypatch):
    """A whole CLASS of bug, found by the guard above and fixed here.

    Each provider owns one clear visual label. A block spelling another block's
    label makes the prompt ambiguous to both models and humans, so assert the
    invariant over every marker."""
    import lib.conversations.project_charter as pc
    import lib.conversations.project_board as pb

    markers = ['[PROJECT CHARTER]', '[PROJECT GOALS]', '[PROJECT BOARD]',
               '[PROJECT CO-PILOT MODE]']

    pw.add_watch_item('/proj/x', 'goal', 'A goal that is live', user_id=TEST_OWNER_USER_ID)
    monkeypatch.setattr(pc, 'read_charter', lambda p, **_kwargs: {
        'exists': True, 'version': 2, 'content': '',   # empty → notice renders
        'decisions': [{'text': 'A rule.', 'summary': 'A rule.'}]})
    monkeypatch.setattr(pb, 'read_board', lambda p, **_kwargs: {
        'tasks': [], 'open': 0, 'claimed': 0, 'done': 0, 'blocked': 0})

    rendered = {
        '[PROJECT CHARTER]': pc.render_charter_injection_block(
            '/proj/x', user_id=1),
        '[PROJECT GOALS]': pw.render_goals_injection_block('/proj/x', user_id=TEST_OWNER_USER_ID),
    }
    assert rendered['[PROJECT CHARTER]'], 'fixture did not arm the charter block'
    assert rendered['[PROJECT GOALS]'], 'fixture did not arm the goals block'

    for own_marker, text in rendered.items():
        for other in markers:
            if other == own_marker:
                continue
            assert other not in text, (
                f'the {own_marker} block spells out {other!r}; describe the '
                f'concept in prose instead of quoting another block label')


def test_the_withdrawn_commit_tool_is_refused_with_a_reason(db):
    """A model that learned the tool from an older transcript must be told where
    the human gate is, not handed an opaque unknown-tool error."""
    from lib.conversations.project_charter import execute_charter_tool
    out = execute_charter_tool('project_charter_commit',
                               {'kind': 'invariant', 'decision': 'x',
                                'summary': 'y'},
                               user_id=1,
                               project_path='/proj/x')
    assert 'project_charter_propose' in out, (
        'the refusal must name the route that still works')
    assert 'human' in out.lower()


# ════════════════════════════════════════════════════════════════════
#  Follow-up Q&A — the human's thread on ONE response (Increment 2 slice)
# ════════════════════════════════════════════════════════════════════

def test_follow_up_is_anchored_grounded_and_persisted(db, monkeypatch):
    """The follow-up answers with the anchor response + LIVE pillar state in
    the prompt, and lands in the SAME append-only trail with the question
    recorded in the evidence JSON."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy(answer='First assessment.')
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the lane', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    anchor = pw.address_watch_item(item_id, force=True, user_id=TEST_OWNER_USER_ID)
    assert anchor['seq'] == 1

    spy.answer = 'Follow-up answer.'
    res = pw.answer_follow_up(item_id, 'What about the blocked epics?', user_id=TEST_OWNER_USER_ID)
    assert res['ok'], res
    snap = res['response']
    assert snap['seq'] == 2 and snap['trigger'] == 'follow_up'
    assert snap['pillar_state']['followUpQuestion'] == 'What about the blocked epics?'
    assert snap['pillar_state']['anchorSeq'] == 1

    prompt = spy.calls[-1][-1]['content']
    assert 'First assessment.' in prompt, 'the anchor response must ride the prompt'
    assert 'What about the blocked epics?' in prompt
    assert 'Ship the lane' in prompt
    assert _NORTH_STAR in prompt, 'a follow-up must still read LIVE pillar state'


def test_follow_up_explicit_seq_anchors_that_response(db, monkeypatch):
    """``response_seq`` picks the anchor — not silently the latest."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy(answer='EARLIEST-ANSWER')
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'c', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.address_watch_item(item_id, force=True, user_id=TEST_OWNER_USER_ID)
    spy.answer = 'LATER-ANSWER'
    pw.address_watch_item(item_id, trigger='epic_completed', force=True, user_id=TEST_OWNER_USER_ID)

    spy.answer = 'A3'
    res = pw.answer_follow_up(item_id, 'dig into the first one', response_seq=1, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['response']['pillar_state']['anchorSeq'] == 1
    prompt = spy.calls[-1][-1]['content']
    assert 'EARLIEST-ANSWER' in prompt
    assert 'LATER-ANSWER' not in prompt


def test_follow_up_does_not_mark_the_recurring_cadence_fresh(db, monkeypatch):
    """A Q&A turn is NOT an assessment: the item fingerprint must be untouched,
    so the next pillar-state change still re-addresses the item itself."""
    _wire_pillars(monkeypatch, done=5)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Drift watch', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    pw.address_watch_item(item_id, user_id=TEST_OWNER_USER_ID)

    def _fp():
        return pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['response_fingerprint']

    before = _fp()
    pw.answer_follow_up(item_id, 'why?', user_id=TEST_OWNER_USER_ID)
    assert _fp() == before, 'a Q&A turn must not mark the cadence fresh'

    _wire_pillars(monkeypatch, done=6)
    pw.address_watch_item(item_id, trigger='epic_completed', user_id=TEST_OWNER_USER_ID)
    kinds = [(t['seq'], t['trigger']) for t in
             pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['responses']]
    assert kinds[0] == (3, 'epic_completed'), kinds
    assert kinds[1] == (2, 'follow_up') and kinds[2] == (1, 'manual')


def test_follow_up_validates_and_never_raises(db, monkeypatch):
    assert pw.answer_follow_up('', 'q?', user_id=TEST_OWNER_USER_ID)['ok'] is False
    assert pw.answer_follow_up('missing', 'q?', user_id=TEST_OWNER_USER_ID)['error'] == 'not found'
    item_id = pw.add_watch_item('/proj/x', 'question', 'Q?', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    assert pw.answer_follow_up(item_id, '   ', user_id=TEST_OWNER_USER_ID)['error'] == 'empty question'
    _wire_pillars(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError('llm down')

    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', _boom)
    assert pw.answer_follow_up(item_id, 'q?', user_id=TEST_OWNER_USER_ID)['ok'] is False
    assert pw.list_watch_items('/proj/x', user_id=TEST_OWNER_USER_ID)['items'][0]['responses'] == [], (
        'a failed synthesis must not write the trail')


def test_follow_up_with_no_prior_response_still_answers(db, monkeypatch):
    """No anchor yet → the prompt says so honestly, anchorSeq=0."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'c', user_id=TEST_OWNER_USER_ID)['item']['item_id']
    res = pw.answer_follow_up(item_id, 'first contact?', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['response']['pillar_state']['anchorSeq'] == 0
    assert '(none yet)' in spy.calls[-1][-1]['content']
