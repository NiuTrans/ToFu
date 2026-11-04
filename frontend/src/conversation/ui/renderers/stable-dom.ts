/**
 * Retained HTML and detail disclosure primitives. Rich markup passes through
 * the existing reconciliation owner; no content policy.
 * Entry points: directClassChild, setStableData, setManagedHtml, setStreamingMarkdownHtml, setCompletedMarkdownHtml,
 * setManagedRichHtml, resetManagedHtml, preserveDetailsOpen, endAnchoredScroller.
 * Dependencies: rich-card-reconciliation.
 */
import { reconcileRichCardHtml } from '../rich-card-reconciliation';

type ManagedHtmlElement = HTMLElement & {
  _tofuRenderedHtml?: string;
};

export function directClassChild<T extends HTMLElement>(
  parent: HTMLElement,
  className: string,
): T | null {
  for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
    if (child.classList.contains(className)) return child as T;
  }
  return null;
}

/** Write one data attribute only when it changes; undefined removes it.
 * Rewriting an equal value still queues a DOM mutation and restyles the node. */
export function setStableData(node: HTMLElement, key: string, value: string | undefined): void {
  if (value === undefined) {
    if (key in node.dataset) delete node.dataset[key];
  } else if (node.dataset[key] !== value) {
    node.dataset[key] = value;
  }
}

export function setManagedHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  node.innerHTML = html;
  managedNode._tofuRenderedHtml = html;
  return true;
}

function replaceChangedMarkdownSuffix(node: HTMLElement, html: string, previous?: string): boolean {
  let boundary = 0;
  let retained: Element | null = null;
  if (previous) {
    // Compute the shared boundary once; comparing every growing prefix would
    // make completion quadratic in the number of already-rendered blocks.
    let unchangedLength = 0;
    while (unchangedLength < previous.length && unchangedLength < html.length
        && previous.charCodeAt(unchangedLength) === html.charCodeAt(unchangedLength)) {
      unchangedLength++;
    }
    for (const child of Array.from(node.children)) {
      const serialized = child.outerHTML;
      const start = previous.indexOf(serialized, boundary);
      const end = start + serialized.length;
      if (start < 0 || previous.slice(boundary, start).trim()
          || end > unchangedLength) break;
      retained = child;
      boundary = end;
    }
  }
  if (!retained) return setManagedHtml(node, html);
  // Only complete parser/sanitizer output authorizes this replacement. A
  // changed multi-block tail must not destroy an identical reading prefix.
  const template = node.ownerDocument.createElement('template');
  template.innerHTML = html.slice(boundary);
  while (retained.nextSibling) node.removeChild(retained.nextSibling);
  node.appendChild(template.content);
  (node as ManagedHtmlElement)._tofuRenderedHtml = html;
  return true;
}

/** Preserve authorized unchanged reader blocks when terminal formatting arrives. */
export function setCompletedMarkdownHtml(node: HTMLElement, html: string): boolean {
  const previous = (node as ManagedHtmlElement)._tofuRenderedHtml;
  if (previous === html) return false;
  return replaceChangedMarkdownSuffix(node, html, previous);
}

export function setStreamingMarkdownHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  const previous = managedNode._tofuRenderedHtml;
  if (previous === html) return false;
  const tail = node.lastElementChild;
  if (!previous || !tail) return replaceChangedMarkdownSuffix(node, html, previous);
  // The parser/sanitizer still authorizes the COMPLETE new HTML. Preserve
  // earlier DOM only when its exact HTML prefix is unchanged and the old
  // final block has the same canonical serialization as the mounted block.
  const oldTail = tail.outerHTML;
  const boundary = previous.lastIndexOf(oldTail);
  if (boundary < 0 || previous.slice(boundary + oldTail.length).trim()
      || !html.startsWith(previous.slice(0, boundary))) {
    return replaceChangedMarkdownSuffix(node, html, previous);
  }
  for (let sibling = tail.nextSibling; sibling; sibling = sibling.nextSibling) {
    if (sibling.nodeType !== 3 || sibling.textContent?.trim()) return replaceChangedMarkdownSuffix(node, html, previous);
  }
  const template = node.ownerDocument.createElement('template');
  template.innerHTML = html.slice(boundary);
  const nextTail = template.content.firstChild;
  // Growing plain paragraphs preserve their Text node and native selection.
  // Any other final block (rich paragraph, code, list, table, quote) is
  // replaced alone, without re-serializing earlier blocks. No source,
  // token or DOM cache is added beyond the existing managed HTML witness.
  if (tail.tagName === 'P' && !tail.attributes.length
      && tail.childNodes.length === 1 && tail.firstChild?.nodeType === 3
      && nextTail?.nodeType === 1 && (nextTail as Element).tagName === 'P'
      && !(nextTail as Element).attributes.length
      && nextTail.childNodes.length === 1 && nextTail.firstChild?.nodeType === 3
      && nextTail.textContent!.startsWith(tail.textContent ?? '')) {
    (tail.firstChild as Text).appendData(nextTail.textContent!.slice(tail.textContent!.length));
    while (tail.nextSibling) node.removeChild(tail.nextSibling);
    template.content.removeChild(nextTail);
    node.appendChild(template.content);
  } else {
    while (tail.nextSibling) node.removeChild(tail.nextSibling);
    node.replaceChild(template.content, tail);
  }
  managedNode._tofuRenderedHtml = html;
  return true;
}

export function setManagedRichHtml(node: HTMLElement, html: string): boolean {
  const managedNode = node as ManagedHtmlElement;
  if (managedNode._tofuRenderedHtml === html) return false;
  reconcileRichCardHtml(node, html);
  managedNode._tofuRenderedHtml = html;
  return true;
}

export function resetManagedHtml(node: HTMLElement): void {
  delete (node as ManagedHtmlElement)._tofuRenderedHtml;
}

/* Unified disclosure-state semantic for the replaceChildren-style blocks:
 * a disclosure's open state is USER INTENT, and the block's default state
 * applies only when the disclosure is first created. Re-rendering a block —
 * for a sibling's arrival, an unrelated field change, or (for a growing
 * system-note-group) a new member that defeats the sameBlock short-circuit —
 * must preserve the disclosure the reader already set, never snap it back to
 * the default. Reads the live <details> (first descendant) before the block
 * rebuilds its node; the caller then sets the rebuilt disclosure's open to
 * the returned value. Returns `defaultOpen` when no prior disclosure exists
 * (first paint, or a renderer-mode switch). */
export function preserveDetailsOpen(
  node: HTMLElement,
  defaultOpen: boolean,
): boolean {
  return node.querySelector('details')?.open ?? defaultOpen;
}

/** Newest-first bounded scroller: it rests at its end (newest content) on
 * first layout, on growth and on `pin()`, until the reader scrolls away from
 * the end; returning to the end resumes following. Layout changes (mounting,
 * opening a fold, streamed growth) re-anchor through a ResizeObserver on the
 * scroller and its content, so a scroller built while detached still opens at
 * its end. One anchor per scroller; `dispose` releases the observer. */
export interface EndAnchoredScroller {
  readonly pinned: boolean;
  pin(): void;
  follow(): void;
  dispose(): void;
}

const endAnchoredScrollers = new WeakMap<HTMLElement, EndAnchoredScroller>();

export function endAnchoredScroller(scroller: HTMLElement, content: HTMLElement): EndAnchoredScroller {
  const existing = endAnchoredScrollers.get(scroller);
  if (existing) return existing;
  let pinned = true;
  const follow = (): void => {
    if (pinned) scroller.scrollTop = scroller.scrollHeight;
  };
  // A closed or detached scroller has no layout and records no intent.
  const onScroll = (): void => {
    if (scroller.clientHeight === 0) return;
    pinned = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 1;
  };
  scroller.addEventListener('scroll', onScroll, { passive: true });
  const Resize = scroller.ownerDocument.defaultView?.ResizeObserver;
  const observer = Resize ? new Resize(follow) : null;
  observer?.observe(scroller);
  observer?.observe(content);
  const anchor: EndAnchoredScroller = {
    get pinned() { return pinned; },
    pin() { pinned = true; follow(); },
    follow,
    dispose() {
      scroller.removeEventListener('scroll', onScroll);
      observer?.disconnect();
      endAnchoredScrollers.delete(scroller);
    },
  };
  endAnchoredScrollers.set(scroller, anchor);
  return anchor;
}
