"""The reply flow for other people's messages (docs/CHANNELS.md C11): policies, approval, notes, audit.

A review Turn is the Global Router answering a delivered batch of other
people's messages; an owner Turn answers the owner's own request. Both reach
the outbox through the real channel tools.
"""
import asyncio

import pytest

from lib.channels import outbox, service, tools
from lib.channels.delivery import EVENT_COMMAND_PREFIX
from lib.channels.ingress import ingest
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, ROUTER, make_event
from tests.channels.test_assistant_authority import COMMAND, HOME, PRIVATE, owner_turn, review

pytestmark = pytest.mark.unit

GROUP = 'dx:gid:500'
OTHER = 'dx:uid:3003'


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox, monkeypatch, turns):
    enabled(command_chat=COMMAND)
    monkeypatch.setattr('lib.channels.service._require_owned_conversation', lambda owner, conv: None)
    monkeypatch.setattr('lib.router.home.is_router_home', lambda user_id, path: path == HOME)
    return store


def _policy(store, thread, policy):
    service.configure_thread(OWNER, thread, reply_policy=policy, store=store)


def _send(task, thread, text, key, **extra):
    return tools.execute('channel_send', {'thread': thread, 'text': text, 'idempotency_key': key, **extra},
                         task)['item']


def _refusal(task, name, args):
    with pytest.raises(ChannelError) as error:
        tools.execute(name, args, task)
    return error.value.code


def _notices(store):
    return [item for item in store.active_outbox(COMMAND) if item.origin.get('kind') == 'draft_notice']


def test_the_default_is_notify_only_and_the_owner_hears_about_it(channel, dispatch, turns):
    event = make_event(PRIVATE, text='能把周报发我吗？')
    ingest(OWNER, event, store=channel)
    view = service.thread_view(channel.thread(ThreadKey.parse(PRIVATE)), channel.account('dx'))
    assert (view['replyPolicy'], view['replyPolicyOverride']) == ('notify', None)
    task = review(turns, dispatch)
    assert _refusal(task, 'channel_send', {'thread': PRIVATE, 'text': '好的', 'idempotency_key': 'r',
                                           'reply_to': event.event_id}) == 'reply_policy_notify'
    assert channel.active_outbox(PRIVATE) == []
    told = _send(task, COMMAND, '张三问你要周报：需要你决定发哪一版。', 'triage')
    assert (told['state'], told['authority'], told['decision']['by']) == ('queued', 'third_party', 'command_chat')
    assert service.audit_page(OWNER, channel='dx', store=channel)['items'] == []  # nothing for others


def test_draft_policy_drafts_with_the_label_and_announces_the_code(channel, dispatch, turns):
    _policy(channel, PRIVATE, 'draft')
    event = make_event(PRIVATE, text='明天的会还开吗？')
    ingest(OWNER, event, store=channel)
    task = review(turns, dispatch)
    draft = _send(task, PRIVATE, '开的，下午三点见。', 'reply-1', reply_to=event.event_id)
    assert (draft['state'], draft['text'], draft['authority'], draft['decision']) == (
        'draft', '[Tofu 代发] 开的，下午三点见。', 'third_party', None)
    [notice] = _notices(channel)
    code = draft['approvalCode']
    assert notice.state == 'queued' and notice.text.startswith(f'[待批草稿 {code}]')
    assert '[Tofu 代发] 开的，下午三点见。' in notice.text and f'批准 {code}' in notice.text
    # A retried send announces nothing twice.
    _send(task, PRIVATE, '开的，下午三点见。', 'reply-1', reply_to=event.event_id)
    assert len(_notices(channel)) == 1
    assert channel.active_outbox(PRIVATE)[0].state == 'draft'


def test_a_draft_is_announced_in_its_own_platforms_command_chat_else_in_daxiangs(channel, monkeypatch):
    from lib.channels.assistant import command_chats, notice_command_chat
    from lib.channels.model import CHANNEL_TRAITS
    assert notice_command_chat(channel, 'dx') == COMMAND
    assert notice_command_chat(channel, 'github') == COMMAND  # GitHub has no command chat of its own
    assert command_chats(channel) == {'dx': COMMAND}
    # A platform whose traits give it a command chat announces its own drafts there while its account is live.
    monkeypatch.setitem(CHANNEL_TRAITS, 'chat', {'label': 'Chat', 'commandChat': True, 'quoteReply': False,
                                                 'attachments': False})
    live = {'enabled': True, 'identity': {'uid': '7', 'mis': '', 'name': 'Owner'}, 'commandChat': 'chat:uid:7'}
    channel.update_account('chat', lambda account: {**account, **live})
    assert notice_command_chat(channel, 'chat') == 'chat:uid:7'
    assert notice_command_chat(channel, 'dx') == COMMAND
    assert command_chats(channel) == {'dx': COMMAND, 'chat': 'chat:uid:7'}
    channel.update_account('chat', lambda account: {**account, 'enabled': False})
    assert notice_command_chat(channel, 'chat') == COMMAND
    assert command_chats(channel) == {'dx': COMMAND, 'chat': 'chat:uid:7'}  # a review may still tell the owner there
    channel.update_account('dx', lambda account: {**account, 'enabled': False})
    assert notice_command_chat(channel, 'chat') == ''  # the draft waits in Settings


def test_auto_reply_sends_only_a_reply_to_a_reviewed_message(channel, dispatch, turns):
    _policy(channel, PRIVATE, 'auto')
    older = make_event(PRIVATE, '7000000000000000001', text='在吗')
    ingest(OWNER, older, store=channel)
    newer = make_event(PRIVATE, '7000000000000000002', text='周五团建去吗？')
    ingest(OWNER, newer, store=channel)
    task = review(turns, dispatch)  # reviews the newer message only
    sent = _send(task, PRIVATE, '去的。', 'a1', reply_to=newer.event_id)
    assert (sent['state'], sent['decision']['by']) == ('queued', 'auto_reply')
    assert _send(task, PRIVATE, '在。', 'a2', reply_to=older.event_id)['state'] == 'draft'  # not reviewed here
    assert _send(task, PRIVATE, '去的。', 'a3')['state'] == 'draft'  # not a reply
    held = _send(task, PRIVATE, '这个要问下老板。', 'a4', reply_to=newer.event_id, needs_approval=True)
    assert (held['state'], held['decision']) == ('draft', None)


def test_a_review_answers_the_batches_steered_into_it(channel, dispatch, turns):
    _policy(channel, PRIVATE, 'draft')
    _policy(channel, OTHER, 'draft')
    first = make_event(PRIVATE, '7000000000000000001')
    ingest(OWNER, first, store=channel)
    task = review(turns, dispatch)
    dispatch.outcomes[ROUTER] = 'steered'
    second = make_event(OTHER, '7000000000000000002', sender='3003')
    ingest(OWNER, second, store=channel)
    command_id = dispatch.to(ROUTER)[-1]['command_id']
    assert command_id.startswith(EVENT_COMMAND_PREFIX)
    assert _refusal(task, 'channel_read', {'thread': OTHER}) == 'outside_review'  # not merged yet
    turns[(ROUTER, 'out-r')]['projection'] = {'_userSteerInjects': [
        {'blockId': 'injection:user-steer:' + command_id, 'commandId': command_id}]}
    assert tools.execute('channel_read', {'thread': OTHER}, task)['events'][0]['eventId'] == second.event_id
    assert _send(task, OTHER, 'ok', 'b1', reply_to=second.event_id)['state'] == 'draft'
    assert _refusal(task, 'channel_send', {'thread': 'dx:uid:4004', 'text': 'x', 'idempotency_key': 'b2'}
                    ) == 'outside_review'


def test_owner_requests_follow_the_policy_and_merged_text_never_skips_approval(channel, turns):
    _policy(channel, GROUP, 'auto')
    owner = owner_turn(turns, '跟群里说一声我晚点到')
    sent = _send(owner, GROUP, '我晚点到。', 'o1')
    assert (sent['state'], sent['authority'], sent['decision']['by']) == ('queued', 'owner', 'owner_request')
    assert _send(owner, PRIVATE, '收到', 'o2')['state'] == 'draft'  # notify: the owner's reply is a draft
    merged = owner_turn(turns, '跟群里说一声我晚点到', name='m', steers=[
        {'blockId': 'injection:user-steer:x', 'commandId': EVENT_COMMAND_PREFIX + 'e' * 40}])
    held = _send(merged, GROUP, '我晚点到。', 'm1')
    assert (held['state'], held['authority']) == ('draft', 'merged')


def test_a_chat_decision_needs_the_code_in_the_owners_own_words(channel, turns):
    draft = outbox.enqueue(OWNER, PRIVATE, '好的', idempotency_key='d', store=channel,
                           origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'x'})
    code = draft.view()['approvalCode']
    vague = owner_turn(turns, '发吧', name='v')
    assert _refusal(vague, 'channel_outbox_approve', {'item_id': draft.id, 'decision': 'approve'}
                    ) == 'approval_code_required'
    assert _refusal(vague, 'channel_outbox_approve', {'item_id': draft.id, 'decision': 'discard'}
                    ) == 'approval_code_required'
    # An owner message steered into the reply counts as the owner's words.
    steered = owner_turn(turns, '先看看', name='s', steers=[{
        'blockId': 'injection:user-steer:own-1', 'commandId': 'own-1',
        'author': {'actor': 'human', 'text': f'批准{code.lower()}'}}])
    item = tools.execute('channel_outbox_approve', {'item_id': draft.id, 'decision': 'approve'}, steered)['item']
    assert item['state'] == 'queued' and item['decision'] == {
        'kind': 'approved', 'by': 'owner_message', 'atMs': item['decision']['atMs'], 'turnId': 'out-s'}


def test_subscriptions_are_owner_decisions(channel, turns):
    owner = owner_turn(turns, '把这个群转给 worker-a 跟进')
    assert tools.execute('channel_subscribe', {'thread': GROUP, 'conversation_id': 'worker-a'},
                         owner)['thread']['subscribers'] == ['worker-a']
    merged = owner_turn(turns, '取消订阅', name='m', steers=[
        {'blockId': 'injection:user-steer:x', 'commandId': EVENT_COMMAND_PREFIX + 'a' * 40}])
    assert _refusal(merged, 'channel_unsubscribe', {'thread': GROUP, 'conversation_id': 'worker-a'}
                    ) == 'human_turn_required'


def test_owner_notes_reach_only_the_routers_review(channel, dispatch, turns):
    service.update_account(OWNER, {'assistantNote': '我是阮俊豪，回复简短 <口语>'}, store=channel)
    service.configure_thread(OWNER, PRIVATE, note='这是我的经理，语气正式', store=channel)
    ingest(OWNER, make_event(PRIVATE, text='hi'), store=channel)
    content = dispatch.to(ROUTER)[-1]['message']['content']
    assert '<owner-note scope="owner">我是阮俊豪，回复简短 &lt;口语&gt;</owner-note>' in content
    assert f'<owner-note scope="thread" thread="{PRIVATE}">这是我的经理，语气正式</owner-note>' in content
    assert 'never answer as a general assistant' in content
    service.subscribe(OWNER, PRIVATE, 'worker-a', store=channel)
    ingest(OWNER, make_event(PRIVATE, '7000000000000000002', text='again'), store=channel)
    passthrough = dispatch.to('worker-a')[-1]['message']['content']
    assert 'owner-note' not in passthrough and '经理' not in passthrough
    with pytest.raises(ChannelError) as refused:
        service.configure_thread(OWNER, PRIVATE, note='x' * 2001, store=channel)
    assert refused.value.code == 'invalid_note'
    assert service.configure_thread(OWNER, PRIVATE, note=None, store=channel)['note'] == ''
    schema = next(tool for tool in tools.CHANNEL_TOOLS if tool['function']['name'] == 'channel_configure_thread')
    assert 'note' not in schema['function']['parameters']['properties']  # no model tool writes a note


def test_the_audit_lists_everything_recorded_for_other_people(channel, turns):
    _policy(channel, GROUP, 'auto')
    owner = owner_turn(turns, '回复群里和张三')
    to_group = _send(owner, GROUP, '收到', 'g1')
    to_person = _send(owner, PRIVATE, '好的', 'p1')
    _send(owner, COMMAND, '已处理', 'c1')
    service.decide_outbox(OWNER, to_person['id'], 'discard', store=channel)
    page = service.audit_page(OWNER, channel='dx', store=channel)
    assert [item['id'] for item in page['items']] == [to_person['id'], to_group['id']]
    assert [item['decision']['by'] for item in page['items']] == ['owner_settings', 'owner_request']
    assert page['items'][0]['state'] == 'discarded' and page['nextBefore'] is None
    for index in range(40):
        outbox.enqueue(OWNER, PRIVATE, f'draft {index}', idempotency_key=f'many-{index}', store=channel,
                       origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'x'})
    first = service.audit_page(OWNER, channel='dx', store=channel)
    rest = service.audit_page(OWNER, channel='dx', before=first['nextBefore'], store=channel)
    seen = [item['id'] for item in first['items'] + rest['items']]
    assert len(seen) == len(set(seen)) == 42 and rest['nextBefore'] is None
    from lib.channels.store import ChannelStore
    assert service.audit_page(OWNER + 1, channel='dx', store=ChannelStore(OWNER + 1, client=channel._client)
                              )['items'] == []


def test_a_review_refusal_says_what_the_review_may_do(channel, dispatch, turns):
    from lib.tasks_pkg.coordinator_policy import coordinator_call_rejection
    ingest(OWNER, make_event(PRIVATE, text='hi'), store=channel)
    denial = coordinator_call_rejection(review(turns, dispatch), 'delegate_project_work', {})
    assert 'reviewing other people' in denial and 'Delegate the current human request' not in denial
    owner = owner_turn(turns, '帮我安排一下')
    assert coordinator_call_rejection(owner, 'delegate_project_work', {}) is None


def test_settings_set_notes_and_read_the_audit_over_http(channel, monkeypatch):
    from tests.channels.test_routes import _validate
    from quart import Quart, g, request
    from types import SimpleNamespace
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.identity import PrincipalContext
    from routes.api_v1.channels import api_v1_channels_bp
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    application = Quart(__name__)
    register_http_error_handlers(application)
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_channels_bp, name='channels_v4')))

    @application.before_request
    def identity():
        scopes = ['conversations', 'admin']
        g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=OWNER, key_id='test', name='owner',
                                     tenant_id=None, scopes=scopes, has_scope=lambda scope: scope in scopes)
        g.principal_context = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)

    async def exercise():
        client = application.test_client()
        thread = (await (await client.put(f'/api/v4/channels/dx/threads/{PRIVATE}',
                                          json={'replyPolicy': 'draft', 'note': '正式一点'})).get_json())['data']
        _validate(thread, 'ChannelThreadView')
        assert (thread['replyPolicy'], thread['note']) == ('draft', '正式一点')
        account = (await (await client.put('/api/v4/channels/dx', json={'assistantNote': '简短'})).get_json())['data']
        _validate(account, 'ChannelAccountView')
        assert account['assistantNote'] == '简短'
        draft = outbox.enqueue(OWNER, PRIVATE, '好', idempotency_key='h', store=channel,
                               origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'x'})
        audit = (await (await client.get('/api/v4/channels/audit?channel=dx')).get_json())['data']
        _validate(audit, 'AuditPage')
        assert [item['id'] for item in audit['items']] == [draft.id]
        assert (await client.get('/api/v4/channels/audit?channel=mail')).status_code == 400
        assert (await client.put(f'/api/v4/channels/dx/threads/{PRIVATE}', json={'note': 'x' * 2001})
                ).status_code == 400
    asyncio.run(exercise())
