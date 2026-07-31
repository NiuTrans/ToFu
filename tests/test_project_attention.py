"""tests/test_project_attention.py — the "needs you" single source of truth.

The star of this slice is the SEVERITY INVERSION fix. Before
``lib/conversations/project_attention.py`` existed, the always-visible
collaboration bar led with ``pendingDecisions`` (charter proposals) rendered
with emphasis, while an epic halted on a structured human question — the ONLY
item that stops a workstream indefinitely, because ``project_dispatch`` skips
it on every heartbeat — was not in the summary payload at all.

So the loudest signal was the least urgent one. These tests pin the corrected
semantics:

  • a board ``block_question`` is ``blocking``;
  • a charter proposal is ``advisory`` (agents self-commit decisions since the
    2026-07-12 de-gating — nothing stops while one is pending);
  • a COOLDOWN block is NOT an item at all (it self-expires; listing it would
    train the operator to ignore the surface) but IS counted in ``waiting``;
  • blocking items sort ahead of advisory ones.

Plus a source-level negative control: no-op the severity sort → the
"blocking first" assertion FAILS.
"""

from __future__ import annotations

import os

import pytest

import lib.presence.registry as reg

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_ATTN_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_attention.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app, monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.execute('DELETE FROM project_charter')
        db.commit()
    monkeypatch.setattr(reg, '_state', {})
    monkeypatch.setattr(reg, '_sweeper_started', True)
    import lib.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _block_with_question(path, conv, task_id, question, options=None):
    """Put an epic into the pending-question state via the REAL block path."""
    from lib.conversations.project_board import block_task
    return block_task(path, conv, task_id, '[human-gated] needs a call',
                      question=question, options=options or [])


# ── Board questions: the only genuinely blocking item ──

def test_board_question_is_a_blocking_item(flask_app):
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-q')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Migrate the schema')['id']
        _block_with_question(p, 'cA', t, 'Postgres or SQLite for the primary?',
                             [{'label': 'Postgres'}, {'label': 'SQLite'}])
        a = build_attention_items(p)
    assert a['blocking'] == 1, a
    assert a['needsYou'] == 1
    item = a['items'][0]
    assert item['type'] == 'board_question'
    assert item['severity'] == 'blocking'
    assert item['id'] == t
    assert item['question'] == 'Postgres or SQLite for the primary?'
    assert [o['label'] for o in item['options']] == ['Postgres', 'SQLite']
    assert item['tab'] == 'board', 'must carry the deep-link target'


def test_answered_question_drops_out(flask_app):
    """Answering clears the gate → the item disappears (no stale nag)."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import answer_task, post_task
    p = os.path.abspath('/tmp/attn-answered')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Pick a store')['id']
        _block_with_question(p, 'cA', t, 'Which store?', [{'label': 'PG'}])
        assert build_attention_items(p)['blocking'] == 1
        answer_task(p, 'cA', t, 'PG')
        a = build_attention_items(p)
    assert a['blocking'] == 0 and a['needsYou'] == 0


# ── Cooldown blocks: counted, never listed (§D3) ──

def test_cooldown_block_is_not_an_item_but_is_counted_as_waiting(flask_app):
    """A block WITHOUT a question self-expires and is re-picked by
    select_dispatchable with zero human involvement. Surfacing it as a task
    would devalue the whole surface — it must land in `waiting`, not `items`."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import block_task, post_task
    p = os.path.abspath('/tmp/attn-cooldown')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Waits on CI')['id']
        block_task(p, 'cA', t, '[sibling] waiting on the parser commit')
        a = build_attention_items(p)
    assert a['needsYou'] == 0, 'a self-expiring cooldown needs no human'
    assert a['items'] == []
    assert a['waiting'] == 1, 'but it IS reported as waiting-on-a-gate'


# ── Charter proposals: advisory, NOT blocking (the inversion fix) ──

def test_charter_proposal_is_advisory_not_blocking(flask_app):
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-proposal')
    with flask_app.app_context():
        propose_amendment(p, 'cA', 'Adopt the new parser')
        a = build_attention_items(p)
    assert a['advisory'] == 1
    assert a['blocking'] == 0, (
        'a proposal must NOT be blocking — agents self-commit decisions since '
        'the 2026-07-12 de-gating, so nothing stops while one is pending')
    assert a['items'][0]['type'] == 'charter_proposal'
    assert a['items'][0]['tab'] == 'charter'


def test_committed_proposal_drops_out(flask_app):
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import (
        commit_charter, propose_amendment,
    )
    p = os.path.abspath('/tmp/attn-committed')
    with flask_app.app_context():
        res = propose_amendment(p, 'cA', 'Adopt the new parser')
        assert build_attention_items(p)['advisory'] == 1
        commit_charter(p, add_decision='Adopt the new parser',
                       updated_by_conv='cA',
                       resolves_proposal=res['proposalId'])
        a = build_attention_items(p)
    assert a['advisory'] == 0 and a['needsYou'] == 0


# ── Ordering: severity first (THE decisive test) ──

def test_blocking_sorts_ahead_of_advisory(flask_app):
    """The whole point of the redesign: whatever else is pending, the item that
    has STOPPED a workstream is first in the list the bar and panel render."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import post_task
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-order')
    with flask_app.app_context():
        # Two advisories created FIRST, so insertion order would put them
        # ahead if severity were not the sort key.
        propose_amendment(p, 'cA', 'Adopt X')
        propose_amendment(p, 'cB', 'Adopt Y')
        t = post_task(p, 'cA', 'Halted epic')['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        a = build_attention_items(p)
    assert a['needsYou'] == 3
    assert a['items'][0]['severity'] == 'blocking', \
        'blocking must lead regardless of creation order'
    assert [i['severity'] for i in a['items'][1:]] == ['advisory', 'advisory']


# ── Conflicts ──

def test_conflict_overlap_is_an_advisory_item(flask_app):
    """Recomputed from the SAME detect_overlaps the live broadcast uses — the
    message is rendered verbatim, and the conv halves of the peer keys are
    projected so the UI can mark 'this involves the conv you're viewing'."""
    from lib.conversations.project_attention import build_attention_items
    p = os.path.abspath('/tmp/attn-conflict')
    with flask_app.app_context():
        reg.announce(p, 'convA', task_id='tA', title='A')
        reg.announce(p, 'convB', task_id='tB', title='B')
        reg.record_files(p, 'convA', [{'path': 'src/shared.py', 'action': 'edit'}])
        reg.record_files(p, 'convB', [{'path': 'src/shared.py', 'action': 'edit'}])
        a = build_attention_items(p)
    conflicts = [i for i in a['items'] if i['type'] == 'conflict']
    assert len(conflicts) == 1
    assert conflicts[0]['severity'] == 'advisory'
    assert conflicts[0]['path'] == 'src/shared.py'
    assert 'shared.py' in conflicts[0]['text']
    assert set(conflicts[0]['convIds']) == {'convA', 'convB'}


# ── conv_id marks ownership without changing membership ──

def test_conv_id_marks_mine_but_never_filters(flask_app):
    """Attention is PROJECT-scoped: an epic blocked on a question needs the
    human regardless of which chat they happen to be looking at. conv_id only
    marks `mine`."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-mine')
    with flask_app.app_context():
        propose_amendment(p, 'convA', 'From A')
        propose_amendment(p, 'convB', 'From B')
        a = build_attention_items(p, 'convA')
    assert a['needsYou'] == 2, 'conv_id must not filter items out'
    mine = {i['text']: i.get('mine') for i in a['items']}
    assert mine['From A'] is True and mine['From B'] is False


# ── Degradation ──

def test_empty_project_and_blank_path(flask_app):
    from lib.conversations.project_attention import build_attention_items
    with flask_app.app_context():
        a = build_attention_items(os.path.abspath('/tmp/attn-empty'))
    assert a['items'] == [] and a['needsYou'] == 0 and a['waiting'] == 0
    blank = build_attention_items('')
    assert blank['items'] == [] and blank['needsYou'] == 0


def test_one_failing_source_does_not_blank_the_surface(flask_app, monkeypatch):
    """Best-effort per SOURCE, not per call: a raising charter read must not
    hide a blocking board question (the fail-safe that matters — the operator
    keeps seeing the thing that stopped work)."""
    import lib.conversations.project_attention as attn
    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-degrade')

    def _boom(_p):
        raise RuntimeError('charter read exploded')

    monkeypatch.setattr(attn, '_charter_proposals', _boom)
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Still visible')['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        a = attn.build_attention_items(p)
    assert a['blocking'] == 1, 'a failing source must not blank the others'


# ── Route + collab-bar summary integration ──

def test_route_brain_attention(flask_app, flask_client):
    import json as _json

    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-route')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Routed halt')['id']
        _block_with_question(p, 'cA', t, 'Which way?', [{'label': 'Left'}])
    r = flask_client.get('/api/v1/project/brain/attention?path=' + p)
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['blocking'] == 1 and data['needsYou'] == 1
    assert data['items'][0]['question'] == 'Which way?'


def test_route_brain_attention_requires_path(flask_client):
    assert flask_client.get('/api/v1/project/brain/attention').status_code == 400


def test_summary_carries_attention_counts(flask_app):
    """The collab bar reads ONE payload. A halted epic must raise `blocking`
    there — the gap this whole redesign exists to close (before it,
    build_brain_summary had no notion of block_question at all)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-summary')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Halted')['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        propose_amendment(p, 'cA', 'Adopt X')
        s = build_brain_summary(p)
    assert s['blocking'] == 1, 'a halted epic must be visible on the bar'
    assert s['advisory'] == 1
    assert s['needsYou'] == 2
    # The legacy field keeps its old meaning (proposals only) so nothing that
    # already reads it changes behaviour.
    assert s['pendingDecisions'] == 1


def test_summary_advisory_only_reports_zero_blocking(flask_app):
    """The inversion fix, stated as a contract: a project whose ONLY pending
    item is a charter proposal must report blocking=0, so the bar renders calm
    rather than alarmed."""
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-calm')
    with flask_app.app_context():
        propose_amendment(p, 'cA', 'Adopt X')
        s = build_brain_summary(p)
    assert s['blocking'] == 0 and s['needsYou'] == 1


# ── Source-level NEGATIVE CONTROL ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  The proposal `text` is COMMITTABLE, not display-only
#
#  The Needs-you tab commits the durable charter decision from this exact
#  field. It was capped at _TEXT_MAX (600) like the display-only conflict
#  message, so committing a longer proposal from that tab silently stored a
#  decision cut mid-sentence — and a charter decision is prompt-injected
#  shared intent, so a truncated one misleads every sibling conversation.
# ════════════════════════════════════════════════════════════════════

def test_proposal_text_is_not_capped_at_the_display_max(flask_app):
    """A proposal longer than _TEXT_MAX must arrive WHOLE, because the panel
    submits this field back as the committed decision."""
    from lib.conversations.project_attention import _TEXT_MAX, build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-longprop')
    long_proposal = 'RULE: keep the two gates separate.\n' + ('detail ' * 300) + 'END'
    assert len(long_proposal) > _TEXT_MAX, 'fixture must exceed the display cap'
    with flask_app.app_context():
        propose_amendment(p, 'cA', long_proposal)
        item = build_attention_items(p)['items'][0]
    assert item['text'] == long_proposal, (
        'the committable proposal text must not be truncated — the Needs-you '
        'tab commits THIS string as the durable decision')


def test_conflict_text_is_still_capped(flask_app):
    """The complement: a conflict message IS display-only, so it keeps the cap.
    Without this, "stop truncating" could be over-applied to every field and
    one pathological advisory could dominate the panel."""
    import lib.conversations.project_attention as attn
    p = os.path.abspath('/tmp/attn-longconflict')

    def _one_huge(peers):
        return [{'path': 'src/x.py', 'peers': ['cA', 'cB'],
                 'message': 'X' * (attn._TEXT_MAX * 3)}]

    reg.announce(p, 'cA', task_id='tA', title='A')
    monkey = attn._conflicts
    assert monkey is not None
    import lib.presence.conflict as confl
    _orig = confl.detect_overlaps
    confl.detect_overlaps = _one_huge
    try:
        with flask_app.app_context():
            items = attn.build_attention_items(p)['items']
    finally:
        confl.detect_overlaps = _orig
    conflicts = [i for i in items if i['type'] == 'conflict']
    assert len(conflicts) == 1
    assert len(conflicts[0]['text']) == attn._TEXT_MAX, \
        'a display-only conflict message must stay capped'


def test_proposal_text_commits_whole_through_the_real_route(flask_app):
    """End-to-end: what the panel receives is what the commit route stores.

    Pins the two halves TOGETHER — payload not truncated AND the commit that
    consumes it persists the same bytes — so re-introducing a cap anywhere
    between them fails here.
    """
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import (
        commit_charter, pending_proposals, propose_amendment, read_charter,
    )
    p = os.path.abspath('/tmp/attn-commit-whole')
    long_proposal = 'RULE: never merge the gates.\n' + ('reason ' * 300) + 'END'
    with flask_app.app_context():
        propose_amendment(p, 'cA', long_proposal)
        item = build_attention_items(p)['items'][0]
        res = commit_charter(
            p, add_decision=item['text'],
            summary=item['text'].split('\n', 1)[0].strip(),
            updated_by_conv='human', resolves_proposal=item['id'])
        assert res.get('ok'), res
        stored = read_charter(p)['decisions'][-1]
        assert pending_proposals(p) == [], 'the proposal must leave the queue'
    assert stored['text'] == long_proposal, \
        'the committed decision must be the WHOLE proposal, not a display slice'


# ── Source-level NEGATIVE CONTROL ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_severity_rank_is_load_bearing(flask_app):
    """NC: invert the severity rank table → advisory outranks blocking → the
    "blocking leads" assertion FAILS.

    Note this neuters the RANK TABLE, not the ``items.sort(...)`` call. An
    earlier draft replaced the sort key with a constant and the test still
    passed — Python's sort is stable and ``build_attention_items`` happens to
    collect board questions before proposals, so insertion order alone produced
    the right answer. That would have been a vacuous NC: it proves the ordering
    survives losing the sort only because of an incidental collection order,
    which no test pins. Inverting the ranks is the honest probe of "is severity
    what decides the order?" — and it also fails if someone later reorders the
    collection loop and deletes the sort.
    """
    def run():
        import lib.conversations.project_attention as attn
        from lib.conversations.project_board import post_task
        from lib.conversations.project_charter import propose_amendment
        p = os.path.abspath('/tmp/attn-nc')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path=?', (p,))
            db.commit()
            propose_amendment(p, 'cA', 'Adopt X')
            t = post_task(p, 'cA', 'Halted epic')['id']
            _block_with_question(p, 'cA', t, 'Which way?')
            a = attn.build_attention_items(p)
        assert a['items'][0]['severity'] != 'blocking', \
            'NC: with the severity ranks inverted the blocking item must not lead'

    _patch_restore(
        _ATTN_SRC,
        "_SEVERITY_RANK = {'blocking': 0, 'advisory': 1}",
        "_SEVERITY_RANK = {'blocking': 1, 'advisory': 0}  # NC (ranks inverted)",
        run,
    )


def test_NC_proposal_text_cap_would_truncate_a_committed_decision(flask_app):
    """NC: re-apply the display cap to the proposal `text` → the panel's
    committable string is a 600-char slice → the whole-commit assertion FAILS.

    This is the shipped bug, reproduced: `_TEXT_MAX` is right for the conflict
    message (pure display) and wrong for this field, because a resolving control
    submits it back. The NC keeps the CONFLICT cap intact, so it isolates the
    one field under test rather than proving "some cap exists somewhere".
    """
    def run():
        import lib.conversations.project_attention as attn
        from lib.conversations.project_charter import propose_amendment
        p = os.path.abspath('/tmp/attn-nc-trunc')
        long_proposal = 'RULE: never merge the gates.\n' + ('reason ' * 300) + 'END'
        with flask_app.app_context():
            propose_amendment(p, 'cA', long_proposal)
            item = attn.build_attention_items(p)['items'][0]
        assert item['text'] != long_proposal, \
            'NC: with the display cap re-applied the committable text must be a slice'
        assert len(item['text']) == attn._TEXT_MAX

    _patch_restore(
        _ATTN_SRC,
        "        'text': p.get('summary') or '',",
        "        'text': (p.get('summary') or '')[:_TEXT_MAX],  # NC (cap restored)",
        run,
    )
