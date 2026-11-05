/** Run inspection content for the studio journal tabs. Renders server
 * observations only, section by section: status (diagnosis and waits), latest
 * coordinator work, usage, execution records with the original-source reader,
 * independent review, and the deliverable. Snapshot deduplication preserves
 * focus and scroll between polls; all actions use UI factories.
 * Entry: mountGoalRunInspector.
 */
import { mountOutcomeReader, type OutcomeRead } from './outcome-reader';
import { reviewEvidenceHtml } from './review-evidence';
import { mountResourceLauncher } from '../../../rich-response/resource-launcher';
import { richChartText, evidenceCharts } from '../evidence-charts';
import { createButton } from '../../../ui/index';
import { esc } from '../components/ui';
import { computeStallFacts, failureReason, latestGoalUpdate, measuredCount, reviewOutcomeLabel, runStatusLabel,
  runTelemetry, taskRecords, waitStatusLabel, type GoalObservation, type StudioCopy } from '../goal-workbench';

export type InspectorSection = 'status'|'latest'|'telemetry'|'records'|'review'|'deliverable';
const PAGE = 20;

export function mountGoalRunInspector(host: HTMLElement, ports: {
  copy: StudioCopy; openTask: (id: string) => void; sections: InspectorSection[]; deliverableId?: string;
  readOutcome?: OutcomeRead; visible?: () => boolean;
}) {
  let signature = '', selectedRun = '', recordOffset = 0, disposed = false;
  let currentRecords: ReturnType<typeof taskRecords> = [];
  const has = (section: InspectorSection) => ports.sections.includes(section);
  const text = (key: string) => esc(ports.copy(key));
  const projection = document.createElement('div');projection.className = 'evo-inspector';
  const recordsSection = document.createElement('section');recordsSection.className = 'evo-ledger-section evo-run-records';
  recordsSection.setAttribute('aria-label', ports.copy('executionRecords'));
  const recordsTitle = document.createElement('h3');recordsTitle.tabIndex = -1;recordsTitle.textContent = ports.copy('executionRecords');
  const recordHost = document.createElement('div');recordHost.className = 'evo-records';
  const recordRange = document.createElement('p');recordRange.className = 'evo-records-range';recordRange.setAttribute('role', 'status');
  const recordActions = document.createElement('div');recordActions.className = 'evo-button-row';
  const newer = createButton({label: ports.copy('recordsNewer'), size: 'sm', onClick: () => {recordOffset = Math.max(0, recordOffset - PAGE);renderRecords();}});
  const older = createButton({label: ports.copy('recordsOlder'), size: 'sm', onClick: () => {recordOffset += PAGE;renderRecords();}});
  newer.dataset.recordAction = 'newer';older.dataset.recordAction = 'older';recordActions.append(newer, older);
  const sourceHost = document.createElement('section');
  const reader = ports.readOutcome && has('records') ? mountOutcomeReader(sourceHost, {copy: ports.copy, read: ports.readOutcome, visible: ports.visible, fallbackFocus: () => recordsTitle.focus({preventScroll: true})}) : undefined;
  recordsSection.append(recordsTitle, sourceHost, recordHost, recordRange, recordActions);
  recordsSection.hidden = true;host.replaceChildren(projection, recordsSection);
  // Compute resources belong beside execution usage, where compute waits surface.
  const resources = has('telemetry') ? mountResourceLauncher(host, richChartText) : undefined;
  const rowNodes = new Map<string, {row: HTMLElement; task: HTMLButtonElement; source?: HTMLButtonElement; restored: HTMLElement; reviewFormat: HTMLElement}>();
  function renderRecords() {
    const visible = currentRecords.slice(recordOffset, recordOffset + PAGE);
    const retained = new Set(visible.map(record => record.taskId));
    for (const [id, node] of rowNodes) if (!retained.has(id)) {node.row.remove();rowNodes.delete(id);}
    let anchor: ChildNode | null = recordHost.firstChild;
    for (const record of visible) {
      let node = rowNodes.get(record.taskId);
      if (!node) {
        const row = document.createElement('div');row.className = 'evo-record';
        const actions = document.createElement('div');actions.className = 'evo-button-row';
        const task = createButton({label: '', size: 'sm', onClick: () => {reader?.visibilityChanged(false);ports.openTask(record.taskId);}});
        task.dataset.taskId = record.taskId;task.dataset.inspectorAction = 'task';actions.append(task);
        const source = reader ? createButton({label: ports.copy('sourceOpen'), size: 'sm', onClick: () => {
          reader.select(selectedRun, record.taskId, currentRecords.find(item => item.taskId === record.taskId)?.restoredFromOutcome === true, source!);
        }}) : undefined;
        if (source) {source.dataset.outcomeTaskId = record.taskId;source.dataset.taskId = record.taskId;source.dataset.inspectorAction = 'source';actions.append(source);}
        const id = document.createElement('code');id.textContent = record.taskId;
        const restored = document.createElement('span');restored.className = 'evo-chip';restored.textContent = ports.copy('sourceRestored');
        const reviewFormat = document.createElement('small');reviewFormat.dataset.reviewFormatRetry = record.taskId;
        row.append(actions, id, restored, reviewFormat);node = {row, task, source, restored, reviewFormat};rowNodes.set(record.taskId, node);
      }
      node.task.textContent = `${record.stage} · ${record.status} ↗`;
      if (node.source) node.source.hidden = !record.outcomeAvailable;
      node.restored.hidden = !record.restoredFromOutcome;
      node.reviewFormat.textContent = record.reviewFormatNote;node.reviewFormat.hidden = !record.reviewFormatNote;
      if (node.row === anchor) anchor = anchor.nextSibling;
      else recordHost.insertBefore(node.row, anchor);
    }
    recordRange.textContent = currentRecords.length > PAGE ? `${recordOffset + 1}–${Math.min(recordOffset + PAGE, currentRecords.length)} / ${currentRecords.length.toLocaleString()}` : '';
    newer.disabled = recordOffset === 0;older.disabled = recordOffset + PAGE >= currentRecords.length;
    recordActions.hidden = currentRecords.length <= PAGE;
  }
  const section = (key: string, label: string, content: string) =>
    `<section class="evo-ledger-section" data-inspection-section="${key}" aria-label="${esc(label)}"><h3>${esc(label)}</h3>${content}</section>`;
  const empty = () => `<p class="evo-empty-mark" aria-label="${text('none')}">—</p>`;
  return {update(observation: GoalObservation) {
    if (disposed) return;
    const next = JSON.stringify(observation);
    if (signature === next) return;
    signature = next;
    const focus = host.contains(document.activeElement) ? (document.activeElement as HTMLElement).dataset.taskId : undefined;
    const focusAction = (document.activeElement as HTMLElement | null)?.dataset.inspectorAction;
    const sameRun = selectedRun === (observation.run?.run_id || '');
    const technicalOpen = sameRun && projection.querySelector<HTMLDetailsElement>('details.evo-technical')?.open;
    const deliverableFocused = ports.deliverableId && document.activeElement?.id === ports.deliverableId;
    const {run, workspace, coordination} = observation;
    resources?.update(run?.run_id ? {runId: run.run_id} : undefined);
    if (selectedRun !== (run?.run_id || '')) {reader?.reset();selectedRun = run?.run_id || '';recordOffset = 0;currentRecords = [];rowNodes.forEach(node => node.row.remove());rowNodes.clear();}
    recordsSection.hidden = !run || !has('records');
    if (!run) {projection.innerHTML = observation.loading ? `<p class="evo-empty-mark">…</p>` : empty();return;}
    const parts: string[] = [];
    if (has('status')) {
      const stall = computeStallFacts(run, ports.copy);
      const wait = stall.length
        ? `<dl class="evo-facts">${stall.map(fact => `<div data-fact="${fact.fact}"${fact.bad ? ' data-tone="bad"' : ''}><dt>${esc(fact.label)}</dt><dd>${[fact.value ? esc(fact.value) : '', fact.code ? `<code>${esc(fact.code)}</code>` : ''].filter(Boolean).join(' · ')}</dd></div>`).join('')}</dl>`
        : run.compute_wait ? `<dl class="evo-facts"><div><dt>${text('computeOperation')}</dt><dd><code>${esc(run.compute_wait.operation_id)}</code></dd></div></dl>` : '';
      const diagnosis = run.error ? `<div class="evo-diagnosis"><p>${esc(failureReason(run.error, ports.copy))}</p><details class="evo-technical"><summary>${text('missionTechnicalDetails')}</summary><pre>${esc(run.error)}</pre></details></div>` : '';
      const status = waitStatusLabel(run, ports.copy) || runStatusLabel(run.status, ports.copy);
      parts.push(section('status', ports.copy('missionStatus'), `<p class="evo-status-line"><span class="evo-chip" data-status="${esc(run.status)}">${esc(status)}</span><span>${text('attempt')} ${measuredCount(run.attempt)}</span></p>${wait}${diagnosis}`));
    }
    if (has('latest')) {
      const latest = latestGoalUpdate(coordination);
      parts.push(section('latest', ports.copy('missionLatestWork'), latest ? `<p class="evo-prose">${esc(latest.content)}</p><small class="evo-seq">#${latest.seq}</small>` : empty()));
    }
    if (has('telemetry')) {
      const telemetry = runTelemetry(run, workspace, ports.copy);
      parts.push(section('telemetry', ports.copy('missionTelemetry'), `<dl class="evo-facts evo-facts-grid"><div><dt>${text('missionTokens')}</dt><dd>${telemetry.tokens}</dd><small>/ ${esc(telemetry.tokenLimit)}</small></div><div><dt>${text('toolCalls')}</dt><dd>${telemetry.calls}</dd><small>/ ${esc(telemetry.callLimit)}</small></div><div><dt>${text('missionElapsedSeconds')}</dt><dd>${telemetry.seconds}</dd><small>/ ${esc(telemetry.timeLimit)}</small></div></dl>`));
    }
    if (has('review')) {
      const review = run.review, criteria = workspace?.criteria || [];
      const rows = (review?.criterion_results || []).map(criterion => {
        const requirement = criteria.find(item => item.criterion_id === criterion.criterion_id)?.requirement || criterion.criterion_id;
        return `<li data-outcome="${esc(criterion.outcome)}"><strong>${esc(requirement)}</strong><span class="evo-chip">${esc(reviewOutcomeLabel(criterion.outcome, ports.copy))}</span><p>${esc(criterion.evidence_ref || '—')}</p></li>`;
      }).join('');
      parts.push(review ? section('review', ports.copy('reviewEvidence'), `<p class="evo-verdict" data-outcome="${esc(review.outcome)}">${esc(reviewOutcomeLabel(review.outcome, ports.copy))}</p>${rows ? `<ul class="evo-checks">${rows}</ul>` : ''}${reviewEvidenceHtml(review, ports.copy, run.learning)}`) : empty());
    }
    if (has('deliverable')) {
      const deliverable = run.result && !run.compute_wait ? run.result : '';
      parts.push(deliverable ? `<pre class="evo-prose evo-deliverable"${ports.deliverableId ? ` id="${esc(ports.deliverableId)}" tabindex="-1"` : ''}>${esc(deliverable)}</pre>` : empty());
    }
    projection.innerHTML = parts.join('');
    if (has('deliverable') && run.result && !run.compute_wait) {
      const original = projection.querySelector<HTMLElement>('.evo-deliverable');
      // The studio HUD always shows the independent verdict, so charts carry no disclaimer here.
      const visual = evidenceCharts(host.ownerDocument, run.result, {note: false});if (visual && original) original.before(visual);
    }
    if (has('records')) {
      const anchorTask = recordOffset ? currentRecords[recordOffset]?.taskId : undefined;
      currentRecords = taskRecords(run, ports.copy);
      if (anchorTask) {const index = currentRecords.findIndex(record => record.taskId === anchorTask);recordOffset = index >= 0 ? index : 0;}
      renderRecords();
    }
    const technical = projection.querySelector<HTMLDetailsElement>('details.evo-technical');
    if (technical && technicalOpen) technical.open = true;
    if (deliverableFocused) host.querySelector<HTMLElement>('[tabindex="-1"]')?.focus({preventScroll: true});
    if (focus) Array.from(host.querySelectorAll<HTMLButtonElement>('[data-task-id]')).find(button => button.dataset.taskId === focus && button.dataset.inspectorAction === focusAction)?.focus({preventScroll: true});
  }, visibilityChanged(visible?: boolean) {reader?.visibilityChanged(visible);},
  dispose() {disposed = true;resources?.dispose();reader?.dispose();rowNodes.clear();currentRecords = [];host.replaceChildren();},
  };
}
