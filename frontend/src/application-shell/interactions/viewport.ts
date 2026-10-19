/** One layout owner for window/orientation/mobile-keyboard geometry. Conversation
 * follow remains an injected semantic intent, never a scroll-position write. */
import { createLifecycleScope } from '../../lifecycle';
export interface ViewportHeightPorts {
  isMobileViewport?(): boolean;
  maintainConversationFollow?(): void;
}
export function installViewportHeightGuard(
  ownerWindow: Window, document: Document, ports: ViewportHeightPorts = {},
): () => void {
  const scope = createLifecycleScope(ownerWindow);
  const targets = [document.documentElement, document.body];
  const previous = targets.map(node => ['height', '--vh100'].map(name => ({
    name, value: node.style.getPropertyValue(name), priority: node.style.getPropertyPriority(name),
  })));
  scope.add(() => targets.forEach((node, i) => previous[i].forEach(entry => {
    if (entry.value) node.style.setProperty(entry.name, entry.value, entry.priority);
    else node.style.removeProperty(entry.name);
  })));
  let frame = 0, early = 0, late = 0, lastHeight = 0;
  function apply() {
    const mobile = ports.isMobileViewport?.() ?? false;
    const h = mobile && ownerWindow.visualViewport
      ? ownerWindow.visualViewport.height : ownerWindow.innerHeight;
    if (!Number.isFinite(h) || h < 1) return;
    const shrinking = lastHeight > h;
    const changed = lastHeight !== h;
    lastHeight = h;
    const px = `${h}px`;
    for (const node of targets) if (node.style.getPropertyValue('height') !== px || node.style.getPropertyPriority('height') !== 'important')
      node.style.setProperty('height', px, 'important');
    if (document.documentElement.style.getPropertyValue('--vh100') !== px)
      document.documentElement.style.setProperty('--vh100', px);
    if (changed) {
      const input = document.getElementById('userInput');
      if (mobile && shrinking && input === document.activeElement) {
        input?.scrollIntoView({ block: 'end', behavior: 'auto' });
      }
      ports.maintainConversationFollow?.();
    }
  }
  function schedule() { if (!frame) frame = scope.frame(() => { frame = 0; apply(); }); }
  try {
    apply(); schedule();
    scope.listen(ownerWindow, 'resize', schedule);
    if (ownerWindow.visualViewport) {
      scope.listen(ownerWindow.visualViewport, 'resize', schedule);
      scope.listen(ownerWindow.visualViewport, 'scroll', schedule);
    }
    scope.listen(ownerWindow, 'orientationchange', () => {
      scope.cancelTimeout(early); scope.cancelTimeout(late);
      early = scope.timeout(apply, 50); late = scope.timeout(apply, 300);
    });
  } catch (error) { scope.destroy(); throw error; }
  return () => scope.destroy();
}
