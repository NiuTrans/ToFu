/** Lazy My Day composition and feature entry. */
import '../runtime/myday-presenters.generated.js';
import { featureRegistry } from '../feature-registry';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import {
  createMyDayTaskActions,
  type MyDayDailyMutationApi,
  type MyDayTaskActions,
  type MyDayTaskInput,
} from './myday/task-actions';
import type { MyDayReport } from './myday/model';
import {
  createMyDayQuickActionLauncher,
  type MyDayComposerInput,
  type MyDayQuickActionLauncher,
} from './myday/quick-action-launcher';

interface MyDayTaskPresentation {
  selectedReport(): { date: string; report: MyDayReport | null };
  acceptAuthoritativeReport(date: string, report: MyDayReport): void;
  persistReport(date: string, report: MyDayReport): void;
  renderReport(report: MyDayReport): void;
  renderCalendar(): void;
  taskInput(): MyDayTaskInput | null;
  composerInput(): MyDayComposerInput | null;
  closeReport(): void;
  createConversation(): void;
  applySearchMode(mode: string): void;
  applyFetchEnabled(enabled: boolean): void;
  applyCodeExecEnabled(enabled: boolean): void;
  applyBrowserEnabled(enabled: boolean): void;
  updateSendButton(): void;
}

type MyDayBindings = Window & {
  Api?: { daily?: MyDayDailyMutationApi };
  MyDayTaskPresentation?: MyDayTaskPresentation;
  _mydayToggleInheritedTodo?: MyDayTaskActions['toggleInheritedTodo'];
  _mydayToggleStreamStatus?: MyDayTaskActions['toggleStreamStatus'];
  _mydayToggleTodo?: MyDayTaskActions['toggleTodo'];
  _mydayDeleteTodo?: MyDayTaskActions['deleteTodo'];
  _mydayDeleteInheritedTodo?: MyDayTaskActions['deleteInheritedTodo'];
  _mydayAddTodo?: MyDayTaskActions['addTodo'];
  _mydayStartTodoConv?: MyDayQuickActionLauncher['startTodo'];
  _mydayStartTodoConvInherited?: MyDayQuickActionLauncher['startInheritedTodo'];
  _mydayStartTodoConvUnfinished?: MyDayQuickActionLauncher['startUnfinishedTodo'];
};

function bindings(): MyDayBindings {
  return featureRegistry as unknown as MyDayBindings;
}

function dailyApi(): MyDayDailyMutationApi {
  const api = bindings().Api?.daily;
  if (!api) throw new Error('My Day daily API is not ready');
  return api;
}

function presentation(): MyDayTaskPresentation {
  const owner = bindings().MyDayTaskPresentation;
  if (!owner) throw new Error('My Day task presentation is not ready');
  return owner;
}

async function prepareMyDayRuntime(): Promise<void> {
  const background = await import('./background');
  await background.prepareMyDayBackground();
}

const taskActions = createMyDayTaskActions({
  api: {
    inheritedTodoToggle: (payload) => dailyApi().inheritedTodoToggle(payload),
    inheritedTodoDelete: (payload) => dailyApi().inheritedTodoDelete(payload),
    todoToggle: (payload) => dailyApi().todoToggle(payload),
    taskDelete: (payload) => dailyApi().taskDelete(payload),
    taskStatus: (payload) => dailyApi().taskStatus(payload),
    taskCreate: (payload) => dailyApi().taskCreate(payload),
  },
  selectedReport: () => presentation().selectedReport(),
  acceptAuthoritativeReport: (date, report) => {
    presentation().acceptAuthoritativeReport(date, report);
  },
  persistReport: (date, report) => presentation().persistReport(date, report),
  renderReport: (report) => presentation().renderReport(report),
  renderCalendar: () => presentation().renderCalendar(),
  taskInput: () => presentation().taskInput(),
  warn: (message, detail) => console.warn(message, detail),
});

bindings()._mydayToggleInheritedTodo = taskActions.toggleInheritedTodo;
bindings()._mydayToggleStreamStatus = taskActions.toggleStreamStatus;
bindings()._mydayToggleTodo = taskActions.toggleTodo;
bindings()._mydayDeleteTodo = taskActions.deleteTodo;
bindings()._mydayDeleteInheritedTodo = taskActions.deleteInheritedTodo;
bindings()._mydayAddTodo = taskActions.addTodo;

const quickActionLauncher = createMyDayQuickActionLauncher({
  selectedReport: () => presentation().selectedReport(),
  composerInput: () => presentation().composerInput(),
  closeReport: () => presentation().closeReport(),
  createConversation: () => presentation().createConversation(),
  applySearchMode: (mode) => presentation().applySearchMode(mode),
  applyFetchEnabled: (enabled) => presentation().applyFetchEnabled(enabled),
  applyCodeExecEnabled: (enabled) => {
    presentation().applyCodeExecEnabled(enabled);
  },
  applyBrowserEnabled: (enabled) => presentation().applyBrowserEnabled(enabled),
  updateSendButton: () => presentation().updateSendButton(),
});

bindings()._mydayStartTodoConv = quickActionLauncher.startTodo;
bindings()._mydayStartTodoConvInherited = quickActionLauncher.startInheritedTodo;
bindings()._mydayStartTodoConvUnfinished = quickActionLauncher.startUnfinishedTodo;

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  await prepareMyDayRuntime();
  return invokeFeatureEntry('myday', name, args, stub);
}

export async function prepare(): Promise<void> {
  await prepareMyDayRuntime();
}
