/** Existing composer prefill port: preserve a human draft, dispatch the normal input event, never auto-send a revision. */
export function prefillWorkUnitComposer(document: Document, message: string, changed: () => void): void {
  const input = document.getElementById('userInput') as HTMLTextAreaElement | null;
  if (!input || !message) return;
  if (!input.value.includes(message)) input.value = input.value ? input.value + '\n\n' + message : message;
  const Event = document.defaultView!.Event;
  input.dispatchEvent(new Event('input', { bubbles: true })); changed(); input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
