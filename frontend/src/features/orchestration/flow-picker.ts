/**
 * Shared desktop/mobile orchestration-flow picker presentation.
 *
 * This typed owner keeps catalogue notices, saved-flow projection, keyboard
 * navigation and the shared icon vocabulary out of retained layout adapters.
 */
import { isErrorEnvelope, normalizeErrorEnvelope } from '../../api/errors';
import { orchestrationRegistry } from './registry';

type Translate = (key: string) => string;

interface FlowCatalogue {
  status?: () => unknown;
}

interface FlowDefinition {
  id?: unknown;
  name?: unknown;
}

export interface FlowPickerItem {
  readonly flow: string;
  readonly name: string;
  readonly desc: string;
}

export interface FlowPickerOptions {
  includeNone?: boolean;
  includeBuiltins?: boolean;
}

export interface FlowPickerWireOptions {
  onSelect?: (flow: string) => unknown;
  onEscape?: () => unknown;
}

interface FlowCatalogueNotice {
  readonly state: string;
  readonly key: string;
  readonly detail: string;
}

const record = (value: unknown): Record<string, unknown> | null => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
);

const translateOrKey = (translate: Translate | undefined, key: string): string => (
  typeof translate === 'function' ? String(translate(key)) : key
);

export function projectOrchestrationFlowCatalogNotice(
  catalog: FlowCatalogue | null | undefined,
): FlowCatalogueNotice | null {
  const status = record(catalog?.status?.()) ?? { state: 'idle' };
  const notices: Readonly<Record<string, string>> = Object.freeze({
    loading: 'toolbar.flowCatalogLoading',
    failed: 'toolbar.flowCatalogFailed',
    stale: 'toolbar.flowCatalogCached',
  });
  const state = typeof status.state === 'string' ? status.state : 'idle';
  const key = notices[state];
  if (!key) return null;

  const cause = status.failure;
  let detail = '';
  if (typeof cause === 'string') detail = cause;
  else if (cause instanceof Error && !isErrorEnvelope(cause)) {
    detail = cause.message;
  } else if (cause != null) {
    detail = normalizeErrorEnvelope(cause)?.message ?? '';
  }
  return Object.freeze({ state, key, detail: detail.trim() });
}

export function renderOrchestrationFlowCatalogNotice(
  element: HTMLElement | null | undefined,
  catalog: FlowCatalogue | null | undefined,
  translate?: Translate,
): boolean {
  if (!element) return false;
  const value = projectOrchestrationFlowCatalogNotice(catalog);
  element.hidden = !value;
  const label = value ? translateOrKey(translate, value.key) : '';
  element.textContent = value?.detail ? `${label} · ${value.detail}` : label;
  if (value) element.setAttribute('data-state', value.state);
  else element.removeAttribute('data-state');
  return Boolean(value);
}

export function projectOrchestrationFlowPickerItems(
  custom: readonly FlowDefinition[] | unknown,
  translate?: Translate,
  options: FlowPickerOptions = {},
): readonly FlowPickerItem[] {
  const items: FlowPickerItem[] = [];
  if (options.includeNone !== false) {
    items.push({
      flow: '',
      name: translateOrKey(translate, 'toolbar.flowNone'),
      desc: translateOrKey(translate, 'toolbar.flowNoneDesc'),
    });
  }
  if (options.includeBuiltins !== false) {
    items.push(
      {
        flow: 'builtin:autopilot',
        name: translateOrKey(translate, 'toolbar.autopilot'),
        desc: translateOrKey(translate, 'toolbar.autopilotDesc'),
      },
    );
  }
  for (const candidate of Array.isArray(custom) ? custom : []) {
    const flow = record(candidate) as FlowDefinition | null;
    if (!flow?.id) continue;
    items.push({
      flow: String(flow.id),
      name: flow.name
        ? String(flow.name) : translateOrKey(translate, 'orch.load.untitled'),
      desc: translateOrKey(translate, 'toolbar.flowCustomDesc'),
    });
  }
  return Object.freeze(items.map((item) => Object.freeze(item)));
}

/** Only Studio-authored definitions belong in the Debug workflow section. */
export function projectOrchestrationSavedWorkflowItems(
  custom: readonly FlowDefinition[] | unknown,
  translate?: Translate,
): readonly FlowPickerItem[] {
  return projectOrchestrationFlowPickerItems(custom, translate, {
    includeNone: false,
    includeBuiltins: false,
  });
}

export function orchestrationFlowPickerDisplayName(
  flow: unknown,
  custom: readonly FlowDefinition[] | unknown,
  translate?: Translate,
): string {
  const value = String(flow || '');
  const match = projectOrchestrationFlowPickerItems(custom, translate)
    .find((item) => item.flow === value);
  return match?.name ?? translateOrKey(translate, 'toolbar.flowCustom');
}

export function reconcileOrchestrationFlowSelection(
  flow: unknown,
  custom: readonly FlowDefinition[] | unknown,
  authoritative: unknown,
): string {
  const value = String(flow || '');
  if (!value || value === 'builtin:autopilot' || authoritative !== true) {
    return value;
  }
  const exists = (Array.isArray(custom) ? custom : []).some((candidate) => {
    const item = record(candidate);
    return item && String(item.id || '') === value;
  });
  return exists ? value : '';
}

const eventRow = (event: Event): HTMLElement | null => {
  const target = event.target as Element | null;
  return target && typeof target.closest === 'function'
    ? target.closest<HTMLElement>('[data-flow]') : null;
};

export function wireOrchestrationFlowPicker(
  list: HTMLElement | null | undefined,
  options: FlowPickerWireOptions = {},
): boolean {
  if (!list) return false;
  const rows = (): HTMLElement[] => Array.from(
    list.querySelectorAll<HTMLElement>('[data-flow]'));
  const focusRow = (row: HTMLElement | null): boolean => {
    if (!row) return false;
    for (const candidate of rows()) {
      candidate.tabIndex = candidate === row ? 0 : -1;
    }
    if (row.ownerDocument?.activeElement !== row) row.focus();
    return true;
  };
  const activate = (row: HTMLElement | null): boolean => {
    if (!row || typeof options.onSelect !== 'function') return false;
    options.onSelect(row.getAttribute('data-flow') || '');
    return true;
  };

  const currentRows = rows();
  const selected = currentRows.find((row) => (
    row.getAttribute('aria-selected') === 'true'
      || row.getAttribute('aria-checked') === 'true'
  )) ?? currentRows[0];
  for (const row of currentRows) row.tabIndex = row === selected ? 0 : -1;

  list.onclick = (event: MouseEvent): void => {
    const row = eventRow(event);
    if (row && list.contains(row)) activate(row);
  };
  (list as HTMLElement & {
    onfocusin: ((event: FocusEvent) => void) | null;
  }).onfocusin = (event: FocusEvent): void => {
    const row = eventRow(event);
    if (row && list.contains(row)) focusRow(row);
  };
  list.onkeydown = (event: KeyboardEvent): void => {
    const current = eventRow(event);
    const values = rows();
    let index = values.indexOf(current as HTMLElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      options.onEscape?.();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(current);
      return;
    }
    if (event.key === 'Home' || event.key === 'End'
        || event.key === 'ArrowDown' || event.key === 'ArrowRight'
        || event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      event.preventDefault();
      if (!values.length) return;
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = values.length - 1;
      else {
        const delta = event.key === 'ArrowDown' || event.key === 'ArrowRight'
          ? 1 : -1;
        index = (Math.max(0, index) + delta + values.length) % values.length;
      }
      focusRow(values[index] ?? null);
    }
  };
  return true;
}

export function orchestrationFlowPickerIcon(flow: unknown): string {
  const stroke = 'fill="none" stroke="currentColor" stroke-width="2" '
    + 'stroke-linecap="round" stroke-linejoin="round"';
  if (flow === 'builtin:autopilot') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" ' + stroke
      + '><circle cx="12" cy="12" r="3"/><path d="M12 1v6m0 10v6m11-11h-6m-10 0H1m17.66-6.34l-4.24 4.24m-5.66 5.66l-4.24 4.24m12.14 0l-4.24-4.24m-5.66-5.66L4.34 4.34"/></svg>';
  }
  if (!flow) {
    return '<svg width="16" height="16" viewBox="0 0 24 24" ' + stroke
      + '><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  }
  return '<svg width="16" height="16" viewBox="0 0 24 24" ' + stroke
    + '><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/>'
    + '<circle cx="18" cy="19" r="3"/>'
    + '<line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/>'
    + '<line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>';
}

type FlowPickerRegistry = {
  projectOrchestrationFlowCatalogNotice?:
    typeof projectOrchestrationFlowCatalogNotice;
  renderOrchestrationFlowCatalogNotice?:
    typeof renderOrchestrationFlowCatalogNotice;
  projectOrchestrationFlowPickerItems?:
    typeof projectOrchestrationFlowPickerItems;
  projectOrchestrationSavedWorkflowItems?:
    typeof projectOrchestrationSavedWorkflowItems;
  orchestrationFlowPickerDisplayName?:
    typeof orchestrationFlowPickerDisplayName;
  reconcileOrchestrationFlowSelection?:
    typeof reconcileOrchestrationFlowSelection;
  wireOrchestrationFlowPicker?: typeof wireOrchestrationFlowPicker;
  orchestrationFlowPickerIcon?: typeof orchestrationFlowPickerIcon;
};

Object.assign(orchestrationRegistry as FlowPickerRegistry, {
  projectOrchestrationFlowCatalogNotice,
  renderOrchestrationFlowCatalogNotice,
  projectOrchestrationFlowPickerItems,
  projectOrchestrationSavedWorkflowItems,
  orchestrationFlowPickerDisplayName,
  reconcileOrchestrationFlowSelection,
  wireOrchestrationFlowPicker,
  orchestrationFlowPickerIcon,
});
