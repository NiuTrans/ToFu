/**
 * Origin and context fold/rail DOM projections. Reads typed metadata and
 * injected safe fragments; no durable state.
 * Entry points: renderOriginBlock, renderContextBlock, renderContextRail.
 * Dependencies: conversation-view-model, ports, localized-copy, turn-chrome.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';
import { initiatorRoleLabel } from './turn-chrome';

export function renderOriginBlock(
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
  const git = (snapshot.git && typeof snapshot.git === 'object')
    ? snapshot.git as Record<string, unknown> : null;
  const todos = Array.isArray(snapshot.todos) ? snapshot.todos : [];
  const doneCount = todos.filter((x) => (
    x && typeof x === 'object' && (x as Record<string, unknown>).status === 'completed'
  )).length;
  const bits = [model, depth, ...modes.flatMap((item) => (
    item && typeof item === 'object' && typeof item.label === 'string'
      ? [item.label] : []
  )), tools.length
    ? textFor(ports, 'turnCtx.toolCount', '{count} tools', { count: tools.length })
    : '',
  roots.length
    ? textFor(ports, 'turnCtx.workspaceCount', '{count} workspaces', { count: roots.length })
    : '',
  git && typeof git.branch === 'string' && git.branch ? `⑂ ${git.branch}` : '',
  todos.length
    ? textFor(ports, 'turnCtx.todoCount', '{done}/{total} todos', { done: doneCount, total: todos.length })
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
  /* Git branch row — one chip per root branch (multi-root workspaces can
   * straddle repos on different branches; label each chip with its root). */
  if (git && typeof git.branch === 'string' && git.branch) {
    const branchRows = Array.isArray(git.branches) && git.branches.length
      ? git.branches as ReadonlyArray<Record<string, unknown>>
      : [{ path: '', short: '', branch: git.branch }];
    const row = document.createElement('div');
    row.className = 'tctx-row tctx-git-row';
    /* The live patch path (info-rail.js _patchLiveCtxRails) locates this row
     * by [data-tctx-git] to rebuild it in place on a mid-turn branch switch.
     * Without the key it misses a rail rendered through THIS path and
     * appends a SECOND branch row — the duplicate-branch bug. */
    row.setAttribute('data-tctx-git', '1');
    const label = document.createElement('span');
    label.className = 'tctx-row-h';
    label.textContent = textFor(ports, 'turnCtx.branchLabel', 'Branch');
    const chips = document.createElement('div');
    chips.className = 'tctx-chips';
    const multi = branchRows.length > 1;
    for (const entry of branchRows) {
      const branch = String(entry.branch || '').trim();
      if (!branch) continue;
      const chip = document.createElement('span');
      chip.className = 'tctx-chip tctx-tone-git';
      chip.textContent = branch;
      if (entry.path) chip.title = `${entry.path} ← ${branch}`;
      if (multi && entry.short) {
        const rootTag = document.createElement('span');
        rootTag.className = 'tctx-branch-root';
        rootTag.textContent = String(entry.short);
        chip.appendChild(rootTag);
      }
      chips.appendChild(chip);
    }
    if (chips.childElementCount) {
      row.append(label, chips);
      rail.appendChild(row);
    }
  }
  /* Checklist row — ONE integrated unit inside the rail: a count chip, the
   * always-on in-flight line, and the full list behind the same "+N" toggle
   * the tools and workspace rows use. Mirrors the retained info-rail renderer
   * so both paint the same structure; the live patch path owns updates. */
  if (todos.length) {
    const norm: Array<{ st: string; text: string }> = [];
    for (const item of todos.slice(0, 24)) {
      if (!item || typeof item !== 'object') continue;
      const rec = item as Record<string, unknown>;
      const st = (rec.status === 'completed' || rec.status === 'in_progress'
        || rec.status === 'blocked') ? String(rec.status) : 'pending';
      const text = String(rec.content || '').trim();
      if (!text) continue;
      norm.push({ st, text });
    }
    if (norm.length) {
      /* The in-flight line the rail surfaces inline AND keeps in the list —
       * never buried in the chip's hover title (the "missing in-progress row"). */
      const current = norm.find((x) => x.st === 'in_progress')
        || norm.find((x) => x.st !== 'completed')
        || norm[norm.length - 1];
      const rest = norm.filter((x) => x !== current);
      const row = document.createElement('div');
      row.className = 'tctx-row tctx-todo-row';
      /* Same live-patch key as the git row — _patchLiveCtxRails rebuilds the
       * checklist via [data-tctx-todo]; missing it duplicates the row. */
      row.setAttribute('data-tctx-todo', '1');
      const wrap = document.createElement('div');
      wrap.className = 'tctx-todos';
      /* ONE chip: count + status glyph. The in-flight item renders as a
       * visible line right after it, matching info-rail byte-for-byte. */
      const count = document.createElement('span');
      count.className = 'tctx-chip tctx-tone-todo tctx-todo-count'
        + (doneCount === norm.length ? ' is-done' : '');
      count.title = `${textFor(ports, 'turnCtx.todoLabel', 'Todos')} ${doneCount}/${norm.length}`;
      count.textContent = `${doneCount}/${norm.length}`;
      wrap.append(count);
      /* The always-visible in-flight line (the row the old design hid). */
      const currentLine = document.createElement('span');
      currentLine.className = `tctx-todo-item tctx-todo-${current.st} tctx-todo-current`;
      currentLine.title = current.text;
      const currentMark = document.createElement('span');
      currentMark.className = 'tctx-todo-mark';
      const currentBody = document.createElement('span');
      currentBody.className = 'tctx-todo-text';
      currentBody.textContent = current.text;
      currentLine.append(currentMark, currentBody);
      wrap.append(currentLine);
      if (rest.length) {
        /* Full list rendered inline — no "+N" collapse. */
        const list = document.createElement('span');
        list.className = 'tctx-todo-list';
        for (const entry of rest) {
          const line = document.createElement('div');
          line.className = `tctx-todo-item tctx-todo-${entry.st}`;
          const mark = document.createElement('span');
          mark.className = 'tctx-todo-mark';
          const body = document.createElement('span');
          body.className = 'tctx-todo-text';
          body.textContent = entry.text;
          body.title = entry.text;
          line.append(mark, body);
          list.appendChild(line);
        }
        wrap.append(list);
      }
      row.append(wrap);
      rail.appendChild(row);
    }
  }
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

export function renderContextBlock(
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

export function renderContextRail(
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
