"""My Day goals: owner goals, decomposed items and an explainable estimate.

Specifications for docs/modules/scheduling_ops.md (My Day goals): items keep
stable IDs across days and regenerations, the owner's manual state is
authoritative, agent suggestions are proposals, and progress is recomputed
from item states (floor(100 * completed / counted)).
"""
from __future__ import annotations

import pytest

from tests.support.myday_records import DAY, OWNER

pytestmark = pytest.mark.unit


def store(records, clock):
    from lib.myday.goals import GoalStore
    return GoalStore(OWNER, client=records, clock=clock)


def view(records, clock, goal_id, **kwargs):
    from lib.myday.goals import goal_view
    return goal_view(store(records, clock).read(goal_id), **kwargs)


def by_text(goal_view_value):
    return {item['text']: item for item in goal_view_value['items']}


def test_goal_is_owner_authored_and_validated_against_the_contract(records, clock):
    from lib.myday.contract import validate
    goal = store(records, clock).create('  把 My Day 改成目标追踪  ')
    assert goal['text'] == '把 My Day 改成目标追踪' and goal['origin'] == 'owner'
    assert goal['state'] == 'active' and goal['items'] == [] and goal['link'] is None
    validate(goal, 'goal')
    with pytest.raises(ValueError):
        store(records, clock).create('   ')
    with pytest.raises(ValueError):
        store(records, clock).create('x' * 1001)
    assert [row['id'] for row in store(records, clock).list()] == [goal['id']]


def test_unfinished_items_carry_over_across_days_without_reset_or_duplication(records, clock):
    goals = store(records, clock)
    goal = goals.create('Ship the release')
    first = goals.add_item(goal['id'], 'Write the changelog')['items'][-1]
    second = goals.add_item(goal['id'], 'Tag the build')['items'][-1]
    third = goals.add_item(goal['id'], 'Announce it')['items'][-1]
    goals.update_item(goal['id'], first['id'], status='completed')
    goals.update_item(goal['id'], second['id'], status='in_progress')
    day_one = view(records, clock, goal['id'])

    clock.now += DAY  # the owner comes back the next day; nothing is regenerated per day
    day_two = view(records, clock, goal['id'])
    assert [item['id'] for item in day_two['items']] == [first['id'], second['id'], third['id']]
    assert [item['status'] for item in day_two['items']] == ['completed', 'in_progress', 'pending']
    assert day_two['items'][0]['completedAt'] == day_one['items'][0]['completedAt']
    assert day_two['progress'] == day_one['progress']

    clock.now += DAY
    goals.update_item(goal['id'], third['id'], status='completed')
    day_three = view(records, clock, goal['id'])
    assert len(day_three['items']) == 3
    assert day_three['items'][0]['completedAt'] < day_three['items'][2]['completedAt']


def test_manual_check_off_persists_across_store_instances_and_is_stamped(records, clock):
    goals = store(records, clock)
    goal = goals.create('Run three times this week')
    item = goals.add_item(goal['id'], 'Run on Monday')['items'][-1]
    assert item['status'] == 'pending' and item['manual'] is False and item['id'].startswith('i-')
    clock.now += 60
    checked = goals.update_item(goal['id'], item['id'], status='completed')['items'][-1]
    assert checked['status'] == 'completed' and checked['manual'] is True
    assert checked['completedAt'] == checked['statusChangedAt'] == int(clock.now * 1000)

    reopened = store(records, clock).read(goal['id'])['items'][-1]  # a restarted process
    assert reopened == checked
    unchecked = goals.update_item(goal['id'], item['id'], status='pending')['items'][-1]
    assert unchecked['status'] == 'pending' and unchecked['completedAt'] is None and unchecked['manual']
    replay = records.puts
    goals.update_item(goal['id'], item['id'], status='pending')
    assert records.puts == replay, 'an unchanged status writes nothing'


def test_progress_is_recomputed_from_item_states_and_never_fabricated(records, clock):
    from lib.myday.goals import merge_suggestions
    goals = store(records, clock)
    goal = goals.create('Learn the piece')
    assert view(records, clock, goal['id'])['progress'] == {
        'counted': 0, 'completed': 0, 'inProgress': 0, 'pending': 0, 'suggested': 0, 'percent': None}
    ids = [goals.add_item(goal['id'], text)['items'][-1]['id'] for text in ('Bars 1-8', 'Bars 9-16', 'Bars 17-24')]
    goals.update(goal['id'], lambda value: merge_suggestions(value, ['Play it slowly', 'Record it'],
                                                                now_ms=int(clock.now * 1000)))
    progress = view(records, clock, goal['id'])['progress']
    assert progress == {'counted': 3, 'completed': 0, 'inProgress': 0, 'pending': 3, 'suggested': 2,
                        'percent': 0}
    goals.update_item(goal['id'], ids[0], status='completed')
    goals.update_item(goal['id'], ids[1], status='in_progress')
    assert view(records, clock, goal['id'])['progress']['percent'] == 33  # floor(100 * 1 / 3)
    goals.decide_suggestions(goal['id'], 'accept')
    progress = view(records, clock, goal['id'])['progress']
    assert (progress['counted'], progress['suggested'], progress['percent']) == (5, 0, 20)
    for item_id in ids:
        goals.update_item(goal['id'], item_id, status='completed')
    assert view(records, clock, goal['id'])['progress']['percent'] == 60
    remaining = [item['id'] for item in view(records, clock, goal['id'])['items']
                 if item['status'] != 'completed']
    goals.update_item(goal['id'], remaining[0], status='completed')
    assert view(records, clock, goal['id'])['progress']['percent'] == 80
    goals.update_item(goal['id'], remaining[1], status='completed')
    assert view(records, clock, goal['id'])['progress']['percent'] == 100


def test_regeneration_is_idempotent_and_never_duplicates_or_touches_owner_state(records, clock):
    from lib.myday.goals import merge_suggestions
    goals = store(records, clock)
    goal = goals.create('Move to the new flat')
    owner_item = goals.add_item(goal['id'], 'Book the movers')['items'][-1]

    def regenerate(texts):
        return goals.update(goal['id'], lambda value: merge_suggestions(
            value, texts, now_ms=int(clock.now * 1000)))

    first = regenerate(['Pack the books', 'Book the movers!', 'Change the address', 'Pack the books'])
    texts = [item['text'] for item in first['items']]
    assert texts == ['Book the movers', 'Pack the books', 'Change the address']
    agent = [item for item in first['items'] if item['origin'] == 'agent']
    assert all(item['proposal'] == 'pending' and item['id'].startswith('a-') for item in agent)

    again = regenerate(['Pack the books', 'Change the address'])
    assert again['items'] == first['items'], 'the same proposals regenerate the same items'
    puts = records.puts
    regenerate(['Pack the books', 'Change the address'])
    assert records.puts == puts, 'an unchanged regeneration writes nothing'

    pack, address = agent
    goals.update_item(goal['id'], pack['id'], status='completed')       # adopts and checks it
    goals.update_item(goal['id'], owner_item['id'], status='in_progress')
    goals.remove_item(goal['id'], address['id'])                         # dismisses the proposal
    after = regenerate(['Label the boxes', 'Change the address', 'Clean the old flat', 'Pack the books'])
    rows = {item['text']: item for item in after['items']}
    assert rows['Pack the books']['status'] == 'completed' and rows['Pack the books']['proposal'] == 'accepted'
    assert rows['Pack the books']['id'] == pack['id']
    assert rows['Book the movers']['status'] == 'in_progress' and rows['Book the movers']['manual']
    assert rows['Change the address']['proposal'] == 'dismissed', 'a dismissed proposal is not re-proposed'
    assert rows['Label the boxes']['proposal'] == 'pending' and rows['Clean the old flat']['proposal'] == 'pending'
    assert len(after['items']) == 5, 'a re-proposed adopted or dismissed item is never added again'
    replaced = regenerate(['Return the keys'])
    texts = {item['text']: item['proposal'] if item['origin'] == 'agent' else 'owner'
             for item in replaced['items']}
    assert texts == {'Book the movers': 'owner', 'Pack the books': 'accepted',
                     'Change the address': 'dismissed', 'Return the keys': 'pending'}, \
        'only untouched pending proposals are replaced'


def test_agent_suggestions_are_proposals_until_the_owner_acts(records, clock):
    from lib.myday.goals import merge_suggestions
    goals = store(records, clock)
    goal = goals.create('Prepare the talk')
    goals.update(goal['id'], lambda value: merge_suggestions(value, ['Outline', 'Slides', 'Rehearse'],
                                                                now_ms=int(clock.now * 1000)))
    shown = view(records, clock, goal['id'])
    assert [(item['proposal'], item['counted']) for item in shown['items']] == [('pending', False)] * 3
    outline, slides, rehearse = (item['id'] for item in shown['items'])
    goals.update_item(goal['id'], outline, proposal='accepted')
    goals.update_item(goal['id'], slides, text='Slides with demos')
    shown = by_text(view(records, clock, goal['id']))
    assert shown['Outline']['proposal'] == 'accepted' and shown['Outline']['counted']
    assert shown['Slides with demos']['proposal'] == 'accepted' and shown['Slides with demos']['origin'] == 'agent'
    assert shown['Rehearse']['proposal'] == 'pending' and not shown['Rehearse']['counted']
    goals.decide_suggestions(goal['id'], 'dismiss')
    shown = view(records, clock, goal['id'])
    assert 'Rehearse' not in by_text(shown), 'dismissed proposals are hidden from the view'
    assert shown['progress']['counted'] == 2 and shown['progress']['suggested'] == 0


def test_regeneration_merges_against_the_latest_record_not_a_stale_snapshot(records, clock):
    from lib.myday.goals import merge_suggestions
    goals = store(records, clock)
    goal = goals.create('Write the paper')
    item = goals.add_item(goal['id'], 'Draft the abstract')['items'][-1]
    # The owner checks an item off while a slow decomposition is about to commit.
    records.before_put = lambda: store(records, clock).update_item(goal['id'], item['id'], status='completed')
    merged = goals.update(goal['id'], lambda value: merge_suggestions(value, ['Run the ablation'],
                                                                         now_ms=int(clock.now * 1000)))
    rows = {row['text']: row for row in merged['items']}
    assert rows['Draft the abstract']['status'] == 'completed' and rows['Draft the abstract']['manual']
    assert rows['Run the ablation']['proposal'] == 'pending'


def test_bounds_and_invalid_commands_fail_without_writing(records, clock):
    from lib.myday.goals import GoalNotFound
    goals = store(records, clock)
    goal = goals.create('Bounded goal')
    puts = records.puts
    with pytest.raises(ValueError):
        goals.add_item(goal['id'], 'x' * 201)
    with pytest.raises(ValueError):
        goals.update_item(goal['id'], 'i-0000000000000000', status='completed')
    with pytest.raises(GoalNotFound):
        goals.add_item('g-0000000000000000', 'nothing')
    with pytest.raises(ValueError):
        goals.update_item(goal['id'], 'i-0000000000000000', status='blocked')
    assert records.puts == puts
    for number in range(128):
        goals.add_item(goal['id'], f'item {number}')
    with pytest.raises(ValueError, match='128'):
        goals.add_item(goal['id'], 'one too many')


def test_archive_keeps_items_and_restores_them(records, clock):
    goals = store(records, clock)
    goal = goals.create('Old goal')
    item = goals.add_item(goal['id'], 'Done thing')['items'][-1]
    goals.update_item(goal['id'], item['id'], status='completed')
    archived = goals.edit_goal(goal['id'], state='archived')
    assert archived['state'] == 'archived' and archived['archivedAt'] == int(clock.now * 1000)
    restored = goals.edit_goal(goal['id'], state='active')
    assert restored['state'] == 'active' and restored['archivedAt'] is None
    assert restored['items'] == archived['items']


def test_records_are_owner_scoped(records, clock):
    from lib.myday.goals import GoalNotFound, GoalStore
    mine = store(records, clock).create('Mine')
    other = GoalStore(OWNER + 1, client=records, clock=clock)
    assert other.list() == []
    with pytest.raises(GoalNotFound):
        other.read(mine['id'])
