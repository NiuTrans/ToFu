import { orchestrationRegistry } from './registry';
import type { OrchestrationEventLine } from './event-format';

export interface TaskModeTimelineOptions {
  document?: Document;
  hostId?: string;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  formatEvent?: (
    event: unknown,
    options: Record<string, unknown>,
  ) => OrchestrationEventLine[] | null | undefined;
  eventContract?: unknown;
  outcomeContract?: unknown;
}

type TaskModeTimelineWindow = Window & {
  createTaskModeTimelineView?: typeof createTaskModeTimelineView;
};

export function createTaskModeTimelineView(
  options: TaskModeTimelineOptions = {},
) {
  const doc = (): Document => options.document ?? document;
  const host = (): HTMLElement | null => doc().getElementById(options.hostId
    || 'tmTimeline');
  const setBusy = (on: unknown): void => {
    host()?.setAttribute('aria-busy', on ? 'true' : 'false');
  };
  const setLive = (on: unknown): void => {
    host()?.setAttribute('aria-live', on ? 'polite' : 'off');
  };
  const clear = (): void => {
    const timeline = host();
    if (!timeline) return;
    timeline.setAttribute('aria-live', 'off');
    timeline.innerHTML = '';
  };
  const append = (html: unknown, className?: unknown): HTMLElement | null => {
    const timeline = host();
    if (!timeline) return null;
    const atBottom = timeline.scrollHeight - timeline.scrollTop
      - timeline.clientHeight < 40;
    const row = doc().createElement('div');
    row.className = `tm-line${className ? ` ${String(className)}` : ''}`;
    row.innerHTML = String(html || '');
    timeline.appendChild(row);
    if (atBottom) timeline.scrollTop = timeline.scrollHeight;
    return row;
  };
  const appendEvent = (event: unknown): OrchestrationEventLine[] => {
    if (typeof options.formatEvent !== 'function') return [];
    const lines = options.formatEvent(event, {
      escape: options.escape,
      translate: options.translate,
      icon: options.icon,
      dimClass: 'tm-dim',
      eventContract: options.eventContract,
      outcomeContract: options.outcomeContract,
    }) || [];
    lines.forEach((line) => append(line.html, line.className));
    return lines;
  };
  return { setBusy, setLive, clear, append, appendEvent };
}

(orchestrationRegistry as unknown as TaskModeTimelineWindow).createTaskModeTimelineView =
  createTaskModeTimelineView;
