/** Answer toolbar and heading navigation. Uses the existing Markdown DOM;
 * retains reader focus across streaming/replay and owns no transcript state.
 * Entry: updateAnswerNavigation. Dependencies: shared buttons and copy ports. */
import { createButton } from '../../../ui';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';

const headingSelector = ':scope > h1, :scope > h2, :scope > h3';
export function updateAnswerNavigation(toolbar: HTMLElement, body: HTMLElement,
  ports: ClassicConversationRendererPorts): void {
  let title = toolbar.querySelector<HTMLElement>(':scope > .conversation-answer-title');
  if (!title) {
    title = toolbar.ownerDocument.createElement('span');
    title.className = 'conversation-answer-title';
    toolbar.prepend(title);
  }
  title.textContent = textFor(ports, 'conversation.answer', 'Answer');
  const headings = Array.from(body.querySelectorAll<HTMLElement>(headingSelector)).slice(0, 32);
  let outline = toolbar.querySelector<HTMLDetailsElement>(':scope > .conversation-answer-outline');
  if (headings.length < 3) { outline?.remove(); return; }
  if (!outline) {
    outline = toolbar.ownerDocument.createElement('details');
    outline.className = 'conversation-answer-outline';
    const summary = toolbar.ownerDocument.createElement('summary');
    const nav = toolbar.ownerDocument.createElement('nav');
    outline.append(summary, nav); toolbar.append(outline);
    outline.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !outline!.open || event.defaultPrevented) return;
      event.preventDefault(); event.stopPropagation(); outline!.open = false; summary.focus();
    });
  }
  const label = textFor(ports, 'conversation.answerOutline', 'On this page');
  outline.querySelector('summary')!.textContent = label;
  const nav = outline.querySelector('nav')!;
  nav.setAttribute('aria-label', label);
  // Reconcile by index. A heading's text can grow while its button is focused.
  headings.forEach((heading, index) => {
    let button = nav.children[index] as HTMLButtonElement | undefined;
    if (!button) {
      button = createButton({ label: '', variant: 'ghost', size: 'sm', wrap: true }, toolbar.ownerDocument);
      nav.append(button);
    }
    button.textContent = heading.textContent || label;
    button.onclick = () => {
      const target = body.querySelectorAll<HTMLElement>(headingSelector)[index];
      if (!target) return;
      outline!.open = false;
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'start', behavior: 'auto' });
    };
  });
  while (nav.children.length > headings.length) nav.lastElementChild!.remove();
}
