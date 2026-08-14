import { createLifecycleScope, type LifecycleScope } from '../lifecycle';
import { snapshotDiagnostics, type FrontendDiagnostics } from './diagnostics';

export interface DebugController {
  readonly diagnostics: FrontendDiagnostics;
  destroy(): void;
}

/** A lifecycle-owned debug attachment for new panels; the legacy drawer remains compatible. */
export function attachDebugController(target: EventTarget = window): DebugController {
  const scope: LifecycleScope = createLifecycleScope();
  let diagnostics = snapshotDiagnostics();
  scope.listen(target, 'tofu:feature-domain-loaded', () => {
    diagnostics = snapshotDiagnostics();
  });
  return {
    get diagnostics() { return diagnostics; },
    destroy: () => scope.destroy(),
  };
}
