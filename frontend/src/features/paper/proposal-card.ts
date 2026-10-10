/** Track B: interactive review card for `paper_propose_edits` tool results.
 *
 * The settled tool round renders a pure-HTML skeleton (tool-result-presentation
 * owns that half); this owner hydrates it: fetch the proposal, list each hunk
 * as a bdiff-style diff with a status badge, and decide hunks individually or
 * wholesale. Accept/reject POST through the typed API client, the card updates
 * in place from the authoritative response, and a successful accept nudges
 * the preview rebuild through the same path as the editor save. */
import { apiFailure } from '../../api/errors';
import type {
  PaperDocumentsApi,
  PaperProposalEditV1,
  PaperProposalV1,
} from '../../api/paper-documents';
import type { Translator } from '../../i18n';
import { renderLineDiffHtml } from '../../conversation/presentation/tool-result-presentation';

export interface PaperProposalCardDeps {
  api: Pick<PaperDocumentsApi, 'getProposal' | 'acceptProposal' | 'rejectProposal'>;
  tr: Translator;
  /** Successful-accept hook (preview rebuild nudge). */
  onApplied?(): void;
}

const MOUNTED_ATTR = 'data-paper-proposal-mounted';

function proposalStatusText(tr: Translator, status: string): string {
  switch (status) {
    case 'applied': return tr('paperProposal.status.applied');
    case 'partial': return tr('paperProposal.status.partial');
    case 'rejected': return tr('paperProposal.status.rejected');
    case 'failed': return tr('paperProposal.status.failed');
    default: return tr('paperProposal.status.pending');
  }
}

function hunkDiffHtml(edit: PaperProposalEditV1): string {
  return renderLineDiffHtml(edit.search ?? '', edit.replace ?? '');
}

function renderCardBody(
  host: HTMLElement,
  deps: PaperProposalCardDeps,
  proposal: PaperProposalV1,
  decide: (action: 'accept' | 'reject', indexes?: number[]) => void,
): void {
  const { tr } = deps;
  const fragment = document.createDocumentFragment();
  const head = document.createElement('div');
  head.className = 'paper-proposal-head';
  const title = document.createElement('span');
  title.className = 'paper-proposal-title';
  title.textContent = tr('paperProposal.title');
  const status = document.createElement('span');
  status.className = `paper-proposal-status paper-proposal-status-${proposal.status}`;
  status.textContent = proposalStatusText(tr, proposal.status);
  head.append(title, status);
  const pending = proposal.edits.filter(edit => edit.status === 'pending');
  if (pending.length) {
    const actions = document.createElement('span');
    actions.className = 'paper-proposal-actions';
    const acceptAll = document.createElement('button');
    acceptAll.type = 'button';
    acceptAll.className = 'paper-proposal-button paper-proposal-accept-all';
    acceptAll.textContent = tr('paperProposal.acceptAll');
    acceptAll.addEventListener('click', () => decide('accept'));
    const rejectAll = document.createElement('button');
    rejectAll.type = 'button';
    rejectAll.className = 'paper-proposal-button paper-proposal-reject-all';
    rejectAll.textContent = tr('paperProposal.rejectAll');
    rejectAll.addEventListener('click', () => decide('reject'));
    actions.append(acceptAll, rejectAll);
    head.append(actions);
  }
  fragment.append(head);

  if (!proposal.edits.length) {
    const empty = document.createElement('p');
    empty.className = 'paper-proposal-empty';
    empty.textContent = tr('paperProposal.empty');
    fragment.append(empty);
  }
  const list = document.createElement('div');
  list.className = 'paper-proposal-hunks';
  for (const edit of proposal.edits.slice(0, 64)) {
    const row = document.createElement('details');
    row.className = `paper-proposal-hunk paper-proposal-hunk-${edit.status}`;
    row.dataset.proposalIndex = String(edit.index);
    const summary = document.createElement('summary');
    summary.className = 'paper-proposal-hunk-summary';
    const index = document.createElement('span');
    index.className = 'paper-proposal-hunk-idx';
    index.textContent = String(edit.index + 1);
    const path = document.createElement('span');
    path.className = 'paper-proposal-hunk-path';
    path.textContent = edit.path;
    path.title = edit.path;
    const description = document.createElement('span');
    description.className = 'paper-proposal-hunk-desc';
    description.textContent = edit.description || edit.path;
    const badge = document.createElement('span');
    badge.className = `paper-proposal-status paper-proposal-status-${edit.status}`;
    badge.textContent = proposalStatusText(tr, edit.status);
    summary.append(index, path, description, badge);
    if (edit.status === 'pending') {
      const hunkActions = document.createElement('span');
      hunkActions.className = 'paper-proposal-actions paper-proposal-hunk-actions';
      const accept = document.createElement('button');
      accept.type = 'button';
      accept.className = 'paper-proposal-button paper-proposal-accept';
      accept.textContent = tr('paperProposal.accept');
      accept.addEventListener('click', event => {
        event.preventDefault();
        decide('accept', [edit.index]);
      });
      const reject = document.createElement('button');
      reject.type = 'button';
      reject.className = 'paper-proposal-button paper-proposal-reject';
      reject.textContent = tr('paperProposal.reject');
      reject.addEventListener('click', event => {
        event.preventDefault();
        decide('reject', [edit.index]);
      });
      hunkActions.append(accept, reject);
      summary.append(hunkActions);
    }
    row.append(summary);
    const diff = document.createElement('div');
    diff.className = 'paper-proposal-hunk-diff';
    diff.innerHTML = hunkDiffHtml(edit);
    row.append(diff);
    if (edit.error) {
      const error = document.createElement('div');
      error.className = 'paper-proposal-hunk-error';
      error.textContent = edit.error;
      row.append(error);
    }
    list.append(row);
  }
  fragment.append(list);
  host.replaceChildren(fragment);
}

/** Hydrate one skeleton element (`data-paper-proposal="<id>"`). Idempotent:
 * a mounted host is marked and never fetched twice. */
export async function mountPaperProposalCard(
  host: HTMLElement,
  deps: PaperProposalCardDeps,
): Promise<void> {
  const proposalId = host.dataset.paperProposal ?? '';
  if (!proposalId || host.hasAttribute(MOUNTED_ATTR)) return;
  host.setAttribute(MOUNTED_ATTR, '1');
  const { tr } = deps;
  const fail = (error: unknown): void => {
    if (!host.isConnected) return;
    const notice = document.createElement('p');
    notice.className = 'paper-proposal-error';
    notice.textContent = tr('paperProposal.loadFailed', { message: apiFailure(error).message });
    host.replaceChildren(notice);
  };

  let proposal: PaperProposalV1;
  try {
    proposal = await deps.api.getProposal({ proposal_id: proposalId }, new AbortController().signal);
  } catch (error) {
    fail(error);
    return;
  }
  if (!host.isConnected) return;

  let busy = false;
  const decide = (action: 'accept' | 'reject', indexes?: number[]): void => {
    if (busy) return;
    busy = true;
    host.classList.add('paper-proposal-busy');
    host.querySelectorAll('button').forEach(button => { button.disabled = true; });
    const body = { proposal_id: proposalId, ...(indexes ? { indexes } : {}) };
    const pending = action === 'accept'
      ? deps.api.acceptProposal(body, new AbortController().signal)
      : deps.api.rejectProposal(body, new AbortController().signal);
    void pending
      .then(result => {
        if (!host.isConnected) return;
        renderCardBody(host, deps, result, decide);
        if (action === 'accept') {
          const results = 'results' in result && Array.isArray(result.results)
            ? result.results
            : [];
          const applied = results.some(row => row.ok)
            || result.edits.some(edit => edit.status === 'applied');
          if (applied) deps.onApplied?.();
        }
      })
      .catch(error => {
        if (!host.isConnected) return;
        host.classList.remove('paper-proposal-busy');
        host.querySelectorAll('button').forEach(button => { button.disabled = false; });
        const notice = document.createElement('p');
        notice.className = 'paper-proposal-error';
        notice.textContent = apiFailure(error).message;
        host.append(notice);
        window.setTimeout(() => notice.remove(), 4200);
      })
      .finally(() => { busy = false; });
  };
  renderCardBody(host, deps, proposal, decide);
}

const SKELETON_SELECTOR = `.paper-proposal[data-paper-proposal]:not([${MOUNTED_ATTR}])`;

/** Watch the chat timeline for proposal skeletons and hydrate them as they
 * mount. Returns the teardown. */
export function installPaperProposalObserver(
  deps: PaperProposalCardDeps,
  root: ParentNode = document,
): () => void {
  const mountAll = (scope: ParentNode): void => {
    if (scope instanceof Element && scope.matches(SKELETON_SELECTOR)) {
      void mountPaperProposalCard(scope as HTMLElement, deps);
    }
    scope.querySelectorAll?.(SKELETON_SELECTOR).forEach(element => {
      void mountPaperProposalCard(element as HTMLElement, deps);
    });
  };
  mountAll(root);
  const target = root instanceof Document
    ? (root.body ?? root.documentElement)
    : root;
  if (!target || typeof MutationObserver !== 'function') return () => undefined;
  const observer = new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType === 1) mountAll(node as Element);
      }
    }
  });
  observer.observe(target, { childList: true, subtree: true });
  return () => observer.disconnect();
}

/* Re-exported for the skeleton half and for tests; the presentation owner
 * renders this exact attribute contract. */
export const PAPER_PROPOSAL_SELECTOR = SKELETON_SELECTOR;
