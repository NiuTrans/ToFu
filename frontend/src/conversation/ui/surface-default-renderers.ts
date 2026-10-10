import { appendTaskTimePresentations } from './task-time-presentations';
/**
 * Text-safe fallback renderers for the conversation surface. Production rich
 * rendering is injected; no state, transport or scheduling.
 */
import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';
import { buildToolFallbackCard } from '../presentation/tool-fallback-presentation';
import { renderProgramExecutionCard } from './program-execution-card';

import { programChildParentCallId } from '../presentation/tool-execution-groups';

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value ?? '');
  }
}

export function renderDefaultBlock(
  node: HTMLElement,
  block: ConversationBlockViewModel,
): void {
  if (block.kind === 'program') {
    renderProgramExecutionCard(node, block.round);
    return;
  }
  node.replaceChildren();
  const document = node.ownerDocument;
  if (block.kind === 'text') {
    const body = document.createElement('div');
    body.className = 'conversation-block__markdown';
    body.textContent = block.displayMarkdown ?? block.markdown;
    node.appendChild(body);
    return;
  }
  if (block.kind === 'thinking') {
    const details = document.createElement('details');
    details.className = 'conversation-block__thinking';
    const summary = document.createElement('summary');
    summary.textContent = 'Thinking';
    const body = document.createElement('div');
    body.textContent = block.displayMarkdown ?? block.markdown;
    details.append(summary, body);
    node.appendChild(details);
    return;
  }
  if (block.kind === 'tool') {

    const parentId = programChildParentCallId(block.round);
    node.classList.toggle('ptool-parent-child', Boolean(parentId));
    if (parentId) node.dataset.parentToolCallId = parentId;
    else delete node.dataset.parentToolCallId;
    const status = typeof block.result?.status === 'string'
      ? block.result.status : '';
    const card = buildToolFallbackCard(document, {
      title: block.name || 'Tool',
      status,
      sections: [
        { label: 'Input', text: jsonText(block.input) },
        { label: 'Result', text: jsonText(block.result) },
      ],
    });
    node.appendChild(card);
    return;
  }
  if (block.kind === 'attachments') {
    const total = block.images.length + block.videos.length
      + block.pdfTexts.length + block.mediaAttachments.length
      + block.conversationReferences.length
      + block.replyQuotes.length;
    node.textContent = `${total} attachment${total === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'injections') {
    node.textContent = `${block.channel}: ${block.items.length}`;
    return;
  }
  if (block.kind === 'system-note') {
    node.textContent = block.text + (block.provenance ? `\n${jsonText(block.provenance)}` : '');
    return;
  }
  if (block.kind === 'system-note-group') {
    node.textContent = `${block.items.length} context injections\n`
      + block.items.map((item) => item.text + (item.provenance ? `\n${jsonText(item.provenance)}` : '')).join('\n')
      + (block.provenance ? `\n${jsonText(block.provenance)}` : '');
    return;
  }
  if (block.kind === 'file-changes') {
    node.textContent = `${block.count} file change${block.count === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'provenance') {
    const labels = [
      block.value.memoryPrefetch ? 'memory' : '',
      block.value.preferencesApplied ? 'preferences' : '',
      block.value.preferencesLearned?.length ? 'learned' : '',
      block.value.relatedConversations ? 'related' : '',
      block.value.mcpLoginHint ? 'login' : '',
      block.value.mcpToolsDelta ? 'mcp' : '',
      block.value.projectPathChange ? 'path' : '',
    ].filter(Boolean);
    node.textContent = labels.join(' · ');
    return;
  }
  if (block.kind === 'rolled-back') {
    const details = document.createElement('details');
    details.className = 'conversation-block__rolled-back';
    const summary = document.createElement('summary');
    summary.textContent = 'Rolled back';
    const pre = document.createElement('pre');
    pre.textContent = [block.value.thinking, block.value.content]
      .map((part) => String(part ?? '').trim())
      .filter(Boolean)
      .join('\n\n');
    details.append(summary, pre);
    node.appendChild(details);
    return;
  }
  if (block.kind === 'image-generation') {
    node.textContent = `${block.value.results.length} generated image result${
      block.value.results.length === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'proposed-plan') {
    const title = document.createElement('strong');
    title.textContent = 'Proposed Plan';
    const body = document.createElement('div');
    body.className = 'conversation-block__markdown';
    body.textContent = block.value.text;
    node.append(title, body);
    return;
  }
  if (block.kind === 'plan-execution') {
    const title = document.createElement('strong');
    title.textContent = block.value.contextMode === 'fresh'
      ? 'Executing plan · fresh task context'
      : 'Executing plan · current context';
    node.appendChild(title);
    return;
  }
  if (block.kind === 'artifacts') {
    node.textContent = `${block.artifacts.length} artifact${
      block.artifacts.length === 1 ? '' : 's'}`;
    return;
  }
  if (block.kind === 'autopilot-run-notice') {
    node.textContent = `Autopilot ended: ${block.value.reason}`;
    return;
  }
  if (block.kind === 'activity-event') {
    node.dataset.activityId = block.value.id;
    node.textContent = block.value.summary
      || [block.value.kind, block.value.status].filter(Boolean).join(' · ');
    return;
  }
  if (block.kind === 'live-status') {
    node.textContent = [block.value.label, block.value.detail]
      .filter(Boolean).join(' · ');
    return;
  }
}

export function renderDefaultTurnAvatar(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.className = 'conversation-turn-avatar message-avatar';
  node.setAttribute('aria-hidden', 'true');
  const paths: Record<ConversationTurnViewModel['actor'], string> = {
    human: 'M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5Z',
    assistant: 'M8 1.75 9.45 6.4 14 8l-4.55 1.6L8 14.25 6.55 9.6 2 8l4.55-1.6L8 1.75Z',
    planner: 'M8 2.25 13.75 8 8 13.75 2.25 8 8 2.25Z',
    critic: 'M8 1.75 13 3.8v3.7c0 3.1-2 5.45-5 6.75-3-1.3-5-3.65-5-6.75V3.8l5-2.05Z',
    virtual_user: 'M8 2.5a2.25 2.25 0 1 0 0 4.5 2.25 2.25 0 0 0 0-4.5Zm-4.25 10c.45-2.15 1.9-3.3 4.25-3.3s3.8 1.15 4.25 3.3',
  };
  const svg = node.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  const path = node.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', paths[turn.actor]);
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.35');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  node.replaceChildren(svg);
}

export function renderDefaultTurnHeader(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  const actor = node.ownerDocument.createElement('span');
  actor.className = 'conversation-turn__actor';
  actor.textContent = turn.actor;
  const status = node.ownerDocument.createElement('span');
  status.className = 'conversation-turn__status';
  status.textContent = turn.commandPending || turn.finish?.label || turn.status;
  node.append(actor, status);
  appendTaskTimePresentations(node, turn);
}

export function renderDefaultTurnActions(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  for (const action of turn.actions ?? []) {
    const button = node.ownerDocument.createElement('button');
    button.type = 'button';
    button.className = `msg-action-btn conversation-action--${action.action}`;
    button.dataset.conversationAction = action.action;
    if (action.operation) button.dataset.operation = action.operation;
    button.disabled = action.disabled;
    button.textContent = action.action;
    node.appendChild(button);
  }
}

export function renderDefaultTurnFooter(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
): void {
  node.replaceChildren();
  if (turn.actor === 'human' || !turn.finish) return;
  const status = node.ownerDocument.createElement('span');
  status.className = `conversation-finish conversation-finish--${turn.finish.tone}`;
  status.textContent = [turn.finish.label, turn.finish.detail]
    .filter(Boolean).join(': ');
  node.appendChild(status);
}

export function renderDefaultTurnContextRail(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'context' }> | null,
): void {
  node.className = 'conversation-turn-context-rail turn-ctx';
  node.replaceChildren();
  if (!block) {
    node.hidden = true;
    return;
  }
  const snapshot = block.value.snapshot;
  const summary = [snapshot.model, snapshot.depth]
    .filter((value): value is string => typeof value === 'string' && Boolean(value));
  node.textContent = summary.join(' · ');
  node.hidden = !node.textContent;
}

export function renderDefaultPlanDecision(
  node: HTMLElement,
  decision: PlanDecisionViewModel,
): void {
  node.textContent = decision.pending ? 'Starting plan…' : 'Plan ready';
}

export function renderDefaultLaneHeader(
  node: HTMLElement,
  lane: ConversationLaneViewModel,
): void {
  node.textContent = [lane.icon, lane.label || lane.title].filter(Boolean).join(' ');
}

export function renderDefaultQueueItem(
  node: HTMLElement,
  item: ConversationQueueItemViewModel,
): void {
  node.textContent = item.text;
}
