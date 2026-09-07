/**
 * Delegated disclosure behavior for tool execution panels and parallel batches.
 *
 * Responsibility: mutate only reader-owned collapse/ARIA state in response to
 * one click. Turn data and execution state remain immutable projection inputs.
 * Entry point: `handleToolExecutionDisclosureClick`.
 * Dependencies: browser DOM only.
 */

function eventTargetElement(event: Event): Element | null {
  const target = event.target as Partial<Element> | null;
  return target && typeof target.closest === 'function'
    ? target as Element : null;
}

export function handleToolExecutionDisclosureClick(event: Event): void {
  const target = eventTargetElement(event);
  if (!target) return;

  const panelHeader = target.closest('.ptool-panel-header[aria-expanded]');
  if (panelHeader) {
    const panel = panelHeader.closest('.ptool-panel');
    if (!panel || panel.classList.contains('ptool-panel-active')) return;
    event.stopPropagation();
    const collapsed = panel.classList.toggle('collapsed');
    panelHeader.setAttribute('aria-expanded', String(!collapsed));
    return;
  }

  const batchHeader = target.closest('.ptool-turn-head');
  const batch = batchHeader?.closest('.ptool-turn');
  if (!batchHeader || !batch) return;
  event.stopPropagation();
  const collapsed = batch.classList.toggle('collapsed');
  batchHeader.setAttribute('aria-expanded', String(!collapsed));
}
