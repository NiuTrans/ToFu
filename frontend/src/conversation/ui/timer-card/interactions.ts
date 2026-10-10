/** Timer disclosures and in-place clock paint. No listeners or timers are owned here. */
import { timerNextPollText } from './labels';
import type { TimerClockPorts } from './contracts';

function acceptsActivation(element?: Element | null, event?: Event | null): boolean {
  // A nested copy button or Markdown link owns its own native activation.
  const target = event?.target as Element | null;
  const control = target?.closest?.('a[href],button,input,select,textarea,summary,[role="button"],[contenteditable="true"]');
  if (control && control !== element && element?.contains(control)) return false;
  if (event?.type === 'keydown') {
    const key = (event as KeyboardEvent).key;
    if (key !== 'Enter' && key !== ' ') return false;
    event.preventDefault();
  }
  event?.stopPropagation();
  return true;
}

export function toggleTimerWatcher(element?: Element | null, event?: Event | null, demand?: () => void): void {
  if (!acceptsActivation(element, event)) return;
  const body = element?.closest('.timer-watcher-block')?.querySelector('.timer-watcher-body');
  if (!body) return;
  const expanded = body.classList.toggle('expanded');
  element?.setAttribute('aria-expanded', String(expanded));
  if (expanded && body.querySelector('[data-timer-next]')) demand?.();
}

export function toggleTimerInstruction(element?: Element | null, event?: Event | null): void {
  if (!acceptsActivation(element, event)) return;
  const row = element?.closest('.timer-meta-row-instr');
  const value = row?.querySelector('.timer-meta-val');
  if (!value || !row) return;
  const expanded = value.classList.toggle('expanded');
  row.setAttribute('aria-expanded', String(expanded));
  const label = row.querySelector('.timer-meta-more');
  if (label) label.textContent = label.getAttribute(expanded ? 'data-less' : 'data-more');
}

export function toggleTimerPoll(element?: Element | null, event?: Event | null): void {
  if (!acceptsActivation(element, event)) return;
  const row = element?.closest('.timer-poll-line');
  const detail = row?.nextElementSibling;
  if (!row || !detail?.classList.contains('timer-poll-detail')) return;
  const expanded = detail.classList.toggle('expanded');
  row.classList.toggle('expanded', expanded);
  row.setAttribute('aria-expanded', String(expanded));
}

/** False releases the existing demand-scoped ticker when no visible valid hint remains. */
export function refreshTimerCountdowns(root: ParentNode, ports: TimerClockPorts): boolean {
  let active = false;
  for (const element of root.querySelectorAll('.timer-next-poll[data-timer-next]')) {
    if (element.closest('.timer-watcher-body:not(.expanded)')) continue;
    const nextTs = Number(element.getAttribute('data-timer-next'));
    const text = element.querySelector('.timer-next-poll-txt');
    if (!Number.isFinite(nextTs) || nextTs <= 0 || !text) continue;
    active = true;
    const value = timerNextPollText(nextTs, ports);
    if (text.textContent !== value) text.textContent = value;
  }
  return active;
}
