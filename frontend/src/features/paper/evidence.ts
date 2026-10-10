/** Q&A source verification. Quotes are queries, never trusted citations.
 * Entry points: decoratePaperEvidence, destroyPaperEvidence. Uses the shared
 * PDF search/navigation and UI dialog; owns one cancellable transient search.
 */
import { readLiveRuntimeBinding } from '../../feature-registry';
import { t } from '../../i18n';
import { createButton } from '../../ui/button';
import { createDialog } from '../../ui/dialog';

let closeActive: (() => void) | null = null;
const selection = (): string => JSON.stringify([
  readLiveRuntimeBinding('_activePaperId'), readLiveRuntimeBinding('_paperHash'),
]);

export function destroyPaperEvidence(): void { closeActive?.(); }

/** Settled bubbles get at most six short quote actions; no model markup is trusted. */
export function decoratePaperEvidence(bubble: HTMLElement): void {
  const identity = selection();
  const quotes = Array.from(bubble.querySelectorAll<HTMLElement>('.paper-qa-msg-content blockquote')).slice(0, 6);
  for (const quote of quotes) {
    const query = (quote.textContent ?? '').trim().replace(/\s+/gu, ' ');
    if (query.length < 8 || query.length > 256) continue;
    const action = createButton({ label: t('paper.evidenceVerify'), size: 'sm', variant: 'secondary',
      onClick: () => {
        if (identity === selection()) void verifyQuote(query, identity);
      },
    });
    const controls = document.createElement('div');
    controls.className = 'paper-evidence-action';
    controls.append(action);
    quote.after(controls);
  }
}

async function verifyQuote(query: string, identity: string): Promise<void> {
  destroyPaperEvidence();
  const controller = new AbortController();
  const body = document.createElement('div');
  body.className = 'paper-evidence';
  const quoted = document.createElement('blockquote');
  quoted.className = 'paper-evidence-query';
  quoted.textContent = query;
  const status = document.createElement('p');
  status.className = 'paper-evidence-status';
  status.setAttribute('role', 'status');
  status.textContent = t('paper.evidenceSearching');
  const matches = document.createElement('ol');
  matches.className = 'paper-evidence-matches';
  body.append(quoted, status, matches);
  const dialog = createDialog({ title: t('paper.evidenceTitle'), body,
    actions: [{ id: 'close', label: t('paper.evidenceClose'), variant: 'secondary' }],
    onClose: () => {
      controller.abort();
      dialog.el.remove();
      if (closeActive === close) closeActive = null;
    },
  });
  const close = (): void => { dialog.close(); };
  closeActive = close;
  dialog.el.classList.add('paper-evidence-dialog');
  document.body.append(dialog.el);
  void dialog.open();
  try {
    const { searchPaperSource, openPaperSourceMatch } = await import('./pdf-viewer');
    if (controller.signal.aborted || identity !== selection()) { close(); return; }
    const result = await searchPaperSource(query, controller.signal);
    if (controller.signal.aborted) return;
    if (identity !== selection()) { close(); return; }
    if (result.status !== 'ready') {
      status.textContent = t(result.status === 'stale' ? 'paper.evidenceStale' : 'paper.evidenceUnavailable');
      return;
    }
    status.textContent = t(result.matches.length ? 'paper.evidenceFound'
      : result.truncated ? 'paper.evidenceIncomplete' : 'paper.evidenceNotFound');
    if (result.matches.length && result.truncated) {
      const note = document.createElement('p');
      note.className = 'paper-evidence-status';
      note.textContent = t('paper.evidenceIncomplete');
      body.append(note);
    }
    for (const match of result.matches) {
      const item = document.createElement('li');
      const excerpt = document.createElement('p');
      excerpt.textContent = match.excerpt;
      item.append(createButton({ label: t('paper.evidencePage', { page: match.page }), variant: 'secondary', size: 'sm',
        onClick: () => {
          if (identity !== selection() || !openPaperSourceMatch(result, match.page)) {
            status.textContent = t('paper.evidenceStale');
            return;
          }
          close();
        },
      }), excerpt);
      matches.append(item);
    }
  } catch {
    if (!controller.signal.aborted && identity === selection()) status.textContent = t('paper.evidenceFailed');
    else if (!controller.signal.aborted) close();
  }
}
