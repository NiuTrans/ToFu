import { orchestrationRegistry } from './registry';
import { createTaskModeRunTitleView, type TaskModeRunTitleOptions } from './task-mode-run-title-view';
import { createTaskModeRunFinalView, type TaskModeRunFinalOptions } from './task-mode-run-final-view';
export type TaskModeRunViewOptions = TaskModeRunTitleOptions & TaskModeRunFinalOptions;
type TaskModeRunViewWindow = Window & {
  createTaskModeRunView?: typeof createTaskModeRunView;
};
export function createTaskModeRunView(options: TaskModeRunViewOptions = {}) {
  const title = createTaskModeRunTitleView(options);
  const final = createTaskModeRunFinalView(options);
  return { clearFinal: final.clear, renderFinal: final.render, renderTitle: title.render };
}
(orchestrationRegistry as unknown as TaskModeRunViewWindow).createTaskModeRunView = createTaskModeRunView;
