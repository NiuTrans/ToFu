/** Stateless Trace DOM adapter. Entry: renderInspectorTrace.
 * Consumes the pure inspector-trace-projection and common safeHtml primitive;
 * asynchronous read identity and back navigation remain with Inspector details.
 */
import {safeHtml as html, type SafeHtmlOutput} from '../html-safety';
import {projectInspectorTrace, type TraceBar, type TraceRow, type TraceTranslate} from './inspector-trace-projection';

function barMarkup(bar: TraceBar): SafeHtmlOutput {
  return html`<div class="${bar.className}" role="img" aria-label="${bar.title}" style="left:${bar.left.toFixed(3)}%;width:${bar.width.toFixed(3)}%" title="${bar.title}">
    ${bar.width > 7 && bar.label ? html`<span class="tr-bar-txt">${bar.label}</span>` : null}</div>`;
}
function rowMarkup(row: TraceRow): SafeHtmlOutput {
  return html`<div class="tr-row"><span class="tr-row-label" title="${row.label}">${row.label}</span>
    <div class="tr-track">${row.bars.map(barMarkup)}</div></div>`;
}
export function renderInspectorTrace(content: HTMLElement | null, title: HTMLElement | null, data: unknown, t: TraceTranslate): void {
  if (!content) return;
  if (title) title.textContent = t('ri.traceTitle');
  const view = projectInspectorTrace(data, t);
  if (view.availability !== 'ready') {
    content.innerHTML = String(html`<div class="ri-main-empty">${t(view.availability === 'expired' ? 'ri.expiredHint' : 'ri.loadFailed')}</div>`);
    return;
  }
  content.innerHTML = String(html`<div class="tr-wrap">
    <div class="tr-chips">${view.chips.map(chip => html`<span class="tr-chip ${chip.className}"><span class="tr-chip-dot" aria-hidden="true"></span>
      ${chip.label} <b>${chip.value}</b>${chip.detail ? html` <i>${chip.detail}</i>` : null}</span>`)}</div>
    ${view.notes.map(note => html`<div class="${note.className}">${note.text}</div>`)}
    ${view.axis.length ? html`<div class="tr-flame">${view.rows.map(rowMarkup)}<div class="tr-axis">${view.axis.map(value => html`<span>${value}</span>`)}</div></div>` : null}
    ${view.statusText ? html`<div class="tr-note"><b>${t('ri.traceStatusHistory')}</b> ${view.statusText}</div>` : null}
    ${view.statusRow ? html`<div class="tr-flame">${rowMarkup(view.statusRow)}</div>` : null}
    ${view.observations ? html`<div class="tr-note"><b>${t('ri.traceClientEvidence')}</b> ${view.observations}</div>` : null}
    </div>`);
}
