/** One disclosed Coordinator message, sourced from its canonical assistant Turn.
 * The reader explicitly acknowledges the captured report ID. Opening a project,
 * fetching, or rendering a sidebar never advances the server's read position.
 */
import { createButton } from '../ui';
import { t } from '../i18n';
import type { CoordinatorReportReadRequest, CoordinatorReportState } from '../api/project-coordination.generated';

export const coordinatorReportLabel = (kind: NonNullable<CoordinatorReportState['latest']>['kind']) => t(`projectCoordination.report.kind.${kind}`);

export function createCoordinatorReport(state: CoordinatorReportState, ports: {
  expanded: boolean; toggle(open: boolean): void; valid(): boolean;
  markRead(request: CoordinatorReportReadRequest): Promise<CoordinatorReportState>; changed(): void;
}): HTMLElement {
  const report = state.latest!;
  const root = document.createElement('details'); root.className = 'coordination-report'; root.open = ports.expanded;
  root.dataset.reportId = report.turnId; root.dataset.unread = String(state.unread);
  root.addEventListener('toggle', () => ports.toggle(root.open));
  const heading = document.createElement('summary'); heading.dataset.coordinationFocus = `report:${report.turnId}`;
  const kind = document.createElement('span'); kind.className = 'coordination-report-kind';
  kind.textContent = coordinatorReportLabel(report.kind);
  const title = document.createElement('strong'); title.textContent = report.title;
  heading.append(kind, title); root.append(heading);
  const body = document.createElement('div'); body.className = 'coordination-report-body';
  body.tabIndex = 0; body.setAttribute('role', 'region'); body.setAttribute('aria-label', report.title);
  const content = document.createElement('p');
  // Plain text is the report tool contract. The full message remains rendered
  // by the existing conversation canvas; never execute or interpret report text.
  content.textContent = report.content.startsWith(report.title + '\n\n')
    ? report.content.slice(report.title.length + 2) : report.content;
  body.append(content); root.append(body);
  if (state.unread) {
    const error = document.createElement('p'); error.setAttribute('role', 'alert'); error.hidden = true;
    const read = createButton({ label: t('projectCoordination.report.markRead'), size: 'sm', onClick: async () => {
      if (!ports.valid() || read.disabled) return;
      read.disabled = true; error.hidden = true;
      try {
        await ports.markRead({ path: state.projectPath, conversationId: state.conversationId,
          bindingRevision: state.bindingRevision, turnId: report.turnId });
        if (ports.valid()) ports.changed();
      } catch (reason) {
        if (ports.valid()) { error.textContent = reason instanceof Error ? reason.message : String(reason); error.hidden = false; }
      } finally { read.disabled = false; }
    } });
    read.dataset.coordinationFocus = `report-read:${report.turnId}`; body.append(read, error);
  } else {
    const read = document.createElement('small'); read.textContent = t('projectCoordination.report.read'); body.append(read);
  }
  return root;
}
