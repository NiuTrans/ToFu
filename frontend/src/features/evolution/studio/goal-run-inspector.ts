/** Shared run-inspection content. Renders server observations only; all actions
 * use UI factories. Snapshot deduplication preserves focus/scroll between polls.
 */
import { mountOutcomeReader, type OutcomeRead } from './outcome-reader';
import { reviewEvidenceHtml } from './review-evidence';
import { mountResourceLauncher } from '../../../rich-response/resource-launcher';
import { richChartText } from '../evidence-charts';
import { evidenceCharts } from '../evidence-charts';
import { createButton } from '../../../ui/index';
import { esc } from '../components/ui';
import { failureReason, latestGoalUpdate, measuredCount, reviewOutcomeLabel, runStatusLabel,
  runTelemetry, taskRecords, type GoalObservation, type StudioCopy } from '../goal-workbench';

export function mountGoalRunInspector(host: HTMLElement, ports: {
  copy: StudioCopy; openTask: (id: string) => void; detailed?: boolean; deliverableId?: string;
  readOutcome?: OutcomeRead; visible?: () => boolean;
}) {
  let signature = '', selectedRun = '', recordOffset = 0, disposed = false;
  let currentRecords: ReturnType<typeof taskRecords> = [];
  const projection = document.createElement('div');
  const recordsSection = document.createElement('section');recordsSection.className = 'evo-mission-section evo-run-records';
  recordsSection.setAttribute('aria-label', ports.copy('executionRecords'));
  const recordsTitle = document.createElement('h3');recordsTitle.tabIndex = -1;recordsTitle.textContent = ports.copy('executionRecords');
  const recordStatus = document.createElement('p');
  const recordNote = document.createElement('small');recordNote.textContent = ports.copy('sourceVerdictNote');
  const recordHost = document.createElement('div');recordHost.className = 'evo-mission-records';
  const recordRange = document.createElement('p');recordRange.setAttribute('role', 'status');
  const recordActions = document.createElement('div');recordActions.className = 'evo-mission-actions';
  const newer = createButton({label: ports.copy('recordsNewer'), size: 'sm', onClick: () => {recordOffset = Math.max(0, recordOffset - 20);renderRecords();}});
  const older = createButton({label: ports.copy('recordsOlder'), size: 'sm', onClick: () => {recordOffset += 20;renderRecords();}});
  newer.dataset.recordAction = 'newer';older.dataset.recordAction = 'older';recordActions.append(newer, older);
  const sourceHost = document.createElement('section');
  const reader = ports.readOutcome && ports.detailed ? mountOutcomeReader(sourceHost, {copy: ports.copy, read: ports.readOutcome, visible: ports.visible, fallbackFocus: () => recordsTitle.focus({preventScroll: true})}) : undefined;
  recordsSection.append(recordsTitle, recordStatus, recordNote, sourceHost, recordHost, recordRange, recordActions);
  recordsSection.hidden = true;host.replaceChildren(projection, recordsSection);
  const resources=ports.detailed?mountResourceLauncher(host,richChartText):undefined;
  const rowNodes = new Map<string, {row: HTMLElement; task: HTMLButtonElement; source?: HTMLButtonElement; id: HTMLElement; restored: HTMLElement; reviewFormat: HTMLElement}>();
  function renderRecords() {
    const visible = currentRecords.slice(recordOffset, recordOffset + 20);
    const retained = new Set(visible.map(record => record.taskId));
    for (const [id, node] of rowNodes) if (!retained.has(id)) {node.row.remove();rowNodes.delete(id);}
    let anchor: ChildNode | null = recordHost.firstChild;
    for (const record of visible) {
      let node = rowNodes.get(record.taskId);
      if (!node) {
        const row = document.createElement('div');row.className = 'evo-mission-record';
        const actions = document.createElement('div');actions.className = 'evo-mission-actions';
        const task = createButton({label: '', size: 'sm', onClick: () => {reader?.visibilityChanged(false);ports.openTask(record.taskId);}});
        task.dataset.taskId = record.taskId;task.dataset.inspectorAction = 'task';actions.append(task);
        const source = reader ? createButton({label: ports.copy('sourceOpen'), size: 'sm', onClick: () => {
          reader.select(selectedRun, record.taskId, currentRecords.find(item => item.taskId === record.taskId)?.restoredFromOutcome === true, source!);
        }}) : undefined;
        if (source) {source.dataset.outcomeTaskId = record.taskId;source.dataset.taskId = record.taskId;source.dataset.inspectorAction = 'source';actions.append(source);}
        const id = document.createElement('small');id.textContent = record.taskId;
        const restored = document.createElement('small');restored.textContent = ports.copy('sourceRestored');
        const reviewFormat = document.createElement('small');reviewFormat.dataset.reviewFormatRetry = record.taskId;
        row.append(actions, id, restored, reviewFormat);node = {row, task, source, id, restored, reviewFormat};rowNodes.set(record.taskId, node);
      }
      node.task.textContent = `${record.stage} · ${record.status} ↗`;
      if (node.source) node.source.hidden = !record.outcomeAvailable;
      node.restored.textContent = record.restoredFromOutcome ? ports.copy('sourceRestored') : '';
      node.restored.hidden = !record.restoredFromOutcome;
      node.reviewFormat.textContent = record.reviewFormatNote;node.reviewFormat.hidden = !record.reviewFormatNote;
      if (node.row === anchor) anchor = anchor.nextSibling;
      else recordHost.insertBefore(node.row, anchor);
    }
    recordRange.textContent = currentRecords.length ? `${ports.copy('recordsRange')}: ${recordOffset + 1}–${Math.min(recordOffset + 20, currentRecords.length)} / ${currentRecords.length.toLocaleString()}` : ports.copy('historyEmpty');
    newer.disabled = recordOffset === 0;older.disabled = recordOffset + 20 >= currentRecords.length;
    recordActions.hidden = currentRecords.length <= 20;
  }
  const text = (key: string) => esc(ports.copy(key));
  return {update(observation: GoalObservation) {
    if (disposed) return;
    const next = JSON.stringify(observation);
    if (signature === next) return;
    signature = next;
    const focus = host.contains(document.activeElement) ? (document.activeElement as HTMLElement).dataset.taskId : undefined;
    const focusAction = (document.activeElement as HTMLElement | null)?.dataset.inspectorAction;

    const sameRun = selectedRun === (observation.run?.run_id || '');
    const expanded = new Set(sameRun ? Array.from(projection.querySelectorAll<HTMLDetailsElement>('details[data-inspection-section][open]')).map(node => node.dataset.inspectionSection) : []);
    const focusedSection = sameRun && document.activeElement?.tagName === 'SUMMARY'
      ? (document.activeElement.parentElement as HTMLElement)?.dataset.inspectionSection : undefined;
    const technicalOpen = sameRun && projection.querySelector<HTMLDetailsElement>('details:not([data-inspection-section])')?.open;
    const summaryFocused = document.activeElement === projection.querySelector('details:not([data-inspection-section]) > summary');

    const deliverableFocused = ports.deliverableId && document.activeElement?.id === ports.deliverableId;
    const {run, workspace, coordination} = observation;
    resources?.update(run?.run_id?{runId:run.run_id}:undefined);
    if (selectedRun !== (run?.run_id || '')) {reader?.reset();selectedRun = run?.run_id || '';recordOffset = 0;currentRecords = [];rowNodes.forEach(node => node.row.remove());rowNodes.clear();}
    recordsSection.hidden = !run || !ports.detailed;
    if (!run) {
      projection.innerHTML = `<p class="evo-mission-empty">${text(observation.loading ? 'missionLoading' : 'missionEmpty')}</p>`;
      return;
    }
    const latest = latestGoalUpdate(coordination), telemetry = runTelemetry(run, workspace, ports.copy);
    const records = taskRecords(run, ports.copy);
    const clip = (value: string) => esc(ports.detailed ? value : value.slice(0, 700) + (value.length > 700 ? '…' : ''));
    const section = (key: string, content: string) => {
      const secondary = !ports.detailed && (['plannedSteps', 'acceptance', 'missionEvidence', 'missionTelemetry'].includes(key) || key === 'deliverable' && !run.result || key === 'reviewEvidence' && !review);
      return secondary
        ? `<details class="evo-mission-section evo-inspection-fold" data-inspection-section="${key}"${expanded.has(key) ? ' open' : ''}><summary>${text(key)}</summary><div class="evo-inspection-content">${content}</div></details>`
        : `<section class="evo-mission-section" data-inspection-section="${key}" aria-label="${text(key)}"><h3>${text(key)}</h3>${content}</section>`;
    };
    const empty = (key: string) => `<p class="evo-mission-empty">${text(key)}</p>`;
    const diagnosis = run.error ? `<div class="evo-mission-diagnosis"><strong>${text('missionNeedsAction')}</strong><p>${esc(failureReason(run.error, ports.copy))}</p>${ports.detailed ? `<details><summary>${text('missionTechnicalDetails')}</summary><pre>${esc(run.error)}</pre></details>` : ''}</div>` : '';
    const stages = workspace?.stages || [];
    const criteria = workspace?.criteria || [];
    const materials = workspace?.materials || [];
    const review = run.review;
    const reviewRows = (review?.criterion_results || []).map(criterion => {
      const requirement = criteria.find(item => item.criterion_id === criterion.criterion_id)?.requirement || criterion.criterion_id;
      return `<li><strong>${esc(requirement)}</strong><span>${esc(reviewOutcomeLabel(criterion.outcome, ports.copy))}</span><p>${esc(criterion.evidence_ref || ports.copy('missionEvidenceMissing'))}</p></li>`;
    }).join('');
    projection.innerHTML = diagnosis +
      section('missionLatestWork', latest ? `<p class="evo-mission-prose">${clip(latest.content)}</p><small>${text('activityRecord')} #${latest.seq}</small>` : empty('missionLatestEmpty')) +
      section('deliverable', run.result ? `<pre class="evo-mission-prose"${ports.deliverableId ? ` id="${esc(ports.deliverableId)}" tabindex="-1"` : ''}>${clip(run.result)}</pre>` : empty('missionDeliveryEmpty')) +
      section('plannedSteps', stages.length ? `<ol class="evo-mission-steps">${stages.map(step => `<li><strong>${esc(step.kind)}</strong><p>${esc(step.detail)}</p></li>`).join('')}</ol><small>${text('missionPlanNote')}</small>` : empty('missionPlanEmpty')) +
      (criteria.length ? section('acceptance', `<ul class="evo-mission-checks">${criteria.map(item => `<li>${esc(item.requirement)}</li>`).join('')}</ul>`) : '') +
      section('missionEvidence', `<p>${materials.length.toLocaleString()} ${text('missionSources')} · ${records.length.toLocaleString()} ${text('executionRecords')}</p><small>${text('shelfEvidenceNote')}</small>${materials.length ? `<ul class="evo-mission-checks">${materials.slice(0, ports.detailed ? materials.length : 3).map(item => `<li><strong>${esc(item.title)}</strong>${item.note ? `<p>${esc(item.note)}</p>` : ''}</li>`).join('')}</ul>` : ''}`) +
      section('reviewEvidence', review ? `<strong>${esc(reviewOutcomeLabel(review.outcome, ports.copy))}</strong>${reviewRows ? `<ul class="evo-mission-checks">${reviewRows}</ul>` : empty('missionEvidenceMissing')}${reviewEvidenceHtml(review, ports.copy, run.learning)}` : empty('reviewPending')) +
      section('missionTelemetry', `<dl class="evo-mission-telemetry"><div><dt>${text('missionTokens')}</dt><dd>${telemetry.tokens}</dd><small>${text('missionLimit')}: ${esc(telemetry.tokenLimit)}</small></div><div><dt>${text('toolCalls')}</dt><dd>${telemetry.calls}</dd><small>${text('missionLimit')}: ${esc(telemetry.callLimit)}</small></div>${ports.detailed ? `<div><dt>${text('missionElapsedSeconds')}</dt><dd>${telemetry.seconds}</dd><small>${text('missionLimit')}: ${esc(telemetry.timeLimit)}</small></div>` : ''}</dl><small>${text('missionTelemetryNote')}</small>`);
    if(ports.detailed && run.result){
      const original=projection.querySelector<HTMLElement>(ports.deliverableId ? '#'+ports.deliverableId : 'pre.evo-mission-prose');
      const visual=evidenceCharts(host.ownerDocument,run.result);if(visual&&original)original.before(visual);
    }
    const anchorTask = recordOffset ? currentRecords[recordOffset]?.taskId : undefined;
    currentRecords = records;
    if (anchorTask) {const index = records.findIndex(record => record.taskId === anchorTask);recordOffset = index >= 0 ? index : 0;}
    if (ports.detailed) {
      recordStatus.textContent = `${run.background_wait ? ports.copy('waitingBackground') : runStatusLabel(run.status, ports.copy)} · ${ports.copy('attempt')} ${measuredCount(run.attempt)}`;
      renderRecords();
    }
    const technical = projection.querySelector<HTMLDetailsElement>('details:not([data-inspection-section])');
    if (technical && technicalOpen) technical.open = true;
    if (summaryFocused) technical?.querySelector('summary')?.focus({preventScroll: true});
    if (focusedSection) projection.querySelector<HTMLDetailsElement>(`details[data-inspection-section="${focusedSection}"]`)?.querySelector('summary')?.focus({preventScroll: true});

    if (deliverableFocused) host.querySelector<HTMLElement>('[tabindex="-1"]')?.focus({preventScroll: true});
    if (focus) Array.from(host.querySelectorAll<HTMLButtonElement>('[data-task-id]')).find(button => button.dataset.taskId === focus && button.dataset.inspectorAction === focusAction)?.focus({preventScroll: true});
  }, visibilityChanged(visible?: boolean) {reader?.visibilityChanged(visible);},
  dispose() {disposed = true;resources?.dispose();reader?.dispose();rowNodes.clear();currentRecords = [];host.replaceChildren();},
  };
}
