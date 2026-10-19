/** Composer depth projection: native keyboard range, continuous pointer preview,
 * discrete commit through selectThinkingDepth. No persistence or provider policy.
 * Entry: createThinkingDepthSlider; dependencies: DOM, localization, commit port. */
// Rolling-upgrade fallback only. Current backends supply the complete ladder.
import type { ReasoningControls } from '../api/api-v1-model-routing.generated';
export const THINKING_DEPTH_LEVELS = ['off', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
export type ThinkingDepth = string;
export const THINKING_DEPTH_LABELS: Record<string, string> = { default: 'Default', off: 'Off', enabled: 'On', lowest: 'Lowest', low: 'Low', medium: 'Med', high: 'High', xhigh: 'xHigh', max: 'Max', ultra: 'Ultra' };
export type ReasoningControl = ReasoningControls;
export interface DepthOptions { levels: readonly ThinkingDepth[]; visible?: boolean; defaultDepth?: string | null; available?: boolean; notice?: string; restoration?: ReasoningControls['restoration'] }
/** Server facts only: confirmed identity uses exact Creator + Model + Provider;
 * pending identity uses exact Provider + Offering. Undefined means old backend. */
export function projectReasoningControl(rows: readonly ReasoningControl[] | undefined,
  ref: { creator_id?: string; model_id?: string; provider_id?: string; offering_id?: string } | null | undefined,
  providerId: string = '', displayedModelId: string = ''): DepthOptions | undefined {
  if (rows === undefined) return undefined;
  const matches = rows.filter(row => ref?.offering_id
    ? row.provider_id === ref.provider_id && row.offering_id === ref.offering_id
    : !!ref?.creator_id && !!ref.model_id && row.creator_id === ref.creator_id
      && row.model_id === ref.model_id && row.provider_id === providerId && !row.offering_id);
  let row = matches.length === 1 ? matches[0] : undefined;
  /* The composer can paint a model with no confirmed v2 ref (a provisional
   * server default, or a conversation that never stored modelRef). The
   * displayed model id IS the intent, so resolve it to its unique confirmed
   * identity instead of reporting the route as unconfigured. Only a unique
   * model_id resolves: model ids are unique across creators in the v2
   * picker, and an ambiguous id must not silently bind one creator. */
  if (!row && !ref?.offering_id && !ref?.model_id && displayedModelId) {
    const byModel = rows.filter(candidate => candidate.model_id === displayedModelId
      && candidate.creator_id && candidate.provider_id === providerId && !candidate.offering_id);
    row = byModel.length === 1 ? byModel[0] : undefined;
  }
  /* A persisted provider pin may outlive the provider (renamed/removed). The
   * exact key then matches nothing and the composer would report the route as
   * unconfigured. Fall back to the model-level auto row, which the compiler
   * always emits for an installed model. */
  if (!row && !ref?.offering_id && providerId && !!ref?.creator_id && !!ref.model_id) {
    const automatic = rows.filter(candidate => candidate.creator_id === ref.creator_id
      && candidate.model_id === ref.model_id && !candidate.provider_id && !candidate.offering_id);
    row = automatic.length === 1 ? automatic[0] : undefined;
  }
  const levels = row?.support === 'supported'
    ? row.depths.filter(depth => depth !== 'off' || row.can_disable === true) : [];
  const notice = row?.availability === 'no_route' ? 'mobile.depthNoRoute'
    : !row || row.support === 'unknown' ? 'mobile.depthUnconfigured'
    : row.support === 'supported' && levels.every(d => d === 'default') ? 'mobile.depthNoCommon'
    : levels.filter(d => d !== 'default').length === 1 ? 'mobile.depthFixed' : '';
  return { levels: [...new Set<ThinkingDepth>(['default', ...levels])],
    /* Visible when a route was resolved — by exact ref, or by the unique
     * model-id fallback above (a resolved row proves the displayed model is
     * a known identity, so its controls should show). */
    visible: row?.support !== 'unsupported' && !!(ref?.model_id || ref?.offering_id || row),
    restoration: row?.restoration, defaultDepth: row?.default_depth, available: row?.support === 'supported' && row.availability !== 'no_route', notice };
}
export function displayThinkingDepth(depth: string | null | undefined): ThinkingDepth {
  if (depth === 'ultra') return 'ultra';
  return (THINKING_DEPTH_LEVELS as readonly string[]).includes(depth as string) ? depth as ThinkingDepth : 'medium';
}
/* A stored depth the current ladder cannot select stays committed verbatim
 * (persistence is gesture-only), but the slider paints the server's mapped
 * replacement rung instead of manufacturing an unusable flagged one. The
 * replacement was clamped against the final route intersection, so the
 * painted position is exactly what the wire would execute. */
function restoredDepth(depth: string, selectable: readonly string[], restoration: DepthOptions['restoration'] | undefined): string {
  if (depth === 'default' || selectable.includes(depth)) return depth;
  const replacement = restoration?.replacements?.[depth] ?? restoration?.fallback;
  return replacement && selectable.includes(replacement) ? replacement : depth;
}
export function createThinkingDepthSlider(ports: {
  document: Document;
  translate: (key: string) => string;
  commit: (depth: ThinkingDepth) => void;
}) {
  let committed: string = 'medium';
  let levels: readonly string[] = THINKING_DEPTH_LEVELS;
  let selectable: readonly string[] = THINKING_DEPTH_LEVELS;
  const maxIndex = () => Math.max(1, levels.length - 1);
  let defaultDepth: string | null | undefined;
  let notice = '';
  let restoration: DepthOptions['restoration'] | undefined;
  const committedIndex = () => Math.max(0, levels.indexOf(committed === 'default'
    ? defaultDepth ?? '' : restoredDepth(committed, selectable, restoration)));
  let position = 2;
  let enabled = false;
  let drag: {
    input: HTMLInputElement; pointerId: number; cancelled: boolean;
    left: number; span: number; grabOffset: number; coordinateDriven: boolean;
  } | null = null;
  const controls = () => Array.from(ports.document.querySelectorAll<HTMLElement>('[data-thinking-depth-slider]'));
  const clamp = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(levels.length - 1, value)) : committedIndex();
  const depthAt = (value: number) => levels[Math.round(clamp(value))];

  function localize() {
    paint(position);
  }
  function paint(value: number, source?: HTMLInputElement) {
    position = clamp(value);
    const depth = depthAt(position);
    const restored = restoredDepth(committed, selectable, restoration);
    const parked = !drag && restored !== committed && depth === restored;
    const displayDepth = parked ? committed
      : committed === 'default' && !drag && !defaultDepth && position === committedIndex() ? 'default' : depth;
    /* The parked thumb still marks the saved intent as unavailable, even
     * though its painted rung is selectable — the rung is the mapped
     * replacement, not the stored choice. */
    const available = !parked && selectable.includes(depth);
    const display = THINKING_DEPTH_LABELS[displayDepth] || displayDepth;
    const label = (THINKING_DEPTH_LABELS[displayDepth] ? ports.translate(`mobile.${displayDepth}`) : displayDepth)
      + (available ? '' : ` — ${ports.translate('mobile.depthUnavailable')}`);
    for (const control of controls()) {
      const input = control.querySelector<HTMLInputElement>('input[type="range"]');
      if (!input) continue;
      const valueEl = control.querySelector<HTMLElement>('[data-depth-value]');
      let reset = control.querySelector<HTMLButtonElement>('[data-depth-default]');
      if (!reset) {
        reset = ports.document.createElement('button');
        reset.type = 'button'; reset.dataset.depthDefault = '';
        reset.className = 'depth-default-button';
        control.querySelector('.depth-slider-heading')?.append(reset);
      }
      reset.textContent = ports.translate('mobile.default');
      reset.setAttribute('aria-pressed', String(committed === 'default' && !drag));
      reset.hidden = !drag && committed !== 'default' && !selectable.includes(committed)
        && restoredDepth(committed, selectable, restoration) !== committed;
      let note = control.querySelector<HTMLElement>('[data-depth-notice]');
      if (!note) {
        note = ports.document.createElement('p'); note.dataset.depthNotice = '';
        note.className = 'depth-slider-notice'; note.setAttribute('role', 'status');
        control.append(note);
      }
      note.textContent = notice ? ports.translate(notice) : '';
      note.hidden = !notice;
      control.style.setProperty('--depth-step-count', String(maxIndex()));
      for (const dots of control.querySelectorAll<HTMLElement>('.depth-slider-dots')) {
        if (dots.childElementCount !== levels.length) {
          dots.replaceChildren(...levels.map((_, index) => {
            const dot = ports.document.createElement('span');
            dot.style.setProperty('--i', String(index));
            return dot;
          }));
        }
      }
      const visibleValue = available ? display : `${display} — ${ports.translate('mobile.depthUnavailable')}`;
      if (valueEl && valueEl.textContent !== visibleValue) valueEl.textContent = visibleValue;
      input.max = String(Math.max(0, levels.length - 1));
      control.dataset.depthAvailable = String(available);
      const step = drag && !drag.cancelled ? 'any' : '1';
      if (input.step !== step) input.step = step;
      // Fractional pointer position drives the native thumb, fill and the
      // rounded effort label together; native input events keep their value.
      if (input !== source && input.value !== String(position)) input.value = String(position);
      if (input.disabled === enabled) input.disabled = !enabled;
      if (input.getAttribute('aria-valuetext') !== label) input.setAttribute('aria-valuetext', label);
      control.style.setProperty('--depth-fraction', String(position / maxIndex()));
    }
  }
  function dragging(event: Event) {
    return !!drag && !drag.cancelled && event.target === drag.input;
  }
  function target(event: Event) {
    const input = event.target as HTMLInputElement | null;
    return input?.matches?.('[data-thinking-depth-slider] input[type="range"]') ? input : null;
  }
  function resetDefault(event: Event) {
    const target = event.target as HTMLElement | null;
    if (!target?.closest?.('[data-thinking-depth-slider] [data-depth-default]')) return;
    committed = 'default';
    paint(committedIndex());
    ports.commit('default');
  }
  function begin(event: PointerEvent) {
    const input = target(event);
    if (!input || !enabled || event.button !== 0 || event.isPrimary === false) return;
    if (drag && !drag.cancelled) return;

    input.focus({ preventScroll: true });
    // Cache geometry once per gesture, not on every move. Native touch ranges
    // have an activation dead zone even with step=any; capture bypasses it.
    const rect = input.getBoundingClientRect();
    const thumb = parseFloat(ports.document.defaultView?.getComputedStyle(input)
      .getPropertyValue('--depth-thumb-size') || '26');
    const left = rect.left + thumb / 2;
    const span = Math.max(1, rect.width - thumb);
    const committedCenter = left + span * committedIndex() / maxIndex();
    // ClientX may lag the native 'any' value when the browser's own gesture
    // pre-renders the press, so the grab anchor follows the snapped value.
    const center = rect.width > thumb ? left + span * Number(input.value) / maxIndex() : committedCenter;
    const grabbed = rect.width <= thumb || Math.abs(event.clientX - center) <= thumb / 2 || Math.abs(event.clientX - committedCenter) <= thumb / 2;
    const grabOffset = grabbed ? event.clientX - center : 0;
    drag = { input, pointerId: event.pointerId, cancelled: false, left, span, grabOffset,
      coordinateDriven: rect.width > thumb };
    input.setPointerCapture?.(event.pointerId);
    event.preventDefault();
    // Zero-layout test DOMs can still exercise native input/change separately.
    if (rect.width > thumb) move(event);
    else paint(Number(input.value), input);
  }
  function move(event: PointerEvent) {
    if (!drag || drag.cancelled || drag.pointerId !== event.pointerId) return;
    // We prevented the browser's native drag, so update the actual range as
    // well as its fill. Setting value does not dispatch a native input event;
    // geometry remains the one snapshot captured at pointerdown.
    paint((event.clientX - drag.left - drag.grabOffset) / drag.span * maxIndex());
  }
  function releaseCapture(input: HTMLInputElement | null, pointerId: number) {
    if (input?.hasPointerCapture?.(pointerId)) input.releasePointerCapture(pointerId);
  }
  function preview(event: Event) {
    const input = target(event);
    if (!input || !enabled) return;
    if (dragging(event) && drag?.coordinateDriven) {
      // Touch engines may also emit a native input after pointermove. Keep
      // the captured position authoritative for both native thumb and fill.
      paint(position);
      return;
    }
    if (drag?.cancelled) {
      if (input === drag.input) { paint(committedIndex()); return; }
    } else if (drag && input !== drag.input) return;
    // Outside a captured pointer gesture the browser's native integer input
    // (keyboard, IME, assistive tech) is authoritative. Round anyway: the
    // input's step may still be 'any' from a cancelled gesture that never
    // got its restore paint.
    paint(drag ? Number(input.value) : Math.round(Number(input.value)), input);
  }
  function finish(input: HTMLInputElement) {
    const active = drag;
    const cancelled = active?.cancelled;
    // Capture the candidate before repainting: paint changes position and
    // input.value. Reading afterward would always recommit the old depth.
    const value = active && !cancelled && input === active.input ? position : Number(input.value);
    drag = null;
    if (active) releaseCapture(active.input, active.pointerId);
    if (cancelled || !enabled) { paint(committedIndex()); return; }
    const depth = depthAt(value);
    if (!selectable.includes(depth) || depth === committed) { paint(committedIndex()); return; }
    committed = depth;
    paint(committedIndex());
    ports.commit(depth as ThinkingDepth);
  }
  function change(event: Event) {
    if (dragging(event) && drag?.coordinateDriven) return;
    const input = target(event);
    if (input) finish(input);
  }
  function release(event: PointerEvent) {
    // Also finish a drag released outside the element or with no native change
    // (e.g. a click on the current thumb). A later native change is idempotent.
    if (drag && drag.pointerId === event.pointerId) finish(drag.input);
  }
  function cancel(event: Event) {
    if (!target(event) && !drag) return;
    if (drag) drag.cancelled = true;
    paint(committedIndex());
    releaseCapture(drag?.input ?? null, drag?.pointerId ?? -1);
  }
  function restore() {
    // A cancelled gesture still owns the control: re-anchor integer stepping
    // and the committed value or the next native gesture starts elsewhere.
    if (drag) { releaseCapture(drag.input, drag.pointerId); drag = null; }
    paint(committedIndex());
  }
  function pointercancel(event: PointerEvent) {
    // Keep the cancelled record so the dead pointer's late input/change/
    // pointerup stragglers stay suppressed until a fresh gesture drains it.
    if (drag && drag.pointerId === event.pointerId) cancel(event);
  }
  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && target(event)) cancel(event);

    if (target(event) && drag
        && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
      // A keyboard gesture starts a fresh integer interaction, even if a
      // cancelled pointer never released or another pointer is still held.
      restore();
    }
  }
  ports.document.defaultView?.addEventListener('tofu:language-change', localize);
  ports.document.defaultView?.addEventListener('blur', cancel);
  ports.document.addEventListener('pointerdown', begin);
  ports.document.addEventListener('click', resetDefault);

  ports.document.addEventListener('pointermove', move);
  ports.document.addEventListener('lostpointercapture', pointercancel);
  ports.document.addEventListener('focusout', cancel);
  ports.document.addEventListener('input', preview);
  ports.document.addEventListener('change', change);
  ports.document.addEventListener('pointerup', release);
  ports.document.addEventListener('pointercancel', pointercancel);
  ports.document.addEventListener('keydown', keydown);
  localize();
  return {
    sync(depth: string | null | undefined, capable: boolean | DepthOptions) {
      const legacy = typeof capable === 'boolean';
      const nextCommitted = legacy ? displayThinkingDepth(depth) : depth || 'default';
      defaultDepth = legacy ? undefined : capable.defaultDepth;
      notice = legacy ? '' : capable.notice ?? '';
      const offered = legacy ? THINKING_DEPTH_LEVELS : [...new Set(capable.levels)];
      const native = offered.filter(depth => depth !== 'default');
      const nextSelectable = native.length ? native : ['default'];
      const nextEnabled = legacy ? capable : capable.available !== false && nextSelectable.length > 1;
      // Background toolbar refreshes often re-project identical state. They
      // must not cancel pointer capture or snap an in-progress gesture back.
      const unchanged = committed === nextCommitted && enabled === nextEnabled
        && selectable.length === nextSelectable.length
        && selectable.every((value, index) => value === nextSelectable[index]);
      if (unchanged && drag && !drag.cancelled) return;
      if (drag) drag.cancelled = true;
      releaseCapture(drag?.input ?? null, drag?.pointerId ?? -1);
      selectable = nextSelectable;
      // Renderer-only, verbatim intent. committed is always the saved depth,
      // never rewritten by the render path. A depth the current ladder cannot
      // select stays committed: the server restoration table maps it onto an
      // executable rung when one exists (parked, flagged); only an unmapped
      // depth is appended as an unavailable, flagged rung. sync() persists
      // nothing: persistence is gesture-only (finish/resetDefault), so the
      // choice survives a later rebinding back to a ladder that can
      // represent it.
      committed = nextCommitted;
      restoration = legacy ? undefined : capable.restoration;
      /* levels drive the native range's indices. The native ladder excludes
       * the non-positional 'default' intent rung (it is the reset button),
       * so the parked replacement's index aligns with the physical thumb. */
      levels = committed === 'default' || selectable.includes(committed)
        || restoredDepth(committed, selectable, restoration) !== committed
        ? selectable : [...selectable, committed];
      const unavailable = !selectable.includes(committed);
      const visible = legacy ? capable : capable.visible !== false || unavailable;
      enabled = nextEnabled;
      for (const id of ['thinkingDepthSection', 'mobileDepthSection']) {
        const section = ports.document.getElementById(id);
        if (section) section.style.display = visible ? '' : 'none';
      }
      paint(committedIndex());
    },
    destroy() {
      releaseCapture(drag?.input ?? null, drag?.pointerId ?? -1);
      drag = null;
      paint(committedIndex());
      ports.document.defaultView?.removeEventListener('tofu:language-change', localize);
      ports.document.defaultView?.removeEventListener('blur', cancel);
      ports.document.removeEventListener('click', resetDefault);
      ports.document.removeEventListener('pointerdown', begin);

      ports.document.removeEventListener('pointermove', move);
      ports.document.removeEventListener('lostpointercapture', pointercancel);
      ports.document.removeEventListener('focusout', cancel);
      ports.document.removeEventListener('input', preview);
      ports.document.removeEventListener('change', change);
      ports.document.removeEventListener('pointerup', release);
      ports.document.removeEventListener('pointercancel', pointercancel);
      ports.document.removeEventListener('keydown', keydown);
    },
  };
}
