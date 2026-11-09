import { describe, expect, it } from 'vitest';
import { calendarMarks, dayActivity, dayKey, groupGoalItems, todaySummary, type MyDayGoal } from './goal-model';

const day = (offset: number) => new Date(2026, 10, 8 + offset, 10, 0, 0).getTime();

const goal = (items: MyDayGoal['items'], state: MyDayGoal['state'] = 'active'): MyDayGoal => ({
  id: 'g-0000000000000001', text: 'Goal', origin: 'owner', state, createdAt: day(-2), updatedAt: day(0),
  archivedAt: null, decomposedAt: null, decomposing: false, link: null, request: null, items,
  progress: { counted: 0, completed: 0, inProgress: 0, pending: 0, suggested: 0, percent: null },
});

const item = (id: string, overrides: Partial<MyDayGoal['items'][number]>) => ({
  id, text: id, origin: 'owner' as const, status: 'pending' as const, manual: true, counted: true,
  createdAt: day(-2), statusChangedAt: day(-2), startedAt: null, completedAt: null, ...overrides,
});

describe('My Day goal model', () => {
  it('groups adopted items by state with suggestions and Worker evidence apart', () => {
    const groups = groupGoalItems(goal([
      item('a', { status: 'completed', completedAt: day(0) }),
      item('b', { status: 'in_progress' }),
      item('c', {}),
      item('d', { origin: 'agent', proposal: 'pending', counted: false }),
      item('e', { origin: 'agent', proposal: 'accepted' }),
      item('f', { origin: 'evidence', status: 'cancelled', counted: false }),
    ]));
    expect(groups.completed.map((row) => row.id)).toEqual(['a']);
    expect(groups.inProgress.map((row) => row.id)).toEqual(['b']);
    expect(groups.pending.map((row) => row.id)).toEqual(['c', 'e']);
    expect(groups.suggested.map((row) => row.id)).toEqual(['d']);
    expect(groups.evidence.map((row) => row.id)).toEqual(['f']);
  });

  it('derives each local day from the stored timestamps, so carried items need no copy', () => {
    const goals = [goal([
      item('done-yesterday', { status: 'completed', completedAt: day(-1) }),
      item('started-yesterday', { status: 'in_progress', startedAt: day(-1) }),
      item('done-today', { status: 'completed', completedAt: day(0), startedAt: day(-1) }),
      item('still-open', {}),
    ])];
    const yesterday = dayActivity(goals, dayKey(day(-1)));
    expect(yesterday[0].completed.map((row) => row.id)).toEqual(['done-yesterday']);
    expect(yesterday[0].started.map((row) => row.id)).toEqual(['started-yesterday', 'done-today']);
    expect(dayActivity(goals, dayKey(day(0)))[0].completed.map((row) => row.id)).toEqual(['done-today']);
    expect(calendarMarks(goals, 2026, 10)).toEqual({ 7: 1, 8: 1 });
    expect(todaySummary(goals, dayKey(day(0)))).toEqual({ activeGoals: 1, completedToday: 1, inProgress: 1 });
    expect(todaySummary([goal(goals[0].items, 'archived')], dayKey(day(0))).activeGoals).toBe(0);
  });
});
