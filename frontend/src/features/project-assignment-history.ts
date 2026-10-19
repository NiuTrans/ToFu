/** Shared bounded history navigation. The parent owns reads and generation
 * fences; this component never fetches, accumulates pages or changes authority. */
import type { ProjectDispatchPage } from '../api/project-dispatch.generated';
import { createButton } from '../ui';
import { t } from '../i18n';

export function createAssignmentHistoryNavigation(page: ProjectDispatchPage | null, cursor: string | undefined,
  navigate: (cursor: string | undefined) => void): HTMLElement {
  const root = document.createElement('nav');
  root.className = 'ui-row ui-row--wrap coordination-history';
  root.setAttribute('aria-label', t('projectCoordination.history.label'));
  if (!cursor && !page?.olderCursor && !page?.newerCursor) return root;
  const label = document.createElement('small');
  label.textContent = t(cursor ? 'projectCoordination.history.past' : 'projectCoordination.history.recent');
  root.append(label);
  for (const [kind, text, next] of [['newer', t('projectCoordination.history.newer'), page?.newerCursor],
    ['older', t('projectCoordination.history.older'), page?.olderCursor]] as const) {
    const button = createButton({ label: text, size: 'sm', variant: 'ghost',
      onClick: () => { if (next) navigate(next); } });
    button.dataset.coordinationFocus = `history-${kind}`;
    button.disabled = !next; root.append(button);
  }
  if (cursor) {
    const latest = createButton({ label: t('projectCoordination.history.latest'), size: 'sm', variant: 'ghost',
      onClick: () => navigate(undefined) });
    latest.dataset.coordinationFocus = 'history-latest'; root.append(latest);
  }
  return root;
}
