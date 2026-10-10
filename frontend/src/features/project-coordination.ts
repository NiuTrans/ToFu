/** Project workspace entrance. Binding belongs to the server; this view never
 * dispatches workers or interprets prose as receipt/delivery evidence.
 * A drawer is ephemeral; navigation reuses the existing conversation canvas.
 */
import { createButton, createDrawer, createAlert, createEmptyState } from '../ui';
import { t } from '../i18n';

import type { CoordinationBinding, ProjectMessagePage } from '../api/project-coordination.generated';
import { createProjectMessageReceipts } from './project-message-receipts';
export interface CoordinationWork {
  id: string;
  conversationId?: string;
  title?: string;
  status: 'active' | 'completed' | 'failed' | 'cancelled';
  resultSummary?: string;
  changedPaths?: string[];
}
export interface ProjectCoordinationPorts {
  path: string;
  currentConversationId: string;
  currentPath(): string;

  currentConversation?(): string;
  binding(path: string): Promise<CoordinationBinding>;

  messages?(path: string, conversationId: string, beforeId: string): Promise<ProjectMessagePage>;
  board(path: string): Promise<{ active: CoordinationWork[]; recentOutcomes: CoordinationWork[] } | null>;
  bind(path: string, conversationId: string, expectedRevision: number): Promise<CoordinationBinding>;
  navigate(conversationId: string): void;
  openEvidence(): void;
}

export function projectCoordinationWorkGroups(active: readonly CoordinationWork[], recent: readonly CoordinationWork[]) {
  return {
    active: active.filter(item => item.status === 'active').slice(0, 100),
    recent: recent.filter(item => ['completed', 'failed', 'cancelled'].includes(item.status)).slice(0, 100),
  };
}

let activeDrawer: ReturnType<typeof createDrawer> | null = null;

export async function openProjectCoordination(ports: ProjectCoordinationPorts): Promise<void> {

  activeDrawer?.close();
  const root = document.createElement('div');
  root.className = 'ui-stack';
  const drawer = createDrawer({ title: t('projectCoordination.title'), body: root });

  activeDrawer = drawer;
  document.body.append(drawer.el);
  let alive = true;
  const closed = drawer.open().then(() => {
    alive = false; drawer.el.remove();
    if (activeDrawer === drawer) activeDrawer = null;
  });
  const text = (content: string, tag = 'p'): HTMLElement => {
    const node = document.createElement(tag);
    node.textContent = content;
    return node;
  };
  root.append(text(t('projectCoordination.loading')));
  const valid = () => alive && ports.currentPath().replace(/[/\\]+$/, '') === ports.path.replace(/[/\\]+$/, '')
    && (!ports.currentConversation || ports.currentConversation() === ports.currentConversationId);
  const navigate = (id: string) => {
    if (!valid()) { drawer.close(); return; }
    drawer.close();
    ports.navigate(id);
  };
  const error = (reason: unknown) => {
    if (!alive) return;
    root.replaceChildren(createAlert({ variant: 'error', title: t('projectCoordination.error'),
      body: reason instanceof Error ? reason.message : String(reason) }).el);
    root.append(createButton({ label: t('projectCoordination.retry'), onClick: () => { void load(); } }));
  };
  const render = (binding: CoordinationBinding, board: { active: CoordinationWork[]; recentOutcomes: CoordinationWork[] } | null) => {
    if (!valid()) { drawer.close(); return; }
    root.replaceChildren(text(ports.path, 'small'));
    const entry = document.createElement('section');
    entry.className = 'ui-card ui-stack';
    entry.append(text(t('projectCoordination.coordinator'), 'h3'));
    if (binding.availability === 'available') {
      entry.append(text(t('projectCoordination.sameCanvas')));
      entry.append(createButton({ label: t('projectCoordination.openCoordinator'), variant: 'primary',
        onClick: () => navigate(binding.conversationId) }));
    } else {
      entry.append(text(t(binding.availability === 'unavailable'
        ? 'projectCoordination.unavailable' : 'projectCoordination.unbound')));
    }
    if (ports.currentConversationId && binding.conversationId !== ports.currentConversationId) {
      const button = createButton({ label: t('projectCoordination.bindCurrent'), onClick: async () => {
        if (!valid()) { drawer.close(); return; }
        button.disabled = true;
        try {
          const updated = await ports.bind(ports.path, ports.currentConversationId, binding.revision);
          if (valid()) render(updated, board);
        } catch (reason) { error(reason); }
      } });
      entry.append(button);
    }
    root.append(entry);
    root.append(createAlert({ variant: 'info', title: t('projectCoordination.capabilityTitle'),
      body: t(binding.capabilities.steerSubagent
        ? 'projectCoordination.capabilityBodyChildMessages'
        : 'projectCoordination.capabilityBody') }).el);

    if (ports.messages && ports.currentConversationId) {
      root.append(createProjectMessageReceipts(
        (beforeId) => ports.messages!(ports.path, ports.currentConversationId, beforeId), valid));
    }
    if (!board) {
      root.append(createAlert({ variant: 'warning', title: t('projectCoordination.boardUnavailable'),
        body: t('projectCoordination.boardUnavailableBody') }).el);
    } else {
      const groups = projectCoordinationWorkGroups(board.active || [], board.recentOutcomes || []);
      for (const [key, rows] of [['active', groups.active], ['recent', groups.recent]] as const) {
        const section = document.createElement('section');
        section.className = 'ui-stack';
        section.append(text(t(`projectCoordination.${key}`), 'h3'));
        if (!rows.length) section.append(createEmptyState({ title: t('projectCoordination.empty') }));
        for (const work of rows) {
          const card = document.createElement('article');
          card.className = 'ui-card ui-stack ui-stack--tight';
          card.append(text(work.title || work.id, 'strong'));
          card.append(text(t(`projectCoordination.status.${work.status}`), 'small'));
          if (work.resultSummary) card.append(text(work.resultSummary));
          if (work.changedPaths?.length) card.append(text(work.changedPaths.slice(0, 5).join(' · '), 'small'));
          if (work.conversationId) card.append(createButton({ label: t('projectCoordination.openWorker'), size: 'sm',
            onClick: () => navigate(work.conversationId!) }));
          section.append(card);
        }
        root.append(section);
      }
    }
    root.append(text(t('projectCoordination.deliveryCaveat'), 'small'));
    root.append(createButton({ label: t('projectCoordination.evidence'), onClick: () => {
      if (!valid()) { drawer.close(); return; }
      drawer.close(); ports.openEvidence();
    } }));
  };
  async function load() {
    if (!valid()) { drawer.close(); return; }
    root.replaceChildren(text(t('projectCoordination.loading')));
    try {
      const [binding, board] = await Promise.all([ports.binding(ports.path), ports.board(ports.path)]);
      if (valid()) render(binding, board);
      else drawer.close();
    } catch (reason) {
      if (valid()) error(reason);
      else drawer.close();
    }
  }
  await load();
  await closed;
}
