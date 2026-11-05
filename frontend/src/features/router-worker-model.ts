/** Worker model control of one bound Router (docs/ROUTER.md R5), shown in the
 * Global Router header and in each Project Router's coordinator surface. The
 * server owns the setting (`/api/v4/router/worker-model`); the choices are the
 * composer model picker's catalog and visibility policy, never a parallel model
 * list. The first choice, "Owner default model", sends `null`. One read when no
 * state is supplied; no polling, timers or writes until the owner chooses.
 */
import type { RouterState } from '../api/router.generated';
import type { PickerModel } from '../application-shell/model-picker';
import { createField, setFieldError } from '../ui';
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

/** "Worker model: …" for a stored model ID, '' meaning the owner default. */
export function workerModelText(model: string, label: (modelId: string) => string): string {
  return t('globalRouter.workerModel', { model: model ? label(model) : t('globalRouter.workerModelDefault') });
}

export function createRouterWorkerModel(ports: RouterWorkerModelPorts, path: string, initial?: RouterState) {
  let alive = true, writing = false, optionsKey = '';
  // null until the server answered; '' is the owner default model.
  let current: string | null = initial ? initial.workerModel : null;
  let readFailure = '';
  let reading: AbortController | null = null;

  const el = document.createElement('details');
  el.className = 'router-worker-model';
  const summary = document.createElement('summary');
  summary.className = 'router-worker-model-summary';
  const field = createField({ label: t('routerWorkerModel.label'), choices: [], hint: t('routerWorkerModel.help') });
  field.el.classList.add('router-worker-model-field');
  field.el.querySelector('label')?.setAttribute('data-i18n', 'routerWorkerModel.label');
  field.el.querySelector('.ui-field__hint')?.setAttribute('data-i18n', 'routerWorkerModel.help');
  const select = field.control as HTMLSelectElement;
  // Lets a repainting host restore focus to this same control.
  select.dataset.coordinationFocus = 'router-worker-model';
  el.append(summary, field.el);

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
    const ids = choices();
    const key = JSON.stringify([current, ids]);
    if (key === optionsKey) return;
    optionsKey = key;
    const ownerDefault = document.createElement('option');
    ownerDefault.value = '';
    ownerDefault.dataset.i18n = 'routerWorkerModel.ownerDefault';
    ownerDefault.textContent = t('routerWorkerModel.ownerDefault');
    select.replaceChildren(ownerDefault, ...ids.map(id => {
      const option = document.createElement('option');
      option.value = id; option.textContent = ports.catalog.label(id);
      return option;
    }));
    select.value = current ?? '';
  }

  function render() {
    if (!alive) return;
    summary.textContent = readFailure ? t('routerWorkerModel.unavailable', { error: readFailure })
      : current === null ? t('globalRouter.workerModel', { model: t('common.loading') })
      : workerModelText(current, ports.catalog.label);
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
    writing = true; setFieldError(field, null); render();
    try {
      const state = await ports.write(path, next);
      if (alive) current = state.workerModel;
    } catch (error) {
      if (alive) setFieldError(field, t('routerWorkerModel.failed', { error: failureText(error) }));
    } finally {
      writing = false;
      // Re-selects the stored value, so a refused choice never looks applied.
      optionsKey = ''; render();
    }
  });
  // The catalog loads independently of this control: list its current models when shown.
  el.addEventListener('toggle', () => {
    if (!el.open) return;
    if (readFailure) read();
    renderOptions();
  });
  select.addEventListener('focus', renderOptions);
  // Labels carry data-i18n; only the parameterized summary needs a repaint.
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
