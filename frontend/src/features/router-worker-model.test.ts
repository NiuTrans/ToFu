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

function setup(options: { read?: () => Promise<RouterState>; initial?: RouterState; models?: PickerModel[] } = {}) {
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
  const select = control.el.querySelector('select')!;
  const summary = control.el.querySelector('summary')!;
  const values = () => [...select.options].map(option => option.value);
  const choose = (value: string) => { select.value = value; select.dispatchEvent(new Event('change')); };
  return { ports, write, control, select, summary, values, choose, setModels: (next: PickerModel[]) => { models = next; } };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

afterEach(() => { i18n.language = 'zh'; vi.restoreAllMocks(); document.body.replaceChildren(); });

describe('Router Worker model control', () => {
  it('reads the stored model once and offers the composer catalog after the owner default', async () => {
    const { ports, select, summary, values } = setup();
    expect(ports.read).toHaveBeenCalledWith('/work/app', expect.any(AbortSignal));
    expect(select.disabled).toBe(true);
    await settle();
    expect(ports.read).toHaveBeenCalledTimes(1);
    expect(summary.textContent).toContain('Label fast-model');
    expect(values()).toEqual(['', 'fast-model', 'strong-model']);
    expect(select.options[0].dataset.i18n).toBe('routerWorkerModel.ownerDefault');
    expect(select.value).toBe('fast-model');
    expect(select.disabled).toBe(false);
  });

  it('writes the chosen model, and null for the owner default model', async () => {
    const { write, summary, choose, select } = setup({ initial: state('') });
    expect(select.value).toBe('');
    choose('strong-model');
    await settle();
    expect(write).toHaveBeenLastCalledWith('/work/app', 'strong-model');
    expect(summary.textContent).toContain('Label strong-model');
    choose('');
    await settle();
    expect(write).toHaveBeenLastCalledWith('/work/app', null);
    expect(summary.textContent).toContain('globalRouter.workerModelDefault');
    expect(write).toHaveBeenCalledTimes(2);
  });

  it('keeps the stored value and shows the refusal when the server rejects a choice', async () => {
    const { write, choose, select, control } = setup({ initial: state('fast-model') });
    write.mockRejectedValueOnce(new Error('not routable for this owner'));
    choose('strong-model');
    expect(control.el.getAttribute('aria-busy')).toBe('true');
    await settle();
    expect(select.value).toBe('fast-model');
    expect(control.el.querySelector('.ui-field__error')!.textContent).toContain('not routable for this owner');
    expect(control.el.hasAttribute('aria-busy')).toBe(false);
  });

  it('shows a stored model the catalog no longer lists and picks up a catalog that loads later', async () => {
    const { values, setModels, control } = setup({ initial: state('retired-model'), models: [] });
    expect(values()).toEqual(['', 'retired-model']);
    setModels(catalog);
    control.el.open = true;
    await settle();
    expect(values()).toEqual(['', 'retired-model', 'fast-model', 'strong-model']);
  });

  it('retries a failed read when reopened', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(state(''));
    const { summary, control } = setup({ read });
    await settle();
    expect(summary.textContent).toContain('routerWorkerModel.unavailable');
    control.el.open = true;
    await settle();
    await settle();
    expect(read).toHaveBeenCalledTimes(2);
    expect(summary.textContent).toContain('globalRouter.workerModelDefault');
  });

  it('repaints its parameterized summary on language change and removes that listener on destroy', async () => {
    const add = vi.spyOn(window, 'addEventListener'), remove = vi.spyOn(window, 'removeEventListener');
    const { summary, control } = setup({ initial: state('') });
    expect(summary.textContent).toMatch(/^zh:/);
    i18n.language = 'en';
    window.dispatchEvent(new CustomEvent('tofu:language-change', { detail: { language: 'en' } }));
    expect(summary.textContent).toMatch(/^en:globalRouter\.workerModel/);
    const listener = add.mock.calls.find(([type]) => type === 'tofu:language-change')![1];
    control.destroy();
    expect(remove).toHaveBeenCalledWith('tofu:language-change', listener);
    expect(control.el.isConnected).toBe(false);
  });
});
