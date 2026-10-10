/**
 * Responsibility: own My Day TODO and stream mutation policy.
 * Entry point: createMyDayTaskActions. Dependencies: injected daily API,
 * selected-report presentation, cache, render, input, and diagnostic ports.
 */
import type { MyDayReport } from './model';

export interface MyDayMutationResponse {
  readonly ok: boolean;
  readonly status?: number;
  json(): Promise<unknown>;
}

export interface MyDayDailyMutationApi {
  inheritedTodoToggle(payload: {
    origin_date: string;
    todo_id: string;
    done: boolean;
  }): Promise<MyDayMutationResponse | null>;
  inheritedTodoDelete(payload: {
    origin_date: string;
    todo_id: string;
  }): Promise<MyDayMutationResponse | null>;
  todoToggle(payload: {
    date: string;
    todo_id: string;
    done: boolean;
  }): Promise<MyDayMutationResponse | null>;
  taskDelete(payload: {
    date: string;
    task_id: string;
  }): Promise<MyDayMutationResponse | null>;
  taskStatus(payload: {
    date: string;
    stream_id: string;
    action: 'cycle';
  }): Promise<MyDayMutationResponse | null>;
  taskCreate(payload: {
    date: string;
    task: string;
  }): Promise<MyDayMutationResponse | null>;
}

export interface MyDayTaskInput {
  value: string;
}

export interface MyDayTaskActionPorts {
  readonly api: MyDayDailyMutationApi;
  selectedReport(): { date: string; report: MyDayReport | null };
  acceptAuthoritativeReport(date: string, report: MyDayReport): void;
  persistReport(date: string, report: MyDayReport): void;
  renderReport(report: MyDayReport): void;
  renderCalendar(): void;
  taskInput(): MyDayTaskInput | null;
  warn(message: string, detail?: unknown): void;
}

export interface MyDayTaskActions {
  toggleInheritedTodo(todoId: string, originDate: string): Promise<void>;
  toggleStreamStatus(streamId: string): Promise<void>;
  toggleTodo(todoId: string): Promise<void>;
  deleteTodo(todoId: string): Promise<void>;
  deleteInheritedTodo(todoId: string, originDate: string): Promise<void>;
  addTodo(): Promise<void>;
}

function responseRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object'
    ? value as Record<string, unknown> : null;
}

function persistBestEffort(
  ports: MyDayTaskActionPorts,
  date: string,
  report: MyDayReport,
): void {
  try { ports.persistReport(date, report); } catch { /* cache is optional */ }
}

export function createMyDayTaskActions(
  ports: MyDayTaskActionPorts,
): Readonly<MyDayTaskActions> {
  const toggleInheritedTodo = async (
    todoId: string,
    originDate: string,
  ): Promise<void> => {
    if (!todoId || !originDate) return;
    const { report } = ports.selectedReport();
    const item = report?.today_todos?.find((row) => row.id === todoId);
    if (!report || !item) return;
    const newDone = !item.done;
    item.done = newDone;
    ports.renderReport(report);

    try {
      const response = await ports.api.inheritedTodoToggle({
        origin_date: originDate,
        todo_id: todoId,
        done: newDone,
      });
      if (!response?.ok) {
        ports.warn('[MyDay] Inherited todo toggle failed:', response?.status);
        item.done = !newDone;
        ports.renderReport(report);
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Inherited todo toggle error:', error);
      item.done = !newDone;
      ports.renderReport(report);
    }
  };

  const toggleStreamStatus = async (streamId: string): Promise<void> => {
    if (!streamId) return;
    const { date, report } = ports.selectedReport();
    const stream = report?.streams?.find((row) => row.id === streamId);
    if (!report || !stream) return;
    const oldStatus = stream.status;
    const oldRemaining = stream.remaining;
    stream._manual = true;

    try {
      const response = await ports.api.taskStatus({
        date,
        stream_id: streamId,
        action: 'cycle',
      });
      const body = response?.ok
        ? responseRecord(await response.json().catch(() => null)) : null;
      const nextStatus = body?.status;
      if (body?.ok && typeof nextStatus === 'string' && nextStatus) {
        stream.status = nextStatus;
        if (nextStatus === 'done') stream.remaining = null;
        persistBestEffort(ports, date, report);
      } else {
        ports.warn('[MyDay] Stream status toggle failed:', response?.status);
        stream.status = oldStatus;
        stream.remaining = oldRemaining;
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Stream status toggle error:', error);
      stream.status = oldStatus;
      stream.remaining = oldRemaining;
    }
    ports.renderReport(report);
    ports.renderCalendar();
  };

  const toggleTodo = async (todoId: string): Promise<void> => {
    if (!todoId) return;
    const { date, report } = ports.selectedReport();
    const item = report?.tomorrow?.find((row) => row.id === todoId);
    if (!report || !item) return;
    const newDone = !item.done;
    item.done = newDone;
    ports.renderReport(report);

    try {
      const response = await ports.api.todoToggle({
        date,
        todo_id: todoId,
        done: newDone,
      });
      if (!response?.ok) {
        ports.warn('[MyDay] Todo toggle failed:', response?.status);
        item.done = !newDone;
        ports.renderReport(report);
      } else {
        persistBestEffort(ports, date, report);
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Todo toggle error:', error);
      item.done = !newDone;
      ports.renderReport(report);
    }
  };

  const deleteTodo = async (todoId: string): Promise<void> => {
    if (!todoId) return;
    const { date, report } = ports.selectedReport();
    if (!date || !report?.tomorrow) return;
    const index = report.tomorrow.findIndex((row) => row.id === todoId);
    if (index < 0) return;
    const [removed] = report.tomorrow.splice(index, 1);
    ports.renderReport(report);

    try {
      const response = await ports.api.taskDelete({
        date,
        task_id: todoId,
      });
      if (!response?.ok) {
        ports.warn('[MyDay] Delete todo failed:', response?.status);
        report.tomorrow.splice(index, 0, removed);
        ports.renderReport(report);
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Delete todo error:', error);
      report.tomorrow.splice(index, 0, removed);
      ports.renderReport(report);
    }
    ports.renderCalendar();
  };

  const deleteInheritedTodo = async (
    todoId: string,
    originDate: string,
  ): Promise<void> => {
    if (!todoId || !originDate) return;
    const { report } = ports.selectedReport();
    if (!report?.today_todos) return;
    const index = report.today_todos.findIndex((row) => row.id === todoId);
    if (index < 0) return;
    const [removed] = report.today_todos.splice(index, 1);
    ports.renderReport(report);

    try {
      const response = await ports.api.inheritedTodoDelete({
        origin_date: originDate,
        todo_id: todoId,
      });
      if (!response?.ok) {
        ports.warn('[MyDay] Delete inherited todo failed:', response?.status);
        report.today_todos.splice(index, 0, removed);
        ports.renderReport(report);
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Delete inherited todo error:', error);
      report.today_todos.splice(index, 0, removed);
      ports.renderReport(report);
    }
    ports.renderCalendar();
  };

  const addTodo = async (): Promise<void> => {
    const input = ports.taskInput();
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    const { date } = ports.selectedReport();
    if (!date) return;

    try {
      const response = await ports.api.taskCreate({ date, task: text });
      if (!response?.ok) {
        throw new Error(`HTTP ${response?.status ?? 'no response'}`);
      }
      const body = responseRecord(await response.json());
      const report = responseRecord(body?.report) as MyDayReport | null;
      if (report) {
        ports.acceptAuthoritativeReport(date, report);
        ports.renderReport(report);
      }
    } catch (error: unknown) {
      ports.warn('[MyDay] Add task failed:', error);
    }
    ports.renderCalendar();
  };

  return Object.freeze({
    toggleInheritedTodo,
    toggleStreamStatus,
    toggleTodo,
    deleteTodo,
    deleteInheritedTodo,
    addTodo,
  });
}
