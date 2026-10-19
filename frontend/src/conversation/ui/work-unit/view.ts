/** Mounted production card projection. Keyed child views preserve human drafts, disclosures, focus and media. */
import { button, node, setText } from './dom';
import { activityLabel, pageProgress, stageLabel, statusLabel } from './labels';
import { createGateView } from './gate';
import { createMediaView } from './media';
import { phrase, ready, terminal, resourcePath, type WorkAction, type WorkState, type WorkUnit, type WorkUnitPorts } from './contracts';
export function createWorkUnitView(card: HTMLElement, taskId: string, ports: WorkUnitPorts, act: (action: WorkAction) => void) {
  const doc = ports.document, t = ports.text, live = card.querySelector<HTMLElement>('.ptool-wu-live')!;
  const status = card.querySelector<HTMLElement>('[data-wu-status]');
  const title = card.querySelector<HTMLElement>('[data-wu-title]');
  const activity = node(doc, 'p', 'ptool-wu-activity', t('workUnit.loading', 'Loading production progress…')); activity.setAttribute('role', 'status');
  const progress = node(doc, 'p', 'ptool-wu-caption');
  const outcome = node(doc, 'p', 'ptool-wu-outcome');
  const gate = createGateView(doc, t, act), media = createMediaView(taskId, ports, act);
  const actions = node(doc, 'div', 'ptool-wu-actions');
  const open = button(doc, t('workUnit.open', 'Open deliverable'), () => media.open());
  const edit = button(doc, t('workUnit.revise', 'Request changes'), () => {
    if (!current || !ready(current)) return;
    const key = current.kind === 'motion-video' ? 'workUnit.editVideoPrompt' : current.kind === 'slides-deck' ? 'workUnit.editSlidesPrompt' : 'workUnit.editWorkPrompt';
    ports.prefill(phrase(t, key, 'Revise the deliverable from task {id}: ', { id: taskId }));
  });
  const restyle = button(doc, t('wu.chipRestyle', 'Change style'), () => ports.prefill(phrase(t, 'workUnit.restylePrompt', 'Change the visual style of slide task {id}: ', { id: taskId })));
  const simplify = button(doc, t('wu.chipSimplify', 'Simplify text'), () => ports.prefill(phrase(t, 'workUnit.simplifyPrompt', 'Simplify the text in slide task {id}, emphasizing each page’s main idea.', { id: taskId })));
  const translate = button(doc, t('wu.chipTranslate', 'Translate to English'), () => ports.prefill(phrase(t, 'workUnit.translatePrompt', 'Translate slide task {id} into English while preserving its layout.', { id: taskId })));
  const stop = button(doc, t('workUnit.stop', 'Stop production'), () => act({ name: 'abort' }));
  const downloads = node(doc, 'div', 'ptool-wu-downloads');
  actions.append(open, downloads, edit, stop);
  const revisions = node(doc, 'details', 'ptool-wu-revisions');
  const revisionsTitle = node(doc, 'summary', ''); const revisionActions = node(doc, 'div', 'ptool-wu-actions');
  revisionActions.append(restyle, simplify, translate); revisions.append(revisionsTitle, revisionActions);
  const process = node(doc, 'details', 'ptool-wu-process');
  const processTitle = node(doc, 'summary', ''); const stages = node(doc, 'ol', 'ptool-wu-stages'); process.append(processTitle, stages);
  const connection = node(doc, 'div', 'ptool-wu-connection'); const connectionText = node(doc, 'p', 'ptool-wu-caption');
  const refresh = button(doc, t('workUnit.retry', 'Retry'), () => act({ name: 'refresh' })); connection.append(connectionText, refresh);
  const operationMessage = node(doc, 'p', 'ptool-wu-action-message'); operationMessage.setAttribute('role', 'status');
  live.replaceChildren(activity, progress, outcome, gate.root, media.root, actions, revisions, operationMessage, connection, process);
  let current: WorkUnit | null = null, stagesFingerprint = '', downloadsFingerprint = '';
  return { update(unit: WorkUnit, state: WorkState): void {
    current = unit;
    if (title && unit.title) setText(title, unit.title);
    if (status) { setText(status, statusLabel(unit, t)); status.dataset.state = unit.pendingGate && !terminal(unit) ? 'waiting' : unit.status; }
    setText(activity, activityLabel(unit, t)); setText(progress, pageProgress(unit, t)); progress.hidden = !progress.textContent;
    const problem = unit.status === 'degraded' ? unit.quality?.reason || t('wu.degradedLine', 'Finished with reduced quality')
      : unit.status === 'failed' ? [unit.failure?.detail, unit.failure?.hint].filter(Boolean).join('\n') : '';
    setText(outcome, problem); outcome.hidden = !problem; outcome.dataset.state = unit.status;
    gate.update(unit, state); media.update(unit, state);
    const canOpen = Boolean(unit.pages?.rendered?.length) || (ready(unit) && unit.kind === 'motion-video' && unit.artifacts?.some(a => a.label === 'mp4' && resourcePath(a.href)));
    open.hidden = !canOpen; setText(open, ready(unit) ? t('workUnit.open', 'Open deliverable') : t('workUnit.openPreview', 'View existing previews'));
    edit.hidden = !ready(unit); setText(edit, t('workUnit.revise', 'Request changes'));
    stop.hidden = terminal(unit) || !resourcePath(unit.links?.abort);
    const operation = state.operations.get('abort'); stop.disabled = Boolean(operation?.busy || operation?.accepted);
    setText(stop, operation?.accepted ? t('workUnit.stopping', 'Stopping…') : t('workUnit.stop', 'Stop production'));
    setText(operationMessage, operation?.error || '');
    revisions.hidden = !ready(unit) || unit.kind !== 'slides-deck'; setText(revisionsTitle, t('workUnit.moreChanges', 'More changes'));
    setText(restyle, t('wu.chipRestyle', 'Change style')); setText(simplify, t('wu.chipSimplify', 'Simplify text')); setText(translate, t('wu.chipTranslate', 'Translate to English'));
    const artifacts = ready(unit) && Array.isArray(unit.artifacts) ? unit.artifacts.filter(a => resourcePath(a.href)) : [];
    const nextDownloads = JSON.stringify([artifacts, t('wu.download', 'Download {label}')]);
    if (nextDownloads !== downloadsFingerprint) {
      downloadsFingerprint = nextDownloads; downloads.replaceChildren();
      for (const artifact of artifacts) {
        const link = node(doc, 'a', 'ptool-wu-download', phrase(t, 'wu.download', 'Download {label}', { label: (artifact.label || 'file').toUpperCase() }));
        link.href = ports.resolveUrl(resourcePath(artifact.href)); link.download = ''; downloads.append(link);
      }
    }
    connection.hidden = !state.transportError;
    setText(connectionText, t('workUnit.connectionError', 'Progress could not be refreshed. The last confirmed state is shown.'));
    setText(refresh, t('workUnit.retry', 'Retry')); setText(processTitle, t('workUnit.process', 'Production and checks'));
    const nextStages = JSON.stringify([unit.stages, t('workUnit.process', 'Production and checks')]);
    if (nextStages !== stagesFingerprint) {
      stagesFingerprint = nextStages; stages.replaceChildren();
      for (const stage of Array.isArray(unit.stages) ? unit.stages : []) {
        const row = node(doc, 'li', 'ptool-wu-stage'); row.dataset.state = stage.state;
        row.append(node(doc, 'span', 'ptool-wu-stage-mark', stage.state === 'done' ? '✓' : stage.state === 'failed' ? '!' : stage.state === 'active' ? '●' : '·'));
        row.append(node(doc, 'span', '', stageLabel(stage.name, t)));
        if ((stage.attempt || 0) > 1) row.append(node(doc, 'span', 'ptool-wu-caption', phrase(t, 'wu.attempt', 'Attempt {n}', { n: stage.attempt })));
        if (stage.lastErrors?.length) row.append(node(doc, 'span', 'ptool-wu-error', stage.lastErrors.join('; ')));
        stages.append(row);
      }
    }
    process.hidden = !stages.childElementCount;
  }, dispose(): void { media.dispose(); } };
}
