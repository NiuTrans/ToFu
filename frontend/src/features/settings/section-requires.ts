import { featureRegistry } from '../../feature-registry';
type RequirementRoot = ParentNode & {
  querySelectorAll(selectors: string): NodeListOf<Element>;
};

type SettingsWindow = Window & {
  applySectionRequirements?: (root?: RequirementRoot) => number;
  debugLog?: (message: string, level?: string) => void;
};

function settingsWindow(): SettingsWindow {
  return featureRegistry as unknown as SettingsWindow;
}

/** True only when every space-separated global named by the section exists. */
export function settingsSymbolsPresent(spec: string | null | undefined): boolean {
  const globals = settingsWindow() as unknown as Record<string, unknown>;
  const names = String(spec ?? '').split(/\s+/).filter(Boolean);
  return names.every((name) => typeof globals[name] !== 'undefined');
}

/**
 * Hide controls whose required JavaScript owner is absent and expose the
 * section's explicit degraded-state notice. Safe to run after every repaint.
 */
export function applySectionRequirements(
  root: RequirementRoot = document,
): number {
  const blocks = root.querySelectorAll(
    '.settings-section-needs-js[data-requires]',
  );
  let degraded = 0;
  blocks.forEach((element) => {
    const requirement = element.getAttribute('data-requires');
    const available = settingsSymbolsPresent(requirement);
    element.classList.toggle('degraded', !available);
    if (available) return;
    degraded += 1;
    settingsWindow().debugLog?.(
      `[Settings] section degraded — missing ${requirement ?? ''} (stale bundle?)`,
      'warning',
    );
  });
  return degraded;
}

settingsWindow().applySectionRequirements = applySectionRequirements;
