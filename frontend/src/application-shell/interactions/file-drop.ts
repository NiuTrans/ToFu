/** Desktop file-drop routing. Modal ownership is checked at delivery; paper takes one PDF, chat preserves upload ordering. */
import type { LifecycleScope } from '../../lifecycle';
export interface FileDropPorts {
  enabled: boolean;
  modalOwnsFiles(): boolean;
  paperMode(): boolean;
  isArchive(file: File): boolean;
  isDocument(file: File): boolean;
  handlePaper(file: File): Promise<unknown> | unknown;
  handlePdf(file: File): Promise<unknown> | unknown;
  handleImage(file: File): Promise<unknown> | unknown;
  handleArchive(file: File): Promise<unknown> | unknown;
  handleDocument(file: File): Promise<unknown> | unknown;
  labels(paper: boolean): Readonly<{ title: string; hint: string }>;
  reportFailure(error: unknown): void;
}
export function installFileDrop(scope: LifecycleScope, document: Document, ports: FileDropPorts): void {
  if (!ports.enabled) return;
  let depth = 0;
  const overlay = document.getElementById('dropOverlay');
  const hasFiles = (event: DragEvent) => event.dataTransfer?.types.includes('Files') || !!event.dataTransfer?.files.length;
  const clear = () => { depth = 0; overlay?.classList.remove('visible'); };
  scope.add(clear);
  scope.listen(document, 'dragenter', event => {
    const e = event as DragEvent;
    if (!hasFiles(e)) return;
    e.preventDefault(); if (ports.modalOwnsFiles()) return;
    if (++depth !== 1 || !overlay) return;
    overlay.classList.add('visible');
    const labels = ports.labels(ports.paperMode());
    const title = overlay.querySelector('.drop-text'), hint = overlay.querySelector('.drop-hint');
    if (title) title.textContent = labels.title; if (hint) hint.textContent = labels.hint;
  });
  scope.listen(document, 'dragover', event => { if (hasFiles(event as DragEvent)) event.preventDefault(); });
  scope.listen(document, 'dragleave', event => {
    // Browsers may conceal DataTransfer types on dragleave; an owned drag still ends.
    if (!depth && !hasFiles(event as DragEvent)) return;
    event.preventDefault(); if (--depth <= 0) clear();
  });
  scope.listen(document, 'drop', event => {
    const e = event as DragEvent;
    if (!hasFiles(e)) return;
    e.preventDefault(); clear();
    // An off-target modal drop must never navigate the tab or attach to chat.
    if (ports.modalOwnsFiles()) return;
    const files = Array.from(e.dataTransfer?.files || []), paper = ports.paperMode();
    void (async () => {
      for (const file of files) {
        if (scope.signal.aborted) return;
        const pdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
        if (paper) { if (pdf) { await ports.handlePaper(file); break; } continue; }
        if (pdf) await ports.handlePdf(file);
        else if (file.type.startsWith('image/')) await ports.handleImage(file);
        else if (ports.isArchive(file)) await ports.handleArchive(file);
        else if (ports.isDocument(file)) await ports.handleDocument(file);
      }
    })().catch(error => { if (!scope.signal.aborted) ports.reportFailure(error); });
  });
}
