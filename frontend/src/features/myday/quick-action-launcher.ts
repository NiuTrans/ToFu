/**
 * Responsibility: launch one My Day suggestion through the composer intent
 * boundary without owning panel state or conversation policy.
 * Entry point: createMyDayQuickActionLauncher. Dependencies: injected panel,
 * composer, conversation, tool-presentation, and send-button ports.
 */
import type { MyDayQuickAction, MyDayReport, MyDayTaskItem } from './model';

export interface MyDayComposerInput {
  value: string;
  readonly scrollHeight: number;
  readonly style: { height: string };
  focus(): void;
}

export interface MyDayQuickActionLauncherPorts {
  selectedReport(): { date: string; report: MyDayReport | null };
  composerInput(): MyDayComposerInput | null;
  closeReport(): void;
  createConversation(): void;
  applySearchMode(mode: string): void;
  applyFetchEnabled(enabled: boolean): void;
  applyCodeExecEnabled(enabled: boolean): void;
  applyBrowserEnabled(enabled: boolean): void;
  updateSendButton(): void;
}

export interface MyDayQuickActionLauncher {
  startTodo(todoId: string): void;
  startInheritedTodo(todoId: string, originDate?: string): void;
  startUnfinishedTodo(index: number): void;
}

function quickAction(item: MyDayTaskItem): MyDayQuickAction | null {
  const value = item.quick_action;
  return value && typeof value === 'object' ? value : null;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function createMyDayQuickActionLauncher(
  ports: MyDayQuickActionLauncherPorts,
): Readonly<MyDayQuickActionLauncher> {
  const launch = (item: MyDayTaskItem | undefined): void => {
    if (!item) return;
    const action = quickAction(item);
    if (!action) return;

    ports.closeReport();
    const input = ports.composerInput();
    if (input) input.value = text(action.prefill) || text(item.text);

    // Order is load-bearing: newChat preserves project/tool state only when
    // it observes the already-prefilled composer.
    ports.createConversation();

    const searchMode = text(action.searchMode);
    ports.applySearchMode(searchMode && searchMode !== 'off'
      ? searchMode : 'off');
    ports.applyFetchEnabled(Boolean(action.fetchEnabled));
    ports.applyCodeExecEnabled(Boolean(action.codeExecEnabled));
    ports.applyBrowserEnabled(Boolean(action.browserEnabled));

    if (input) {
      input.style.height = 'auto';
      input.style.height = `${input.scrollHeight}px`;
      input.focus();
    }
    ports.updateSendButton();
  };

  const startTodo = (todoId: string): void => {
    if (!todoId) return;
    const item = ports.selectedReport().report?.tomorrow
      ?.find((row) => row.id === todoId);
    launch(item);
  };

  const startInheritedTodo = (
    todoId: string,
    _originDate?: string,
  ): void => {
    if (!todoId) return;
    const item = ports.selectedReport().report?.today_todos
      ?.find((row) => row.id === todoId);
    launch(item);
  };

  const startUnfinishedTodo = (index: number): void => {
    if (!Number.isInteger(index) || index < 0) return;
    launch(ports.selectedReport().report?.unfinished?.[index]);
  };

  return Object.freeze({ startTodo, startInheritedTodo, startUnfinishedTodo });
}
