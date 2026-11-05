/**
 * Responsibility: reconcile diagnostic message nodes without rebuilding unchanged evidence.
 * Entry: createDiagnosticsMessageReconciler. Dependencies: injected card renderer and DOM.
 * The presenter owns reads, scope, dividers and scrolling. This owner retains only a weak
 * version per card; complete wire comparisons never enter attributes or global caches.
 */
interface MessageBlock<Message> extends HTMLElement { _msgRef: Message }
interface MessagePorts<Message> {
  identity(message: Message): string;
  summarize(message: Message, index: number): string;
  create(message: Message, index: number): HTMLElement;
  open(block: MessageBlock<Message>): void;
}
export interface RenderOptions {
  /** Identity for flat feeds; position preserves legacy streaming snapshot semantics. */
  reuse?: 'identity' | 'position' | false;
  leading?: readonly Node[];
  beforeMessage?: ReadonlyMap<number, readonly Node[]>;
}
interface Version { wire: string | null; identity: string; index: number; language: string }
interface Candidates { all: HTMLElement[]; exact: Map<string, HTMLElement[]> }

function serialize(message: unknown): string | null {
  try { return JSON.stringify(message) ?? null; }
  catch { return null; } // Non-wire/cyclic values must never count as unchanged.
}
function takeUnused(queue: HTMLElement[] | undefined, used: Set<HTMLElement>): HTMLElement | undefined {
  while (queue?.length) {
    const block = queue.pop()!;
    if (!used.has(block)) { used.add(block); return block; }
  }
  return undefined;
}

export function createDiagnosticsMessageReconciler<Message>(ports: MessagePorts<Message>) {
  const versions = new WeakMap<HTMLElement, Version>();
  return {
    render(container: HTMLElement, messages: readonly Message[], options: RenderOptions = {}): void {
      const reuse = options.reuse ?? 'identity';
      const language = container.ownerDocument.documentElement.lang;
      const snapshots = messages.map((message, index): Version => ({
        wire: serialize(message), identity: ports.identity(message), index, language,
      }));
      const previous: HTMLElement[] = [];
      if (reuse !== false) {
        for (let node = container.firstElementChild; node; node = node.nextElementSibling) {
          if (versions.has(node as HTMLElement)) previous.push(node as HTMLElement);
        }
      }
      const matched: Array<HTMLElement | undefined> = new Array(messages.length);
      if (reuse === 'position') {
        for (let i = 0; i < messages.length; i++) matched[i] = previous[i];
      } else if (reuse === 'identity') {
        const groups = new Map<string, Candidates>();
        // Reverse insertion lets pop consume occurrences in their original order,
        // without quadratic shifts or scans for repeated identities.
        for (let i = previous.length - 1; i >= 0; i--) {
          const block = previous[i], version = versions.get(block)!;
          let group = groups.get(version.identity);
          if (!group) { group = { all: [], exact: new Map() }; groups.set(version.identity, group); }
          group.all.push(block);
          if (version.wire !== null && version.language === language) {
            let queue = group.exact.get(version.wire);
            if (!queue) { queue = []; group.exact.set(version.wire, queue); }
            queue.push(block);
          }
        }
        const used = new Set<HTMLElement>();
        // Reserve every exact match first: an edited duplicate must not consume
        // a later unchanged occurrence and transfer its disclosure/focus.
        snapshots.forEach((version, index) => {
          if (version.wire !== null) matched[index] = takeUnused(
            groups.get(version.identity)?.exact.get(version.wire), used);
        });
        snapshots.forEach((version, index) => {
          matched[index] ??= takeUnused(groups.get(version.identity)?.all, used);
        });
      }

      const active = container.ownerDocument.activeElement;
      let focusAfter: HTMLElement | null = null;
      const selection = container.ownerDocument.defaultView?.getSelection();
      const selected = reuse !== false && selection && !selection.isCollapsed
        && selection.anchorNode && selection.focusNode
        && container.contains(selection.anchorNode) && container.contains(selection.focusNode)
        ? { anchor: selection.anchorNode, anchorOffset: selection.anchorOffset,
          focus: selection.focusNode, focusOffset: selection.focusOffset } : null;
      const nodes: Node[] = [...options.leading ?? []];
      for (const [index, message] of messages.entries()) {
        nodes.push(...options.beforeMessage?.get(index) ?? []);
        const prior = matched[index], next = snapshots[index];
        const before = prior ? versions.get(prior) : undefined;
        const unchanged = prior && next.wire !== null && before?.wire === next.wire
          && before.language === language;
        const block = (unchanged ? prior : ports.create(message, index)) as MessageBlock<Message>;
        // Opening can synchronously render the lazy body. Publish the current
        // reference first, including when an equal read supplies a fresh object.
        block._msgRef = message;
        if (!unchanged) {
          block.dataset.idx = String(index);
          block.dataset.mid = next.identity;
          if (prior?.classList.contains('open')) ports.open(block);
          if (prior?.querySelector<HTMLDetailsElement>('.debug-raw')?.open) {
            const raw = block.querySelector<HTMLDetailsElement>('.debug-raw');
            if (raw) raw.open = true;
          }
        } else if (before.index !== index) {
          block.dataset.idx = String(index);
          const summary = block.querySelector('.debug-msg-summary');
          if (summary) summary.textContent = ports.summarize(message, index);
        }
        if (prior && active && prior.contains(active)) {
          if (active.matches('.debug-msg-header')) focusAfter = block.querySelector('.debug-msg-header');
          else if (active.matches('.debug-raw > summary')) focusAfter = block.querySelector('.debug-raw > summary');
        }
        versions.set(block, next);
        nodes.push(block);
      }
      nodes.push(...options.beforeMessage?.get(messages.length) ?? []);

      // Remove obsolete dividers first. Otherwise inserting a new divider ahead
      // of an old one would unnecessarily detach each unchanged message after it.
      const wanted = new Set(nodes);
      for (const node of Array.from(container.childNodes)) {
        if (!wanted.has(node)) { versions.delete(node as HTMLElement); node.remove(); }
      }
      let cursor = container.firstChild;
      for (const node of nodes) {
        if (node === cursor) cursor = cursor.nextSibling;
        else container.insertBefore(node, cursor);
      }
      if (focusAfter && container.ownerDocument.activeElement !== focusAfter) {
        focusAfter.focus({ preventScroll: true });
      }
      // A browser move can clear a Range even though its text nodes survived.
      // Restore only surviving endpoints; never select replacement evidence.
      if (selected && container.contains(selected.anchor) && container.contains(selected.focus)
          && (selection!.anchorNode !== selected.anchor || selection!.anchorOffset !== selected.anchorOffset
            || selection!.focusNode !== selected.focus || selection!.focusOffset !== selected.focusOffset)) {
        selection!.setBaseAndExtent(selected.anchor, selected.anchorOffset, selected.focus, selected.focusOffset);
      }
    },
  };
}
