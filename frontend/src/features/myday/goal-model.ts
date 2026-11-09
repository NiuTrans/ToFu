/**
 * My Day goal view model: wire types and pure day projections.
 *
 * Responsibility: type the server's goal views
 * (contracts/myday_goals_v1.schema.json goalView) and derive the presentation
 * the board needs from them: item grouping, local-day activity and calendar
 * marks. Entry points: groupGoalItems, dayKey, dayActivity, calendarMarks,
 * todaySummary. Dependencies: none; server data is the only authority.
 */

export type MyDayItemStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

export interface MyDayWorkerEvidence {
  readonly assignmentId: string;
  readonly conversationId: string;
  readonly review: 'accept' | 'rework' | 'reject' | null;
  readonly state: MyDayItemStatus;
  readonly reason: string;
}

export interface MyDayGoalItem {
  readonly id: string;
  readonly text: string;
  readonly origin: 'owner' | 'agent' | 'evidence';
  readonly proposal?: 'pending' | 'accepted';
  status: MyDayItemStatus;
  manual: boolean;
  readonly counted: boolean;
  readonly createdAt: number | null;
  readonly statusChangedAt: number | null;
  readonly startedAt: number | null;
  completedAt: number | null;
  readonly evidence?: MyDayWorkerEvidence;
}

export interface MyDayProgress {
  readonly counted: number;
  readonly completed: number;
  readonly inProgress: number;
  readonly pending: number;
  readonly suggested: number;
  readonly percent: number | null;
}

export interface MyDayGoalLink {
  readonly projectPath: string;
  readonly coordinatorId: string;
  readonly sourceAttemptId: string;
  readonly requestTurnId: string;
  readonly label: string;
  readonly linkedAt: number;
}

export interface MyDayRequestEvidence {
  readonly available: boolean;
  readonly settled: boolean;
  readonly workers: number;
  readonly reason: string | null;
}

export interface MyDayGoal {
  readonly id: string;
  readonly text: string;
  readonly origin: 'owner';
  readonly state: 'active' | 'archived';
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly archivedAt: number | null;
  readonly decomposedAt: number | null;
  readonly decomposing: boolean;
  readonly items: MyDayGoalItem[];
  readonly progress: MyDayProgress;
  readonly link: MyDayGoalLink | null;
  readonly request: MyDayRequestEvidence | null;
}

export interface MyDayLinkableRequest {
  readonly projectPath: string;
  readonly coordinatorId: string;
  readonly sourceAttemptId: string;
  readonly requestTurnId: string;
  readonly label: string;
  readonly createdAt: number;
  readonly workers: number;
}

export interface MyDayItemGroups {
  readonly inProgress: MyDayGoalItem[];
  readonly pending: MyDayGoalItem[];
  readonly completed: MyDayGoalItem[];
  readonly suggested: MyDayGoalItem[];
  readonly evidence: MyDayGoalItem[];
}

/** Local calendar day of a timestamp, as YYYY-MM-DD. */
export function dayKey(ms: number): string {
  const date = new Date(ms);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

/** Owner and adopted items by state; open suggestions and Worker evidence apart. */
export function groupGoalItems(goal: MyDayGoal): MyDayItemGroups {
  const groups: MyDayItemGroups = { inProgress: [], pending: [], completed: [], suggested: [], evidence: [] };
  for (const item of goal.items) {
    if (item.origin === 'evidence') groups.evidence.push(item);
    else if (item.origin === 'agent' && item.proposal === 'pending') groups.suggested.push(item);
    else if (item.status === 'completed') groups.completed.push(item);
    else if (item.status === 'in_progress') groups.inProgress.push(item);
    else groups.pending.push(item);
  }
  return groups;
}

export interface MyDayDayActivity {
  readonly goal: MyDayGoal;
  readonly completed: MyDayGoalItem[];
  readonly started: MyDayGoalItem[];
  readonly created: boolean;
}

/** What the owner did on one local day, from the stored item timestamps. */
export function dayActivity(goals: readonly MyDayGoal[], date: string): MyDayDayActivity[] {
  const activity: MyDayDayActivity[] = [];
  for (const goal of goals) {
    const completed = goal.items.filter((item) => item.completedAt !== null
      && item.status === 'completed' && dayKey(item.completedAt) === date);
    const started = goal.items.filter((item) => item.startedAt !== null && dayKey(item.startedAt) === date
      && !completed.includes(item));
    const created = dayKey(goal.createdAt) === date;
    if (completed.length || started.length || created) activity.push({ goal, completed, started, created });
  }
  return activity;
}

/** Day of month -> number of items completed that day, for one calendar month. */
export function calendarMarks(goals: readonly MyDayGoal[], year: number, month0: number): Record<number, number> {
  const marks: Record<number, number> = {};
  for (const goal of goals) {
    for (const item of goal.items) {
      if (item.status !== 'completed' || item.completedAt === null) continue;
      const done = new Date(item.completedAt);
      if (done.getFullYear() === year && done.getMonth() === month0) {
        marks[done.getDate()] = (marks[done.getDate()] || 0) + 1;
      }
    }
  }
  return marks;
}

export interface MyDayTodaySummary {
  readonly activeGoals: number;
  readonly completedToday: number;
  readonly inProgress: number;
}

export function todaySummary(goals: readonly MyDayGoal[], today: string): MyDayTodaySummary {
  const active = goals.filter((goal) => goal.state === 'active');
  let completedToday = 0;
  let inProgress = 0;
  for (const goal of active) {
    for (const item of goal.items) {
      if (!item.counted) continue;
      if (item.status === 'completed' && item.completedAt !== null && dayKey(item.completedAt) === today) {
        completedToday += 1;
      }
      if (item.status === 'in_progress') inProgress += 1;
    }
  }
  return { activeGoals: active.length, completedToday, inProgress };
}
