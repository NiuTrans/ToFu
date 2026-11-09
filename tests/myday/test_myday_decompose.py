"""Decomposition proposes items; the merge keeps the owner's decisions; one run per goal."""
from __future__ import annotations

import asyncio
import threading

import pytest

from tests.support.myday_records import Clock, OWNER, Records

pytestmark = pytest.mark.unit


def test_prompt_carries_decided_items_declined_suggestions_and_workers():
    from lib.myday.decompose import build_messages
    goal = {'text': '把 My Day 改成目标追踪', 'items': [
        {'origin': 'owner', 'text': '写设计文档', 'status': 'completed'},
        {'origin': 'agent', 'proposal': 'accepted', 'text': '补测试', 'status': 'in_progress'},
        {'origin': 'agent', 'proposal': 'pending', 'text': '未采纳的建议', 'status': 'pending'},
        {'origin': 'agent', 'proposal': 'dismissed', 'text': '被拒绝的建议', 'status': 'pending'},
        {'origin': 'evidence', 'text': 'override', 'status': 'completed'},
    ]}
    system, user = build_messages(goal, [{'title': '[DB] measure load', 'state': 'in_progress'}])
    assert system['role'] == 'system' and '{"items": [' in system['content']
    content = user['content']
    assert '把 My Day 改成目标追踪' in content
    assert '- 写设计文档 (done)' in content and '- 补测试 (in progress)' in content
    assert '未采纳的建议' not in content, 'a regeneration proposes the current set afresh'
    assert 'declined' in content and '被拒绝的建议' in content
    assert '- [DB] measure load (in progress)' in content


@pytest.mark.parametrize('answer, expected', [
    ('```json\n{"items": ["Draft the outline", "  ", "Book a room"]}\n```', ['Draft the outline', 'Book a room']),
    ('{"items": [{"text": "Call Ann"}, 3]}', ['Call Ann']),
    ('["Just a list"]', ['Just a list']),
    ('{"items": []}', []),
    ('I cannot help with that.', None),
    ('{"answer": "no items key"}', None),
])
def test_answers_parse_into_texts_or_are_rejected(answer, expected):
    from lib.myday.decompose import parse_items
    assert parse_items(answer) == expected


def test_failed_or_unusable_answers_leave_the_goal_unchanged():
    from lib.myday.decompose import DecompositionError, propose_items
    from lib.myday.goals import GoalStore
    records = Records()
    goals = GoalStore(OWNER, client=records, clock=Clock())
    goal = goals.create('Plan the trip')

    def failing(messages, owner):
        raise RuntimeError('provider down')
    with pytest.raises(DecompositionError, match='provider down'):
        propose_items(OWNER, goal, llm=failing)
    with pytest.raises(DecompositionError):
        propose_items(OWNER, goal, llm=lambda messages, owner: 'sorry')
    assert goals.read(goal['id']) == goal


def test_concurrent_decompositions_of_one_goal_share_one_model_call():
    from lib.myday.goals import GoalStore
    from lib.myday.service import Flights, MyDayGoals
    records = Records()
    store = GoalStore(OWNER, client=records, clock=Clock())
    goal = store.create('Write the thesis')
    calls, gate = [], threading.Event()

    def propose(owner, value, *, workers):
        calls.append(value['id'])
        gate.wait(5)
        return ['Write chapter 1', 'Write chapter 2']

    service = MyDayGoals(OWNER, store=store, propose=propose, flights=Flights())

    async def scenario():
        first = asyncio.ensure_future(service.decompose(goal['id']))
        await asyncio.sleep(0.05)
        view = await service.view()
        assert view['goals'][0]['decomposing'] is True
        second = asyncio.ensure_future(service.decompose(goal['id']))
        await asyncio.sleep(0.05)
        first.cancel()  # a disconnected client never cancels the shared run
        gate.set()
        result = await second
        with pytest.raises(asyncio.CancelledError):
            await first
        return result, await service.view()

    result, after = asyncio.run(scenario())
    assert calls == [goal['id']]
    assert [item['text'] for item in result['items']] == ['Write chapter 1', 'Write chapter 2']
    assert result['decomposedAt'] is not None and after['goals'][0]['decomposing'] is False
    assert all(item['proposal'] == 'pending' and not item['counted'] for item in result['items'])


def test_concurrent_view_loads_share_one_read_and_a_write_starts_a_fresh_one():
    from lib.myday.goals import GoalStore
    from lib.myday.service import Flights, MyDayGoals
    records = Records()
    clock = Clock()
    store = GoalStore(OWNER, client=records, clock=clock)
    store.create('Read more')
    clock.now += 60  # the next goal is newer: views list goals oldest first
    lists, gate = [], threading.Event()
    real_list = store.list

    def slow_list():
        lists.append(1)
        gate.wait(5)
        return real_list()
    store.list = slow_list
    service = MyDayGoals(OWNER, store=store, flights=Flights())

    async def scenario():
        loads = [asyncio.ensure_future(service.view()) for _ in range(8)]
        await asyncio.sleep(0.05)
        created = await service.write(lambda goals: goals.create('Sleep earlier'))
        fresh = asyncio.ensure_future(service.view())
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.gather(*loads), await fresh, created

    shared, fresh, created = asyncio.run(scenario())
    assert len(lists) == 2, 'eight concurrent opens share one read; the read after a write is new'
    assert all(view == shared[0] for view in shared), 'every joined open gets the one load\'s result'
    assert [goal['text'] for goal in fresh['goals']] == ['Read more', 'Sleep earlier']
    assert created['text'] == 'Sleep earlier'
