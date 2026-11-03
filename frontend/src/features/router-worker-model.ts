/** Worker model control of one bound Router (docs/ROUTER.md R5), shown in the
 * Global Router header and in each Project Router's coordinator surface. The
 * server owns the setting (`/api/v4/router/worker-model`); the choices are the
 * composer model picker's catalog and visibility policy, never a parallel model
 * list. The first choice, "Owner default model", sends `null`. One compact
 * labelled select: its closed value is the at-a-glance state and its tooltip the
 * explanation. One read when no state is supplied; no polling, timers or writes
 * until the owner chooses.
 */
import type { RouterState } from '../api/router.generated';
import type { PickerModel } from '../application-shell/model-picker';
import { createButton, createField, setFieldError } from '../ui';
import { t } from '../i18n';

/** The composer picker's catalog and policy, injected by the retained bridge. */
export interface WorkerModelCatalog {
  models(): readonly PickerModel[];
  visible(model: PickerModel): boolean;
  compare?(left: PickerModel, right: PickerModel): number;
  label(modelId: string): string;
}

export interface RouterWorkerModelPorts {
  /** GET /api/v4/router/worker-model?path= */
  read(path: string, signal: AbortSignal): Promise<RouterState>;
  /** POST /api/v4/router/worker-model; null returns to the owner default model. */
  write(path: string, workerModel: string | null): Promise<RouterState>;
  catalog: WorkerModelCatalog;
}

export type RouterWorkerModelControl = ReturnType<typeof createRouterWorkerModel>;

function failureText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function createRouterWorkerModel(ports: RouterWorkerModelPorts, path: string, initial?: RouterState) {
  let alive = true, writing = false, optionsKey = '';
  // null until the server answered; '' is the owner default model.
  let current: string | null = initial ? initial.workerModel : null;
  let readFailure = '', writeFailure = '';
  let reading: AbortController | null = null;

  const el = document.createElement('div');
  el.className = 'router-worker-model';
  const field = createField({ label: t('routerWorkerModel.label'), choices: [], size: 'sm' });
  field.el.querySelector('label')?.setAttribute('data-i18n', 'routerWorkerModel.label');
  const select = field.control as HTMLSelectElement;
  // The explanation is the field's tooltip and the select's accessible description.
  for (const node of [field.el, select]) {
    node.dataset.i18nTitle = 'routerWorkerModel.help';
    node.title = t('routerWorkerModel.help');
  }
  // Lets a repainting host restore focus to this same control.
  select.dataset.coordinationFocus = 'router-worker-model';
  // A failed read replaces the field: there is no stored value to show or change.
  const failure = document.createElement('span');
  failure.className = 'router-worker-model-failure';
  const retry = createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: () => read() });
  retry.dataset.i18n = 'projectCoordination.retry';
  el.append(field.el, failure, retry);

  function choices(): string[] {
    const catalog = ports.catalog;
    const models = catalog.models().filter(model => model.model_id && catalog.visible(model));
    if (catalog.compare) models.sort(catalog.compare);
    const ids = [...new Set(models.map(model => model.model_id))];
    // A stored model the catalog no longer lists stays visible as the current value.
    if (current && !ids.includes(current)) ids.unshift(current);
    return ids;
  }

  function renderOptions() {
    if (!alive) return;
    const ids = current === null ? [] : choices();
    const key = JSON.stringify([current, ids]);
    if (key === optionsKey) return;
    optionsKey = key;
    // Until the server answers, the disabled select says so instead of claiming the owner default.
    const firstKey = current === null ? 'common.loading' : 'routerWorkerModel.ownerDefault';
    const first = document.createElement('option');
    first.value = '';
    first.dataset.i18n = firstKey;
    first.textContent = t(firstKey);
    select.replaceChildren(first, ...ids.map(id => {
      const option = document.createElement('option');
      option.value = id; option.textContent = ports.catalog.label(id);
      return option;
    }));
    select.value = current ?? '';
  }

  function render() {
    if (!alive) return;
    failure.textContent = readFailure ? t('routerWorkerModel.unavailable', { error: readFailure }) : '';
    field.el.hidden = !!readFailure;
    failure.hidden = retry.hidden = !readFailure;
    setFieldError(field, writeFailure ? t('routerWorkerModel.failed', { error: writeFailure }) : null);
    renderOptions();
    select.disabled = writing || current === null;
    if (writing) el.setAttribute('aria-busy', 'true');
    else el.removeAttribute('aria-busy');
  }

  function read() {
    if (!alive || reading) return;
    const controller = new AbortController();
    reading = controller; readFailure = '';
    render();
    let request: Promise<RouterState>;
    try { request = ports.read(path, controller.signal); } catch (error) { request = Promise.reject(error); }
    request.then(state => {
      if (!alive || reading !== controller) return;
      current = state.workerModel;
    }, error => {
      if (!alive || reading !== controller) return;
      readFailure = failureText(error);
    }).finally(() => {
      if (reading === controller) reading = null;
      render();
    });
  }

  select.addEventListener('change', async () => {
    if (!alive || writing || current === null) return;
    const next = select.value || null;
    if ((next ?? '') === current) return;
    writing = true; writeFailure = ''; render();
    try {
      const state = await ports.write(path, next);
      if (alive) current = state.workerModel;
    } catch (error) {
      if (alive) writeFailure = failureText(error);
    } finally {
      writing = false;
      // Re-selects the stored value, so a refused choice never looks applied.
      optionsKey = ''; render();
    }
  });
  // The catalog loads independently of this control: list its current models when focused.
  select.addEventListener('focus', renderOptions);
  // Labels, options and tooltips carry data-i18n; only the parameterized failures need a repaint.
  window.addEventListener('tofu:language-change', render);

  if (initial) render();
  else read();

  return {
    el, path,
    /** Adopt a fresher server state for the same Router. */
    update(state: RouterState) {
      if (!alive || writing) return;
      current = state.workerModel; readFailure = '';
      render();
    },
    destroy() {
      alive = false; reading?.abort(); reading = null;
      window.removeEventListener('tofu:language-change', render);
      el.remove();
    },
  };
}
