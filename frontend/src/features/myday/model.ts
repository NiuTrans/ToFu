/** Shared structural model for lazy My Day commands and retained panel ports. */

export interface MyDayQuickAction {
  readonly prefill?: unknown;
  readonly searchMode?: unknown;
  readonly fetchEnabled?: unknown;
  readonly codeExecEnabled?: unknown;
  readonly browserEnabled?: unknown;
}

export interface MyDayTaskItem {
  readonly id?: unknown;
  readonly text?: unknown;
  readonly quick_action?: MyDayQuickAction | null;
  done?: boolean;
  readonly [key: string]: unknown;
}

export interface MyDayStreamItem {
  readonly id?: unknown;
  status?: unknown;
  remaining?: unknown;
  _manual?: boolean;
  readonly [key: string]: unknown;
}

export interface MyDayReport {
  today_todos?: MyDayTaskItem[];
  tomorrow?: MyDayTaskItem[];
  unfinished?: MyDayTaskItem[];
  streams?: MyDayStreamItem[];
  [key: string]: unknown;
}
