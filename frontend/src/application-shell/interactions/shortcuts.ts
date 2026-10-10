/** Global shell shortcuts with explicit Escape priority; no eager feature loading. */
import type { LifecycleScope } from '../../lifecycle';
export interface ShellShortcutPorts {
  toggleSearch(): void;
  closeSearch(): void;
  closeApply(): void;
  closePreview(): void;
  closeSettings(): void;
  closeProject(): void;
  closeLocalControl(): void;
  closeDailyReport(): void;
}
export function installShellShortcuts(scope: LifecycleScope, document: Document, ports: ShellShortcutPorts): void {
  const priorities: readonly [string, () => void][] = [
    ['applyModal', ports.closeApply], ['previewModal', ports.closePreview],
    ['settingsModal', ports.closeSettings], ['projectModal', ports.closeProject],
    ['localControlModal', ports.closeLocalControl], ['dailyReportModal', ports.closeDailyReport],
  ];
  scope.listen(document, 'keydown', event => {
    const e = event as KeyboardEvent;
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); ports.toggleSearch(); return; }
    if (e.key !== 'Escape') return;
    const search = document.getElementById('sidebarSearchWrapper');
    if (search && search.style.display !== 'none') { ports.closeSearch(); e.preventDefault(); return; }
    for (const [id, close] of priorities) {
      if (!document.getElementById(id)?.classList.contains('open')) continue;
      close(); e.preventDefault(); return;
    }
  });
}
