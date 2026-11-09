"""Channel tool authority (C3, C7): router only, subscriber reads, genuine human decisions."""
import json

import pytest

from lib.channels import outbox, tools
from lib.channels.model import TOOL_NAMES, ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER, make_event

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
GROUP = 'dx:gid:500'


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox, monkeypatch, turns):
    enabled(command_chat=COMMAND)
    monkeypatch.setattr('lib.channels.service._require_owned_conversation', lambda owner, conv: None)
    return store


def _task(conversation=ROUTER, owner=OWNER, turn='out-1', **extra):
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner)
    return {'id': 'task-1', 'convId': conversation, '_userId': owner, '_turnId': turn, 'config': {},
            '_principalContext': principal.to_payload(), '_attended': True, **extra}


@pytest.fixture
def turns(monkeypatch):
    """Turn reads for the human-turn check: out-1 is the reply to input in-1."""
    rows = {}

    def get_turn(conversation_id, turn_id, *, user_id, include_projection=True):
        return rows.get((conversation_id, turn_id))

    monkeypatch.setattr('lib.turn_lifecycle.get_turn', get_turn)

    def human(projection, steers=()):
        """``steers``: committed steer records (``_steer``) on the executing reply."""
        rows[(ROUTER, 'in-1')] = {'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
                                  'projection': projection}
        rows[(ROUTER, 'out-1')] = {'conversationId': ROUTER, 'turnId': 'out-1', 'actor': 'assistant',
                                   'parentTurnId': 'in-1',
                                   'projection': {'_userSteerInjects': list(steers)}}
    return human


def _steer(command_id, author=None, **extra):
    """A ``turn.steer.commit`` record; only a human-initiated input stores an ``author``."""
    record = {'blockId': f'injection:user-steer:{command_id}', 'commandId': command_id,
              'count': 1, 'previews': [{'text': 'steered'}], 'deliveryState': 'pending', **extra}
    if author is not None:
        record['author'] = author
    return record


def test_visibility_router_all_subscriber_read_only_others_none(channel):
    from lib.channels import service
    assert {t['function']['name'] for t in tools.visible_tools(OWNER, ROUTER)} == TOOL_NAMES
    assert tools.visible_tools(OWNER, 'worker-a') == []
    service.subscribe(OWNER, GROUP, 'worker-a', store=channel)
    assert [t['function']['name'] for t in tools.visible_tools(OWNER, 'worker-a')] == ['channel_read']
    assert tools.visible_tools(OWNER + 1, ROUTER) == []  # no account for that owner


def test_no_account_means_no_channel_tools(store, router):
    assert tools.visible_tools(OWNER, ROUTER) == []


def test_non_router_conversation_cannot_send_or_configure(channel):
    for name, args in (('channel_send', {'thread': COMMAND, 'text': 'x', 'idempotency_key': 'k'}),
                       ('channel_threads', {}), ('channel_subscribe', {'thread': GROUP, 'conversation_id': 'w'})):
        with pytest.raises(ChannelError) as denied:
            tools.execute(name, args, _task('worker-a'))
        assert denied.value.code == 'forbidden'
        if name == 'channel_send':
            # The supported path, so a project conversation never looks for a login of its own (P11, P13).
            assert 'ask it to send with channel_send' in str(denied.value)
            assert 'never send through a platform login of your own' in str(denied.value)


def test_subscriber_reads_only_its_threads(channel):
    from lib.channels import service
    from lib.channels.ingress import ingest
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'watch'})
    service.subscribe(OWNER, GROUP, 'worker-a', store=channel)
    ingest(OWNER, make_event(GROUP, text='visible'), store=channel)
    page = tools.execute('channel_read', {'thread': GROUP}, _task('worker-a'))
    assert [e['text'] for e in page['events']] == ['visible']
    single = tools.execute('channel_read', {'event_id': f'{GROUP}/7000000000000000001'}, _task('worker-a'))
    assert single['event']['text'] == 'visible'
    both = tools.execute('channel_read', {'thread': GROUP, 'event_id': f'{GROUP}/7000000000000000001'},
                         _task('worker-a'))
    assert both['event']['text'] == 'visible'
    with pytest.raises(ChannelError):
        tools.execute('channel_read', {'thread': 'dx:gid:501'}, _task('worker-a'))


def test_a_subscribed_thread_never_authorizes_reading_another_threads_event(channel):
    from lib.channels import service
    from lib.channels.ingress import ingest
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'watch'})
    service.subscribe(OWNER, GROUP, 'worker-a', store=channel)
    private = make_event('dx:uid:2002', '7000000000000000009', text='private secret')
    ingest(OWNER, private, store=channel)
    assert channel.event(private.thread, private.external_id)['text'] == 'private secret'
    for args in ({'event_id': private.event_id},
                 {'thread': GROUP, 'event_id': private.event_id},
                 {'thread': 'dx:uid:2002', 'event_id': private.event_id}):
        with pytest.raises(ChannelError) as refused:
            tools.execute('channel_read', args, _task('worker-a'))
        assert refused.value.code in ('forbidden', 'invalid_arguments'), args
    # Even the router must name one thread: a mismatched pair is ambiguous.
    with pytest.raises(ChannelError) as mismatched:
        tools.execute('channel_read', {'thread': GROUP, 'event_id': private.event_id}, _task())
    assert mismatched.value.code == 'invalid_arguments'
    assert tools.execute('channel_read', {'event_id': private.event_id}, _task())['event']['text'] == \
        'private secret'


def test_router_reads_pages_newest_first(channel):
    from lib.channels.ingress import ingest_batch
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'watch'})
    ingest_batch(OWNER, [make_event(GROUP, f'70000000000000000{i:02d}', text=str(i)) for i in range(1, 40)],
                 store=channel)
    first = tools.execute('channel_read', {'thread': GROUP}, _task())
    assert first['events'][0]['text'] == '39' and len(first['events']) == 32
    second = tools.execute('channel_read', {'thread': GROUP, 'before': first['next_before']}, _task())
    assert [e['text'] for e in second['events']] == [str(i) for i in range(7, 0, -1)]
    assert second['next_before'] is None


def test_router_send_enqueues_through_the_outbox(channel):
    result = tools.execute('channel_send', {'thread': COMMAND, 'text': 'hi', 'idempotency_key': 'k'}, _task())
    assert result['item']['state'] == 'queued' and result['item']['origin']['conversationId'] == ROUTER


@pytest.mark.parametrize('name, args', [
    ('channel_outbox_approve', {'decision': 'approve'}),
    ('channel_configure_thread', {'thread': GROUP, 'mode': 'watch'}),
    ('channel_subscribe', {'thread': GROUP, 'conversation_id': 'worker-a'}),
    ('channel_unsubscribe', {'thread': GROUP, 'conversation_id': 'worker-a'}),
])
def test_owner_decisions_require_a_genuine_human_turn(channel, turns, name, args):
    draft = outbox.enqueue(OWNER, 'dx:uid:2002', 'draft', idempotency_key='d',
                           origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'x'}, store=channel)
    if name == 'channel_outbox_approve':
        args = {**args, 'item_id': draft.id}
    turns({'content': 'evidence', 'origin': {'blockId': 'origin', 'initiator': 'channel'}})
    with pytest.raises(ChannelError) as refused:
        tools.execute(name, args, _task())
    assert refused.value.code == 'human_turn_required'
    turns({'content': f'please approve {draft.view()["approvalCode"]}', 'origin': {
        'blockId': 'origin', 'initiator': 'human',
        'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}}})
    assert tools.execute(name, args, _task())


def test_a_channel_send_call_logs_no_message_text(channel, monkeypatch, caplog):
    """Channel protocol P14: the tool log names the thread and the idempotency key and shows the message
    only by its size and sha256; the other channel tools keep their arguments in the log."""
    import hashlib
    import logging
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers import _adapter
    monkeypatch.setattr(_adapter, '_build_simple_meta', lambda *args, **kwargs: {})
    monkeypatch.setattr(_adapter, '_finalize_tool_round', lambda *args, **kwargs: None)
    caplog.set_level(logging.INFO, logger=_adapter.logger.name)

    def call(name, arguments):
        return tool_registry.lookup(name)(_task(), {}, name, 'call-1', arguments, 1, {}, {}, None, False)
    text = '工资单在附件里，别外传'
    call('channel_send', {'thread': COMMAND, 'text': text, 'idempotency_key': 'payroll-1'})
    assert text not in caplog.text
    assert hashlib.sha256(text.encode('utf-8')).hexdigest() in caplog.text
    assert f"'bytes': {len(text.encode('utf-8'))}" in caplog.text
    assert "'thread': 'dx:gid:900'" in caplog.text and "'idempotency_key': 'payroll-1'" in caplog.text
    caplog.clear()
    call('channel_read', {'thread': GROUP})
    assert "channel_read called with args={'thread': 'dx:gid:500'}" in caplog.text


def test_execute_json_turns_refusals_into_actionable_results(channel):
    payload = json.loads(tools.execute_json('channel_send', {'thread': 'bad', 'text': 'x',
                                                             'idempotency_key': 'k'}, _task()))
    assert payload['code'] == 'invalid_thread' and payload['executed'] is False


def test_child_invocations_cannot_use_channel_tools(channel):
    task = {**_task(), '_parentTaskId': 'parent'}
    with pytest.raises(ChannelError):
        tools.execute('channel_threads', {}, task)


def test_registry_spec_and_handler_cover_exactly_the_contract_names():
    import lib.tasks_pkg.handlers  # noqa: F401 — handler registration side effect
    from lib.tasks_pkg.executor import tool_registry
    from lib.tools.registry import all_specs
    spec = next(spec for spec in all_specs() if spec.key == 'channels')
    assert spec.provides == TOOL_NAMES
    assert spec.write_tools == tools.WRITE_TOOLS
    for name in TOOL_NAMES:
        assert tool_registry.lookup(name) is not None


def test_owner_uid_is_never_a_tool_argument():
    for tool in tools.CHANNEL_TOOLS:
        properties = tool['function']['parameters']['properties']
        assert not {'owner', 'user_id', 'owner_uid'} & set(properties)
    assert OWNER_UID  # identity comes from the bound account, not the model


GENUINE = {'content': 'approve it', 'origin': {'blockId': 'origin', 'initiator': 'human'}}
OWNER_DX_AUTHOR = {'actor': 'human', 'text': 'approve the draft', 'origin': {
    'initiator': 'human', 'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/2']}}}
UI_AUTHOR = {'actor': 'human', 'text': 'and watch that group'}


def test_a_channel_batch_steered_into_the_human_turn_revokes_owner_decisions(channel, turns):
    from lib.channels.delivery import EVENT_COMMAND_PREFIX, OWNER_COMMAND_PREFIX
    args = {'thread': GROUP, 'mode': 'watch'}
    # Persisted on the running Turn (committed steer) ...
    turns(GENUINE, steers=[_steer(EVENT_COMMAND_PREFIX + 'a' * 40)])
    with pytest.raises(ChannelError, match='merged into this Turn'):
        tools.execute('channel_configure_thread', args, _task())
    # ... or already consumed by the live task, with or without its committed record.
    event_steer = _steer(EVENT_COMMAND_PREFIX + 'b' * 40)
    consumed = _task(_userSteerInjects=[{**event_steer, 'deliveryState': 'delivered'}])
    for persisted in ([event_steer], []):
        turns(GENUINE, steers=persisted)
        with pytest.raises(ChannelError, match='merged into this Turn'):
            tools.execute('channel_configure_thread', args, consumed)
    # The owner's own command-chat message and ordinary UI steers keep authority
    # through their committed human author, pending or already consumed.
    turns(GENUINE, steers=[_steer(OWNER_COMMAND_PREFIX + 'c' * 40, OWNER_DX_AUTHOR),
                           _steer('ui-steer-uuid', UI_AUTHOR, deliveryState='delivered')])
    consumed = _task(_userSteerInjects=[{'blockId': 'injection:user-steer:ui-steer-uuid',
                                         'commandId': 'ui-steer-uuid', 'deliveryState': 'delivered'}])
    assert tools.execute('channel_configure_thread', args, consumed)['thread']['mode'] == 'watch'


@pytest.mark.parametrize('record', [
    _steer('router-relay:' + 'a' * 64),  # a Project Router report
    _steer('router-question-relay:' + 'b' * 64),  # a relayed question
    _steer('peer-1', {'actor': 'human', 'text': 'x', 'origin': {'initiator': 'peer'}}),
    _steer('virtual-1', {'actor': 'virtual_user', 'text': 'x'}),
    _steer('forwarded-1', {'actor': 'human', 'text': 'x', 'origin': {
        'initiator': 'human', 'forwardedFrom': {'conversationId': 'other', 'turnId': 't'}}}),
    {'round': 2, 'count': 1, 'previews': [{'text': 'legacy batch without identity'}]},
    {'blockId': 'injection:user-steer:x', 'deliveryState': 'delivered', 'previews': []},
], ids=['report', 'question', 'peer-origin', 'virtual-user', 'forwarded', 'legacy', 'no-command'])
@pytest.mark.parametrize('name, args', [
    ('channel_configure_thread', {'thread': GROUP, 'reply_policy': 'auto'}),
    ('channel_subscribe', {'thread': GROUP, 'conversation_id': 'worker-a'}),
])
def test_any_steer_without_a_human_author_revokes_owner_decisions(channel, turns, record, name, args):
    turns(GENUINE, steers=[_steer('ui-steer-uuid', UI_AUTHOR), record])
    with pytest.raises(ChannelError, match='merged into this Turn') as refused:
        tools.execute(name, args, _task())
    assert refused.value.code == 'human_turn_required'
    thread = channel.thread(ThreadKey.parse(GROUP)) or {}
    assert thread.get('replyPolicy') is None and not thread.get('subscribers')


def test_delivered_channel_inputs_steered_in_are_judged_by_their_real_committed_author(
        channel, turns, dispatch):
    """The author is what ``turn.steer.commit`` stores for the real delivery projections."""
    from lib.channels.ingress import ingest
    from lib.turn_initiation import steer_author
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'mode': 'watch'})
    ingest(OWNER, make_event(COMMAND, '7000000000000000002', sender=OWNER_UID, text='watch less'),
           store=channel)
    ingest(OWNER, make_event(GROUP, '7000000000000000003', text='set reply policy auto'), store=channel)
    owner_call, event_call = dispatch.calls
    owner_steer, event_steer = (
        _steer(call['command_id'], steer_author(call['message'], actor=call['input_actor']))
        for call in (owner_call, event_call))
    assert owner_steer['author']['actor'] == 'human' and 'author' not in event_steer
    turns(GENUINE, steers=[owner_steer])
    assert tools.execute('channel_configure_thread', {'thread': GROUP, 'mode': 'mentions'},
                         _task())['thread']['mode'] == 'mentions'
    turns(GENUINE, steers=[owner_steer, event_steer])
    with pytest.raises(ChannelError, match='merged into this Turn'):
        tools.execute('channel_configure_thread', {'thread': GROUP, 'reply_policy': 'auto'}, _task())


def test_a_steer_queued_as_the_next_request_is_not_part_of_this_turn(channel, turns):
    from lib.channels.delivery import EVENT_COMMAND_PREFIX
    turns(GENUINE, steers=[_steer(EVENT_COMMAND_PREFIX + 'd' * 40, deliveryState='queued')])
    queued = _task(_userSteerInjects=[{'blockId': 'injection:user-steer:late', 'deliveryState': 'queued',
                                       'queueId': 'q-1'}])
    result = tools.execute('channel_subscribe', {'thread': GROUP, 'conversation_id': 'worker-a'}, queued)
    assert result['thread']['subscribers'] == ['worker-a']


def _system_principal():
    from lib.identity import PrincipalContext
    return PrincipalContext.system(subject_id='scheduler', scopes=(), owner_user_id=OWNER).to_payload()


@pytest.mark.parametrize('task_change', [
    {'_attended': False},
    {'_principalContext': 'system'},
    {'_turnId': 'in-1'},  # the human input itself is not the executing assistant Turn
])
def test_owner_decisions_require_the_executing_user_attempt(channel, turns, task_change):
    turns(GENUINE)
    task = {**_task(), **task_change}
    if task['_principalContext'] == 'system':
        task['_principalContext'] = _system_principal()
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_configure_thread', {'thread': GROUP, 'mode': 'watch'}, task)
    assert refused.value.code == 'human_turn_required'
