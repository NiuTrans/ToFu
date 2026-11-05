"""GitHub adapter (C8) against a fake GitHub REST server: ingress, cadence, catch-up, outbox."""
import pytest

from lib.channels import outbox, service
from lib.channels.github.adapter import GitHubAdapter, due_work
from lib.channels.github.client import GitHubRateLimited
from lib.channels.github.normalize import KIND_ISSUE_COMMENT, external_id
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import (
    GH_ALICE, GH_BOB, GH_NOW, GH_TOKEN, GH_USER, OWNER, ROUTER, RecordClient, gh_http_date, gh_time,
    github_adapter,
)

pytestmark = pytest.mark.unit

REPO = 'Octo/Repo'
PR = ThreadKey.parse('github:octo/repo#12')
OWNER_UID = str(GH_USER['id'])


@pytest.fixture
def world(github):
    """PR #12: alice mentions the owner, bob does not; carol replies to the owner's review comment."""
    github.issue(REPO, 12, title='Fix the poller', created=GH_NOW - 7200, pull=True)
    github.comment(REPO, 12, user=GH_ALICE, body='@octo-owner plan A or B?', created=GH_NOW - 300)
    github.comment(REPO, 12, user=GH_BOB, body='LGTM', created=GH_NOW - 200)
    parent = github.comment(REPO, 12, user=GH_USER, body='lock this', created=GH_NOW - 7000, review=True)
    github.comment(REPO, 12, user={'id': 1003, 'login': 'carol'}, body='locked now', created=GH_NOW - 100,
                   review=True, reply_to=parent['id'])
    github.notify(REPO, 12, title='Fix the poller', updated=GH_NOW - 100, pull=True)
    return github


def _router_blocks(dispatch):
    return [call['message']['content'] for call in dispatch.to(ROUTER)]


def test_poll_routes_new_comments_once_and_respects_last_modified(
        store, router, dispatch, mailbox, github_enabled, world):
    github_enabled()
    adapter = github_adapter(store, world)
    summary = adapter.poll_once()
    assert summary['status'] == 200 and summary['threads'] == [str(PR)]
    [first] = world.calls('GET', '/notifications')
    assert first['query'] == {'all': 'true', 'per_page': '50', 'since': gh_time(GH_NOW - 600)}
    assert first['headers']['Authorization'] == f'Bearer {GH_TOKEN}'
    assert first['headers']['X-GitHub-Api-Version'] == '2022-11-28'
    assert 'If-Modified-Since' not in first['headers']
    # mentions (default): alice's @-mention and carol's reply to the owner's review comment.
    [block] = _router_blocks(dispatch)
    assert '@octo-owner plan A or B?' in block and 'locked now' in block and 'LGTM' not in block
    assert 'Channel events (GitHub)' in block and 'owner_authored="false"' in block
    assert dispatch.to(ROUTER)[0]['message']['origin']['initiator'] == 'channel'
    # The parent outside the window was resolved once by ID.
    assert len(world.calls('GET', f'/repos/octo/repo/pulls/comments/{world.review_comments[("octo/repo", 12)][0]["id"]}')) == 1
    account = store.account('github')
    assert account['poll']['intervalS'] == 60 and account['poll']['nextAtMs'] == GH_NOW * 1000 + 60_000
    assert account['poll']['lastModified'] == world.last_modified
    assert account['frontierAtMs'] == (GH_NOW - 100) * 1000
    assert account['status']['state'] == 'connected'
    thread = store.thread(PR)
    assert thread['title'] == 'Fix the poller' and thread['github']['isPullRequest'] is True
    # Unchanged notifications answer 304: no thread is read and nothing is delivered again.
    before = len(world.requests)
    adapter.poll_once()
    assert world.requests[before]['headers']['If-Modified-Since'] == world.last_modified
    assert len(world.requests) == before + 1 and len(dispatch.calls) == 1


def test_x_poll_interval_is_the_only_cadence(store, router, dispatch, mailbox, github_enabled, world):
    now = GH_NOW * 1000
    assert due_work(now, next_poll_at=now + 5000, poll_requested=True, outbox_pending=False,
                    blocked_until=0) == (False, False, 5.0)
    assert due_work(now, next_poll_at=now, poll_requested=False, outbox_pending=False,
                    blocked_until=0) == (False, True, 0.0)
    # Before GitHub sent an interval, the existing scheduler tick drives passes.
    assert due_work(now, next_poll_at=None, poll_requested=False, outbox_pending=False,
                    blocked_until=0) == (False, False, None)
    assert due_work(now, next_poll_at=None, poll_requested=True, outbox_pending=True,
                    blocked_until=0) == (True, True, 0.0)
    assert due_work(now, next_poll_at=now, poll_requested=True, outbox_pending=True,
                    blocked_until=now + 2000) == (False, False, 2.0)
    github_enabled()
    world.poll_interval = '120'  # GitHub raised the interval under load
    github_adapter(store, world).poll_once()
    assert store.account('github')['poll']['nextAtMs'] == now + 120_000
    # A restarted session resumes the persisted schedule instead of polling early.
    restarted = github_adapter(store, world)
    assert restarted._next_poll_at == now + 120_000


def test_route_failure_replays_the_window_and_routes_exactly_once(
        store, router, dispatch, mailbox, github_enabled, world):
    from lib.channels.delivery import DeliveryFailed
    github_enabled()
    dispatch.outcomes[ROUTER] = 'rejected'
    with pytest.raises(DeliveryFailed):
        github_adapter(store, world).poll_once()
    account = store.account('github')
    assert account['frontierAtMs'] == (GH_NOW - 600) * 1000 and 'lastModified' not in account['poll']
    assert not (store.thread(PR) or {}).get('github')
    # Restarted session: same window, recorded-but-unrouted events route with the same command IDs.
    dispatch.outcomes.clear()
    github_adapter(store, world).poll_once()
    first, second = dispatch.to(ROUTER)
    assert first['command_id'] == second['command_id']
    github_adapter(store, world).poll_once()
    assert len(dispatch.to(ROUTER)) == 2
    assert store.account('github')['frontierAtMs'] == (GH_NOW - 100) * 1000


def test_watched_thread_is_read_directly_with_a_stable_since_and_etag(
        store, router, dispatch, mailbox, github_enabled, world):
    github_enabled()
    world.notifications.clear()
    service.configure_thread(OWNER, str(PR), mode='watch', store=store)
    watched_at = store.thread(PR)['modeChangedAtMs']
    world.now = watched_at // 1000 + 60
    # The owner switches plans in a PR comment: no notification is needed.
    world.comment(REPO, 12, user=GH_USER, body='Switching to plan B', created=world.now - 5)
    adapter = github_adapter(store, world)
    adapter.poll_once()
    [call] = dispatch.to(ROUTER)
    assert 'Switching to plan B' in call['message']['content']
    assert 'owner_authored="true"' in call['message']['content']
    assert call['message']['origin']['initiator'] == 'channel'  # evidence, never human input
    assert 'plan A or B' not in call['message']['content']  # the past before watch is not replayed
    path = '/repos/octo/repo/issues/12/comments'
    adapter.poll_once()  # since moved past the seen comment: a fresh ETag is taken
    adapter.poll_once()
    third = world.calls('GET', path)[-1]
    assert third['headers'].get('If-None-Match') and third['query']['since'] == world.calls('GET', path)[-2]['query']['since']
    assert len(dispatch.to(ROUTER)) == 1


def test_owner_comments_outside_watch_are_context_never_router_input(
        store, router, dispatch, mailbox, github_enabled, world):
    github_enabled()
    world.comment(REPO, 12, user=GH_USER, body='thinking about it', created=GH_NOW - 150)
    github_adapter(store, world).poll_once()
    content = _router_blocks(dispatch)[0]
    assert 'thinking about it' not in content
    stored = store.event(PR, external_id((GH_NOW - 150) * 1000, KIND_ISSUE_COMMENT,
                                         world.comments[('octo/repo', 12)][-1]['id']))
    assert stored['disposition'] == 'context'


def test_rate_limit_pauses_the_session_until_github_allows(store, router, dispatch, github_enabled, world):
    github_enabled()
    reset = GH_NOW + 900

    def limited(record):
        if record['path'] == '/notifications':
            return world.reply(403, {'message': 'API rate limit exceeded'}, record['url'],
                               {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(reset)})
        return None
    world.responders.append(limited)
    adapter = github_adapter(store, world)
    with pytest.raises(GitHubRateLimited) as raised:
        adapter.poll_once()
    adapter._block(raised.value.resume_at_ms)
    account = store.account('github')
    assert account['poll']['nextAtMs'] == reset * 1000
    assert account['status']['state'] == 'disconnected' and 'rate limit' in account['status']['detail']
    assert due_work(GH_NOW * 1000, next_poll_at=adapter._next_poll_at, poll_requested=True,
                    outbox_pending=True, blocked_until=adapter._blocked_until)[:2] == (False, False)


def test_a_refused_token_ends_the_session_as_needs_login(store, router, dispatch, github_enabled, world):
    github_enabled()
    world.users.clear()
    adapter = github_adapter(store, world)
    adapter._worker = None
    adapter._run()
    account = store.account('github')
    assert account['status']['state'] == 'needs_login'
    assert GH_TOKEN not in str(account)


# ── outbox ────────────────────────────────────────────────────────────

def _enqueue(store, key='k1', text='Plan B is in.'):
    return outbox.enqueue(OWNER, str(PR), text, idempotency_key=key,
                          origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=store)


def test_github_replies_are_drafts_until_the_owner_approves(store, router, dispatch, github_enabled, world):
    github_enabled()
    item = _enqueue(store)
    assert item.state == 'draft' and item.text.startswith('[Tofu 代发] ')
    adapter = github_adapter(store, world)
    adapter._outbox_pass()
    assert world.posted == []
    outbox.approve(OWNER, item.id, store=store)
    adapter._outbox_pass()
    [posted] = world.posted
    assert posted['body'] == f'[Tofu 代发] Plan B is in.\n\n<!-- tofuOutbox:{item.id} -->'
    sent, _ = store.outbox_item(item.id)
    assert sent.state == 'sent' and sent.external_id == external_id(GH_NOW * 1000, KIND_ISSUE_COMMENT, posted['id'])
    adapter._outbox_pass()
    assert len(world.posted) == 1
    with pytest.raises(ChannelError) as refused:
        outbox.enqueue(OWNER, str(PR), 'x', idempotency_key='k2', reply_to=f'{PR}/1',
                       origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=store)
    assert refused.value.code == 'reply_to_unsupported'


def test_posted_comment_echo_is_not_ingested(store, router, dispatch, mailbox, github_enabled, world):
    github_enabled()
    service.configure_thread(OWNER, str(PR), mode='watch', reply_policy='auto', store=store)
    world.notifications.clear()
    world.now = store.thread(PR)['modeChangedAtMs'] // 1000 + 30
    adapter = github_adapter(store, world)
    item = _enqueue(store)
    adapter._outbox_pass()
    assert store.outbox_item(item.id)[0].state == 'sent'
    adapter.poll_once()
    assert dispatch.calls == []  # Tofu's own comment never becomes input


@pytest.mark.parametrize('created', [True, False])
def test_unknown_post_outcome_is_reconciled_before_any_resend(
        store, router, dispatch, github_enabled, world, created):
    github_enabled()
    service.configure_thread(OWNER, str(PR), reply_policy='auto', store=store)
    item = _enqueue(store)
    calls = {'n': 0}

    def flaky(record):
        if record['method'] == 'POST' and calls['n'] == 0:
            calls['n'] += 1
            if created:
                world._create(('octo/repo', 12), record, dict(GH_USER), record['url'])
            return world.reply(502, {'message': 'Bad Gateway'}, record['url'])
        return None
    world.responders.append(flaky)
    adapter = github_adapter(store, world)
    adapter._last_mutation = None
    adapter._outbox_pass()
    assert store.outbox_item(item.id)[0].state == 'unknown'
    adapter._last_mutation = None
    adapter._outbox_pass()
    assert store.outbox_item(item.id)[0].state == 'sent'
    assert len(world.posted) == 1 and len(world.calls('POST', '/repos/octo/repo/issues/12/comments')) == (1 if created else 2)


def test_definite_refusals_fail_without_blocking_other_items(store, router, github_enabled, world):
    github_enabled()
    service.configure_thread(OWNER, str(PR), reply_policy='auto', store=store)
    world.responders.append(lambda record: world.reply(422, {'message': 'Validation Failed'}, record['url'])
                            if record['method'] == 'POST' else None)
    item = _enqueue(store)
    github_adapter(store, world)._outbox_pass()
    failed, _ = store.outbox_item(item.id)
    assert (failed.state, failed.error) == ('failed', 'invalid_input')


def test_executors_send_only_their_own_channel(store, router, github_enabled, enabled, world):
    github_enabled()
    enabled()
    service.configure_thread(OWNER, 'dx:uid:2002', reply_policy='auto', store=store)
    outbox.enqueue(OWNER, 'dx:uid:2002', 'hi', idempotency_key='dx',
                   origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=store)
    github_adapter(store, world)._outbox_pass()
    assert world.calls('POST', '/repos/octo/repo/issues/12/comments') == []
    assert [item.state for item in store.active_outbox(channel='dx')] == ['queued']


def test_owners_are_isolated(records, router, dispatch, mailbox, vault, world):
    from lib.channels.store import ChannelStore
    other_token = 'ghp_FAKEsecondOwnerToken000000000002'
    world.users[other_token] = {'id': 999, 'login': 'someone-else', 'name': 'Else'}
    stores = {OWNER: ChannelStore(OWNER, client=records), OWNER + 1: ChannelStore(OWNER + 1, client=records)}
    for owner, token, uid in ((OWNER, GH_TOKEN, OWNER_UID), (OWNER + 1, other_token, '999')):
        entry = f'channel.u{owner}.github.token'
        vault.set_entry(entry, token)
        stores[owner].update_account('github', lambda account, entry=entry, uid=uid: {
            **account, 'enabled': True, 'identity': {'uid': uid, 'mis': 'x', 'name': ''},
            'credential': {'name': entry, 'managed': True}, 'frontierAtMs': (GH_NOW - 600) * 1000})
    github_adapter(stores[OWNER], world, owner=OWNER).poll_once()
    assert {call['headers']['Authorization'] for call in world.requests} == {f'Bearer {GH_TOKEN}'}
    assert stores[OWNER].thread(PR) is not None and stores[OWNER + 1].thread(PR) is None
    assert all(key.startswith(f'u{OWNER}/') for key in records.keys('/event/'))
    assert service.thread_page(OWNER + 1, channel='github', store=stores[OWNER + 1])['items'] == []


def test_worker_thread_starts_polls_and_stops_cleanly(store, router, dispatch, mailbox, github_enabled, world):
    github_enabled()
    adapter = github_adapter(store, world)
    adapter.start()
    try:
        for _ in range(200):
            if store.account('github')['status']['state'] == 'connected':
                break
            import time
            time.sleep(0.01)
        assert store.account('github')['status']['state'] == 'connected'
        assert len(world.calls('GET', '/notifications')) == 1  # then waits for X-Poll-Interval
        adapter.tick()
        assert len(world.calls('GET', '/notifications')) == 1
    finally:
        assert adapter.stop(2.0)
    assert not adapter.alive()


def test_cross_channel_plan_switch_reaches_the_same_router_as_evidence(
        store, router, dispatch, mailbox, enabled, github_enabled, world):
    """IM chooses plan A (human input); a PR comment switches to plan B (evidence, same router)."""
    from lib.channels.ingress import ingest
    from tests.channels.conftest import OWNER_UID as DX_OWNER, make_event
    enabled(command_chat='dx:gid:900')
    github_enabled()
    ingest(OWNER, make_event('dx:gid:900', sender=DX_OWNER, text='Use plan A for octo/repo#12'), store=store)
    service.configure_thread(OWNER, str(PR), mode='watch', store=store)  # the router watches the PR
    world.notifications.clear()
    world.now = store.thread(PR)['modeChangedAtMs'] // 1000 + 60
    world.comment(REPO, 12, user=GH_USER, body='Switching to plan B: drop the lock.', created=world.now - 5)
    github_adapter(store, world).poll_once()
    command, comment = dispatch.to(ROUTER)
    assert command['message']['content'] == 'Use plan A for octo/repo#12'
    assert command['message']['origin']['initiator'] == 'human'
    assert comment['message']['origin']['initiator'] == 'channel'
    assert comment['message']['origin']['channel']['thread'] == str(PR)
    block = comment['message']['content']
    assert 'Switching to plan B' in block and 'owner_authored="true"' in block
    assert 'never owner instructions' in block


def test_mode_change_times_are_moved_onto_githubs_clock(store, router, dispatch, mailbox, github_enabled, world):
    """This host's clock runs ten minutes ahead of GitHub: a comment right after the switch still arrives."""
    github_enabled()
    world.notifications.clear()
    service.configure_thread(OWNER, str(PR), mode='watch', store=store)
    switched = store.thread(PR)['modeChangedAtMs'] // 1000
    world.now = switched - 600 + 60  # GitHub's clock, one minute after the switch
    world.comment(REPO, 12, user=GH_USER, body='Plan B after all', created=world.now - 30)
    github_adapter(store, world, clock=lambda: (world.now + 600) * 1000).poll_once()
    [call] = dispatch.to(ROUTER)
    assert 'Plan B after all' in call['message']['content']
