/**
 * Turn avatar, header, actions and footer DOM projections. Reads typed route
 * and timing evidence; emits delegated intent datasets.
 * Entry points: initiatorRoleLabel, renderClassicTurnAvatar, renderTurnHeader,
 * renderTurnActions, renderTurnFooter.
 * Dependencies: conversation-view-model, turn-finish, ports, stable-dom,
 * icons, localized-copy.
 */
import { createButton } from '../../../ui';
import { appendTaskTimePresentations } from '../task-time-presentations';
import {
  type ConversationTurnAction,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import { presentTurnExecutionTiming } from '../../presentation/turn-finish';
import type { ClassicConversationRendererPorts } from './ports';
import {
  setManagedRichHtml,
  resetManagedHtml,
} from './stable-dom';
import {
  lineSvgIcon,
  actionIcon,
} from './icons';
import {
  textFor,
  formatDuration,
} from './localized-copy';

export function initiatorRoleLabel(
  origin: ConversationTurnViewModel['metadata']['origin'],
): string {
  const initiator = origin?.initiator;
  const initiatedLabels: Partial<Record<NonNullable<typeof initiator>, string>> = {
    autopilot: 'Autopilot', proactive: 'Proactive Agent', timer: 'Timer',
    brain: 'Project Brain', peer: 'Peer', operator: 'Operator',
    swarm: 'Auto-continued',
  };
  return initiator ? initiatedLabels[initiator] ?? '' : '';
}

function defaultRoleLabel(turn: ConversationTurnViewModel): string {
  const initiatedLabel = initiatorRoleLabel(turn.metadata.origin);
  if (initiatedLabel) return initiatedLabel;
  const labels: Record<ConversationTurnViewModel['actor'], string> = {
    human: 'You', assistant: 'Agent', planner: 'Planner', critic: 'Critic',
    virtual_user: 'Autopilot',
  };
  return labels[turn.actor];
}

export function renderClassicTurnAvatar(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-avatar message-avatar';
  node.setAttribute('aria-hidden', 'true');
  const html = ports.renderTurnAvatarHtml?.(turn) ?? '';
  if (html) {
    node.innerHTML = html;
    return;
  }
  const iconPaths: Record<ConversationTurnViewModel['actor'], ReadonlyArray<string>> = {
    human: ['M8 3.25a2.25 2.25 0 1 1 0 4.5 2.25 2.25 0 0 1 0-4.5Z',
      'M3.75 13c.35-2.1 1.8-3.25 4.25-3.25S11.9 10.9 12.25 13'],
    assistant: ['M8 2.5c.35 2.15 1.35 3.15 3.5 3.5-2.15.35-3.15 1.35-3.5 3.5C7.15 7.35 6.15 6.35 4 6c2.15-.35 3.15-1.35 4-3.5Z',
      'M12.25 9.75c.18 1.05.7 1.57 1.75 1.75-1.05.18-1.57.7-1.75 1.75-.18-1.05-.7-1.57-1.75-1.75 1.05-.18 1.57-.7 1.75-1.75Z'],
    planner: ['M8 2.75 13.25 8 8 13.25 2.75 8 8 2.75Z',
      'M5.75 8h4.5'],
    critic: ['M8 2.5 12.5 4v3.4c0 2.75-1.45 4.7-4.5 6.1-3.05-1.4-4.5-3.35-4.5-6.1V4L8 2.5Z',
      'm6.1 8 1.25 1.25L10.2 6.4'],
    virtual_user: ['M8 2.75 12.75 5.5v5L8 13.25 3.25 10.5v-5L8 2.75Z',
      'M5.75 8h4.5'],
  };
  node.replaceChildren(lineSvgIcon(node.ownerDocument, iconPaths[turn.actor]));
}

function orchestrationBadge(
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
  document: Document,
): HTMLElement | null {
  const metadata = turn.metadata.orchestration;
  let className = '';
  let label = '';
  if (turn.actor === 'planner') {
    className = 'ep-verdict-planner';
    label = textFor(ports, 'orchestration.plan', 'Plan');
  } else if (turn.actor === 'critic') {
    if (metadata?.approved) {
      className = 'ep-verdict-stop';
      label = textFor(ports, 'orchestration.approved', 'Approved');
    } else if (metadata?.stuck) {
      className = 'ep-verdict-stuck';
      label = textFor(ports, 'orchestration.stuck', 'Stuck');
    } else if (metadata?.nextPhase === 'planner') {
      className = 'ep-verdict-replan';
      label = textFor(ports, 'orchestration.replan', 'Replan');
    } else {
      className = 'ep-verdict-continue';
      label = textFor(ports, 'orchestration.iteration',
        `Iteration ${metadata?.iteration ?? ''}`, {
          n: metadata?.iteration ?? '',
        }).trim();
    }
  }
  if (!label) return null;
  const badge = document.createElement('span');
  badge.className = `ep-verdict-badge ${className}`;
  badge.textContent = label;
  return badge;
}

function defaultActionLabel(action: ConversationTurnAction): string {
  return action.charAt(0).toUpperCase() + action.slice(1);
}

function turnTimestampPresentation(
  timestamp: string | number,
  ports: ClassicConversationRendererPorts,
): { short: string; exact: string; dateTime: string } {
  const date = new Date(timestamp);
  const valid = Number.isFinite(date.getTime());
  const exact = ports.formatTimestamp?.(timestamp)
    ?? (valid ? date.toLocaleString() : String(timestamp ?? ''));
  return {
    short: valid ? date.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }) : exact,
    exact,
    dateTime: valid ? date.toISOString() : '',
  };
}

export function renderTurnHeader(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-header message-header';
  const role = node.ownerDocument.createElement('span');
  role.className = 'message-role';
  role.textContent = (ports.roleLabel ?? defaultRoleLabel)(turn);
  const time = node.ownerDocument.createElement('time');
  time.className = 'message-time';
  const timestamp = turn.source.projection.timestamp ?? turn.source.createdAt;
  const timestampPresentation = turnTimestampPresentation(timestamp, ports);
  time.textContent = timestampPresentation.short;
  if (timestampPresentation.dateTime) {
    time.dateTime = timestampPresentation.dateTime;
  }
  if (timestampPresentation.exact) {
    time.title = timestampPresentation.exact;
    time.setAttribute('aria-label', timestampPresentation.exact);
  }
  const badge = orchestrationBadge(turn, ports, node.ownerDocument);
  node.replaceChildren(role, time, ...(badge ? [badge] : []));
  appendTaskTimePresentations(node, turn);
}

export function renderTurnActions(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-actions message-actions';
  node.replaceChildren();
  for (const action of turn.actions ?? []) {
    const button = action.action === 'review'
      ? createButton({ label: '', variant: 'ghost', size: 'sm' }, node.ownerDocument)
      : node.ownerDocument.createElement('button');
    button.type = 'button';
    button.classList.add('msg-action-btn', `conversation-action--${action.action}`);
    if (action.action === 'inspect') {
      button.classList.add('ri-anchor');
      button.title = textFor(
        ports, 'ri.openTip', 'View how this reply was produced',
      );
    }
    button.dataset.conversationAction = action.action;
    if (action.operation) button.dataset.operation = action.operation;
    button.disabled = action.disabled;
    const label = (ports.actionLabel ?? defaultActionLabel)(
      action.action, turn, action,
    );
    const icon = actionIcon(node.ownerDocument, action.action);
    if (icon) button.appendChild(icon);
    const labelNode = node.ownerDocument.createElement('span');
    labelNode.className = 'msg-action-label';
    labelNode.textContent = label;
    button.appendChild(labelNode);
    button.setAttribute('aria-label', label);
    if (!button.title) button.title = label;
    node.appendChild(button);
  }
  node.hidden = node.childElementCount === 0;
}

export function renderTurnFooter(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-footer message-finish';
  const terminal = ['completed', 'interrupted', 'truncated', 'failed']
    .includes(turn.status);
  if (turn.actor === 'human' || !terminal) {
    node.replaceChildren();
    resetManagedHtml(node);
    node.hidden = true;
    return;
  }
  if (ports.renderTurnFooterHtml) {
    setManagedRichHtml(node, ports.renderTurnFooterHtml(turn));
    node.hidden = !node.childElementCount && !node.textContent?.trim();
    return;
  }
  resetManagedHtml(node);
  node.replaceChildren();
  const appendTag = (className: string, text: string): void => {
    if (!text) return;
    const tag = node.ownerDocument.createElement('span');
    tag.className = `finish-tag ${className}`;
    tag.textContent = text;
    node.appendChild(tag);
  };
  if (turn.finish && turn.finish.label !== 'Completed') {
    appendTag(`terminal-${turn.finish.tone}`,
      [turn.finish.label, turn.finish.detail].filter(Boolean).join(': '));
  }
  if (turn.metadata.translation.pending) {
    appendTag('translation-pending',
      textFor(ports, 'sidebar.translating', 'Translating…'));
  }
  appendTag('model-tag', turn.metadata.model ?? '');
  const routeSnapshot = turn.metadata.routeSnapshot;
  if (routeSnapshot?.provider_id) {
    appendTag('model-route', [
      routeSnapshot.provider_id,
      routeSnapshot.wire_model_id,
      routeSnapshot.connection_id,
    ].filter(Boolean).join(' · '));
  }
  const modelRoute = turn.metadata.orchestration?.modelRoute;
  if (modelRoute?.selectedModel && modelRoute.resolvedModel
      && modelRoute.selectedModel !== modelRoute.resolvedModel) {
    appendTag('warn model-route', textFor(
      ports,
      'finishInfo.modelRouteTag',
      'Routed {from} → {to}',
      { from: modelRoute.selectedModel, to: modelRoute.resolvedModel },
    ));
  }
  if (!turn.metadata.fallbackInTimeline
      && (turn.metadata.fallback?.model || turn.metadata.fallback?.reason)) {
    appendTag('fallback-tag', [
      turn.metadata.fallback.model,
      turn.metadata.fallback.reason,
    ].filter(Boolean).join(' · '));
  }
  const timing = presentTurnExecutionTiming(
    turn.source?.projection.timingTrace,
    Number(turn.source?.updatedAt) || 0,
    turn.taskId || undefined,
  );
  const settledAt = timing.settledAtMs;
  if (settledAt > 0) {
    const settledDate = new Date(settledAt);
    if (!Number.isNaN(settledDate.getTime())) {
      const clockText = settledDate.toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
      const durationText = timing.durationMs !== null
        ? formatDuration(timing.durationMs / 1000) : '';
      appendTag('timing',
        durationText ? `${clockText} · ${durationText}` : clockText);
    }
  }
  node.hidden = node.childElementCount === 0;
}
