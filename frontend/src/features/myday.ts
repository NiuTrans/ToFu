/** Lazy My Day composition and feature entry. */
import '../runtime/myday-presenters.generated.js';
import '../styles/application/16-my-day-goals.css';
import { featureRegistry } from '../feature-registry';
import { t } from '../i18n';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import {
  createMyDayGoalBoard,
  type MyDayBoardElement,
  type MyDayGoalApi,
  type MyDayGoalBoard,
} from './myday/goal-board';

interface MyDayPanelPresentation {
  renderCalendar(): void;
  showToday(): void;
}

type MyDayBindings = Window & {
  Api?: { daily?: MyDayGoalApi };
  MyDayPanelPresentation?: MyDayPanelPresentation;
  MyDayGoalBoard?: MyDayGoalBoard;
};

function bindings(): MyDayBindings {
  return featureRegistry as unknown as MyDayBindings;
}

function dailyApi(): MyDayGoalApi {
  const api = bindings().Api?.daily;
  if (!api) throw new Error('My Day daily API is not ready');
  return api;
}

function presentation(): MyDayPanelPresentation {
  const owner = bindings().MyDayPanelPresentation;
  if (!owner) throw new Error('My Day panel presentation is not ready');
  return owner;
}

async function prepareMyDayRuntime(): Promise<void> {
  const background = await import('./background');
  await background.prepareMyDayBackground();
}

const api: MyDayGoalApi = {
  goals: () => dailyApi().goals(),
  goalCreate: (text) => dailyApi().goalCreate(text),
  goalUpdate: (goalId, body) => dailyApi().goalUpdate(goalId, body),
  goalItemAdd: (goalId, text) => dailyApi().goalItemAdd(goalId, text),
  goalItemUpdate: (goalId, itemId, body) => dailyApi().goalItemUpdate(goalId, itemId, body),
  goalItemRemove: (goalId, itemId) => dailyApi().goalItemRemove(goalId, itemId),
  goalSuggestions: (goalId, decision) => dailyApi().goalSuggestions(goalId, decision),
  goalDecompose: (goalId) => dailyApi().goalDecompose(goalId),
  goalLink: (goalId, body) => dailyApi().goalLink(goalId, body),
  goalUnlink: (goalId) => dailyApi().goalUnlink(goalId),
  goalRequests: () => dailyApi().goalRequests(),
  report: (date) => dailyApi().report(date),
};

bindings().MyDayGoalBoard = createMyDayGoalBoard({
  api,
  translate: t as unknown as (key: string, values?: Record<string, unknown>) => string,
  element: (id: MyDayBoardElement) => document.getElementById(id),
  renderCalendar: () => presentation().renderCalendar(),
  showToday: () => presentation().showToday(),
  warn: (message, detail) => console.warn(message, detail),
});

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
