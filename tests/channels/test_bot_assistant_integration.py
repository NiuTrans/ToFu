"""Bot platforms under the owner's assistant (docs/CHANNELS.md C11, C12), for an owner on QQ only.

A draft for a QQ chat is announced in the owner's QQ command chat and goes out
through the QQ session; a review of a QQ batch may tell the owner there, and
nowhere outside what it reviews.
"""
import pytest

from lib.channels import outbox, service, tools
from lib.channels.bots import ids
from lib.channels.model import ChannelError
from tests.channels.bot_fakes import OTHER_ID, OWNER_ID
from tests.channels.conftest import OWNER
from tests.channels.test_assistant_authority import HOME, review
from tests.channels.test_bot_channels import FakeTransport, bot, inbound  # noqa: F401  (bot is a fixture)

pytestmark = pytest.mark.unit

COMMAND = str(ids.thread_key('qq', 'uid', OWNER_ID))
STRANGER = str(ids.thread_key('qq', 'uid', OTHER_ID))
GROUP = str(ids.thread_key('qq', 'gid', 'GROUPOPENID01'))


@pytest.fixture
def owner_on_qq(bot, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr('lib.channels.service._require_owned_conversation', lambda owner, conv: None)
    monkeypatch.setattr('lib.router.home.is_router_home', lambda user_id, path: path == HOME)
    transport = FakeTransport()
    session = bot('qq', paired=True, transport=transport)
    assert store.account('dx') is None
    return session, transport


def _notices(store, thread):
    return [item for item in store.active_outbox(thread) if item.origin.get('kind') == 'draft_notice']


def test_a_qq_draft_is_announced_in_the_qq_command_chat_and_sent_by_the_qq_session(owner_on_qq, store):
    session, transport = owner_on_qq
    service.configure_thread(OWNER, GROUP, reply_policy='draft', store=store)
    draft = outbox.enqueue(OWNER, GROUP, '周报今晚发。', idempotency_key='g1', store=store,
                           origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 't'})
    assert draft.state == 'draft'
    [notice] = _notices(store, COMMAND)
    assert notice.state == 'queued' and '[Tofu 代发] 周报今晚发。' in notice.text
    session.execute_outbox()
    [sent] = [item for item in transport.sent if item['peer'] == OWNER_ID]
    assert sent['kind'] == 'uid' and sent['text'] == notice.text
    assert all(item['peer'] != 'GROUPOPENID01' for item in transport.sent)  # the draft itself waits


def test_a_review_of_a_qq_batch_may_tell_the_owner_in_the_qq_command_chat(owner_on_qq, store, dispatch, turns):
    session, _transport = owner_on_qq
    session.deliver([inbound('m1', '周报什么时候发？', peer=OTHER_ID)])
    task = review(turns, dispatch)
    told = tools.execute('channel_send', {'thread': COMMAND, 'text': '有人在 QQ 问周报。', 'idempotency_key': 'tell'},
                         task)['item']
    assert (told['state'], told['authority'], told['decision']['by']) == ('queued', 'third_party', 'command_chat')
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': GROUP, 'text': 'x', 'idempotency_key': 'g'}, task)
    assert refused.value.code == 'outside_review'
    with pytest.raises(ChannelError) as notify:
        tools.execute('channel_send', {'thread': STRANGER, 'text': '好的', 'idempotency_key': 's'}, task)
    assert notify.value.code == 'reply_policy_notify'  # the default for every thread, bot chats included
