/** Typed delivery presenters for the retained artifact panel.
 * Entry points: renderPublishedFile, renderScriptWarning.
 * Dependencies: native UI factories. No path resolution, markup or network IO;
 * only panel-owned published-ID endpoints/actions can initiate delivery.
 */
import { createButton, createDialog } from '../ui';

export interface ArtifactDeliveryPorts {
  text?(key: string, fallback: string): string;
  download(): void;
}

export function renderPublishedFile(host: HTMLElement, value: unknown, ports: ArtifactDeliveryPorts): void {
  const doc = host.ownerDocument;
  const text = (key: string, fallback: string) => ports.text?.(key, fallback) ?? fallback;
  const meta = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const heading = doc.createElement('h2');
  heading.textContent = text('delivery.file', 'Published attachment');
  const description = doc.createElement('p');
  description.textContent = text('delivery.fileHelp', 'This file is available to download. It is not executed or embedded in the application.');
  const details = doc.createElement('dl');
  for (const [key, fallback, data] of [
    ['delivery.filename', 'Filename', meta.filename],
    ['delivery.mime', 'File type', meta.mime],
    ['delivery.bytes', 'Bytes', meta.sizeBytes],
    ['delivery.hash', 'SHA-256', meta.sha256],
  ]) {
    const term = doc.createElement('dt'); term.textContent = text(String(key), String(fallback));
    const definition = doc.createElement('dd'); definition.textContent = typeof data === 'string' || typeof data === 'number' ? String(data) : '—';
    // Hashes and filenames must not widen a narrow-screen panel.
    definition.style.overflowWrap = 'anywhere';
    details.append(term, definition);
  }
  const download = createButton({ label: text('delivery.download', 'Download original'), wrap: true, onClick: ports.download }, doc);
  host.replaceChildren(heading, description, details, download);
}

export function renderScriptWarning(host: HTMLElement, allow: () => void,
  translate?: (key: string, fallback: string) => string,
  options: { interactiveDownload?: () => void; scriptsAllowed?: boolean } = {}): () => void {
  const doc = host.ownerDocument;
  const text = (key: string, fallback: string) => translate?.(key, fallback) ?? fallback;
  let disposed = false;
  let closeConfirmation: (() => void) | null = null;
  const banner = doc.createElement('div'); banner.className = 'artifact-script-banner'; banner.setAttribute('role', 'note');
  const message = doc.createElement('span');
  const risk = text('delivery.interactiveRisk', 'Opening the original interactive document can execute untrusted code, make network requests, or send data. Download it only if you trust the source.');
  message.textContent = options.scriptsAllowed ? risk : text('delivery.scriptRisk', 'Scripts are blocked. Running this document executes untrusted code and may make network requests or send data. Only enable it if you trust the source.');
  const control = createButton({ label: text('delivery.runScripts', 'Run scripts (code / network risk)'), size: 'sm', wrap: true, onClick: allow }, doc);
  control.dataset.artAction = 'allow-scripts';
  // The typed owner handles the click; retained delegation must not activate it twice.
  control.addEventListener('click', event => event.stopPropagation());
  banner.append(message);
  if (!options.scriptsAllowed) banner.append(control);
  if (options.interactiveDownload) {
    const original = createButton({ label: text('delivery.interactiveDownload', 'Download original interactive document'), size: 'sm', wrap: true,
      onClick: async () => {

        if (disposed || closeConfirmation) return;
        const dialog = createDialog({ title: text('delivery.interactiveConfirm', 'Download interactive code?'), body: risk,
          actions: [{ id: 'cancel', label: text('delivery.cancel', 'Cancel') },
            { id: 'download', label: text('delivery.interactiveProceed', 'Download with code / network risk'), variant: 'danger' }] });
        doc.body.append(dialog.el);
        closeConfirmation = () => { dialog.close(); dialog.el.remove(); };
        try {
          if (await dialog.open() === 'download' && !disposed && banner.isConnected) options.interactiveDownload?.();
        } finally { dialog.el.remove(); closeConfirmation = null; }
      } }, doc);
    original.addEventListener('click', event => event.stopPropagation());
    banner.append(original);
  }
  host.append(banner);
  return () => { disposed = true; closeConfirmation?.(); banner.remove(); };
}
