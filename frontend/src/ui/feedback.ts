/**
 * tofu-ui feedback primitives: spinner / skeleton / progress / empty state.
 *
 * Responsibility: small presentational factories with correct aria wiring
 * (status roles, progressbar values). These have no interactive behavior,
 * so the factories stay one-liners.
 * Entry points: createSpinner, createSkeleton, createProgress,
 *   createEmptyState.
 * Dependencies: createButton (button.ts) for the empty-state action;
 *   styles from 30-ui-kit.css.
 */

import { createButton } from './button';
import type { ButtonOptions } from './button';

export interface SpinnerOptions {
  size?: 'sm' | 'md' | 'lg';
  label?: string; // accessible announcement, default '加载中'
}

export function createSpinner(options: SpinnerOptions = {}): HTMLElement {
  const el = document.createElement('span');
  const size = options.size ?? 'md';
  el.className = `ui-spinner${size === 'md' ? '' : ` ui-spinner--${size}`}`;
  el.setAttribute('role', 'status');
  el.setAttribute('aria-label', options.label ?? '加载中');
  return el;
}

export interface SkeletonOptions {
  lines?: number; // text-line placeholders; 0 → one block of `height`
  height?: string; // e.g. '120px' — block variant only
}

export function createSkeleton(options: SkeletonOptions = {}): HTMLElement {
  const wrap = document.createElement('div');
  wrap.setAttribute('aria-hidden', 'true');
  const lines = options.lines ?? 0;
  if (lines > 0) {
    for (let i = 0; i < lines; i += 1) {
      const line = document.createElement('div');
      line.className = 'ui-skeleton ui-skeleton--text';
      // Deterministic rhythm: the last line reads as a paragraph tail.
      if (i === lines - 1) line.style.width = '60%';
      wrap.appendChild(line);
    }
  } else {
    const block = document.createElement('div');
    block.className = 'ui-skeleton';
    block.style.height = options.height ?? '80px';
    wrap.appendChild(block);
  }
  return wrap;
}

export interface ProgressOptions {
  value?: number; // 0–100; omit/undefined with indeterminate
  indeterminate?: boolean;
  label?: string;
}

export interface ProgressHandle {
  el: HTMLDivElement;
  setValue: (value: number) => void;
}

export function createProgress(options: ProgressOptions = {}): ProgressHandle {
  const el = document.createElement('div');
  const indeterminate = options.indeterminate ?? options.value === undefined;
  el.className = `ui-progress${indeterminate ? ' ui-progress--indeterminate' : ''}`;
  el.setAttribute('role', 'progressbar');
  if (options.label) el.setAttribute('aria-label', options.label);

  const bar = document.createElement('div');
  bar.className = 'ui-progress__bar';
  el.appendChild(bar);

  const setValue = (value: number): void => {
    const clamped = Math.min(100, Math.max(0, value));
    el.style.setProperty('--ui-progress-value', `${clamped}%`);
    el.setAttribute('aria-valuenow', String(Math.round(clamped)));
  };
  if (!indeterminate) {
    el.setAttribute('aria-valuemin', '0');
    el.setAttribute('aria-valuemax', '100');
    setValue(options.value ?? 0);
  }
  return { el, setValue };
}

export interface EmptyStateOptions {
  icon?: Node;
  title: string;
  description?: string;
  action?: ButtonOptions;
}

export function createEmptyState(options: EmptyStateOptions): HTMLElement {
  const el = document.createElement('div');
  el.className = 'ui-empty';
  if (options.icon) {
    const icon = document.createElement('div');
    icon.className = 'ui-empty__icon';
    icon.appendChild(options.icon);
    el.appendChild(icon);
  }
  const title = document.createElement('div');
  title.className = 'ui-empty__title';
  title.textContent = options.title;
  el.appendChild(title);
  if (options.description) {
    const description = document.createElement('div');
    description.className = 'ui-empty__description';
    description.textContent = options.description;
    el.appendChild(description);
  }
  if (options.action) {
    const action = document.createElement('div');
    action.className = 'ui-empty__action';
    action.appendChild(createButton(options.action));
    el.appendChild(action);
  }
  return el;
}
