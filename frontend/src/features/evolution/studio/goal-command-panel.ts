/** Always-visible mission command surface. Actions are explicit shared controls;
 * animation is presentation only and never serves as execution telemetry.
 */
import { createButton } from '../../../ui/index';
import { isTerminalRun, runStatusLabel, taskRecords, type GoalObservation, type StudioCopy } from '../goal-workbench';
import { mountGoalRunInspector } from './goal-run-inspector';

export function mountGoalCommandPanel(host: HTMLElement, ports: {
  copy: StudioCopy; openTask: (id: string) => void; stop: () => void;
  retry: () => void; details: () => void; conversation: () => void; create: () => void;
}) {
  let current: GoalObservation = {};
  host.className = 'evo-mission-panel';
  host.setAttribute('aria-labelledby', 'evo-mission-title');
  const header = document.createElement('header');header.className = 'evo-mission-header';
  const title = document.createElement('h2');title.id = 'evo-mission-title';title.textContent = ports.copy('missionWorkspace');
  const status = document.createElement('p');status.className = 'evo-mission-status';status.setAttribute('role', 'status');
  const actions = document.createElement('div');actions.className = 'evo-mission-actions';
  const inspect = createButton({label: ports.copy('missionOpenExecution'), variant: 'primary', size: 'sm', onClick: () => {
    if (!current.run) return;
    const records = taskRecords(current.run, ports.copy);
    const id = current.run.task_id || records[0]?.taskId;
    if (id) ports.openTask(id);
  }});
  inspect.dataset.missionAction = 'execution';
  const stop = createButton({label: ports.copy('cancel'), variant: 'danger', size: 'sm', onClick: ports.stop});
  stop.dataset.missionAction = 'stop';
  const retry = createButton({label: ports.copy('missionRetry'), size: 'sm', title: ports.copy('missionRetryNote'), onClick: ports.retry});
  retry.dataset.missionAction = 'retry';
  const details = createButton({label: ports.copy('missionDetails'), size: 'sm', onClick: ports.details});
  const conversation = createButton({label: ports.copy('talkCoordinator'), variant: 'ghost', size: 'sm', onClick: ports.conversation});
  const create = createButton({label: ports.copy('newGoal'), variant: 'primary', onClick: ports.create});
  const human = createButton({label: ports.copy('askHuman'), variant: 'primary', size: 'sm', onClick: ports.details});
  human.dataset.missionAction = 'answer';
  actions.append(inspect, stop, retry, human, details, conversation, create);
  const notice = document.createElement('p');notice.className = 'evo-mission-notice';notice.textContent = ports.copy('missionMotionNote');
  const body = document.createElement('div');body.className = 'evo-mission-body';
  header.append(title, status, actions, notice);host.append(header, body);
  const inspector = mountGoalRunInspector(body, ports);
  return {update(observation: GoalObservation, busy = false) {
    current = observation;
    const {run, questions} = observation;
    title.textContent = ports.copy(observation.loading ? 'missionLoading' : 'missionWorkspace');
    const nextStatus = run ? `${run.background_wait ? ports.copy('waitingBackground') : runStatusLabel(run.status, ports.copy)}${questions ? ' · ' + ports.copy('askHuman') : ''}` : ports.copy('roomNoGoal');
    if (status.textContent !== nextStatus) status.textContent = nextStatus;
    status.dataset.state = questions ? 'waiting' : run?.status || 'idle';
    inspect.hidden = !run || !(run.task_id || run.receipts?.length);
    stop.hidden = !run || isTerminalRun(run);stop.disabled = busy;
    retry.hidden = !run || !['failed', 'needs_attention', 'cancelled'].includes(run.status);retry.disabled = busy;
    human.hidden = !questions;
    details.hidden = conversation.hidden = !run;
    create.hidden = !!run || !!observation.loading;
    inspector.update(observation);
  },visibilityChanged:inspector.visibilityChanged,dispose:inspector.dispose};
}
