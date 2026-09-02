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
  • a file CONFLICT is NOT an item either (notify-only, self-clearing, no
    resolving control — 2026-08-01 owner directive); it stays in the collab
    bar's live detail lines and the Team tab;
  • blocking items sort ahead of advisory ones.

Plus a source-level negative control: no-op the severity sort → the
"blocking first" assertion FAILS.
"""

from __future__ import annotations

import os
import time

import pytest

import lib.presence.registry as reg

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_ATTN_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_attention.py')
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')


@pytest.fixture(autouse=True)
def _clean(chat_sidecar, monkeypatch):
    # Every test uses a distinct project path, so the module-scoped sidecar
    # needs no per-test storage cleanup; keep only the in-process stubs.
    monkeypatch.setattr(reg, '_state', {})
    monkeypatch.setattr(reg, '_sweeper_started', True)
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _block_with_question(path, conv, task_id, question, options=None):
    """Put an epic into the pending-question state via the REAL block path."""
    from lib.conversations.project_board import block_task
    return block_task(path, conv, task_id, '[human-gated] needs a call',
                      question=question, options=options or [], user_id=TEST_OWNER_USER_ID)


# ── Board questions: the only genuinely blocking item ──

def test_board_question_is_a_blocking_item(flask_app):
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-q')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Migrate the schema', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'cA', t, 'Postgres or SQLite for the primary?',
                             [{'label': 'Postgres'}, {'label': 'SQLite'}])
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
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
        t = post_task(p, 'cA', 'Pick a store', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'cA', t, 'Which store?', [{'label': 'PG'}])
        assert build_attention_items(p, user_id=TEST_OWNER_USER_ID)['blocking'] == 1
        answer_task(p, 'cA', t, 'PG', user_id=TEST_OWNER_USER_ID)
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
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
        t = post_task(p, 'cA', 'Waits on CI', user_id=TEST_OWNER_USER_ID)['id']
        block_task(p, 'cA', t, '[sibling] waiting on the parser commit', user_id=TEST_OWNER_USER_ID)
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
    assert a['needsYou'] == 0, 'a self-expiring cooldown needs no human'
    assert a['items'] == []
    assert a['waiting'] == 1, 'but it IS reported as waiting-on-a-gate'


# ── Charter proposals: advisory, NOT blocking (the inversion fix) ──

def test_charter_proposal_is_advisory_not_blocking(flask_app):
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-proposal')
    with flask_app.app_context():
        propose_amendment(p, 'cA', 'Adopt the new parser', user_id=TEST_OWNER_USER_ID)
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
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
        res = propose_amendment(p, 'cA', 'Adopt the new parser', user_id=TEST_OWNER_USER_ID)
        assert build_attention_items(p, user_id=TEST_OWNER_USER_ID)['advisory'] == 1
        commit_charter(p, add_decision='Adopt the new parser',
                       updated_by_conv='cA',
                       resolves_proposal=res['proposalId'], user_id=TEST_OWNER_USER_ID)
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
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
        propose_amendment(p, 'cA', 'Adopt X', user_id=TEST_OWNER_USER_ID)
        propose_amendment(p, 'cB', 'Adopt Y', user_id=TEST_OWNER_USER_ID)
        t = post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
    assert a['needsYou'] == 3
    assert a['items'][0]['severity'] == 'blocking', \
        'blocking must lead regardless of creation order'
    assert [i['severity'] for i in a['items'][1:]] == ['advisory', 'advisory']


# ── Conflicts: NOT attention items (2026-08-01 owner directive) ──

def test_conflict_overlap_is_not_an_attention_item(flask_app):
    """A file overlap is notify-only and SELF-CLEARING (it recomputes from the
    presence registry and vanishes when a peer goes idle), and it has no
    resolving control — the system deliberately never locks, the operator
    decides whether to intervene. An item that needs NOTHING from the human
    must not occupy the "needs you" surface (owner directive 2026-08-01: "since
    you don't need me to handle it, don't display it here"). The overlap stays
    visible where LIVE STATUS belongs: the summary's conflictMessages (collab
    bar detail lines — pinned by test_summary_conflicts_from_file_overlap) and
    the Team tab."""
    from lib.conversations.project_attention import build_attention_items
    p = os.path.abspath('/tmp/attn-conflict')
    with flask_app.app_context():
        reg.announce(p, 'convA', task_id='tA', title='A', user_id=TEST_OWNER_USER_ID)
        reg.announce(p, 'convB', task_id='tB', title='B', user_id=TEST_OWNER_USER_ID)
        reg.record_files(
            p, 'convA', [{'path': 'src/shared.py', 'action': 'edit'}],
            user_id=TEST_OWNER_USER_ID,
        )
        reg.record_files(
            p, 'convB', [{'path': 'src/shared.py', 'action': 'edit'}],
            user_id=TEST_OWNER_USER_ID,
        )
        a = build_attention_items(p, user_id=TEST_OWNER_USER_ID)
    assert a['items'] == [], \
        'a live conflict must never be an attention item'
    assert a['needsYou'] == 0 and a['advisory'] == 0


# ── conv_id marks ownership without changing membership ──

def test_conv_id_marks_mine_but_never_filters(flask_app):
    """Attention is PROJECT-scoped: an epic blocked on a question needs the
    human regardless of which chat they happen to be looking at. conv_id only
    marks `mine`."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-mine')
    with flask_app.app_context():
        propose_amendment(p, 'convA', 'From A', user_id=TEST_OWNER_USER_ID)
        propose_amendment(p, 'convB', 'From B', user_id=TEST_OWNER_USER_ID)
        a = build_attention_items(p, 'convA', user_id=TEST_OWNER_USER_ID)
    assert a['needsYou'] == 2, 'conv_id must not filter items out'
    mine = {i['text']: i.get('mine') for i in a['items']}
    assert mine['From A'] is True and mine['From B'] is False


# ── Degradation ──

def test_empty_project_and_blank_path(flask_app):
    from lib.conversations.project_attention import build_attention_items
    with flask_app.app_context():
        a = build_attention_items(os.path.abspath('/tmp/attn-empty'), user_id=TEST_OWNER_USER_ID)
    assert a['items'] == [] and a['needsYou'] == 0 and a['waiting'] == 0
    blank = build_attention_items('', user_id=TEST_OWNER_USER_ID)
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
        t = post_task(p, 'cA', 'Still visible', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        a = attn.build_attention_items(p, user_id=TEST_OWNER_USER_ID)
    assert a['blocking'] == 1, 'a failing source must not blank the others'


# ── Route + collab-bar summary integration ──

def test_route_brain_attention(flask_app, flask_client):
    import json as _json

    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-route')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Routed halt', user_id=TEST_OWNER_USER_ID)['id']
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
        t = post_task(p, 'cA', 'Halted', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'cA', t, 'Which way?')
        propose_amendment(p, 'cA', 'Adopt X', user_id=TEST_OWNER_USER_ID)
        s = build_brain_summary(p, user_id=TEST_OWNER_USER_ID)
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
        propose_amendment(p, 'cA', 'Adopt X', user_id=TEST_OWNER_USER_ID)
        s = build_brain_summary(p, user_id=TEST_OWNER_USER_ID)
    assert s['blocking'] == 0 and s['needsYou'] == 1


# ── Source-level NEGATIVE CONTROL ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  The proposal `text` is COMMITTABLE, not display-only
#
#  The Needs-you tab commits the durable charter decision from this exact
#  field. It was capped at _TEXT_MAX (600) like a display-only string, so
#  committing a longer proposal from that tab silently stored a decision cut
#  mid-sentence — and a charter decision is prompt-injected shared intent,
#  so a truncated one misleads every sibling conversation.
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
        propose_amendment(p, 'cA', long_proposal, user_id=TEST_OWNER_USER_ID)
        item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
    assert item['text'] == long_proposal, (
        'the committable proposal text must not be truncated — the Needs-you '
        'tab commits THIS string as the durable decision')


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
        propose_amendment(p, 'cA', long_proposal, user_id=TEST_OWNER_USER_ID)
        item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
        res = commit_charter(
            p, add_decision=item['text'],
            summary=item['text'].split('\n', 1)[0].strip(),
            updated_by_conv='human', resolves_proposal=item['id'], user_id=TEST_OWNER_USER_ID)
        assert res.get('ok'), res
        stored = read_charter(p, user_id=TEST_OWNER_USER_ID)['decisions'][-1]
        assert pending_proposals(p, user_id=TEST_OWNER_USER_ID) == [], 'the proposal must leave the queue'
    assert stored['text'] == long_proposal, \
        'the committed decision must be the WHOLE proposal, not a display slice'


# ── Source-level NEGATIVE CONTROL ──


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
            propose_amendment(p, 'cA', 'Adopt X', user_id=TEST_OWNER_USER_ID)
            t = post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
            _block_with_question(p, 'cA', t, 'Which way?')
            a = attn.build_attention_items(p, user_id=TEST_OWNER_USER_ID)
        assert a['items'][0]['severity'] != 'blocking', \
            'NC: with the severity ranks inverted the blocking item must not lead'

    _patch_restore(
        _ATTN_SRC,
        '_SEVERITY_RANK = {"blocking": 0, "advisory": 1}',
        '_SEVERITY_RANK = {"blocking": 1, "advisory": 0}  # NC (ranks inverted)',
        run,
    )


def test_NC_proposal_text_cap_would_truncate_a_committed_decision(flask_app):
    """NC: re-apply the display cap to the proposal `text` → the panel's
    committable string is a 600-char slice → the whole-commit assertion FAILS.

    This is the shipped bug, reproduced: `_TEXT_MAX` is right for a purely
    display-only string and wrong for this field, because a resolving control
    submits it back.
    """
    def run():
        import lib.conversations.project_attention as attn
        from lib.conversations.project_charter import propose_amendment
        p = os.path.abspath('/tmp/attn-nc-trunc')
        long_proposal = 'RULE: never merge the gates.\n' + ('reason ' * 300) + 'END'
        with flask_app.app_context():
            propose_amendment(p, 'cA', long_proposal, user_id=TEST_OWNER_USER_ID)
            item = attn.build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
        assert item['text'] != long_proposal, \
            'NC: with the display cap re-applied the committable text must be a slice'
        assert len(item['text']) == attn._TEXT_MAX

    _patch_restore(
        _ATTN_SRC,
        '            "text": p.get("summary") or "",',
        '            "text": (p.get("summary") or "")[:_TEXT_MAX],  # NC (cap restored)',
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  Provenance + background on a halted-epic card (2026-08 owner complaint:
#  "I can't tell which conversation asked me this, and the background is
#  incomplete"). Before blocked_by, the asker existed only in the feed /
#  audit trail — owner_conv_id projects '' for a blocked epic because it
#  is not claimed.
# ════════════════════════════════════════════════════════════════════

def _insert_conv(conv_id, title):
    """Minimal conversations row so the title lookup resolves for real."""
    import tests._seed as seed
    seed.seed_conversation(conv_id, title=title)


def test_blocked_by_is_surfaced_with_title_and_mine(flask_app):
    """The card must answer 'which conversation asked me this?' from the ROW:
    blocked_by → askedByConvId + the resolved title, and the same id drives
    `mine` marking exactly like a proposal's author."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-provenance')
    with flask_app.app_context():
        _insert_conv('attn-asker-1', 'Egress 出口调研')
        t = post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
        _block_with_question(p, 'attn-asker-1', t, 'Which way?')
        a = build_attention_items(p, 'attn-asker-1', user_id=TEST_OWNER_USER_ID)
    item = a['items'][0]
    assert item['askedByConvId'] == 'attn-asker-1', item
    assert item['askedByTitle'] == 'Egress 出口调研', item
    assert item.get('mine') is True, 'the asker id must drive mine marking'


def test_blocked_by_falls_back_to_created_by_conv(flask_app):
    """Rows blocked BEFORE the column existed (blocked_by='') still get a
    provenance chip — from the epic's poster, never nothing."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_board import post_task
    p = os.path.abspath('/tmp/attn-legacy-block')
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
        # A pre-column row (blocked_by='') — blocking with an EMPTY conv id
        # stores exactly that (the legacy fixture's UPDATE equivalent).
        _block_with_question(p, '', t, 'Which way?')
        item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
    assert item['askedByConvId'] == 'cA', item
    assert item['askedByTitle'] == '', 'no conv row → id-only chip, never blank id'


def test_reason_uses_the_background_allowance(flask_app):
    """The reason is the card's BACKGROUND section — it must arrive WHOLE
    past the display-only 600-char cap (the 'incomplete background'
    complaint), bounded only by _REASON_MAX (= the storage cap)."""
    from lib.conversations.project_attention import _TEXT_MAX, build_attention_items
    from lib.conversations.project_board import block_task, post_task
    p = os.path.abspath('/tmp/attn-longreason')
    # (block_task .strip()s the reason — build the fixture without a tail space)
    reason = ('[human-gated] needs a call. ' + ('evidence ' * 90)).strip()
    assert _TEXT_MAX < len(reason) <= 2000, 'fixture must exceed the display cap'
    with flask_app.app_context():
        t = post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task(p, 'cA', t, reason, question='Which way?', user_id=TEST_OWNER_USER_ID)
        item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
    assert item['reason'] == reason, \
        'the background must not be truncated at the display-only cap'


def test_proposal_carries_author_provenance(flask_app):
    """The charter-proposal card is a decision awaiting the human too — it
    must wear the SAME askedBy* provenance as the halted-epic card: the
    author's conv id + the resolved conversation title."""
    from lib.conversations.project_attention import build_attention_items
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/tmp/attn-prop-prov')
    with flask_app.app_context():
        _insert_conv('attn-proposer-1', '架构决策讨论')
        propose_amendment(p, 'attn-proposer-1', 'Adopt the new parser', user_id=TEST_OWNER_USER_ID)
        item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
    assert item['type'] == 'charter_proposal', item
    assert item['askedByConvId'] == 'attn-proposer-1', item
    assert item['askedByTitle'] == '架构决策讨论', item


def test_NC_blocked_by_write_is_load_bearing(flask_app):
    """NC: strip the blocked_by=? write from block_task → the asker is LOST
    (the chip falls back to the epic's poster) → the provenance assertions
    FAIL. Proves the column write, not some incidental read, carries the
    answer to 'who asked me this?'."""
    def run(mod):
        p = os.path.abspath('/tmp/attn-nc-prov')
        with flask_app.app_context():
            t = mod.post_task(p, 'cA', 'Halted epic', user_id=TEST_OWNER_USER_ID)['id']
            mod.block_task(p, 'attn-asker-2', t, '[human-gated] needs a call',
                           question='Which way?', user_id=TEST_OWNER_USER_ID)
            from lib.conversations.project_attention import build_attention_items
            item = build_attention_items(p, user_id=TEST_OWNER_USER_ID)['items'][0]
        assert item['askedByConvId'] != 'attn-asker-2', \
            'NC: with the blocked_by write neutered the asker must be lost'
        assert item['askedByConvId'] == 'cA', \
            'NC: the fallback to the poster is what remains'

    # The anchor is the SIDECAR branch's conv_id handoff (the suite runs
    # sidecar-only; the legacy branch's blocked_by UPDATE never executes, and
    # the op-side write lives in the sidecar process). Stripping the handoff
    # makes the op store blocked_by='' — the exact column-loss the NC proves.
    _patch_restore(
        _BOARD_SRC,
        '                "action": "block",\n'
        '                "project_path": normalized_path,\n'
        '                "user_id": int(user_id),\n'
        '                "conv_id": conv_id,',
        '                "action": "block",\n'
        '                "project_path": normalized_path,\n'
        '                "user_id": int(user_id),\n'
        '                "conv_id": "",  # NC (blocked_by handoff stripped)',
        run,
    )
