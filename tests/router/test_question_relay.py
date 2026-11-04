"""Project Router questions relay to the Global Router as passive notices (R3)."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.router.home import router_home_location
from lib.router.relay import question_relay_command_id, relay_command_id, relay_project_question
from lib.router.watchdog.binding import watchdog_location
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch

pytestmark = pytest.mark.unit
QUESTION = {'projectPath': '/work/project', 'coordinatorId': 'coordinator-project', 'turnId': 'question-turn',
            'toolCallId': 'call-ask', 'guidanceId': 'hg_question01', 'blocking': True,
            'question': 'Which migration window should the rollout use?',
            'options': [{'label': 'Tonight'}, {'label': 'Weekend', 'description': 'Saturday 02:00, lowest traffic'}],
            'questions': []}
ENABLED = {'channel': 'dx', 'enabled': True, 'identity': {'uid': '1001'}, 'commandChat': 'dx:gid:900'}


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    state = SimpleNamespace(binding={'availability': 'available', 'conversationId': 'router-conv',
                                     'revision': 1, 'projectPath': router_home_location(7)},
                            account=None, calls=[])
    monkeypatch.setattr('lib.router.binding.global_router',
                        lambda owner, *, create, client=None: deepcopy(state.binding))
    monkeypatch.setattr('lib.channels.store.ChannelStore.account',
                        lambda store, channel: deepcopy(state.account) if channel == 'dx' else None)

    def dispatch(conversation, message, tools, **kwargs):
        state.calls.append({'conversation': conversation, 'message': deepcopy(message), **kwargs})
        return ScheduledTurnDispatch('steered')
    state.dispatch = dispatch
    return state


def _relay(router, question=QUESTION):
    return relay_project_question(7, question, dispatch=router.dispatch)


def test_notice_names_the_question_and_wakes_the_router_with_a_peer_initiator(router):
    assert _relay(router) == 'steered'
    [call] = router.calls
    assert call['conversation'] == 'router-conv' and call['user_id'] == 7
    assert call['inject_mode'] == 'steer'
    message = call['message']
    assert message['_initiator'] == 'peer'
    assert message['origin'] == {'initiator': 'peer', 'sourceConversationId': 'coordinator-project'}
    content = message['content']
    for fact in ('nonhuman notice', 'not authority', '/work/project', 'coordinator-project', 'question-turn',
                 'hg_question01', 'blocking=true', 'waiting for the answer', QUESTION['question'],
                 '- Tonight', '- Weekend: Saturday 02:00, lowest traffic',
                 'First read that conversation with get_conversation',
                 'already answered there, output exactly [SILENT] alone',
                 'route_to_project(project_path="/work/project"', 'source_turn_id', 'source_steer_id',
                 'verbatim', 'Answers question hg_question01'):
        assert fact in content, fact
    assert call['command_id'] == question_relay_command_id(7, 'coordinator-project', 'question-turn', 'call-ask')
    assert call['command_id'] != relay_command_id(7, 'coordinator-project', 'question-turn')


def test_a_nonblocking_question_states_that_the_project_router_works_on_its_default(router):
    _relay(router, {**QUESTION, 'blocking': False})
    content = router.calls[0]['message']['content']
    assert 'blocking=false' in content and 'works on the default its question states' in content
    assert 'waiting for the answer' not in content


def test_a_form_lists_every_question_with_its_options(router):
    _relay(router, {**QUESTION, 'question': 'Multiple questions (batch form):\n1. Scope?\n2. Window?',
                    'options': [], 'questions': [
                        {'question': 'Which scope?', 'response_type': 'free_text', 'options': []},
                        {'question': 'Which window?', 'response_type': 'choice',
                         'options': [{'label': 'Tonight'}, {'label': 'Weekend'}]}]})
    content = router.calls[0]['message']['content']
    assert 'Questions (one form):\n1. Which scope?\n2. Which window?\n   - Tonight\n   - Weekend' in content
    assert 'batch form' not in content


@pytest.mark.parametrize('account', [ENABLED, {**ENABLED, 'enabled': False}, {**ENABLED, 'commandChat': None},
                                     None])
def test_the_notice_names_the_owners_enabled_daxiang_command_chat(router, account):
    router.account = account
    _relay(router)
    content = router.calls[0]['message']['content']
    if account is ENABLED:
        assert ('channel_send(thread="dx:gid:900", idempotency_key="hg_question01")' in content
                and 'sends at once without approval' in content
                and 'a turn this notice starts sends nothing to 大象 by itself' in content)
    else:
        assert 'channel_send' not in content and '大象' not in content
        assert 'ask the owner in your reply here' in content and 'blocking=false' in content


def test_relay_is_idempotent_per_question_tool_call(router):
    _relay(router)
    # A re-executed tool call mints a new question id; the call identity stays.
    _relay(router, {**QUESTION, 'guidanceId': 'hg_reexecuted'})
    _relay(router, {**QUESTION, 'toolCallId': 'call-later'})
    _relay(router, {**QUESTION, 'turnId': 'later-turn'})
    first, replay, other_call, other_turn = (call['command_id'] for call in router.calls)
    assert first == replay
    assert len({first, other_call, other_turn}) == 3


@pytest.mark.parametrize('case', ['router-home', 'watchdog', 'no-router', 'router-own-question'])
def test_nothing_is_relayed_without_a_distinct_available_router(router, case):
    question = dict(QUESTION)
    if case == 'router-home':
        question['projectPath'] = router_home_location(7)
    elif case == 'watchdog':
        question['projectPath'] = watchdog_location(7)
    elif case == 'no-router':
        router.binding = {'availability': 'unbound', 'conversationId': '', 'revision': 0}
    else:
        question['coordinatorId'] = 'router-conv'
    assert _relay(router, question) is None
    assert not router.calls
