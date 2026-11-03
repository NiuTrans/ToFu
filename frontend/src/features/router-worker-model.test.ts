// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRouterWorkerModel, type RouterWorkerModelPorts } from './router-worker-model';
import type { RouterState } from '../api/router.generated';
import type { PickerModel } from '../application-shell/model-picker';
const i18n = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('../i18n', () => ({
  t: (key: string, values?: unknown) => `${i18n.language}:${key}${values ? JSON.stringify(values) : ''}`,
}));

const state = (workerModel: string): RouterState => ({ projectPath: '/work/app', conversationId: 'coordinator', revision: 3,
  availability: 'available', capabilities: { assign: false, sendMessage: false, steerSubagent: true }, workerModel });
const catalog: PickerModel[] = [
  { model_id: 'strong-model', provider_id: 'a' },
  { model_id: 'fast-model', provider_id: 'a' },
  { model_id: 'fast-model', provider_id: 'b' },
  { model_id: 'hidden-model' },
];

function setup(options: { read?: RouterWorkerModelPorts['read']; initial?: RouterState; models?: PickerModel[] } = {}) {
  let models = options.models ?? catalog;
  const write = vi.fn(async (_path: string, model: string | null) => state(model ?? ''));
  const ports: RouterWorkerModelPorts = {
    read: vi.fn(options.read ?? (async () => state('fast-model'))),
    write,
    catalog: {
      models: () => models,
      visible: model => model.model_id !== 'hidden-model',
      compare: (left, right) => String(left.model_id).localeCompare(String(right.model_id)),
      label: id => `Label ${id}`,
    },
  };
  const control = createRouterWorkerModel(ports, '/work/app', options.initial);
  document.body.replaceChildren(control.el);
  const field = control.el.querySelector<HTMLElement>('.ui-field')!;
  const select = control.el.querySelector('select')!;
  const failure = control.el.querySelector<HTMLElement>('.router-worker-model-failure')!;
  const retry = control.el.querySelector<HTMLButtonElement>('button')!;
  const values = () => [...select.options].map(option => option.value);
  // The closed select is the at-a-glance state: what it shows is the selected option.
  const shown = () => select.selectedOptions[0]?.textContent;
  const choose = (value: string) => { select.value = value; select.dispatchEvent(new Event('change')); };
  return { ports, write, control, field, select, failure, retry, values, shown, choose,
    setModels: (next: PickerModel[]) => { models = next; } };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

afterEach(() => { i18n.language = 'zh'; vi.restoreAllMocks(); document.body.replaceChildren(); });

describe('Router Worker model control', () => {
  it('is one compact labelled select that reads the stored model once and shows it as its value', async () => {
    const { ports, control, field, select, shown, values } = setup();
    // No disclosure to open first: the label sits beside the select on one row.
    expect(control.el.querySelector('details, summary')).toBeNull();
    expect(field.classList.contains('ui-field--sm')).toBe(true);
    const label = field.querySelector('label')!;
    expect(label.htmlFor).toBe(select.id);
    expect(label.dataset.i18n).toBe('routerWorkerModel.label');
    expect(select.title).toBe('zh:routerWorkerModel.help');
    expect(select.dataset.i18nTitle).toBe('routerWorkerModel.help');
    expect(ports.read).toHaveBeenCalledWith('/work/app', expect.any(AbortSignal));
    // Until the server answers it claims no value, not even the owner default.
    expect(select.disabled).toBe(true);
    expect(shown()).toBe('zh:common.loading');
    await settle();
    expect(ports.read).toHaveBeenCalledTimes(1);
    expect(shown()).toBe('Label fast-model');
    expect(values()).toEqual(['', 'fast-model', 'strong-model']);
    expect(select.options[0].dataset.i18n).toBe('routerWorkerModel.ownerDefault');
    expect(select.disabled).toBe(false);
  });

  it('writes the chosen model, and null for the owner default model', async () => {
    const { write, shown, choose, select } = setup({ initial: state('') });
    expect(select.value).toBe('');
    expect(shown()).toBe('zh:routerWorkerModel.ownerDefault');
    choose('strong-model');
    await settle();
    expect(write).toHaveBeenLastCalledWith('/work/app', 'strong-model');
    expect(shown()).toBe('Label strong-model');
    choose('');
    await settle();
    expect(write).toHaveBeenLastCalledWith('/work/app', null);
    expect(shown()).toBe('zh:routerWorkerModel.ownerDefault');
    expect(write).toHaveBeenCalledTimes(2);
  });

  it('keeps the stored value and shows the refusal when the server rejects a choice', async () => {
    const { write, choose, select, control } = setup({ initial: state('fast-model') });
    write.mockRejectedValueOnce(new Error('not routable for this owner'));
    choose('strong-model');
    expect(control.el.getAttribute('aria-busy')).toBe('true');
    expect(select.disabled).toBe(true);
    await settle();
    expect(select.value).toBe('fast-model');
    expect(control.el.querySelector('.ui-field__error')!.textContent).toContain('not routable for this owner');
    expect(control.el.hasAttribute('aria-busy')).toBe(false);
    expect(select.disabled).toBe(false);
  });

  it('shows a stored model the catalog no longer lists and picks up a catalog that loads later', () => {
    const { values, setModels, select } = setup({ initial: state('retired-model'), models: [] });
    expect(values()).toEqual(['', 'retired-model']);
    setModels(catalog);
    select.dispatchEvent(new Event('focus'));
    expect(values()).toEqual(['', 'retired-model', 'fast-model', 'strong-model']);
    expect(select.value).toBe('retired-model');
  });

  it('replaces the field with the read failure and retries only on request', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(state(''));
    const { field, failure, retry, select, shown } = setup({ read });
    expect(retry.hidden).toBe(true);
    await settle();
    expect(field.hidden).toBe(true);
    expect(failure.hidden).toBe(false);
    expect(failure.textContent).toBe('zh:routerWorkerModel.unavailable{"error":"offline"}');
    expect(retry.dataset.i18n).toBe('projectCoordination.retry');
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    retry.click();
    await settle();
    expect(read).toHaveBeenCalledTimes(2);
    expect(field.hidden).toBe(false);
    expect(failure.hidden).toBe(true);
    expect(retry.hidden).toBe(true);
    expect(shown()).toBe('zh:routerWorkerModel.ownerDefault');
    expect(select.disabled).toBe(false);
  });

  it('aborts an unanswered read on destroy and never paints its late answer', async () => {
    let signal: AbortSignal | undefined;
    let answer: ((value: RouterState) => void) | undefined;
    const read = vi.fn((_path: string, abort: AbortSignal) => {
      signal = abort;
      return new Promise<RouterState>(resolve => { answer = resolve; });
    });
    const { control, select, shown } = setup({ read });
    control.destroy();
    expect(signal?.aborted).toBe(true);
    answer?.(state('fast-model'));
    await settle();
    expect(select.value).toBe('');
    expect(shown()).toBe('zh:common.loading');
  });

  it('repaints its parameterized failure on language change and removes that listener on destroy', async () => {
    const add = vi.spyOn(window, 'addEventListener'), remove = vi.spyOn(window, 'removeEventListener');
    const { write, choose, control } = setup({ initial: state('') });
    write.mockRejectedValueOnce(new Error('refused'));
    choose('strong-model');
    await settle();
    const error = () => control.el.querySelector('.ui-field__error')!.textContent;
    expect(error()).toBe('zh:routerWorkerModel.failed{"error":"refused"}');
    i18n.language = 'en';
    window.dispatchEvent(new CustomEvent('tofu:language-change', { detail: { language: 'en' } }));
    expect(error()).toBe('en:routerWorkerModel.failed{"error":"refused"}');
    const listener = add.mock.calls.find(([type]) => type === 'tofu:language-change')![1];
    control.destroy();
    expect(remove).toHaveBeenCalledWith('tofu:language-change', listener);
    expect(control.el.isConnected).toBe(false);
  });
});
