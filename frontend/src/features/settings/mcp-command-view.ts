/** MCP feedback and dialog lifetime, subordinate to the common Settings opening.
 * Commands retain detached values; this owner retains only presentation resources.
 * Entry: McpCommandViews.capture/open/close. No transport or command cancellation.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

export interface McpCommandView {
  readonly session: SettingsReadSession;
  isCurrent(): boolean;
}
export type McpDialogKind = 'install' | 'add';
export interface McpDialogView extends McpCommandView {
  readonly kind: McpDialogKind;
  readonly target?: {id: string; name: string};
  readonly scope: LifecycleScope;
  readonly version: number;
  beginSubmission(): void;
  finishSuccess(button: HTMLButtonElement | null, idleLabel: string, version: number, delay: number): void;
}
const overlays = {install: 'mcpInstallOverlay', add: 'mcpAddOverlay'} as const;

export class McpCommandViews {
  private dialog: McpDialogView | null = null;
  private feedback: McpCommandView | null = null;

  capture(): McpCommandView | null {
    const session = currentSettingsReadSession();
    if (!session?.isCurrent()) return null;
    if (this.feedback?.session !== session) this.feedback = null;
    if (!this.feedback) {
      const view: McpCommandView = {session,
        isCurrent: () => session.isCurrent() && this.feedback === view};
      this.feedback = view;
    }
    return this.feedback;
  }
  current(kind: McpDialogKind): McpDialogView | null {
    return this.dialog?.kind === kind && this.dialog.isCurrent() ? this.dialog : null;
  }
  open(kind: McpDialogKind, target?: {id: string; name: string}): McpDialogView | null {
    if (this.dialog) this.close(this.dialog.kind);
    this.feedback = null;
    const opening = this.capture(), overlay = document.getElementById(overlays[kind]);
    if (!opening || !overlay) return null;
    const scope = createLifecycleScope(window);
    let version = 0;
    let closeTimer = 0;
    let completion: {button: HTMLButtonElement | null; idleLabel: string} | null = null;
    const allowEdit = () => {
      scope.cancelTimeout(closeTimer);
      if (completion?.button) { completion.button.disabled = false; completion.button.textContent = completion.idleLabel; }
      completion = null;
    };
    const view: McpDialogView = {
      kind, scope, session: opening.session, target: target && {id: target.id, name: target.name},
      get version() { return version; },
      isCurrent: () => opening.session.isCurrent() && !scope.signal.aborted && this.dialog === view
        && document.getElementById(overlays[kind]) === overlay,
      beginSubmission() { scope.cancelTimeout(closeTimer); completion = null; },
      finishSuccess: (button, idleLabel, submittedVersion, delay) => {
        if (!view.isCurrent()) return;
        completion = {button, idleLabel};
        if (version !== submittedVersion) { allowEdit(); return; }
        if (button) button.disabled = true;
        closeTimer = scope.timeout(() => { if (view.isCurrent()) this.close(kind); }, delay);
      },
    };
    this.dialog = view;
    scope.listen(opening.session.scope.signal, 'abort', () => this.close(kind), {once: true});
    for (const event of ['input', 'change']) scope.listen(document, event, e => {
      if (e.target instanceof window.Node && overlay.contains(e.target)) { version++; allowEdit(); }
    });
    scope.add(() => {
      overlay.style.display = 'none';
      overlay.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea').forEach(input => { input.value = ''; });
    });
    return view;
  }
  close(kind: McpDialogKind): void {
    if (this.dialog?.kind !== kind) return;
    const view = this.dialog;
    this.dialog = null;
    this.feedback = null;
    view.scope.destroy();
  }
}

/** Busy feedback retires before the next Settings opening can reuse its node. */
export function ownMcpBusyButton(view: McpCommandView, button: HTMLButtonElement | null, restore: () => void): LifecycleScope {
  const scope = createLifecycleScope(window);
  scope.listen(view.session.scope.signal, 'abort', () => scope.destroy(), {once: true});
  scope.add(() => {
    if ((!currentSettingsReadSession() || view.session.isCurrent())
        && (!button || document.getElementById(button.id) === button)) restore();
  });
  return scope;
}
