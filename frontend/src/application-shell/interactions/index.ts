/** Application DOM interaction composition. No import-time effects; one scope owns native owners and rolls back failed installation. */
import { createLifecycleScope } from '../../lifecycle';
import { installSidebarInteractions, type SidebarInteractionPorts } from './sidebar';
import { installComposerInput, type ComposerInputPorts } from './composer-input';
import { installFileDrop, type FileDropPorts } from './file-drop';
import { installChromeGeometry, type ChromeGeometryPorts } from './chrome-geometry';
import { installShellShortcuts, type ShellShortcutPorts } from './shortcuts';
export { installViewportHeightGuard } from './viewport';
export interface ApplicationInteractionPorts {
  window: Window; document: Document;
  initializeFolders(): void;
  sidebar: SidebarInteractionPorts; composer: ComposerInputPorts; files: FileDropPorts;
  geometry: ChromeGeometryPorts; shortcuts: ShellShortcutPorts;
}
export function installApplicationInteractions(ports: ApplicationInteractionPorts): () => void {
  const scope = createLifecycleScope(ports.window), document = ports.document;
  try {
    const list = document.getElementById('convList');
    if (list) installSidebarInteractions(scope, ports.window, list, ports.sidebar);
    ports.initializeFolders();
    const input = document.getElementById('userInput') as HTMLTextAreaElement | null;
    if (input) installComposerInput(scope, input, ports.composer);
    installFileDrop(scope, document, ports.files);
    const settings = document.getElementById('settingsModal');
    if (settings) scope.listen(settings, 'click', event => { if (event.target === settings) ports.shortcuts.closeSettings(); });
    installChromeGeometry(scope, document, ports.geometry);
    installShellShortcuts(scope, document, ports.shortcuts);
  } catch (error) { scope.destroy(); throw error; }
  return () => scope.destroy();
}
