/** Standing instructions of the Global Router (docs/ROUTER.md R4): a compact,
 * on-demand list in the router header where the owner pauses, resumes or
 * cancels an instruction. The server owns every record and transition; nothing
 * is read until the owner opens the list, and nothing polls. Pause and resume
 * are owner-only (no model tool); cancellation is final.
 */
import type { RouterInstruction, RouterInstructionPage } from '../api/router.generated';
import { createButton } from '../ui';
import { t } from '../i18n';

export interface RouterInstructionPorts {
  /** GET /api/v4/router/instructions: active and paused instructions. */
  list(cursor: string | undefined, signal: AbortSignal): Promise<RouterInstructionPage>;
  pause(instructionId: string): Promise<RouterInstruction>;
  resume(instructionId: string): Promise<RouterInstruction>;
  cancel(instructionId: string): Promise<RouterInstruction>;
}

type Command = 'pause' | 'resume' | 'cancel';
const COMMAND_LABEL = { pause: 'routerInstructions.pause', resume: 'routerInstructions.resume',
  cancel: 'routerInstructions.cancel' } as const;
const STATE_LABEL = { active: 'routerInstructions.state.active', paused: 'routerInstructions.state.paused',
  cancelled: 'routerInstructions.state.cancelled' } as const;

function failureText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function createRouterInstructions(ports: RouterInstructionPorts) {
  let alive = true, loaded = false, failure = '';
  let rows: RouterInstruction[] = [];
  let cursor: string | null = null;
  let reading: AbortController | null = null;
  const busy = new Set<string>();

  const el = document.createElement('details');
  el.className = 'router-instructions';
  const summary = document.createElement('summary');
  summary.dataset.i18n = 'routerInstructions.title';
  summary.textContent = t('routerInstructions.title');
  const status = document.createElement('p');
  status.className = 'router-instructions-status';
  status.setAttribute('role', 'status');
  const list = document.createElement('ul');
  list.className = 'router-instructions-list';
  const footer = document.createElement('div');
  footer.className = 'router-instructions-footer';
  el.append(summary, status, list, footer);

  function localizedButton(key: (typeof COMMAND_LABEL)[Command] | 'routerInstructions.more' | 'routerInstructions.retry',
    onClick: () => void, variant: 'ghost' | 'danger' = 'ghost') {
    const button = createButton({ label: t(key), size: 'sm', variant, onClick });
    button.dataset.i18n = key;
    return button;
  }

  function row(instruction: RouterInstruction): HTMLLIElement {
    const item = document.createElement('li');
    item.className = 'router-instruction';
    item.dataset.instructionId = instruction.instructionId;
    item.dataset.state = instruction.state;
    const head = document.createElement('div');
    head.className = 'router-instruction-head';
    const label = document.createElement('strong');
    label.textContent = instruction.label;
    const state = document.createElement('span');
    state.className = 'router-instruction-state';
    state.dataset.i18n = STATE_LABEL[instruction.state];
    state.textContent = t(STATE_LABEL[instruction.state]);
    head.append(label, state);
    const text = document.createElement('p');
    text.className = 'router-instruction-text';
    text.textContent = instruction.text;
    text.title = instruction.text;
    const scope = document.createElement('p');
    scope.className = 'router-instruction-scope';
    const project = document.createElement('span');
    if (instruction.projectPath) project.textContent = instruction.projectPath;
    else { project.dataset.i18n = 'routerInstructions.anyProject'; project.textContent = t('routerInstructions.anyProject'); }
    scope.append(project);
    if (instruction.thread) {
      const thread = document.createElement('span');
      thread.textContent = t('routerInstructions.thread', { thread: instruction.thread });
      scope.append(thread);
    }
    const actions = document.createElement('div');
    actions.className = 'router-instruction-actions';
    const commands: Command[] = instruction.state === 'active' ? ['pause', 'cancel']
      : instruction.state === 'paused' ? ['resume', 'cancel'] : [];
    for (const command of commands) {
      const button = localizedButton(COMMAND_LABEL[command], () => { void run(command, instruction.instructionId); },
        command === 'cancel' ? 'danger' : 'ghost');
      button.dataset.instructionAction = command;
      button.disabled = busy.has(instruction.instructionId);
      actions.append(button);
    }
    item.append(head, text, scope, actions);
    return item;
  }

  function render() {
    if (!alive) return;
    const focused = document.activeElement instanceof HTMLElement && list.contains(document.activeElement)
      ? document.activeElement : null;
    const focusId = focused?.closest<HTMLElement>('.router-instruction')?.dataset.instructionId;
    const focusAction = focused?.dataset.instructionAction;
    status.replaceChildren();
    if (reading) {
      status.dataset.i18n = 'common.loading'; status.textContent = t('common.loading');
    } else {
      delete status.dataset.i18n;
      if (failure) status.textContent = failure;
      else if (loaded && !rows.length) {
        status.dataset.i18n = 'routerInstructions.empty'; status.textContent = t('routerInstructions.empty');
      }
    }
    list.replaceChildren(...rows.map(row));
    footer.replaceChildren();
    if (!reading && failure && !loaded) footer.append(localizedButton('routerInstructions.retry', () => { void load(false); }));
    else if (!reading && cursor) footer.append(localizedButton('routerInstructions.more', () => { void load(true); }));
    if (focused && !focused.isConnected) {
      const item = [...list.querySelectorAll<HTMLElement>('.router-instruction')].find(node => node.dataset.instructionId === focusId);
      const replacement = item?.querySelector<HTMLElement>(`[data-instruction-action="${focusAction}"]`)
        ?? item?.querySelector<HTMLElement>('button') ?? summary;
      replacement.focus({ preventScroll: true });
    }
  }

  async function load(more: boolean) {
    if (!alive || reading) return;
    const controller = new AbortController();
    reading = controller; failure = '';
    render();
    try {
      const page = await ports.list(more && cursor ? cursor : undefined, controller.signal);
      if (!alive || reading !== controller) return;
      const known = new Set(more ? rows.map(item => item.instructionId) : []);
      rows = [...(more ? rows : []), ...page.instructions.filter(item => !known.has(item.instructionId))];
      cursor = page.nextCursor; loaded = true;
    } catch (error) {
      if (!alive || reading !== controller) return;
      failure = t('routerInstructions.failed', { error: failureText(error) });
    } finally {
      if (reading === controller) reading = null;
      render();
    }
  }

  async function run(command: Command, instructionId: string) {
    if (!alive || busy.has(instructionId)) return;
    busy.add(instructionId); failure = '';
    render();
    try {
      const next = await ports[command](instructionId);
      if (!alive) return;
      rows = next.state === 'cancelled' ? rows.filter(item => item.instructionId !== instructionId)
        : rows.map(item => (item.instructionId === instructionId ? next : item));
    } catch (error) {
      if (alive) failure = t('routerInstructions.actionFailed', { error: failureText(error) });
    } finally {
      busy.delete(instructionId);
      render();
    }
  }

  // Every opening reads the current first page; a closed list holds no demand.
  el.addEventListener('toggle', () => {
    if (el.open) void load(false);
    else { reading?.abort(); reading = null; }
  });

  return {
    el,
    destroy() {
      alive = false; reading?.abort(); reading = null; el.remove();
    },
  };
}
