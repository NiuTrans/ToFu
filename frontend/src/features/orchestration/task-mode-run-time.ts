import { orchestrationRegistry } from './registry';
export interface TaskModeRunTimeOptions {
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  now?: () => number;
  isTerminal?: (run: unknown) => unknown;
}

type TaskModeRunTimeWindow = Window & {
  createTaskModeRunTime?: typeof createTaskModeRunTime;
};

export function createTaskModeRunTime(options: TaskModeRunTimeOptions = {}) {
  const translate = (key: string, params?: Record<string, unknown>): string =>
    String(options.translate ? options.translate(key, params) : key);
  const now = (): number => options.now ? options.now() : Date.now();
  const terminal = (run: unknown): boolean => Boolean(
    options.isTerminal ? options.isTerminal(run) : false);
  const relativeTime = (millisecondsValue: unknown): string => {
    const milliseconds = Number(millisecondsValue);
    if (!milliseconds) return '';
    const seconds = Math.floor(Math.max(0, now() - milliseconds) / 1000);
    if (seconds < 60) return translate('tm.ago.seconds', { n: seconds });
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return translate('tm.ago.minutes', { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return translate('tm.ago.hours', { n: hours });
    return translate('tm.ago.days', { n: Math.floor(hours / 24) });
  };
  const duration = (runValue: unknown): string => {
    const run = runValue && typeof runValue === 'object'
      ? runValue as Record<string, unknown> : {};
    const start = Number(run.created_at || 0);
    if (!start) return '';
    const done = terminal(run);
    const end = done ? Number(run.finished_at || run.updated_at || 0) : now();
    if (!end || end < start) return '';
    const seconds = Math.round((end - start) / 1000);
    const label = seconds < 60 ? `${seconds}s`
      : seconds < 3600
        ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
        : `${Math.floor(seconds / 3600)}h ${
          Math.floor((seconds % 3600) / 60)}m`;
    return `${done ? '' : `${translate('tm.dur.running')} · `}${label}`;
  };
  return { duration, relativeTime };
}

(orchestrationRegistry as unknown as TaskModeRunTimeWindow).createTaskModeRunTime = createTaskModeRunTime;
