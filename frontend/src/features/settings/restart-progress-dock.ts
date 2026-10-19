/** Non-modal presentation for the existing restart observer.
 * The observer still owns requests, identity, failure/retry and completion.
 * This view retains only the last 32 distinct diagnostic projections per card;
 * removal releases its weakly-held view state. No timers or transport.
 */
import { createButton } from '../../ui';
import type { RestartBoot } from './restart-observation';

interface DockState { logs: HTMLElement; entries: string[]; signature: string; }
const views = new WeakMap<HTMLElement, DockState>();

export function dockRestartProgress(card: HTMLElement | null, label: string): void {
  if (!card) return;
  const doc = card.ownerDocument;
  doc.getElementById('restart-progress-dock')?.remove();
  const dock = doc.createElement('aside');
  dock.id = 'restart-progress-dock';
  dock.className = 'boot-splash';
  card.classList.add('boot-splash__card');
  const logs = doc.createElement('pre');
  logs.className = 'boot-splash__logs';
  logs.hidden = true;
  logs.id = 'restart-debug-logs';
  logs.tabIndex = 0;
  logs.setAttribute('aria-label', 'Debug logs');
  const toggle = createButton({label, variant: 'ghost', size: 'sm', onClick: () => {
    logs.hidden = !logs.hidden;
    toggle.setAttribute('aria-expanded', String(!logs.hidden));
  }}, doc);
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', logs.id);
  card.append(toggle, logs);
  dock.appendChild(card);
  doc.body.appendChild(dock);
  doc.getElementById('updateModal')?.classList.remove('open');
  views.set(card, {logs, entries: [], signature: ''});
}

export function recordRestartDiagnostic(card: HTMLElement | null, boot: RestartBoot | string): void {
  if (!card) return;
  const view = views.get(card);
  if (!view) return;
  const phase = typeof boot === 'string' ? null : boot.failedPhase || boot.currentPhase;
  const line = typeof boot === 'string' ? boot : [boot.status,
    phase ? `[${phase.index}/${boot.phaseTotal || '?'}] ${phase.key}` : '',
    boot.error || '',
  ].filter(Boolean).join(' · ');
  if (line === view.signature) return;
  view.signature = line;
  view.entries.push(`${new Date().toLocaleTimeString()}  ${line.slice(0, 2000)}`);
  view.entries = view.entries.slice(-32);
  view.logs.textContent = view.entries.join('\n');
}

export function offerRestartReload(card: HTMLElement | null, label: string, dismissLabel: string): void {
  if (!card) return;
  // Existing boot-ready consumers refresh catalog/model data without reload.
  card.ownerDocument.defaultView?.dispatchEvent(new Event('tofu:boot-ready'));
  card.appendChild(createButton({label, variant: 'secondary', size: 'sm',
    onClick: () => card.ownerDocument.defaultView?.location.reload(),
  }, card.ownerDocument));
  card.appendChild(createButton({label: dismissLabel, variant: 'ghost', size: 'sm',
    onClick: () => card.closest('#restart-progress-dock')?.remove(),
  }, card.ownerDocument));
}
