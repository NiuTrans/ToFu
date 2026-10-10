/** Bridge from the paper preview panel to the retained main chat composer.
 * The composer (`#userInput`) and its send pipeline stay authoritative; this
 * module only fills the textarea and invokes the SAME `sendMessage` command
 * the send button uses, so optimistic echo, retry and lane-busy handling are
 * unchanged. An existing draft is never clobbered: our text appends after a
 * blank line, matching the pipeline's own draft-restore shape. */
import { featureRegistry } from '../../feature-registry';

export interface ChatComposerPorts {
  input?: () => HTMLTextAreaElement | null;
  send?: () => (() => unknown) | undefined;
}

const defaultPorts: Required<ChatComposerPorts> = {
  input: () => document.getElementById('userInput') as HTMLTextAreaElement | null,
  send: () => {
    const candidate = featureRegistry.sendMessage;
    return typeof candidate === 'function' ? candidate as () => unknown : undefined;
  },
};

/** Prefill the main composer with `text` and auto-send it. Returns false
 * when the composer or the send command is unavailable (nothing was sent,
 * no draft was touched). */
export function prefillAndSendChatMessage(text: string, ports: ChatComposerPorts = {}): boolean {
  const message = text.trim();
  if (!message) return false;
  const input = (ports.input ?? defaultPorts.input)();
  const send = (ports.send ?? defaultPorts.send)();
  if (!input || !send) return false;
  input.value = input.value.trim() ? `${input.value.replace(/\s+$/, '')}\n\n${message}` : message;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  send();
  return true;
}
