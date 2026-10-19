/** Explicit shared-file metadata in the existing drawer, not a navigation tree.
 * Exact read replies must match id/revision/digest/path. Untrusted content is
 * rendered ONLY through textContent; no Markdown/HTML execution or inference.
 */
import { createAlert, createEmptyState } from '../ui';
import { t } from '../i18n';
import type { ProjectSharedFilesPage, ProjectSharedFilesRead, ProjectSharedFilesContent, ProjectSharedFilesEntry } from '../api/project-continuity.generated';
import { createContinuityReadFence, sameProjectPath, type ContinuityScope } from './project-continuity-scope';
import { createContinuityButton as createButton, showError, stack, text } from './project-continuity-view';

import type { ProjectDispatchPorts } from './project-dispatch';
import { createSharedFileCommands, type SharedFileCommandPorts } from './project-shared-file-commands';
export interface ProjectSharedFilesPorts extends Partial<SharedFileCommandPorts> {
  list(path: string, cursor?: string): Promise<ProjectSharedFilesPage>;
  read(path: string, body: ProjectSharedFilesRead): Promise<ProjectSharedFilesContent>;
}
export function sharedContentMatches(entry: ProjectSharedFilesEntry, content: ProjectSharedFilesContent): boolean {
  return content.trust === 'untrusted_data' && content.reference.id === entry.id
    && content.reference.revision === entry.revision && content.reference.digest === entry.digest
    && content.digest === entry.digest && content.path === entry.path;
}
export function createProjectSharedFiles(ports: ProjectSharedFilesPorts, scope: ContinuityScope, dispatch?: ProjectDispatchPorts): HTMLElement {
  const root = stack('section'); root.dataset.testid = 'project-shared-files';
  root.append(text(t('projectShared.title'), 'h3'));
  const status = stack(), entries = stack(), contentHost = stack(), paging = stack();
  root.append(status, entries, paging, contentHost);
  const fence = createContinuityReadFence(scope.valid), contentFence = createContinuityReadFence(scope.valid);
  let loading = false;

  const commands = ports.register && ports.refresh ? { register: ports.register, refresh: ports.refresh } : null;
  if (commands && dispatch) root.append(createSharedFileCommands(commands, dispatch, scope, () => { void load(); }));
  function render(page: ProjectSharedFilesPage) {
    entries.replaceChildren(); paging.replaceChildren();
    if (!page.entries.length) entries.append(createEmptyState({ title: t('projectShared.empty') }));
    for (const entry of page.entries.slice(0, 16)) {
      const card = stack('article', true); card.append(text(entry.path, 'strong'), text(entry.purpose));
      card.append(text(t(`projectShared.state.${entry.knowledgeState}`)), text(t(`projectShared.status.${entry.status}`)));
      card.append(text(t('projectShared.reference', { revision: entry.revision, digest: entry.digest }), 'small'));
      card.append(text(`${entry.source.kind} · ${entry.source.subject} · ${entry.source.assignmentId ?? '—'} · ${entry.checkpointSha ?? '—'}`, 'small'));
      const open = createButton({ label: t('projectShared.open'), disabled: entry.status !== 'current', onClick: async () => {
        if (!scope.valid() || entry.status !== 'current') return;
        const epoch = contentFence.begin(); open.disabled = true; contentHost.replaceChildren();
        try {
          const content = await ports.read(scope.path, { projectPath: scope.path, reference: { id: entry.id, revision: entry.revision, digest: entry.digest } });
          if (!contentFence.accepts(epoch)) return;
          if (!sharedContentMatches(entry, content)) throw new Error(t('projectShared.pinMismatch'));
          contentHost.append(createAlert({ variant: 'warning', title: t('projectShared.title'), body: t('projectShared.untrusted') }).el,
            text(content.path, 'strong'), text(content.content, 'pre'));
        } catch (reason) { if (contentFence.accepts(epoch)) showError(contentHost, reason); }
        finally { if (scope.valid()) open.disabled = false; }
      } }); card.append(open);
      if (commands && dispatch) card.append(createSharedFileCommands(commands, dispatch, scope, () => { void load(); }, entry));
      entries.append(card);
    }
    if (page.nextCursor) paging.append(createButton({ label: t('projectShared.next'), onClick: () => { void load(page.nextCursor!); } }));
  }
  async function load(cursor?: string) {
    if (!scope.valid() || loading) return;
    loading = true; const epoch = fence.begin(); contentFence.invalidate(); contentHost.replaceChildren();
    try {
      const page = await ports.list(scope.path, cursor);
      if (!fence.accepts(epoch)) return;
      if (!sameProjectPath(page.projectPath, scope.path)) throw new Error(t('projectDispatch.stale'));
      status.replaceChildren(); render(page);
    } catch (reason) { if (fence.accepts(epoch)) showError(status, reason); }
    finally { loading = false; }
  }
  root.append(createButton({ label: t('projectContinuity.refresh'), onClick: () => { void load(); } }));
  void load(); return root;
}
