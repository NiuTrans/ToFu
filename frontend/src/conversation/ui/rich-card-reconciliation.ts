/**
 * Reconcile sanitized presenter HTML inside one Surface-owned rich leaf.
 * `reconcileRichCardHtml` patches rich leaves; `moveRetainedNodeBefore` protects
 * their Surface-owned ancestors during reordering. Neither installs handlers
 * or keeps state after the synchronous transaction.
 * Presenters may supply sibling-scoped `data-rich-key` identities; legacy
 * cards use existing domain IDs, structural classes and disclosure labels.
 * This is not a sanitizer: callers retain the existing safe-HTML boundary.
 */
type ReconciliationParent = Node & ParentNode;

type MovableParent = ReconciliationParent & {
  moveBefore?: (node: Node, before: Node | null) => void;
};

const SWARM_READER_CLASSES = ['sw-collapsed', 'sw-a-open', 'sw-tl-open'];
const READER_CLASSES = new Set([
  ...SWARM_READER_CLASSES, 'expanded', 'collapsed', 'eb-expanded', 'tp-expanded',
  'hg-selected', 'hg-submitting',
]);
const IDENTITY_ATTRIBUTES = [
  'data-rich-key', 'data-agent-id', 'data-call-id', 'id',
  'data-cmd-key', 'data-output-key', 'data-rn', 'data-prn',
];

interface NodeIdentity {
  family: string;
  explicit: boolean;
  label: string;
}

function nodeIdentity(node: Node): NodeIdentity {
  const element = node.nodeType === 1 ? node as Element : null;
  if (!element) return { family: String(node.nodeType), explicit: false, label: '' };
  // Even an explicit ID cannot authorize reusing a focused control for a
  // different action (including keyboard actions), destination, or form field.
  const actions = Array.from(element.attributes)
    .filter(({ name }) => name === 'data-conversation-action'
      || name === 'data-tofu-action' || name.startsWith('data-tofu-action-'))
    .map(({ name, value }) => [name, value]).sort(([a], [b]) => a.localeCompare(b));
  const formIdentity = ['name', 'type', 'data-gid', 'data-option-index', 'data-batch-index', 'data-label']
    .map((name) => element.getAttribute(name) ?? '');
  const kind = JSON.stringify([
    element.namespaceURI, element.tagName, actions, formIdentity,
    element.tagName === 'A' ? element.getAttribute('href') : null,
  ]);
  for (const name of IDENTITY_ATTRIBUTES) {
    // Commands live inside an already-keyed tool slot. Running cards omit
    // data-rn and short commands gain data-output-key only on settlement;
    // these lifecycle attributes must not turn the same card into a new key.
    if (element.classList.contains('ptool-cmd-block')
        && ['data-cmd-key', 'data-output-key', 'data-rn'].includes(name)) continue;
    const value = element.getAttribute(name);
    if (value) return { family: `${kind}:${name}:${value}`, explicit: true, label: '' };
  }
  const structuralClass = Array.from(element.classList).find(
    (name) => !READER_CLASSES.has(name),
  ) ?? '';
  const family = JSON.stringify([kind, structuralClass]);
  const summary = element.tagName === 'DETAILS'
    ? Array.from(element.children).find((child) => child.tagName === 'SUMMARY')
    : null;
  // Batch-edit numbering/status changes are not edit identity. The existing
  // presenters already expose stable path/description spans in their summary.
  const editPath = summary?.querySelector('.ptool-batch-path');
  const editDescription = summary?.querySelector('.ptool-batch-desc');
  const label = summary
    ? editPath || editDescription
      ? JSON.stringify([editPath?.textContent ?? '', editDescription?.textContent ?? ''])
      : summary.textContent?.trim() ?? ''
    : element.getAttribute('href') || element.getAttribute('src')
      || element.querySelector('a[href]')?.getAttribute('href') || '';
  return { family, explicit: false, label };
}

function syncElementAttributes(current: Element, next: Element): void {

  // Reveal a newly actionable program without replacing its source/focused nodes.
  // Same-attention refreshes retain the reader's fold, including dismissed errors.
  const attention = next.getAttribute('data-program-attention');
  const revealProgram = current.matches('details.ptc-card') && next.matches('details.ptc-card')
    && attention !== current.getAttribute('data-program-attention')
    && (attention === 'error' || attention === 'handoff' || attention === 'incomplete');
  if (revealProgram) current.setAttribute('open', '');
  // Read transition state BEFORE patching attributes. Swarm automatically
  // reveals newly-running agents, unless the reader already chose a fold.
  const isRunning = (element: Element): boolean =>
    ['running', 'thinking'].includes(element.getAttribute('data-sw-agent-status') ?? '');
  const swarmReaderChoice = current.classList.contains('sw-agent')
    && next.classList.contains('sw-agent')
    && ['true', 'false'].includes(current.getAttribute('data-sw-reader-open') ?? '');
  const newlyRunning = !isRunning(current) && isRunning(next) && !swarmReaderChoice;
  // Capture ownership before syncing: gaining data-collapsible is a new
  // control with presenter defaults, not a prior reader choice.
  const wasCollapsible = current.hasAttribute('data-collapsible')
    && next.hasAttribute('data-collapsible');
  const sameCommandPhase = current.classList.contains('ptool-cmd-block')
    && next.classList.contains('ptool-cmd-block')
    && current.classList.contains('ptool-cmd-running')
      === next.classList.contains('ptool-cmd-running');
  const commandClasses = ['cmd-open', 'output-open'].filter((name) => {
    const key = name === 'cmd-open' ? 'data-cmd-key' : 'data-output-key';
    return sameCommandPhase && current.hasAttribute(key)
      && current.getAttribute(key) === next.getAttribute(key);
  });
  const commandFold = next.classList.contains('ptool-cmd-fold');
  const preserveAria = !commandFold && current.hasAttribute('aria-expanded')
    && next.hasAttribute('aria-expanded');
  const guidancePending = current.closest('.hg-card.hg-submitting:not(.hg-expired)')
    && next.closest('.hg-card:not(.hg-expired)');
  const keepAttribute = (name: string): boolean => (
    (name === 'open' && current.tagName === 'DETAILS')
    || (name === 'data-sw-reader-open' && swarmReaderChoice)
    || (name === 'aria-expanded' && preserveAria)

    || (name === 'disabled' && Boolean(guidancePending) && current.hasAttribute('disabled'))
  );
  for (const attribute of Array.from(current.attributes)) {
    if (attribute.name !== 'class' && !keepAttribute(attribute.name)
        && !next.hasAttribute(attribute.name)) {
      current.removeAttribute(attribute.name);
    }
  }
  for (const attribute of Array.from(next.attributes)) {
    if (attribute.name !== 'class' && !keepAttribute(attribute.name)
        && current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
    }
  }
  const classes = new Set(Array.from(next.classList));
  // Same-phase snapshots must not close a command the reader just opened.
  // Across running/settled transitions (or output removal), the presenter and
  // its existing interaction-key sets remain authoritative: live output is
  // always visible, settled output is folded. Never globally pin output-open.
  for (const name of [...READER_CLASSES, ...commandClasses]) {
    // Only an existing collapsible owns a reader choice. New controls use the
    // presenter's defaults. Command classes are included only within a phase.
    if (name === 'collapsed' && !wasCollapsible) continue;
    if (name === 'hg-submitting' && (!current.matches('.hg-card:not(.hg-expired)')
        || !next.matches('.hg-card:not(.hg-expired)'))) continue;
    if (name === 'hg-selected' && (!current.matches('button.hg-option-card')
        || !next.matches('button.hg-option-card'))) continue;
    if (name === 'sw-a-open' && newlyRunning) continue;
    if (current.classList.contains(name)) classes.add(name);
    else classes.delete(name);
  }
  const className = Array.from(classes).join(' ');
  if ((current.getAttribute('class') ?? '') !== className) {
    if (className) current.setAttribute('class', className);
    else current.removeAttribute('class');
  }

  if (commandFold) {
    const card = current.closest('.ptool-cmd-block');
    const stateClass = next.classList.contains('ptool-cmd-fold-output')
      ? 'output-open' : 'cmd-open';
    if (card) current.setAttribute('aria-expanded', String(card.classList.contains(stateClass)));
  }
}

function reconcileNode(current: Node, next: Node): void {
  if (current.isEqualNode(next)) return;
  if (current.nodeType === 3 || current.nodeType === 8) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  if (current.nodeType !== 1) return;
  syncElementAttributes(current as Element, next as Element);
  reconcileChildren(current as ReconciliationParent, next as ReconciliationParent);

  const element = current as Element;
  if (element.classList.contains('timer-meta-more')) {
    const expanded = element.parentElement?.querySelector('.timer-meta-val')
      ?.classList.contains('expanded');
    const label = element.getAttribute(expanded ? 'data-less' : 'data-more');
    if (label !== null && element.textContent !== label) element.textContent = label;
  }
}

function reconcileChildren(currentParent: ReconciliationParent, nextParent: ReconciliationParent): void {
  const currentNodes = Array.from(currentParent.childNodes);
  const nextNodes = Array.from(nextParent.childNodes);
  const identities = new Map<Node, NodeIdentity>();
  const counts = new Map<string, { current: number; next: number }>();
  for (const [side, nodes] of [['current', currentNodes], ['next', nextNodes]] as const) {
    for (const node of nodes) {
      const identity = nodeIdentity(node);
      identities.set(node, identity);
      const count = counts.get(identity.family) ?? { current: 0, next: 0 };
      count[side] += 1;
      counts.set(identity.family, count);
    }
  }
  const keyFor = (node: Node): string => {
    const identity = identities.get(node)!;
    const count = counts.get(identity.family)!;
    // Unique structural slots can update their label/count. Repeated legacy
    // disclosures need semantic labels so an insertion cannot steal state.
    // Indistinguishable duplicate keys pair by occurrence, never last wins.
    return JSON.stringify([identity.family, !identity.explicit
      && (count.current > 1 || count.next > 1) ? identity.label : '']);
  };
  const byKey = new Map<string, { nodes: Node[]; used: number }>();
  for (const current of currentNodes) {
    const key = keyFor(current);
    const queue = byKey.get(key) ?? { nodes: [], used: 0 };
    queue.nodes.push(current);
    byKey.set(key, queue);
  }
  nextNodes.forEach((next, index) => {
    const queue = byKey.get(keyFor(next));
    const current = queue?.nodes[queue.used++];
    const retained = current ?? next.cloneNode(true);
    if (current) reconcileNode(current, next);
    const atIndex = currentParent.childNodes[index] ?? null;
    if (retained !== atIndex) {
      const movable = currentParent as MovableParent;
      // New Chromium supports a state-preserving move. Other browsers use
      // insertBefore plus the call-scoped focus/selection/scroll snapshot.
      if (movable.moveBefore && retained.parentNode === currentParent
          && retained.isConnected) movable.moveBefore(retained, atIndex);
      else currentParent.insertBefore(retained, atIndex);
    }
  });
  for (const queue of byKey.values()) {
    for (let index = queue.used; index < queue.nodes.length; index += 1) {
      currentParent.removeChild(queue.nodes[index]);
    }
  }
}

/** A synchronous DOM transaction, not a second browser-state authority. */
function captureReaderState(root: HTMLElement): () => void {
  // A new or detached subtree cannot own the document's focus or selection.
  // Reading Selection.anchorNode/scroll offsets here flushed layout between
  // every newly inserted tool card on conversation switch.
  if (!root.isConnected || !root.hasChildNodes()) return () => {};
  const document = root.ownerDocument;
  const active = document.activeElement;
  const focused = active && root.contains(active) ? active as HTMLElement : null;
  const input = focused && ['INPUT', 'TEXTAREA'].includes(focused.tagName)
    ? focused as HTMLInputElement | HTMLTextAreaElement : null;
  const inputSelection = input && input.selectionStart !== null ? {
    start: input.selectionStart, end: input.selectionEnd!, direction: input.selectionDirection!,
    value: input.value,
  } : null;
  const selection = document.getSelection();
  const anchor = selection?.anchorNode ?? null;
  const focus = selection?.focusNode ?? null;
  const selected = anchor && focus && (root.contains(anchor) || root.contains(focus)) ? {
    anchor, focus, anchorOffset: selection!.anchorOffset, focusOffset: selection!.focusOffset,
    text: selection!.toString(),
  } : null;
  const scrolled = [root, ...Array.from(root.querySelectorAll<HTMLElement>('*'))]
    .filter((element) => element.scrollTop !== 0 || element.scrollLeft !== 0)
    .map((element) => ({ element, top: element.scrollTop, left: element.scrollLeft }));
  return () => {
    if (focused && root.contains(focused)) {
      if (document.activeElement !== focused) focused.focus({ preventScroll: true });
      if (input && inputSelection && input.value === inputSelection.value) {
        if (input.selectionStart !== inputSelection.start || input.selectionEnd !== inputSelection.end
            || input.selectionDirection !== inputSelection.direction) {
          input.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction);
        }
      }
    }
    if (selection && selected && selected.anchor.isConnected && selected.focus.isConnected
        && selected.anchorOffset <= (selected.anchor.nodeType === 3
          ? selected.anchor.textContent!.length : selected.anchor.childNodes.length)
        && selected.focusOffset <= (selected.focus.nodeType === 3
          ? selected.focus.textContent!.length : selected.focus.childNodes.length)
        && (selection.anchorNode !== selected.anchor || selection.focusNode !== selected.focus
          || selection.anchorOffset !== selected.anchorOffset || selection.focusOffset !== selected.focusOffset)) {
      // Appending to an endpoint text node may clear a still-valid selection.
      // Validate the entire selected range, not the entire endpoint nodes;
      // this also refuses to select removed/revised middle output.
      const range = document.createRange();
      range.setStart(selected.anchor, selected.anchorOffset);
      range.setEnd(selected.focus, selected.focusOffset);
      if (range.collapsed && selected.anchor !== selected.focus) {
        range.setStart(selected.focus, selected.focusOffset);
        range.setEnd(selected.anchor, selected.anchorOffset);
      } else if (range.collapsed && selected.anchorOffset > selected.focusOffset) {
        range.setStart(selected.focus, selected.focusOffset);
        range.setEnd(selected.anchor, selected.anchorOffset);
      }
      if (range.toString() === selected.text) {
        selection.setBaseAndExtent(selected.anchor, selected.anchorOffset, selected.focus, selected.focusOffset);
      }
    }
    for (const { element, top, left } of scrolled) {
      if (root.contains(element)) {
        if (element.scrollTop !== top) element.scrollTop = top;
        if (element.scrollLeft !== left) element.scrollLeft = left;
      }
    }
  };
}

/** Protect retained Surface ancestors as well as the rich leaves themselves. */
export function moveRetainedNodeBefore(
  parent: HTMLElement, node: HTMLElement, before: Node | null,
): void {
  const movable = parent as MovableParent;
  // Native moves preserve focus, but Chromium may still clear DOM selection.
  // Both move paths therefore share the same reader-state transaction.
  const restore = node.isConnected ? captureReaderState(node) : null;
  try {
    if (movable.moveBefore && node.parentNode === parent && node.isConnected) {
      movable.moveBefore(node, before);
    } else {
      parent.insertBefore(node, before);
    }
  } finally {
    restore?.();
  }
}

/** The caller owns render caching and leaf teardown; no nodes escape this call. */
export function reconcileRichCardHtml(root: HTMLElement, safeHtml: string): void {
  const template = root.ownerDocument.createElement('template');
  template.innerHTML = safeHtml;
  const restore = captureReaderState(root);
  reconcileChildren(root, template.content);
  restore();
}
