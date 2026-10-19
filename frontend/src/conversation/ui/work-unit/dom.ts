/** Small DOM helpers for mounted WorkUnit views. Shared controls retain design-system ownership. */
import { createButton } from '../../../ui';
export function node<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, className: string, text = ''): HTMLElementTagNameMap[K] {
  const element = doc.createElement(tag); element.className = className; element.textContent = text; return element;
}
export function setText(element: Element, value: string): void { if (element.textContent !== value) element.textContent = value; }
export function button(doc: Document, label: string, action: () => void, primary = false): HTMLButtonElement {
  return createButton({ label, variant: primary ? 'primary' : 'secondary', size: 'sm', wrap: true, onClick: action }, doc);
}
