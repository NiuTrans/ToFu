/** Durable acceptance shell shared by producer receipts and explicitly accepted scene revisions. No lifecycle inference. */
import { escapeHtml } from '../../../html-safety';
import type { WorkText } from './contracts';
export function renderWorkUnitShell(taskId: string, kind: string, title: string, label: string,
  text: WorkText, options: { icon?: string; details?: string; round?: string | number } = {}): string {
  return `<article class="ptool-work-card" data-wu-task="${escapeHtml(taskId)}" data-wu-kind="${escapeHtml(kind)}" data-rn="${escapeHtml(String(options.round ?? ''))}">
    <header class="ptool-work-header"><span class="ptool-work-icon" aria-hidden="true">${options.icon || ''}</span>
      <div class="ptool-work-heading"><span class="ptool-convmeta-src">${escapeHtml(label)}</span><h3 class="ptool-text" data-wu-title>${escapeHtml(title)}</h3></div>
      <span class="ptool-work-status" data-wu-status data-state="queued">${escapeHtml(text('workUnit.accepted', 'Accepted'))}</span></header>
    <div class="ptool-wu-live"><p class="ptool-wu-caption">${escapeHtml(text('workUnit.loading', 'Loading production progress…'))}</p></div>
    ${options.details ? `<details class="ptool-work-receipt"><summary>${escapeHtml(text('workUnit.requestDetails', 'Request details'))}</summary>${options.details}</details>` : ''}
    <div class="ptool-wu-revision-tasks"></div>
  </article>`;
}
