/**
 * Native block renderers that preserve the established chat visual language.
 * Markdown parsing/localization are injected pure ports; this module alone
 * applies their output to ConversationSurface-owned nodes.
 */
import type { TurnActivityEntry } from '../../api/conversation-sync.generated';
import type {
  ConversationBlockViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnAction,
  ConversationTurnActionViewModel,
  ConversationTurnViewModel,
} from '../presentation/conversation-view-model';
import type { ConversationSurfaceRenderers } from './conversation-surface';

export interface ClassicConversationMarkdownOptions {
  /** Human-authored input is plain text: neutralize raw '<tag>' HTML so it
   * renders literally instead of being swallowed as an unknown element. */
  escapeRawHtml?: boolean;
}
export interface ClassicConversationRendererPorts {
  renderSafeMarkdownHtml(
    markdown: string,
    options?: ClassicConversationMarkdownOptions,
  ): string;
  /** Transitional trusted mascot markup from the retained branding module. */
  renderTurnAvatarHtml?(turn: ConversationTurnViewModel): string;
  /**
   * Full-fidelity context fragments. The fold and rail intentionally have
   * different DOM homes; both strings must already be sanitized.
   */
  renderTurnContextParts?(
    block: Extract<ConversationBlockViewModel, { kind: 'context' }>,
  ): { fold: string; rail: string } | null;
  /** Transitional rich tool/program-timeline port; output is sanitized. */
  renderToolBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'tool' | 'program' }>,
    turn: ConversationTurnViewModel,
  ): string;
  /** Transitional injection-row port; output must already be sanitized. */
  renderInjectionBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'injections' }>,
    turn: ConversationTurnViewModel,
  ): string;
  /** Transitional full-fidelity finish bar; output must already be sanitized. */
  renderTurnFooterHtml?(turn: ConversationTurnViewModel): string;
  /** Transitional provenance strip; output must already be sanitized. */
  renderProvenanceBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'provenance' }>,
    turn: ConversationTurnViewModel,
  ): string;
  roleLabel?(turn: ConversationTurnViewModel): string;
  actionLabel?(
    action: ConversationTurnAction,
    turn: ConversationTurnViewModel,
    actionView?: ConversationTurnActionViewModel,
  ): string;
  formatTimestamp?(timestamp: string | number): string;
  resolveMediaUrl?(url: string): string;
  localizedText?(
    key: string,
    fallback: string,
    values?: Readonly<Record<string, string | number>>,
  ): string;
}

function initiatorRoleLabel(
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

function renderOriginBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'origin' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const origin = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--origin';
  node.replaceChildren();
  if (origin.initiator === 'peer' || origin.initiator === 'operator') {
    const banner = document.createElement('div');
    banner.className = `peer-msg-banner${
      origin.initiator === 'operator' ? ' peer-msg-banner-operator' : ''}`;
    const label = document.createElement('span');
    label.className = 'peer-msg-text';
    label.textContent = textFor(
      ports,
      origin.initiator === 'operator' ? 'peer.operatorBanner' : 'peer.messageBanner',
      origin.initiator === 'operator'
        ? 'Message from the project operator' : 'Message from a peer conversation',
    );
    if (origin.sourceConversationId) {
      const source = document.createElement('button');
      source.type = 'button';
      source.className = 'peer-msg-from conversation-origin-link';
      source.dataset.conversationAction = 'open-conversation';
      source.dataset.operation = origin.sourceConversationId;
      source.textContent = `conv ${origin.sourceConversationId.slice(0, 8)}`;
      label.append(' ', source);
    }
    banner.appendChild(label);
    node.appendChild(banner);
    return;
  }
  if (origin.initiator === 'brain' && origin.brain) {
    const card = document.createElement('div');
    card.className = 'brain-dispatch-card';
    const heading = document.createElement('div');
    heading.className = 'bdc-head';
    const title = document.createElement('span');
    title.className = 'bdc-title-label';
    title.textContent = textFor(ports, 'brain.dispatchTitle', 'Brain dispatch');
    heading.appendChild(title);
    if (origin.brain.answered) {
      const answered = document.createElement('span');
      answered.className = 'bdc-answered';
      answered.textContent = textFor(
        ports, 'brain.answeredChip', 'carries a human answer',
      );
      heading.appendChild(answered);
    }
    if (origin.brain.epicId) {
      const epicId = document.createElement('span');
      epicId.className = 'bdc-epic-id';
      epicId.textContent = origin.brain.epicId;
      heading.appendChild(epicId);
    }
    card.appendChild(heading);
    if (origin.brain.epicTitle) {
      const epic = document.createElement('button');
      epic.type = 'button';
      epic.className = 'bdc-epic-title conversation-origin-link';
      epic.dataset.conversationAction = 'open-project-brain';
      epic.textContent = origin.brain.epicTitle;
      card.appendChild(epic);
    }
    const facts = [
      ['brain.fromLabel', 'From', origin.brain.originatorTitle
        || origin.brain.originatorConv],
      ['brain.methodLabel', 'Method', origin.brain.method],
      ['brain.reasonLabel', 'Why me', origin.brain.route],
    ] as const;
    const metadata = document.createElement('div');
    metadata.className = 'bdc-meta';
    for (const [key, fallback, value] of facts) {
      if (!value) continue;
      const item = document.createElement('span');
      item.className = 'bdc-meta-item';
      const label = document.createElement('span');
      label.className = 'bdc-meta-label';
      label.textContent = textFor(ports, key, fallback);
      item.append(label, String(value));
      metadata.appendChild(item);
    }
    if (metadata.childElementCount) card.appendChild(metadata);
    node.appendChild(card);
    return;
  }
  if (origin.initiator !== 'human' && origin.initiator !== 'autopilot') {
    const banner = document.createElement('div');
    banner.className = 'proactive-banner';
    const label = document.createElement('span');
    label.className = 'pb-text';
    label.textContent = initiatorRoleLabel(origin);
    banner.appendChild(label);
    node.appendChild(banner);
  }
}

function buildContextNodes(
  document: Document,
  block: Extract<ConversationBlockViewModel, { kind: 'context' }>,
  ports: ClassicConversationRendererPorts,
): { fold: HTMLElement; rail: HTMLElement } {
  const snapshot = block.value.snapshot;
  const roots = Array.isArray(snapshot.roots) ? snapshot.roots : [];
  const tools = Array.isArray(snapshot.tools) ? snapshot.tools : [];
  const modes = Array.isArray(snapshot.modes) ? snapshot.modes : [];
  const model = typeof snapshot.model === 'string' ? snapshot.model : '';
  const depth = typeof snapshot.depth === 'string' ? snapshot.depth : '';
  const bits = [model, depth, ...modes.flatMap((item) => (
    item && typeof item === 'object' && typeof item.label === 'string'
      ? [item.label] : []
  )), tools.length
    ? textFor(ports, 'turnCtx.toolCount', '{count} tools', { count: tools.length })
    : '',
  roots.length
    ? textFor(ports, 'turnCtx.workspaceCount', '{count} workspaces', { count: roots.length })
    : ''].filter(Boolean);
  const fold = document.createElement('div');
  fold.className = 'tctx-fold';
  const dot = document.createElement('span');
  dot.className = 'tctx-fold-dot';
  const foldText = document.createElement('span');
  const foldLine = bits.join(' · ');
  foldText.textContent = foldLine;
  /* The fold truncates with ellipsis on tight panes; the title keeps the
   * full line one hover away. */
  fold.title = foldLine;
  fold.append(dot, foldText);
  const rail = document.createElement('aside');
  rail.className = 'turn-ctx';
  const head = document.createElement('div');
  head.className = 'tctx-head';
  for (const value of [model, depth]) {
    if (!value) continue;
    const chip = document.createElement('span');
    chip.className = value === model ? 'tctx-model' : 'tctx-depth';
    chip.textContent = value;
    head.appendChild(chip);
  }
  for (const mode of modes) {
    if (!mode || typeof mode !== 'object' || typeof mode.label !== 'string') continue;
    const chip = document.createElement('span');
    chip.className = 'tctx-mode-badge';
    chip.textContent = mode.label;
    head.appendChild(chip);
  }
  if (head.childElementCount) rail.appendChild(head);
  const appendRows = (
    labelText: string, values: ReadonlyArray<unknown>, className: string,
  ): void => {
    if (!values.length) return;
    const row = document.createElement('div');
    row.className = 'tctx-row';
    const label = document.createElement('span');
    label.className = 'tctx-row-h';
    label.textContent = labelText;
    const valuesNode = document.createElement('div');
    valuesNode.className = className;
    for (const value of values) {
      if (!value || typeof value !== 'object') continue;
      const record = value as Record<string, unknown>;
      const item = document.createElement('span');
      item.className = className === 'tctx-paths' ? 'tctx-path' : 'tctx-chip';
      item.textContent = String(record.label || record.short || record.path || '');
      if (record.path) item.title = String(record.path);
      valuesNode.appendChild(item);
    }
    row.append(label, valuesNode);
    rail.appendChild(row);
  };
  appendRows(textFor(ports, 'turnCtx.toolsLabel', 'Tools'), tools, 'tctx-chips');
  appendRows(textFor(ports, 'turnCtx.workspaceLabel', 'Workspace'), roots, 'tctx-paths');
  return { fold, rail };
}

function trustedPartInnerHtml(
  document: Document,
  html: string,
  wrapperClass: string,
): string {
  if (!html) return '';
  const holder = document.createElement('div');
  holder.innerHTML = html;
  const wrapper = holder.firstElementChild;
  return wrapper?.classList.contains(wrapperClass)
    ? wrapper.innerHTML : html;
}

function renderContextBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'context' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--context';
  const rendered = ports.renderTurnContextParts?.(block);
  if (rendered !== undefined) {
    node.innerHTML = rendered?.fold ?? '';
    node.hidden = !node.childElementCount && !node.textContent?.trim();
    return;
  }
  node.replaceChildren(buildContextNodes(node.ownerDocument, block, ports).fold);
  node.hidden = false;
}

function renderContextRail(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'context' }> | null,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-turn-context-rail turn-ctx';
  node.replaceChildren();
  if (!block) {
    node.hidden = true;
    return;
  }
  const rendered = ports.renderTurnContextParts?.(block);
  if (rendered !== undefined) {
    node.innerHTML = trustedPartInnerHtml(
      node.ownerDocument, rendered?.rail ?? '', 'turn-ctx',
    );
  } else {
    const rail = buildContextNodes(node.ownerDocument, block, ports).rail;
    while (rail.firstChild) node.appendChild(rail.firstChild);
  }
  node.hidden = !node.childElementCount && !node.textContent?.trim();
}

function renderClassicTurnAvatar(
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

function renderProposedPlanBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'proposed-plan' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--proposed-plan';
  const card = document.createElement('article');
  card.className = 'plan-card';
  const header = document.createElement('header');
  header.className = 'plan-card-head';
  const icon = document.createElement('span');
  icon.className = 'plan-card-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.appendChild(lineSvgIcon(document, [
    'M4 2.75h8a1 1 0 0 1 1 1v8.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-8.5a1 1 0 0 1 1-1Z',
    'M5.5 6h5M5.5 8.5h5M5.5 11h3',
  ]));
  const title = document.createElement('span');
  title.className = 'plan-card-title';
  title.dataset.i18n = 'plan.cardTitle';
  title.textContent = textFor(ports, 'plan.cardTitle', 'Proposed Plan');
  header.append(icon, title);
  if (block.translationPending) {
    const status = document.createElement('span');
    status.className = 'plan-card-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const statusDot = document.createElement('span');
    statusDot.className = 'plan-card-status-dot';
    statusDot.setAttribute('aria-hidden', 'true');
    const statusText = document.createElement('span');
    statusText.dataset.i18n = 'plan.translationPending';
    statusText.textContent = textFor(
      ports, 'plan.translationPending', 'Translating…',
    );
    status.append(statusDot, statusText);
    header.appendChild(status);
  }
  const body = document.createElement('div');
  body.className = 'plan-card-body md-content';
  body.dataset.translationState = block.translationStreaming
    ? 'streaming' : block.displayMode;
  body.innerHTML = ports.renderSafeMarkdownHtml(block.displayMarkdown);
  card.dataset.translationState = body.dataset.translationState;
  card.append(header, body);
  node.replaceChildren(card);
}

function renderPlanExecutionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'plan-execution' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--plan-execution';
  const details = document.createElement('details');
  details.className = 'plan-execution-card';
  const summary = document.createElement('summary');
  summary.className = 'plan-execution-head';
  const title = document.createElement('span');
  title.className = 'plan-execution-title';
  title.textContent = textFor(
    ports, 'plan.executionTitle', 'Executing accepted plan',
  );
  const context = document.createElement('span');
  context.className = 'plan-execution-context';
  context.textContent = block.value.contextMode === 'fresh'
    ? textFor(ports, 'plan.contextFresh', 'Fresh task context')
    : textFor(ports, 'plan.contextCurrent', 'Current context');
  summary.append(title, context);
  const body = document.createElement('div');
  body.className = 'plan-execution-body md-content';
  body.innerHTML = ports.renderSafeMarkdownHtml(block.value.planText);
  details.append(summary, body);
  node.replaceChildren(details);
}

interface CompactionAccounting {
  tokensBefore?: number;
  tokensAfter?: number;
  reductionPercent?: number;
  tokenCountKind?: string;
}

function compactionGlyph(document: Document): SVGSVGElement {
  /* Two rails converge through a narrow waist: a context boundary, not an
   * error/status glyph. The same mark connects manual cards and Turn events. */
  return lineSvgIcon(document, [
    'M2.5 3.25h3L8 6.5l2.5-3.25h3',
    'M2.5 12.75h3L8 9.5l2.5 3.25h3',
  ]);
}

function finiteCompactionCount(value: number | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.round(value) : null;
}

function formatCompactionCount(value: number, estimated: boolean): string {
  return `${estimated ? '≈' : ''}${value.toLocaleString()}`;
}

function buildCompactionTokenFlow(
  document: Document,
  accounting: CompactionAccounting,
  ports: ClassicConversationRendererPorts,
): HTMLElement | null {
  const before = finiteCompactionCount(accounting.tokensBefore);
  if (before == null) return null;
  const after = finiteCompactionCount(accounting.tokensAfter);
  const estimated = accounting.tokenCountKind !== 'exact';
  const explicitReduction = finiteCompactionCount(accounting.reductionPercent);
  const reduction = explicitReduction == null
    ? (after == null || before === 0
      ? null : Math.max(0, Math.min(100, Math.round((1 - after / before) * 100))))
    : Math.min(100, explicitReduction);
  const flow = document.createElement('span');
  flow.className = 'compaction-token-flow';
  if (after == null) flow.classList.add('is-pending');
  flow.dataset.tokenCountKind = estimated ? 'estimated' : 'exact';

  const stage = (
    className: string,
    labelKey: string,
    labelFallback: string,
    value: number | null,
  ): HTMLElement => {
    const node = document.createElement('span');
    node.className = `compaction-token-stage ${className}`;
    if (value != null) node.dataset.tokenCount = String(value);
    const label = document.createElement('span');
    label.className = 'compaction-token-label';
    label.textContent = textFor(ports, labelKey, labelFallback);
    const count = document.createElement('span');
    count.className = 'compaction-token-value';
    count.textContent = value == null
      ? textFor(ports, 'activity.compaction.pending', 'Calculating')
      : formatCompactionCount(value, estimated);
    node.append(label, count);
    return node;
  };

  const arrow = document.createElement('span');
  arrow.className = 'compaction-token-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  arrow.textContent = '→';
  const beforeStage = stage(
    'is-before', 'activity.compaction.before', 'Before', before,
  );
  const afterStage = stage(
    'is-after', 'activity.compaction.after', 'After', after,
  );
  flow.append(beforeStage, arrow, afterStage);
  if (reduction != null) {
    const saved = document.createElement('span');
    saved.className = 'compaction-token-saved';
    saved.textContent = textFor(
      ports, 'activity.compaction.saved', '{pct}% less', { pct: reduction },
    );
    flow.appendChild(saved);
  }
  const beforeLabel = textFor(ports, 'activity.compaction.before', 'Before');
  const afterLabel = textFor(ports, 'activity.compaction.after', 'After');
  flow.setAttribute('aria-label', [
    `${beforeLabel} ${formatCompactionCount(before, estimated)} tokens`,
    after == null
      ? `${afterLabel} ${textFor(ports, 'activity.compaction.pending', 'Calculating')}`
      : `${afterLabel} ${formatCompactionCount(after, estimated)} tokens`,
  ].join(', '));
  return flow;
}

function renderCompactionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'compaction' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const value = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--compaction';
  const details = document.createElement('details');
  details.className = 'compact-card';
  const summary = document.createElement('summary');
  summary.className = 'compact-card-head';
  const icon = document.createElement('span');
  icon.className = 'compact-card-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.appendChild(compactionGlyph(document));
  const title = document.createElement('span');
  title.className = 'compact-card-title';
  title.textContent = textFor(ports, 'compactCard.title', 'Context compacted');
  summary.append(icon, title);
  const tokenFlow = buildCompactionTokenFlow(document, value, ports);
  if (tokenFlow) summary.appendChild(tokenFlow);
  const toggle = document.createElement('span');
  toggle.className = 'compact-card-toggle';
  toggle.textContent = textFor(ports, 'compactCard.expand', 'Expand summary');
  summary.appendChild(toggle);
  const body = document.createElement('div');
  body.className = 'compact-card-body';
  const markdown = document.createElement('div');
  markdown.className = 'md-content';
  markdown.innerHTML = ports.renderSafeMarkdownHtml(
    block.summaryMarkdown.replace(/^##[^\n]*\n+/, ''),
  );
  body.appendChild(markdown);
  if (value.archiveId) {
    const view = document.createElement('button');
    view.type = 'button';
    view.className = 'compact-card-view';
    view.dataset.conversationAction = 'open-compaction';
    view.dataset.operation = value.archiveId;
    view.textContent = textFor(
      ports, 'compactCard.viewSnapshot', 'View pre-compaction snapshot',
    );
    body.appendChild(view);
  }
  details.append(summary, body);
  node.replaceChildren(details);
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

/* 5-char cap mirrors the knowledge panel's _knowledgeFileGlyph. */
function documentAttachmentGlyph(fileName: string): string {
  const extension = /\.([A-Za-z0-9]{1,5})$/.exec(fileName)?.[1];
  return extension ? extension.toUpperCase() : 'DOC';
}

type ManagedHtmlElement = HTMLElement & {
  _tofuRenderedHtml?: string;
};

interface InteractiveElementState {
  path: ReadonlyArray<number>;
  open?: boolean;
  expanded: boolean;
  collapsed: boolean;
  ariaExpanded?: string;
  scrollTop: number;
}

function directClassChild<T extends HTMLElement>(
  parent: HTMLElement,
  className: string,
): T | null {
  return (Array.from(parent.children).find(
    (child) => child.classList.contains(className),
  ) as T | undefined) ?? null;
}

function setManagedHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  node.innerHTML = html;
  managedNode._tofuRenderedHtml = html;
  return true;
}

function pathFromRoot(root: HTMLElement, target: Element): number[] | null {
  const path: number[] = [];
  let current: Element | null = target;
  while (current && current !== root) {
    const parentElement: HTMLElement | null = current.parentElement;
    if (!parentElement) return null;
    path.unshift(Array.prototype.indexOf.call(parentElement.children, current));
    current = parentElement;
  }
  return current === root ? path : null;
}

function elementAtPath(
  root: HTMLElement,
  path: ReadonlyArray<number>,
): HTMLElement | null {
  let current: Element = root;
  for (const index of path) {
    const child = current.children.item(index);
    if (!child) return null;
    current = child;
  }
  return current as HTMLElement;
}

function captureInteractiveElementStates(
  root: HTMLElement,
): InteractiveElementState[] {
  return Array.from(root.querySelectorAll<HTMLElement>(
    'details, [aria-expanded], .expanded, [data-collapsible="true"]',
  )).flatMap((element) => {
    const path = pathFromRoot(root, element);
    if (!path) return [];
    return [{
      path,
      ...(element.tagName === 'DETAILS'
        ? { open: (element as HTMLDetailsElement).open } : {}),
      expanded: element.classList.contains('expanded'),
      collapsed: element.classList.contains('collapsed'),
      ...(element.hasAttribute('aria-expanded')
        ? { ariaExpanded: element.getAttribute('aria-expanded') ?? 'false' }
        : {}),
      scrollTop: element.scrollTop,
    }];
  });
}

function restoreInteractiveElementStates(
  root: HTMLElement,
  states: ReadonlyArray<InteractiveElementState>,
): void {
  for (const state of states) {
    const element = elementAtPath(root, state.path);
    if (!element) continue;
    if (state.open !== undefined && element.tagName === 'DETAILS') {
      (element as HTMLDetailsElement).open = state.open;
    }
    if (state.expanded) element.classList.add('expanded');
    if (element.hasAttribute('data-collapsible')) {
      element.classList.toggle('collapsed', state.collapsed);
    }
    if (state.ariaExpanded !== undefined) {
      element.setAttribute('aria-expanded', state.ariaExpanded);
    }
    if (state.scrollTop > 0) element.scrollTop = state.scrollTop;
  }
}

const SWARM_READER_STATE_CLASSES = Object.freeze([
  'sw-collapsed', 'sw-a-open', 'sw-tl-open',
]);

type ReconciliationParent = Node & ParentNode;

function syncSwarmElementAttributes(
  current: Element,
  next: Element,
): void {
  for (const attribute of Array.from(current.attributes)) {
    if (attribute.name !== 'class' && !next.hasAttribute(attribute.name)) {
      current.removeAttribute(attribute.name);
    }
  }
  for (const attribute of Array.from(next.attributes)) {
    if (attribute.name !== 'class'
        && current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
    }
  }
  const nextClasses = new Set(
    (next.getAttribute('class') ?? '').split(/\s+/).filter(Boolean),
  );
  for (const className of SWARM_READER_STATE_CLASSES) {
    if (current.classList.contains(className)) nextClasses.add(className);
    else nextClasses.delete(className);
  }
  const className = Array.from(nextClasses).join(' ');
  if ((current.getAttribute('class') ?? '') !== className) {
    current.setAttribute('class', className);
  }
}

function reconcileSwarmNode(current: Node, next: Node): void {
  if (current.nodeType === 3 && next.nodeType === 3) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  const currentElement = current.nodeType === 1 ? current as Element : null;
  const nextElement = next.nodeType === 1 ? next as Element : null;
  if (current.nodeType !== next.nodeType
      || currentElement?.tagName !== nextElement?.tagName) {
    current.parentNode?.replaceChild(next.cloneNode(true), current);
    return;
  }
  if (!currentElement || !nextElement) return;
  syncSwarmElementAttributes(currentElement, nextElement);
  reconcileSwarmChildren(
    currentElement as ReconciliationParent,
    nextElement as ReconciliationParent,
  );
}

function reconcileSwarmChildren(
  currentParent: ReconciliationParent,
  nextParent: ReconciliationParent,
): void {
  const currentNodes = Array.from(currentParent.childNodes);
  const nextNodes = Array.from(nextParent.childNodes);
  for (let index = 0; index < nextNodes.length; index += 1) {
    const current = currentNodes[index];
    if (current) reconcileSwarmNode(current, nextNodes[index]);
    else currentParent.appendChild(nextNodes[index].cloneNode(true));
  }
  for (let index = currentNodes.length - 1; index >= nextNodes.length; index -= 1) {
    currentParent.removeChild(currentNodes[index]);
  }
}

/** Preserve live Swarm nodes so streaming updates do not restart animations. */
function reconcileSwarmRichHtml(node: HTMLElement, html: string): boolean {
  if (!node.querySelector('.sw-panel')) return false;
  const template = node.ownerDocument.createElement('template');
  template.innerHTML = html;
  if (!template.content.querySelector('.sw-panel')) return false;
  reconcileSwarmChildren(
    node as ReconciliationParent,
    template.content as ReconciliationParent,
  );
  return true;
}

function setManagedRichHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  const interactiveStates = captureInteractiveElementStates(node);
  const activeElement = node.ownerDocument.activeElement;
  const focusedPath = activeElement && node.contains(activeElement)
    ? pathFromRoot(node, activeElement)
    : null;
  if (!reconcileSwarmRichHtml(node, html)) node.innerHTML = html;
  managedNode._tofuRenderedHtml = html;
  restoreInteractiveElementStates(node, interactiveStates);
  const restoredFocus = focusedPath ? elementAtPath(node, focusedPath) : null;
  restoredFocus?.focus({ preventScroll: true });
  return true;
}

function resetManagedHtml(node: HTMLElement): void {
  delete (node as ManagedHtmlElement)._tofuRenderedHtml;
}

function disclosureChevron(document: Document, className: string): HTMLElement {
  const wrapper = document.createElement('span');
  wrapper.className = `${className} conversation-disclosure-chevron`;
  wrapper.setAttribute('aria-hidden', 'true');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'm5 6.5 3 3 3-3');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  wrapper.appendChild(svg);
  return wrapper;
}

function lineSvgIcon(
  document: Document,
  pathData: ReadonlyArray<string>,
): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  for (const data of pathData) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.35');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
  }
  return svg;
}

/* Lucide-style 24px stroke glyphs for the turn action bar. Unknown actions
 * render label-only so a new action never breaks the bar. */
const ACTION_ICON_PATHS: Record<string, ReadonlyArray<string>> = {
  copy: [
    'M10 8h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2z',
    'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2',
  ],
  inspect: [
    'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z',
    'M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
  ],
  edit: [
    'M12 20h9',
    'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  ],
  regenerate: [
    'M21 12a9 9 0 1 1-2.64-6.36L21 8',
    'M21 3v5h-5',
  ],
  resume: ['M6 4.5 19 12 6 19.5z'],
  translate: [
    'm5 8 6 6',
    'm4 14 6-6 2-3',
    'M2 5h12',
    'M7 2h1',
    'm22 22-5-10-5 10',
    'M14 18h6',
  ],
  export: [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'm7 10 5 5 5-5',
    'M12 15V3',
  ],
  'promote-decision': [
    'M9 18h6',
    'M10 22h4',
    'M8.5 14.5A7 7 0 1 1 15.5 14.5C14.5 15.3 14 16 14 18h-4c0-2-0.5-2.7-1.5-3.5z',
  ],
  branch: [
    'M6 3v12',
    'M18 6m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
    'M6 18m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
    'M18 9a9 9 0 0 1-9 9',
  ],
  delete: [
    'M3 6h18',
    'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2',
    'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6',
    'M10 11v6',
    'M14 11v6',
  ],
};

function actionIcon(
  document: Document,
  action: string,
): SVGSVGElement | null {
  const pathData = ACTION_ICON_PATHS[action];
  if (!pathData) return null;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('msg-action-icon');
  for (const data of pathData) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
  }
  return svg;
}

function renderTextBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'text' }>,
  ports: ClassicConversationRendererPorts,
  turn?: ConversationTurnViewModel,
): void {
  node.className = 'conversation-block conversation-block--text message-body';
  node.dataset.deliverable = String(block.deliverable);
  node.dataset.terminal = String(block.terminal);
  let body = directClassChild<HTMLElement>(node, 'md-content');
  if (!body) {
    body = node.ownerDocument.createElement('div');
    node.prepend(body);
  } else if (node.firstElementChild !== body) {
    node.prepend(body);
  }
  body.className = 'md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(
    block.displayMarkdown ?? block.markdown,
    turn?.actor === 'human' ? { escapeRawHtml: true } : undefined,
  ));
  renderTranslationAlternative(node, block, ports);
}

function renderThinkingBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'thinking' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--thinking';
  const document = node.ownerDocument;
  let details = directClassChild<HTMLDetailsElement>(node, 'thinking-block');
  if (!details) {
    details = document.createElement('details');
    details.open = !block.terminal;
    node.prepend(details);
  } else if (node.firstElementChild !== details) {
    node.prepend(details);
  }
  details.className = 'thinking-block';
  /* Streaming blocks are created open; once the reasoning closes (next round
   * starts or the turn settles) collapse them again so finished thinking
   * never towers over the answer. A complete block the reader opened
   * manually never re-transitions through 'active', so it stays put. */
  const wasActive = details.dataset.state === 'active';
  if (block.terminal && wasActive) details.open = false;
  details.dataset.state = block.terminal ? 'complete' : 'active';
  let summary = directClassChild<HTMLElement>(details, 'thinking-header');
  if (!summary) {
    summary = document.createElement('summary');
    details.prepend(summary);
  }
  summary.className = 'thinking-header';
  let stateDot = directClassChild<HTMLElement>(summary, 'thinking-state-dot');
  if (!stateDot) {
    stateDot = document.createElement('span');
    stateDot.className = 'thinking-state-dot';
    stateDot.setAttribute('aria-hidden', 'true');
    summary.prepend(stateDot);
  }
  let label = directClassChild<HTMLElement>(summary, 'thinking-label');
  if (!label) {
    label = document.createElement('span');
    summary.appendChild(label);
  }
  label.className = 'thinking-label';
  const labelText = block.terminal
    ? textFor(ports, 'stream.thinking.done', 'Thinking Process')
    : textFor(ports, 'stream.thinking.active', 'Thinking…');
  label.textContent = labelText;
  if (block.terminal) label.removeAttribute('aria-live');
  else label.setAttribute('aria-live', 'polite');
  let toggle = directClassChild<HTMLElement>(summary, 'thinking-toggle');
  if (!toggle) {
    toggle = disclosureChevron(document, 'thinking-toggle');
    summary.appendChild(toggle);
  } else if (!toggle.classList.contains('conversation-disclosure-chevron')) {
    const replacement = disclosureChevron(document, 'thinking-toggle');
    toggle.replaceWith(replacement);
    toggle = replacement;
  }
  let content = directClassChild<HTMLElement>(details, 'thinking-content');
  if (!content) {
    content = document.createElement('div');
    details.appendChild(content);
  }
  content.className = 'thinking-content';
  let body = directClassChild<HTMLElement>(content, 'thinking-text');
  if (!body) {
    body = document.createElement('div');
    content.appendChild(body);
  }
  body.className = 'thinking-text thinking-md md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(
    block.displayMarkdown ?? block.markdown,
  ));
  renderTranslationAlternative(node, block, ports);
}

function renderLiveStatusBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'live-status' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const value = block.value;
  node.className = 'conversation-block conversation-block--live-status';
  node.setAttribute('role', 'status');
  node.setAttribute('aria-live', 'polite');
  node.setAttribute('aria-atomic', 'true');
  const document = node.ownerDocument;
  const phase = value.phase || 'working';
  const phaseFallback = phase === 'executor_preparing'
    ? "Task accepted; binding it to this server's task scheduler…"
    : phase === 'executor_queued'
      ? "Waiting in this server's AI task queue; syncing queue position and "
        + 'available slots (not model/API quota)…'
      : phase === 'worker_starting'
        ? 'Server execution slot acquired; starting the task…'
        : '';
  const localizedDetail = value.detailKey
    ? textFor(ports, value.detailKey,
      value.detail || value.label || phaseFallback,
      value.detailArgs)
    : '';
  let label = localizedDetail || value.detail || value.label;
  if (phase === 'warming') {
    label = textFor(ports, 'autopilot.warming', 'Autopilot is starting…');
  } else if (phase === 'thinking_active' || phase === 'llm_thinking') {
    label = textFor(ports, 'stream.phase.reasoning', 'Reasoning…');
  } else if (phase === 'tool_exec' && value.tools?.length) {
    label = value.detail || value.tools.join(', ');
  } else if (phase === 'retrying' && !localizedDetail && !value.detail) {
    label = textFor(ports, 'stream.phase.retrying', 'Retrying…');
  } else if (phase === 'storage_wedged') {
    label = textFor(
      ports, 'stream.phase.storageWedged', 'Storage write stalled — retrying…',
    );
  } else if (phase === 'waiting' && !localizedDetail && !value.detail) {
    label = textFor(
      ports, 'stream.phase.waitingWorkerStatus',
      'No execution status yet — resynchronizing…',
    );
  } else if (phase === 'responding' && !value.detail) {
    label = textFor(ports, 'autopilot.warming', 'Autopilot is responding…');
  }
  const modelRoute = value.modelRoute;
  if (modelRoute?.selectedModel && modelRoute.resolvedModel
      && modelRoute.selectedModel !== modelRoute.resolvedModel
      && value.detailKey !== 'stream.phase.modelRouted') {
    const routeLabel = textFor(
      ports,
      'stream.phase.modelRouted',
      'Model routing: {from} → {to} ({role}, {tier})',
      {
        from: modelRoute.selectedModel,
        to: modelRoute.resolvedModel,
        role: modelRoute.role,
        tier: modelRoute.tier,
      },
    );
    label = label ? `${routeLabel} · ${label}` : routeLabel;
  }

  if (phase === 'warming') {
    const status = document.createElement('div');
    status.className = 'stream-status';
    const pulse = document.createElement('div');
    pulse.className = 'pulse';
    status.append(pulse, label);
    node.replaceChildren(status);
    return;
  }

  const row = document.createElement('div');
  row.className = `stream-phase${
    phase === 'retrying' ? ' stream-phase-retrying' : ''}${
    phase === 'storage_wedged' ? ' stream-phase-wedged' : ''}${
    phase === 'thinking_active' || phase === 'llm_thinking'
      ? ' stream-phase-thinking' : ''}`;
  const textNode = document.createElement('span');
  textNode.className = 'stream-phase-text';
  textNode.textContent = label;
  if ((phase === 'thinking_active' || phase === 'llm_thinking')
      && value.thinkingLength != null) {
    const counter = document.createElement('span');
    counter.className = 'stream-phase-counter';
    counter.textContent = textFor(
      ports, 'stream.phase.chars', `${value.thinkingLength} chars`,
      { n: value.thinkingLength },
    );
    textNode.appendChild(counter);
  }
  const dots = document.createElement('span');
  dots.className = 'stream-phase-dots';
  dots.append(
    document.createElement('span'),
    document.createElement('span'),
    document.createElement('span'),
  );
  for (const dot of Array.from(dots.children)) dot.textContent = '.';
  row.append(textNode, dots);
  node.replaceChildren(row);
}

function renderTranslationAlternative(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'text' | 'thinking' }>,
  ports: ClassicConversationRendererPorts,
): void {
  let details = directClassChild<HTMLDetailsElement>(node, 'bilingual-block');
  /* Only the deliverable answer carries the per-block original/translation
   * alternative. Mid-turn narration and thinking follow the turn-level
   * display mode — a per-block toggle there is timing-dependent noise. */
  if (block.kind !== 'text' || !block.deliverable
      || !block.translatedMarkdown) {
    details?.remove();
    return;
  }
  const alternative = block.displayMode === 'translated'
    ? block.markdown : block.translatedMarkdown;
  const alternativeMode = block.displayMode === 'translated'
    ? 'original' : 'translated';
  const document = node.ownerDocument;
  if (!details) {
    details = document.createElement('details');
    node.appendChild(details);
  }
  details.className = `bilingual-block bilingual-${alternativeMode}`;
  let summary = directClassChild<HTMLElement>(details, 'bilingual-header');
  if (!summary) {
    summary = document.createElement('summary');
    details.prepend(summary);
  }
  summary.className = 'bilingual-header';
  let label = directClassChild<HTMLElement>(summary, 'bilingual-label');
  if (!label) {
    label = document.createElement('span');
    summary.appendChild(label);
  }
  label.className = 'bilingual-label';
  label.textContent = alternativeMode === 'original'
    ? textFor(ports, 'chat.bilingualOriginal', 'Original')
    : textFor(ports, 'chat.bilingualTranslated', 'Translation');
  let toggle = directClassChild<HTMLElement>(summary, 'bilingual-toggle');
  if (!toggle) {
    toggle = disclosureChevron(document, 'bilingual-toggle');
    summary.appendChild(toggle);
  } else if (!toggle.classList.contains('conversation-disclosure-chevron')) {
    const replacement = disclosureChevron(document, 'bilingual-toggle');
    toggle.replaceWith(replacement);
  }
  let wrapper = directClassChild<HTMLElement>(details, 'bilingual-body');
  if (!wrapper) {
    wrapper = document.createElement('div');
    details.appendChild(wrapper);
  }
  wrapper.className = 'bilingual-body';
  let body = directClassChild<HTMLElement>(wrapper, 'md-content');
  if (!body) {
    body = document.createElement('div');
    wrapper.appendChild(body);
  }
  body.className = 'md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(alternative));
}

function displayJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value ?? '');
  }
}

function renderToolBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'tool' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--tool';
  const richHtml = ports.renderToolBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  if (node.dataset.rendererMode !== 'fallback') {
    node.replaceChildren();
    resetManagedHtml(node);
    node.dataset.rendererMode = 'fallback';
  }
  let details = directClassChild<HTMLDetailsElement>(node, 'conversation-tool');
  if (!details) {
    details = node.ownerDocument.createElement('details');
    details.className = 'conversation-tool';
    node.appendChild(details);
  }
  details.className = 'conversation-tool';
  let summary = directClassChild<HTMLElement>(details, 'conversation-tool__summary');
  if (!summary) {
    summary = node.ownerDocument.createElement('summary');
    summary.className = 'conversation-tool__summary';
    details.appendChild(summary);
  }
  const status = block.result.status || block.round?.status || '';
  summary.textContent = [block.name || 'Tool', status].filter(Boolean).join(' · ');
  let input = directClassChild<HTMLElement>(details, 'conversation-tool__input');
  if (!input) {
    input = node.ownerDocument.createElement('pre');
    details.appendChild(input);
  }
  input.className = 'conversation-tool__input';
  input.textContent = displayJson(block.input);
  let result = directClassChild<HTMLElement>(details, 'conversation-tool__result');
  if (!result) {
    result = node.ownerDocument.createElement('pre');
    details.appendChild(result);
  }
  result.className = 'conversation-tool__result';
  result.textContent = displayJson(block.result.content ?? block.result);
}

function renderProgramBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'program' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--program';
  const richHtml = ports.renderToolBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  const priorOpen = node.querySelector('details')?.open ?? false;
  node.dataset.rendererMode = 'fallback';
  node.replaceChildren();
  resetManagedHtml(node);
  const details = node.ownerDocument.createElement('details');
  details.className = 'conversation-tool conversation-program';
  details.open = priorOpen;
  const summary = node.ownerDocument.createElement('summary');
  summary.className = 'conversation-tool__summary';
  const title = ports.localizedText?.(
    'ptc.title', 'Program orchestration',
  ) ?? 'Program orchestration';
  const status = typeof block.round.programStatus === 'string'
    ? block.round.programStatus : block.round.status;
  summary.textContent = [title, status].filter(Boolean).join(' · ');
  const code = node.ownerDocument.createElement('pre');
  code.className = 'conversation-tool__input';
  code.textContent = String(block.round.programCode ?? '');
  const result = node.ownerDocument.createElement('pre');
  result.className = 'conversation-tool__result';
  result.textContent = displayJson(block.round.programResult);
  details.append(summary, code, result);
  node.appendChild(details);
}

function activityInterpolationArgs(
  value: Readonly<Record<string, unknown>> | undefined,
): Readonly<Record<string, string | number>> | undefined {
  if (!value) return undefined;
  const entries = Object.entries(value).flatMap(([key, item]) => (
    typeof item === 'string' || typeof item === 'number'
      ? [[key, item] as const] : []
  ));
  return entries.length ? Object.fromEntries(entries) : undefined;
}

function activityDuration(value: number | undefined): string {
  if (value == null || value < 0) return '';
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(value / 60_000);
  const seconds = Math.floor((value % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function activitySummaryWithoutDecorativeEmoji(value: string): string {
  const cleaned = value.replace(
    /^(?:(?:\p{Extended_Pictographic}|\uFE0F|\u200D|\u20E3)+\s*)+/u,
    '',
  ).trimStart();
  return cleaned || value;
}

function activityMarkerIcon(
  document: Document,
  entry: TurnActivityEntry,
): SVGSVGElement {
  if (entry.reasonCode === 'context_compaction' && entry.archiveId) {
    return compactionGlyph(document);
  }
  if (entry.severity === 'error' || entry.status === 'failed') {
    return lineSvgIcon(document, [
      'M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z',
      'm6 6 4 4M10 6l-4 4',
    ]);
  }
  if (entry.severity === 'warning'
      || ['switched', 'skipped', 'aborted'].includes(entry.status)) {
    return lineSvgIcon(document, [
      'M7.1 2.8 2.2 11.7a1 1 0 0 0 .88 1.5h9.84a1 1 0 0 0 .88-1.5L8.9 2.8a1.03 1.03 0 0 0-1.8 0Z',
      'M8 6v3.1M8 11.35h.01',
    ]);
  }
  if (entry.status === 'succeeded') {
    return lineSvgIcon(document, [
      'M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z',
      'm5.5 8 1.65 1.65L10.8 6',
    ]);
  }
  if (entry.status === 'running' || entry.status === 'waiting') {
    return lineSvgIcon(document, [
      'M13 8a5 5 0 1 1-1.46-3.54',
      'M11.5 2.75v2.2H9.3',
    ]);
  }
  return lineSvgIcon(document, [
    'M8 3a5 5 0 1 1 0 10A5 5 0 0 1 8 3Z',
    'M8 6.25v3.25M8 11.25h.01',
  ]);
}

function buildActivityEventRow(
  document: Document,
  entry: TurnActivityEntry,
  ports: ClassicConversationRendererPorts,
  terminalError?: Extract<ConversationBlockViewModel, {
    kind: 'activity-event'
  }>['terminalError'],
  previousOpen?: boolean,
): HTMLElement {
  const isCompaction = entry.reasonCode === 'context_compaction'
    && Boolean(entry.archiveId);
  const row = document.createElement(terminalError ? 'details' : 'div');
  row.className = `activity-event activity-event--${entry.kind} `
    + `activity-event--${entry.status} activity-event--${entry.severity}`
    + (entry.parentSpanId ? ' activity-event--nested' : '')
    + (isCompaction ? ' activity-event--compaction' : '')
    + (terminalError ? ' activity-event--terminal-error' : '');
  if (row.tagName === 'DETAILS') {
    (row as HTMLDetailsElement).open = previousOpen ?? true;
  }
  row.dataset.activityId = entry.id;
  row.dataset.activitySpanId = entry.spanId;
  const marker = document.createElement('span');
  marker.className = 'activity-event__marker';
  marker.setAttribute('aria-hidden', 'true');
  marker.appendChild(activityMarkerIcon(document, entry));

  const body = document.createElement('div');
  body.className = 'activity-event__body';
  const line = document.createElement('div');
  line.className = 'activity-event__line';
  const occurredAtDate = new Date(entry.occurredAt);
  const occurredAtText = ports.formatTimestamp?.(entry.occurredAt)
    || (Number.isFinite(occurredAtDate.getTime())
      ? occurredAtDate.toLocaleString() : '');
  if (occurredAtText) line.title = occurredAtText;
  const summary = document.createElement('span');
  summary.className = 'activity-event__summary';
  const localizedSummary = entry.summaryKey
    ? textFor(
      ports, entry.summaryKey, entry.summary || `${entry.kind} · ${entry.status}`,
      activityInterpolationArgs(entry.summaryArgs),
    )
    : entry.summary || `${entry.kind} · ${entry.status}`;
  summary.textContent = activitySummaryWithoutDecorativeEmoji(localizedSummary);
  line.appendChild(summary);
  const chips = document.createElement('span');
  chips.className = 'activity-event__chips';
  const duration = activityDuration(entry.durationMs);
  if (duration) {
    const durationNode = document.createElement('span');
    durationNode.className = 'activity-event__duration';
    durationNode.textContent = duration;
    chips.appendChild(durationNode);
  }
  if (entry.count > 1) {
    const repeated = document.createElement('span');
    repeated.className = 'activity-event__repeated';
    repeated.textContent = `×${entry.count}`;
    repeated.title = textFor(
      ports, 'activity.timeline.repeated', 'Repeated {count} times',
      { count: entry.count },
    );
    chips.appendChild(repeated);
  }
  if (isCompaction && entry.archiveId) {
    const inspect = document.createElement('button');
    inspect.type = 'button';
    inspect.className = 'activity-event__compaction-open';
    inspect.dataset.conversationAction = 'open-compaction';
    inspect.dataset.operation = entry.archiveId;
    inspect.textContent = textFor(
      ports, 'compactCard.viewSnapshot', 'View pre-compaction snapshot',
    );
    chips.appendChild(inspect);
  }
  if (terminalError) {
    chips.appendChild(disclosureChevron(document, 'activity-event__chevron'));
  }
  if (chips.childNodes.length) line.appendChild(chips);
  body.appendChild(line);
  if (isCompaction) {
    const tokenFlow = buildCompactionTokenFlow(document, entry, ports);
    if (tokenFlow) body.appendChild(tokenFlow);
  }
  if (entry.detail && !terminalError) {
    const detail = document.createElement('div');
    detail.className = 'activity-event__detail';
    detail.textContent = entry.detailKey
      ? textFor(
        ports, entry.detailKey, entry.detail,
        activityInterpolationArgs(entry.detailArgs),
      ) : entry.detail;
    body.appendChild(detail);
  }
  const summaryReasonKey = typeof entry.summaryArgs?.reasonKey === 'string'
    ? entry.summaryArgs.reasonKey : '';
  const reasonFact = isCompaction || entry.reasonCode === summaryReasonKey
    ? ''
    : entry.reasonCode?.startsWith('stream.retryReason.')
      ? textFor(ports, entry.reasonCode, entry.reasonCode)
      : entry.reasonCode;
  const triggerFact = isCompaction && entry.trigger
    ? entry.trigger.replaceAll('_', ' ') : '';
  const facts = [
    entry.model, entry.providerId, entry.statusCode
      ? `HTTP ${entry.statusCode}` : '', triggerFact, reasonFact,
  ].filter(Boolean);
  const appendFacts = (parent: HTMLElement): void => {
    if (!facts.length) return;
    const factsNode = document.createElement('div');
    factsNode.className = 'activity-event__facts';
    factsNode.textContent = facts.join(' · ');
    parent.appendChild(factsNode);
  };
  if (terminalError) {
    const summary = document.createElement('summary');
    summary.className = 'activity-event__terminal-summary';
    summary.append(marker, body);
    const content = document.createElement('div');
    content.className = 'activity-event__terminal-content';
    const error = document.createElement('pre');
    error.className = 'activity-event__terminal-envelope';
    try {
      error.textContent = JSON.stringify(terminalError, null, 2);
    } catch {
      error.textContent = terminalError.message;
    }
    content.appendChild(error);
    appendFacts(content);
    row.append(summary, content);
  } else {
    appendFacts(body);
    row.append(marker, body);
  }
  return row;
}

function renderActivityEventBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'activity-event' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const previousDetails = node.querySelector<HTMLDetailsElement>(
    ':scope > details.activity-event--terminal-error',
  );
  node.className = 'conversation-block conversation-block--activity-event';
  node.replaceChildren(
    buildActivityEventRow(
      node.ownerDocument,
      block.value,
      ports,
      block.terminalError,
      previousDetails?.open,
    ),
  );
}

function renderInjectionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'injections' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = `conversation-block conversation-block--injections conversation-injection--${
    block.channel}`;
  const richHtml = ports.renderInjectionBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  if (node.dataset.rendererMode !== 'fallback') {
    node.replaceChildren();
    resetManagedHtml(node);
    node.dataset.rendererMode = 'fallback';
  }
  const details = node.ownerDocument.createElement('details');
  details.className = 'conversation-injection-fallback';
  const summary = node.ownerDocument.createElement('summary');
  summary.textContent = `${block.channel} · ${block.items.length}`;
  const content = node.ownerDocument.createElement('pre');
  content.textContent = JSON.stringify(block.items[0] ?? {}, null, 2);
  details.append(summary, content);
  node.replaceChildren(details);
}

function renderFileChangesBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'file-changes' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  const previousDetails = node.querySelector<HTMLDetailsElement>(
    ':scope > details.conversation-file-changes',
  );
  node.className = 'conversation-block conversation-block--file-changes';
  const details = node.ownerDocument.createElement('details');
  details.className = `file-changes-bar conversation-file-changes${
    block.state === 'undone' ? ' fc-undone' : ''}`;
  details.open = previousDetails?.open ?? block.files.length <= 5;
  const summary = node.ownerDocument.createElement('summary');
  summary.className = 'fc-summary';
  const label = node.ownerDocument.createElement('span');
  label.className = 'fc-summary-text';
  const isUndone = block.state === 'undone';
  label.textContent = textFor(
    ports,
    isUndone ? 'fileChanges.undone' : 'fileChanges.filesChanged',
    `${block.count} file${block.count === 1 ? '' : 's'} ${
      isUndone ? 'restored' : 'changed'}`,
    { n: block.count, s: block.count === 1 ? '' : 's' },
  );
  const chevronIcon = disclosureChevron(node.ownerDocument, 'fc-chevron');
  summary.append(label, chevronIcon);
  const fileList = node.ownerDocument.createElement('div');
  fileList.className = 'fc-details';
  for (const file of block.files) {
    const path = typeof file.path === 'string' ? file.path : '';
    if (!path) continue;
    const row = node.ownerDocument.createElement('div');
    row.className = `fc-file${file.ok === false ? ' fc-file-err' : ''}${
      file.pending ? ' fc-file-pending' : ''}`;
    row.title = [file.root, path].filter(Boolean).join(':');
    const pathNode = node.ownerDocument.createElement('span');
    pathNode.className = 'fc-path';
    pathNode.textContent = `${file.root ? `${file.root}:` : ''}${path}`;
    const action = node.ownerDocument.createElement('span');
    action.className = 'fc-action';
    action.textContent = file.action || '';
    row.append(pathNode, action);
    fileList.appendChild(row);
  }
  const actions = node.ownerDocument.createElement('div');
  actions.className = 'fc-actions conversation-file-changes__actions';
  if (turn.status === 'completed' && block.commandAvailable) {
    const action = node.ownerDocument.createElement('button');
    action.type = 'button';
    action.addEventListener('click', (event) => {
      event.preventDefault();
    });
    if (block.state === 'applied') {
      action.className = 'fc-undo-btn';
      action.dataset.conversationAction = 'undo-turn-files';
      action.textContent = textFor(ports, 'fileChanges.undo', 'Undo');
      actions.append(action);
    } else if (block.state === 'undone') {
      action.className = 'fc-redo-btn';
      action.dataset.conversationAction = 'redo-turn-files';
      action.textContent = textFor(ports, 'fileChanges.redo', 'Redo');
      actions.append(action);
    } else if (block.state === 'undoing' || block.state === 'redoing') {
      action.className = 'fc-undo-btn';
      action.disabled = true;
      action.textContent = block.state === 'undoing' ? 'Undoing…' : 'Redoing…';
      actions.append(action);
    }
  }
  if (block.error != null) {
    const error = node.ownerDocument.createElement('span');
    error.className = 'conversation-file-changes__error';
    error.textContent = String(block.error);
    actions.append(error);
  }
  summary.append(actions);
  details.append(summary, fileList);
  node.replaceChildren(details);
}

function renderProvenanceBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'provenance' }>,
  turn: ConversationTurnViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--provenance';
  const richHtml = ports.renderProvenanceBlockHtml?.(block, turn) ?? '';
  if (richHtml) {
    node.dataset.rendererMode = 'rich';
    setManagedRichHtml(node, richHtml);
    return;
  }
  if (node.dataset.rendererMode !== 'fallback') {
    node.replaceChildren();
    resetManagedHtml(node);
    node.dataset.rendererMode = 'fallback';
  }
  const details = node.ownerDocument.createElement('details');
  details.className = 'turn-prov conversation-provenance-fallback';
  const summary = node.ownerDocument.createElement('summary');
  summary.textContent = textFor(ports, 'provenance.context', 'Turn context');
  const content = node.ownerDocument.createElement('pre');
  content.textContent = JSON.stringify(block.value, null, 2);
  details.append(summary, content);
  node.replaceChildren(details);
}

function renderRolledBackBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'rolled-back' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--rolled-back';
  const document = node.ownerDocument;
  const thinking = String(block.value.thinking ?? '').trim();
  const content = String(block.value.content ?? '').trim();
  const fragment = document.createDocumentFragment();
  if (thinking) {
    fragment.appendChild(rolledBackDisclosure(
      document, ports, 'thinking-prior',
      textFor(ports, 'rolledBack.thinking', 'Earlier Thinking (rolled back)'),
      thinking,
    ));
  }
  if (content) {
    fragment.appendChild(rolledBackDisclosure(
      document, ports, 'content-prior',
      textFor(ports, 'rolledBack.content', 'Interrupted Draft (rolled back)'),
      content,
    ));
  }
  node.replaceChildren(fragment);
}

/* Display-only disclosure for one rewound lane. Reuses the thinking-block
 * visual language; the -prior variants (dashed border, muted label) mark it
 * as history that will never rejoin the live stream. Collapsed by default. */
function rolledBackDisclosure(
  document: Document,
  ports: ClassicConversationRendererPorts,
  variantClass: string,
  labelText: string,
  markdown: string,
): HTMLDetailsElement {
  const details = document.createElement('details');
  details.className = `thinking-block ${variantClass}`;
  details.dataset.state = 'complete';
  const summary = document.createElement('summary');
  summary.className = 'thinking-header';
  const stateDot = document.createElement('span');
  stateDot.className = 'thinking-state-dot';
  stateDot.setAttribute('aria-hidden', 'true');
  const label = document.createElement('span');
  label.className = 'thinking-label';
  label.textContent = labelText;
  summary.append(stateDot, label, disclosureChevron(document, 'thinking-toggle'));
  const contentEl = document.createElement('div');
  contentEl.className = 'thinking-content';
  const body = document.createElement('div');
  body.className = 'thinking-text thinking-md md-content';
  setManagedHtml(body, ports.renderSafeMarkdownHtml(markdown));
  contentEl.appendChild(body);
  details.append(summary, contentEl);
  return details;
}
function safeMediaUrl(
  raw: string | undefined,
  ports: ClassicConversationRendererPorts,
): string {
  if (!raw) return '';
  const resolved = (ports.resolveMediaUrl?.(raw) ?? raw).trim();
  if (/^(?:https?:|blob:)/i.test(resolved)) return resolved;
  if (/^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(resolved)) {
    return resolved;
  }
  if (/^(?:\/|\.\.?\/)/.test(resolved)) return resolved;
  return '';
}

function compactFileSize(bytes: number | undefined): string {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function renderImageGenerationBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'image-generation' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const generation = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--image-generation message-body';
  const wrapper = document.createElement('div');
  wrapper.className = generation.mode === 'batch'
    ? 'ig-batch-wrapper' : 'ig-result-wrapper';
  if (generation.mode === 'batch' || generation.results.length !== 1) {
    const banner = document.createElement('div');
    banner.className = 'ig-batch-banner';
    const successes = generation.results.filter((result) => result.ok).length;
    banner.textContent = generation.status === 'running'
      ? textFor(ports, 'chat.igGenerating', 'Generating…')
      : `${generation.results.length} results · ${successes} succeeded`;
    wrapper.appendChild(banner);
  }
  const grid = document.createElement('div');
  grid.className = generation.mode === 'batch'
    ? `ig-batch-grid ig-cols-${Math.min(Math.max(generation.results.length, 1), 2)}`
    : 'ig-result-card-grid';
  generation.results.forEach((result, index) => {
    const slot = document.createElement('div');
    slot.className = 'ig-batch-slot';
    slot.dataset.slotIdx = String(index);
    const source = safeMediaUrl(result.imageUrl, ports);
    if (result.ok && source) {
      const card = document.createElement('div');
      card.className = 'ig-result-card';
      const preview = document.createElement('button');
      preview.type = 'button';
      preview.className = 'ig-result-preview';
      preview.dataset.conversationAction = 'preview-generated-image';
      preview.dataset.operation = String(index);
      preview.setAttribute('aria-label', result.prompt || 'Preview generated image');
      const image = document.createElement('img');
      image.src = source;
      image.alt = result.prompt || 'Generated image';
      image.loading = 'lazy';
      preview.appendChild(image);
      const footer = document.createElement('div');
      footer.className = 'ig-result-footer';
      const prompt = document.createElement('span');
      prompt.className = 'ig-result-prompt';
      prompt.textContent = result.prompt || result.model;
      prompt.title = result.prompt || '';
      const metadata = document.createElement('div');
      metadata.className = 'ig-result-meta';
      for (const value of [
        result.model,
        result.providerId ? `@${result.providerId}` : '',
        result.aspectRatio,
        compactFileSize(result.fileSize),
        result.elapsedSeconds == null ? '' : `${result.elapsedSeconds.toFixed(1)}s`,
      ].filter(Boolean)) {
        const pill = document.createElement('span');
        pill.className = 'ig-meta-pill';
        pill.textContent = String(value);
        metadata.appendChild(pill);
      }
      const actions = document.createElement('div');
      actions.className = 'ig-result-actions';
      const download = document.createElement('a');
      download.href = source;
      download.download = `generated-image-${index + 1}`;
      download.rel = 'noopener';
      download.title = 'Download';
      download.setAttribute('aria-label', 'Download');
      download.appendChild(lineSvgIcon(document, [
        'M8 2.5v7.25', 'm5.25 7 2.75 2.75L10.75 7', 'M3 13h10',
      ]));
      actions.appendChild(download);
      footer.append(prompt, metadata, actions);
      card.append(preview, footer);
      slot.appendChild(card);
    } else if (generation.status === 'running' || result.error === 'pending') {
      slot.classList.add('ig-generating', 'ig-batch-loading');
      const spinner = document.createElement('div');
      spinner.className = 'ig-gen-spinner';
      const title = document.createElement('div');
      title.className = 'ig-gen-title';
      title.textContent = result.model;
      const status = document.createElement('div');
      status.className = 'ig-gen-subtitle';
      status.textContent = textFor(ports, 'chat.igGenerating', 'Generating…');
      slot.append(spinner, title, status);
    } else {
      const error = document.createElement('div');
      error.className = `ig-batch-error ig-error-${result.errorType || 'generic'}`;
      const title = document.createElement('div');
      title.className = 'ig-error-title';
      title.textContent = result.model || 'Image generation';
      const detail = document.createElement('div');
      detail.className = 'ig-error-text';
      detail.textContent = result.error || 'Generation failed';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'ig-slot-retry-btn';
      retry.dataset.conversationAction = 'retry-image-generation';
      retry.dataset.operation = String(index);
      retry.textContent = textFor(ports, 'chat.retry', 'Retry');
      error.append(title, detail, retry);
      slot.appendChild(error);
    }
    grid.appendChild(slot);
  });
  if (!generation.results.length && generation.error != null) {
    const error = document.createElement('div');
    error.className = 'ig-batch-error ig-error-generic';
    error.textContent = typeof generation.error === 'string'
      ? generation.error : displayJson(generation.error);
    grid.appendChild(error);
  }
  wrapper.appendChild(grid);
  if (generation.status === 'running') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'ig-gen-cancel';
    cancel.dataset.conversationAction = 'cancel-image-generation';
    cancel.textContent = textFor(ports, 'chat.cancelGeneration', 'Cancel');
    wrapper.appendChild(cancel);
  }
  node.replaceChildren(wrapper);
}

function formatArtifactBytes(value: number | undefined): string {
  if (!Number.isFinite(value) || Number(value) <= 0) return '';
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderArtifactsBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'artifacts' }>,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--artifacts artifact-chip-row';
  const byPath = new Map<string, typeof block.artifacts[number]>();
  const withoutPath: typeof block.artifacts[number][] = [];
  for (const artifact of block.artifacts) {
    if (!artifact.sourcePath) {
      withoutPath.push(artifact);
      continue;
    }
    const previous = byPath.get(artifact.sourcePath);
    if (!previous || (artifact.version ?? 0) > (previous.version ?? 0)
        || ((artifact.version ?? 0) === (previous.version ?? 0)
          && (artifact.createdAt ?? 0) > (previous.createdAt ?? 0))) {
      byPath.set(artifact.sourcePath, artifact);
    }
  }
  const visible = [...withoutPath, ...byPath.values()];
  const chips = visible.map((artifact) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `artifact-chip artifact-chip-${artifact.format || 'doc'}`;
    button.dataset.conversationAction = 'open-artifact';
    button.dataset.operation = artifact.id;
    button.dataset.artifactId = artifact.id;
    button.title = `Open ${artifact.title}`;
    const main = document.createElement('span');
    main.className = 'artifact-chip-main';
    const title = document.createElement('span');
    title.className = 'artifact-chip-title';
    title.textContent = artifact.title;
    const meta = document.createElement('span');
    meta.className = 'artifact-chip-meta';
    const format = document.createElement('span');
    format.className = 'artifact-chip-fmt';
    format.textContent = artifact.format.toUpperCase();
    meta.appendChild(format);
    const size = formatArtifactBytes(artifact.sizeBytes);
    if (size) {
      const sizeNode = document.createElement('span');
      sizeNode.className = 'artifact-chip-size';
      sizeNode.textContent = size;
      meta.appendChild(sizeNode);
    }
    main.append(title, meta);
    button.appendChild(main);
    return button;
  });
  node.replaceChildren(...chips);
}

function textFor(
  ports: ClassicConversationRendererPorts,
  key: string,
  fallback: string,
  values?: Readonly<Record<string, string | number>>,
): string {
  const localized = ports.localizedText?.(key, fallback, values);
  if (localized != null) return localized;
  if (!values) return fallback;
  /* A port-less fallback must still be renderable — substitute the same
   * {placeholder} vocabulary translateMessage applies. */
  return fallback.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name] ?? '')
      : token
  ));
}

function renderAutopilotRunNoticeBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'autopilot-run-notice' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const labels: Record<typeof block.value.reason, readonly [string, string]> = {
    yielded_to_human: ['autopilot.endedYielded',
      'Autopilot stood down — you sent a message, so it stopped here'],
    aborted_mid_vu: ['autopilot.endedAborted',
      'Autopilot stopped — the turn was cancelled while it was working'],
    superseded: ['autopilot.endedSuperseded',
      'Autopilot stood down — a newer turn took over this conversation'],
    budget_exhausted: ['autopilot.endedBudget',
      'Autopilot stopped early — it hit its turn budget (needs review)'],
    no_progress: ['autopilot.endedNoProgress',
      'Autopilot stopped early — it stopped making progress (needs review)'],
    stuck: ['autopilot.endedStuck',
      'Autopilot stopped early — it was repeating itself (needs review)'],
  };
  const document = node.ownerDocument;
  const notice = document.createElement('div');
  notice.className = 'ap-run-notice';
  notice.dataset.apNotice = '1';
  notice.dataset.apRunId = block.value.runId;
  notice.dataset.apReason = block.value.reason;
  const label = document.createElement('span');
  label.className = 'ap-run-notice-label';
  const [key, fallback] = labels[block.value.reason];
  label.textContent = textFor(ports, key, fallback);
  notice.appendChild(label);
  if (block.value.unsent && block.value.content) {
    const details = document.createElement('details');
    details.className = 'ap-run-notice-unsent';
    const summary = document.createElement('summary');
    summary.textContent = textFor(
      ports,
      'autopilot.unsentReply',
      'This reply was written but never sent to the conversation',
    );
    const content = document.createElement('pre');
    content.className = 'ap-run-notice-text';
    content.textContent = block.value.content;
    details.append(summary, content);
    notice.appendChild(details);
  }
  node.className = 'conversation-block conversation-block--autopilot-run-notice';
  node.replaceChildren(notice);
}

function formatDuration(seconds: number | undefined): string {
  const rounded = Math.max(0, Math.round(seconds ?? 0));
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

function appendBadgeText(
  parent: HTMLElement,
  name: string,
  meta: string,
  iconText = '',
): void {
  if (iconText) {
    const icon = parent.ownerDocument.createElement('span');
    icon.className = 'reply-quote-badge-icon';
    icon.textContent = iconText;
    parent.appendChild(icon);
  }
  const info = parent.ownerDocument.createElement('span');
  info.className = 'reply-quote-badge-info';
  const title = parent.ownerDocument.createElement('span');
  title.className = 'reply-quote-badge-name';
  title.textContent = name;
  const metadata = parent.ownerDocument.createElement('span');
  metadata.className = 'reply-quote-badge-meta';
  metadata.textContent = meta;
  info.append(title, metadata);
  parent.appendChild(info);
}

function renderAttachmentsBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'attachments' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--attachments message-body';
  const document = node.ownerDocument;
  const children: HTMLElement[] = [];
  if (block.images.length) {
    const grid = document.createElement('div');
    grid.className = 'msg-image-grid';
    const sourceLabels: Readonly<Record<string, string>> = {
      clip_render: 'CLIP', vector_clip: 'VEC', page_render: 'SCAN',
      embedded: 'RAW', pixmap_fallback: 'PIX', pymupdf4llm: 'FIG',
      figure_page_render: 'FIG',
    };
    block.images.forEach((item, index) => {
      const source = safeMediaUrl(item.preview, ports);
      const interactive = Boolean(source && !source.endsWith('...'));
      const thumb = document.createElement(interactive ? 'button' : 'div');
      thumb.className = `msg-img-thumb conversation-attachment-button${
        item.pdfPage ? ' pdf-page' : ''}${interactive ? '' : ' placeholder'}`;
      if (interactive) {
        (thumb as HTMLButtonElement).type = 'button';
        thumb.dataset.conversationAction = 'preview-image';
        thumb.dataset.operation = String(index);
        thumb.setAttribute('aria-label', item.caption || 'Preview image');
      }
      if (item.caption) thumb.title = item.caption;
      if (interactive) {
        const image = document.createElement('img');
        image.src = source;
        image.alt = item.caption || 'Image attachment';
        image.width = 80;
        image.height = 80;
        image.loading = 'lazy';
        thumb.appendChild(image);
      } else {
        const placeholder = document.createElement('span');
        placeholder.className = 'msg-img-placeholder-icon';
        placeholder.textContent = 'IMG';
        thumb.appendChild(placeholder);
      }
      const sourceLabel = sourceLabels[item.pdfImageSource ?? '']
        || (item.pdfPage ? 'PDF' : '');
      if (sourceLabel) {
        const badge = document.createElement('span');
        badge.className = 'msg-img-badge';
        badge.textContent = sourceLabel;
        thumb.appendChild(badge);
      }
      const size = document.createElement('span');
      size.className = 'msg-img-size';
      size.textContent = item.pdfPage
        ? `P${item.pdfPage}/${item.pdfTotal ?? '?'} · ${item.sizeKB ?? '?'}KB`
        : `${item.sizeKB ?? '?'}KB`;
      thumb.appendChild(size);
      grid.appendChild(thumb);
    });
    children.push(grid);
  }
  const videoItems = [
    ...block.videos.map((item, index) => ({ item, legacyIndex: index })),
    ...block.mediaAttachments.filter((item) => item.kind === 'video')
      .map((item) => ({ item, legacyIndex: null })),
  ];
  if (videoItems.length) {
    const list = document.createElement('div');
    list.className = 'msg-video-list';
    videoItems.forEach(({ item, legacyIndex }) => {
      const unified = legacyIndex === null
        ? item as typeof block.mediaAttachments[number] : null;
      const legacy = legacyIndex === null
        ? null : item as typeof block.videos[number];
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'msg-video-card conversation-attachment-button';
      const videoUrl = safeMediaUrl(
        unified?.sourceUrl ?? legacy?.video_url, ports);
      if (videoUrl) {
        card.dataset.conversationAction = unified ? 'open-media' : 'open-video';
        card.dataset.operation = unified
          ? unified.attachmentId : String(legacyIndex);
      } else {
        card.disabled = true;
      }
      const itemName = unified?.name ?? legacy?.name ?? 'video';
      card.title = itemName;
      const thumb = document.createElement('span');
      thumb.className = 'msg-video-thumb';
      const poster = safeMediaUrl(
        unified?.previewUrl ?? legacy?.poster, ports);
      if (poster) {
        const image = document.createElement('img');
        image.src = poster;
        image.alt = `${itemName} poster`;
        image.width = 72;
        image.height = 44;
        image.loading = 'lazy';
        thumb.appendChild(image);
      }
      const play = document.createElement('span');
      play.className = 'msg-video-play';
      play.appendChild(lineSvgIcon(document, [
        'M5.5 3.75 12 8l-6.5 4.25v-8.5Z',
      ]));
      thumb.appendChild(play);
      const info = document.createElement('span');
      info.className = 'msg-video-info';
      const name = document.createElement('span');
      name.className = 'msg-video-name';
      name.textContent = itemName;
      const metadata = document.createElement('span');
      metadata.className = 'msg-video-meta';
      const frameCount = unified
        ? (unified.frameCount ?? 0)
        : (legacy?.frame_count ?? legacy?.frames?.length ?? 0);
      metadata.textContent = [
        (unified?.durationSeconds ?? legacy?.duration_s)
          ? formatDuration(unified?.durationSeconds ?? legacy?.duration_s ?? 0)
          : '',
        `${frameCount} ${textFor(ports, 'upload.videoFrames', 'frames')}`,
        unified
          ? unified.status
          : (legacy?.transcript
            ? textFor(ports, 'upload.videoTranscript', 'transcript') : ''),
      ].filter(Boolean).join(' · ');
      info.append(name, metadata);
      card.append(thumb, info);
      list.appendChild(card);
    });
    children.push(list);
  }
  const documentItems = [
    ...block.pdfTexts.map((item, index) => ({ item, legacyIndex: index })),
    ...block.mediaAttachments.filter((item) => item.kind === 'document')
      .map((item) => ({ item, legacyIndex: null })),
  ];
  if (documentItems.length) {
    const documents = document.createElement('div');
    documents.className = 'pdf-attachments-indicator';
    documentItems.forEach(({ item, legacyIndex }) => {
      const unified = legacyIndex === null
        ? item as typeof block.mediaAttachments[number] : null;
      const legacy = legacyIndex === null
        ? null : item as typeof block.pdfTexts[number];
      const badge = document.createElement('button');
      badge.type = 'button';
      badge.className = 'pdf-attach-badge conversation-attachment-button';
      badge.dataset.conversationAction = unified ? 'open-media' : 'preview-document';
      badge.dataset.operation = unified
        ? unified.attachmentId : String(legacyIndex);
      badge.title = unified?.name ?? legacy?.name ?? 'Document';
      const icon = document.createElement('span');
      icon.className = 'pdf-attach-icon';
      icon.textContent = documentAttachmentGlyph(unified?.name ?? legacy?.name ?? '');
      const info = document.createElement('span');
      info.className = 'pdf-attach-info';
      const name = document.createElement('span');
      name.className = 'pdf-attach-name';
      const fullName = unified?.name ?? legacy?.name ?? 'Document';
      name.textContent = fullName.length > 25 ? `${fullName.slice(0, 23)}…` : fullName;
      const metadata = document.createElement('span');
      metadata.className = 'pdf-attach-meta';
      const length = unified
        ? (unified.textChars ?? 0)
        : (legacy?.textLength ?? legacy?.text?.length ?? 0);
      const lengthText = length >= 1024 ? `${(length / 1024).toFixed(1)}KB`
        : `${length} chars`;
      metadata.textContent = [
        `${unified?.pages ?? legacy?.pages ?? '?'} pages`, lengthText,
        unified ? unified.status : (legacy?.isScanned ? 'scanned' : ''),
        (unified?.method ?? legacy?.method) === 'vlm' ? 'VLM' : '',
      ].filter(Boolean).join(' · ');
      info.append(name, metadata);
      badge.append(icon, info);
      documents.appendChild(badge);
    });
    children.push(documents);
  }
  for (const quote of block.replyQuotes) {
    const badge = document.createElement('div');
    badge.className = 'reply-quote-badge';
    const preview = quote.replace(/\s+/g, ' ').slice(0, 80);
    badge.title = quote.slice(0, 300);
    const lines = quote.split('\n').length;
    appendBadgeText(badge, `${preview}${quote.length > 80 ? '…' : ''}`,
      `${quote.length} chars · ${lines} line${lines === 1 ? '' : 's'}`);
    children.push(badge);
  }
  for (const reference of block.conversationReferences) {
    const badge = document.createElement('div');
    badge.className = 'reply-quote-badge conv-ref-badge';
    const title = reference.title || reference.id || 'Conversation';
    badge.title = title;
    appendBadgeText(badge, title,
      textFor(ports, 'chat.convRefMeta', 'Conversation reference'), '@');
    children.push(badge);
  }
  node.replaceChildren(...children);
}

function renderBranchLaneHeader(
  node: HTMLElement,
  lane: Parameters<NonNullable<ConversationSurfaceRenderers['renderLaneHeader']>>[1],
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-lane-header branch-panel-header';
  node.replaceChildren();
  const toggle = node.ownerDocument.createElement('button');
  toggle.type = 'button';
  toggle.className = 'branch-panel-title conversation-branch-toggle';
  toggle.dataset.conversationAction = 'toggle-branch';
  toggle.setAttribute('aria-expanded', String(Boolean(lane.expanded)));
  const icon = node.ownerDocument.createElement('span');
  icon.className = 'branch-panel-icon';
  icon.textContent = lane.icon || '';
  const title = node.ownerDocument.createElement('span');
  title.className = 'branch-node-label';
  title.textContent = lane.title;
  const count = node.ownerDocument.createElement('span');
  count.className = 'branch-panel-count';
  count.textContent = textFor(ports, 'branch.userTurns',
    `${lane.humanTurnCount} user turns`, { n: lane.humanTurnCount });
  toggle.append(icon, title, count);
  node.appendChild(toggle);
  if (lane.live) {
    const stop = node.ownerDocument.createElement('button');
    stop.type = 'button';
    stop.className = 'branch-panel-stop';
    stop.dataset.conversationAction = 'stop-branch';
    stop.textContent = textFor(ports, 'branch.stop', 'Stop');
    node.appendChild(stop);
  }
  const remove = node.ownerDocument.createElement('button');
  remove.type = 'button';
  remove.className = 'branch-panel-delete';
  remove.dataset.conversationAction = 'delete-branch';
  remove.setAttribute('aria-label', textFor(
    ports, 'branch.deleteBranch', 'Delete branch',
  ));
  remove.textContent = textFor(ports, 'branch.delete', 'Delete');
  node.appendChild(remove);
}

/* Clock glyph — static SVG, no emoji/unicode icons (CLAUDE.md §3.4). */
const QUEUE_ITEM_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';

function renderQueueStatusBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'queue-status' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--queue-status';
  node.dataset.queueId = block.value.queueId;
  const status = node.ownerDocument.createElement('div');
  status.className = 'conversation-queue-status';
  const icon = node.ownerDocument.createElement('span');
  icon.className = 'queue-item-icon';
  icon.innerHTML = QUEUE_ITEM_ICON_SVG;
  const label = node.ownerDocument.createElement('span');
  label.className = 'conversation-queue-status__label';
  label.textContent = `${textFor(ports, 'sidebar.queued', 'Queued')} #${
    block.value.position}`;
  const cancel = node.ownerDocument.createElement('button');
  cancel.type = 'button';
  cancel.className = 'msg-action-btn queue-item-cancel';
  cancel.dataset.conversationAction = 'remove-queue';
  cancel.textContent = textFor(ports, 'queue.cancelMsg', 'Cancel');
  status.append(icon, label, cancel);
  node.replaceChildren(status);
}

function renderNativeQueueItem(
  node: HTMLElement,
  item: ConversationQueueItemViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = `conversation-queue-item message user-msg qsrc-${
    item.source.isPeerMessage
      ? (item.source.isPeerHuman ? 'operator' : 'agent')
      : (item.kind === 'workflow_step' ? 'workflow' : 'own')}`;
  const document = node.ownerDocument;
  const header = document.createElement('header');
  header.className = 'message-header conversation-queue-item__header';
  const icon = document.createElement('span');
  icon.className = 'queue-item-icon';
  icon.innerHTML = QUEUE_ITEM_ICON_SVG;
  const label = document.createElement('span');
  label.className = 'message-role';
  label.textContent = textFor(ports, 'sidebar.queued', 'Queued');
  const number = document.createElement('span');
  number.className = 'queue-item-number';
  number.textContent = `#${item.position}`;
  header.append(icon, label, number);
  if (item.source.isPeerMessage && item.source.fromConv) {
    const source = document.createElement('span');
    source.className = 'queue-item-src queue-item-src-static';
    source.textContent = `${textFor(
      ports,
      item.source.isPeerHuman ? 'queue.fromOperator' : 'queue.fromConv',
      item.source.isPeerHuman ? 'from operator' : 'from',
    )} ${item.source.fromConv}`;
    header.appendChild(source);
  }
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'msg-action-btn queue-item-cancel';
  cancel.dataset.conversationAction = 'remove-queue';
  cancel.textContent = textFor(ports, 'queue.cancelMsg', 'Cancel');
  header.appendChild(cancel);
  const body = document.createElement('div');
  body.className = 'message-body conversation-queue-item__body';
  body.textContent = item.text || textFor(ports, 'queue.attachment', 'Attachment');
  if (item.text) body.title = item.text;
  const attachmentKinds = [
    item.source.hasImages ? 'img' : '',
    item.source.hasPdfs ? 'pdf' : '',
    item.source.hasAttachments ? 'media' : '',
    item.source.hasRefs ? 'ref' : '',
    item.source.hasQuotes ? 'quote' : '',
  ].filter(Boolean);
  if (attachmentKinds.length) {
    const attachments = document.createElement('span');
    attachments.className = 'queue-item-attachments';
    attachments.textContent = attachmentKinds.join(' · ');
    body.appendChild(attachments);
  }
  node.replaceChildren(header, body);
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

export function createClassicConversationRenderers(
  ports: ClassicConversationRendererPorts,
): Pick<
  ConversationSurfaceRenderers,
  'renderTurnAvatar' | 'renderBlock' | 'renderTurnHeader' | 'renderTurnActions'
    | 'renderTurnFooter' | 'renderTurnContextRail' | 'renderLaneHeader'
    | 'renderQueueItem'
> {
  return {
    renderTurnAvatar(node, turn) {
      renderClassicTurnAvatar(node, turn, ports);
    },
    renderBlock(node, block, context) {
      if (block.kind === 'text') {
        renderTextBlock(node, block, ports, context.turn);
      } else if (block.kind === 'thinking') {
        renderThinkingBlock(node, block, ports);
      } else if (block.kind === 'tool') {
        renderToolBlock(node, block, context.turn, ports);
      } else if (block.kind === 'program') {
        renderProgramBlock(node, block, context.turn, ports);
      } else if (block.kind === 'attachments') {
        renderAttachmentsBlock(node, block, ports);
      } else if (block.kind === 'injections') {
        renderInjectionBlock(node, block, context.turn, ports);
      } else if (block.kind === 'file-changes') {
        renderFileChangesBlock(node, block, context.turn, ports);
      } else if (block.kind === 'provenance') {
        renderProvenanceBlock(node, block, context.turn, ports);
      } else if (block.kind === 'origin') {
        renderOriginBlock(node, block, ports);
      } else if (block.kind === 'context') {
        renderContextBlock(node, block, ports);
      } else if (block.kind === 'compaction') {
        renderCompactionBlock(node, block, ports);
      } else if (block.kind === 'rolled-back') {
        renderRolledBackBlock(node, block, ports);
      } else if (block.kind === 'image-generation') {
        renderImageGenerationBlock(node, block, ports);
      } else if (block.kind === 'proposed-plan') {
        renderProposedPlanBlock(node, block, ports);
      } else if (block.kind === 'plan-execution') {
        renderPlanExecutionBlock(node, block, ports);
      } else if (block.kind === 'artifacts') {
        renderArtifactsBlock(node, block);
      } else if (block.kind === 'autopilot-run-notice') {
        renderAutopilotRunNoticeBlock(node, block, ports);
      } else if (block.kind === 'activity-event') {
        renderActivityEventBlock(node, block, ports);
      } else if (block.kind === 'queue-status') {
        renderQueueStatusBlock(node, block, ports);
      } else if (block.kind === 'live-status') {
        renderLiveStatusBlock(node, block, ports);
      } else {
        /* Exhaustive fallback for a mismatched generated/runtime contract. */
        node.textContent = '';
      }
    },
    renderTurnHeader(node, turn) {
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
    },
    renderTurnActions(node, turn) {
      node.className = 'conversation-turn-actions message-actions';
      node.replaceChildren();
      for (const action of turn.actions ?? []) {
        const button = node.ownerDocument.createElement('button');
        button.type = 'button';
        button.className = `msg-action-btn conversation-action--${action.action}`;
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
        node.appendChild(button);
      }
      node.hidden = node.childElementCount === 0;
    },
    renderTurnFooter(node, turn) {
      node.className = 'conversation-turn-footer message-finish';
      node.replaceChildren();
      const terminal = ['completed', 'interrupted', 'truncated', 'failed']
        .includes(turn.status);
      if (turn.actor === 'human' || !terminal) {
        node.hidden = true;
        return;
      }
      if (ports.renderTurnFooterHtml) {
        setManagedRichHtml(node, ports.renderTurnFooterHtml(turn));
        node.hidden = !node.childElementCount && !node.textContent?.trim();
        return;
      }
      resetManagedHtml(node);
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
      const settledAt = Number(turn.source?.updatedAt) || 0;
      const createdAt = Number(turn.source?.createdAt) || 0;
      if (settledAt > 0) {
        const settledDate = new Date(settledAt);
        if (!Number.isNaN(settledDate.getTime())) {
          const clockText = settledDate.toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
          });
          const durationText = createdAt > 0 && settledAt >= createdAt
            ? formatDuration((settledAt - createdAt) / 1000) : '';
          appendTag('timing',
            durationText ? `${clockText} · ${durationText}` : clockText);
        }
      }
      node.hidden = node.childElementCount === 0;
    },
    renderTurnContextRail(node, block) {
      renderContextRail(node, block, ports);
    },
    renderLaneHeader(node, lane) {
      renderBranchLaneHeader(node, lane, ports);
    },
    renderQueueItem(node, item) {
      renderNativeQueueItem(node, item, ports);
    },
  };
}
