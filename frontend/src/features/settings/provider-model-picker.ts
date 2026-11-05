/** Provider model picker sessions and asynchronous submission feedback.
 * The retained view owns markup; the routing projection owns reads and the
 * injected addModels transaction owns writes/CAS. Closing releases the view
 * and its listeners without cancelling an already authorized model add.
 */
import type {ModelRoutingDocument, ModelRoutingModel} from '../../api/api-v1-model-routing.generated';
import {
  createProviderSettingsProjection, providerAddableCatalogModels, providerWireModelIds,
  type ProviderSettingsContext,
} from './provider-settings-projection';

export interface ProviderModelPickerView {
  root: HTMLElement;
  customInput: HTMLInputElement;
  customSubmit: HTMLButtonElement;
  listedImport: HTMLButtonElement;
  list: HTMLElement;
  count: HTMLElement;
}

export interface ListedImportOutcome {
  added: number;
  listed: number;
  refreshed: boolean;
}

type PickerNotice =
  | {kind: 'empty-selection' | 'invalid-wire' | 'duplicate-wire' | 'custom-added'}
  | {kind: 'save-error' | 'listed-error'; error: unknown}
  | {kind: 'catalog-added'; count: number}
  | ({kind: 'listed-imported'} & ListedImportOutcome);

interface ProviderModelPickerPorts {
  document(): Partial<ModelRoutingDocument> | null;
  addModels(providerId: string, models: ModelRoutingModel[], wireIds: string[]): Promise<number>;
  importListed(providerId: string): Promise<ListedImportOutcome>;
  createView(context: ProviderSettingsContext): ProviderModelPickerView;
  renderRows(view: ProviderModelPickerView, rows: ModelRoutingModel[], checked: ReadonlySet<string>,
    emptyReason: 'filtered' | 'exhausted'): void;
  renderCount(view: ProviderModelPickerView, count: number): void;
  notify(notice: PickerNotice): void;
}

interface PickerSession {
  providerId: string;
  query: string;
  checked: Set<string>;
  customEditRevision: number;
  view: ProviderModelPickerView | null;
  cleanup: (() => void) | null;
}

function modelKey(model: ModelRoutingModel): string {
  return (model.creator_id || '') + '::' + (model.model_id || '');
}

class ProviderModelPicker {
  private current: PickerSession | null = null;

  constructor(private readonly ports: ProviderModelPickerPorts) {}

  open(providerId: string): void {
    const context = this.context(providerId);
    if (!context) return;
    this.close();
    const view = this.ports.createView(context);
    const session: PickerSession = {
      providerId, query: '', checked: new Set(), customEditRevision: 0,
      view, cleanup: null,
    };
    this.current = session;
    view.root.ownerDocument.body.appendChild(view.root);
    session.cleanup = this.listen(session, view);
    this.render(session);
  }

  close(): void {
    if (this.current) this.retire(this.current);
  }

  filter(value: string): void {
    const session = this.current;
    if (!session || !this.view(session)) return;
    session.query = value;
    this.render(session);
  }

  toggle(key: string, checked: boolean): void {
    const session = this.current;
    const view = this.view(session);
    if (!session || !view || !key) return;
    if (checked) session.checked.add(key);
    else session.checked.delete(key);
    this.ports.renderCount(view, session.checked.size);
  }

  async submitCustom(): Promise<void> {
    const session = this.current;
    // Keep values, never DOM references, across the transaction's await.
    const rawValue = this.view(session)?.customInput.value;
    if (!session || rawValue === undefined) return;
    const revision = session.customEditRevision;
    const wireId = rawValue.trim();
    if (!wireId) { this.view(session)?.customInput.focus(); return; }
    const context = this.context(session.providerId);
    if (!context) { this.retire(session); return; }
    if (wireId.length > 256 || /[\u0000-\u001f]/.test(wireId)) {
      this.ports.notify({kind: 'invalid-wire'});
      return;
    }
    if (providerWireModelIds(context).has(wireId)) {
      this.ports.notify({kind: 'duplicate-wire'});
      return;
    }
    let added = 0;
    try {
      added = await this.ports.addModels(session.providerId, [], [wireId]);
    } catch (error) {
      this.ports.notify({kind: 'save-error', error});
    }
    const view = this.view(session);
    if (added && view && revision === session.customEditRevision && view.customInput.value === rawValue) {
      const active = view.root.ownerDocument.activeElement;
      view.customInput.value = '';
      if (active === view.customInput || active === view.customSubmit) view.customInput.focus();
    }
    this.render(session);
    if (added) this.ports.notify({kind: 'custom-added'});
  }

  async submitCatalog(): Promise<void> {
    const session = this.current;
    if (!session || !this.view(session)) return;
    if (!session.checked.size) {
      this.ports.notify({kind: 'empty-selection'});
      return;
    }
    const available = this.available(session);
    if (!available) return;
    const submitted = new Set(session.checked);
    const byKey = new Map(available.map(model => [modelKey(model), model]));
    const models = [...submitted].flatMap(key => {
      const model = byKey.get(key);
      return model ? [model] : [];
    });
    let added = 0;
    try {
      added = await this.ports.addModels(session.providerId, models, []);
    } catch (error) {
      this.ports.notify({kind: 'save-error', error});
    }
    if (added && this.view(session)) {
      for (const key of submitted) session.checked.delete(key);
    }
    // Refused preparation keeps eligible choices for retry. Failed persistence
    // can leave a complete staged batch, which the fresh projection excludes.
    this.render(session);
    if (added) this.ports.notify({kind: 'catalog-added', count: added});
  }

  /** Load every model the provider lists that is not added yet. */
  async importListed(): Promise<void> {
    const session = this.current;
    const view = this.view(session);
    if (!session || !view || view.listedImport.disabled) return;
    view.listedImport.disabled = true;
    let outcome: ListedImportOutcome | null = null;
    try {
      outcome = await this.ports.importListed(session.providerId);
    } catch (error) {
      this.ports.notify({kind: 'listed-error', error});
    }
    const current = this.view(session);
    if (current) current.listedImport.disabled = false;
    this.render(session);
    if (outcome) this.ports.notify({kind: 'listed-imported', ...outcome});
  }

  private context(providerId: string): ProviderSettingsContext | null {
    return createProviderSettingsProjection(this.ports.document(), providerId).context(providerId);
  }

  private available(session: PickerSession): ModelRoutingModel[] | null {
    const context = this.context(session.providerId);
    if (!context) { this.retire(session); return null; }
    return providerAddableCatalogModels(this.ports.document() || {}, context);
  }

  private render(session: PickerSession): void {
    const view = this.view(session);
    if (!view) return;
    const available = this.available(session);
    if (!available) return;
    const keys = new Set(available.map(modelKey));
    for (const key of session.checked) if (!keys.has(key)) session.checked.delete(key);
    const query = session.query.trim().toLowerCase();
    const rows = available.filter(model => !query ||
      [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase().includes(query));
    rows.sort((left, right) => String(left.display_name || left.model_id).localeCompare(
      String(right.display_name || right.model_id), undefined, {numeric: true, sensitivity: 'base'}));
    this.ports.renderRows(view, rows.slice(0, 120), session.checked, available.length ? 'filtered' : 'exhausted');
    this.ports.renderCount(view, session.checked.size);
  }

  private view(session: PickerSession | null): ProviderModelPickerView | null {
    if (!session || this.current !== session) return null;
    if (session.view?.root.isConnected) return session.view;
    this.retire(session);
    return null;
  }

  private retire(session: PickerSession): void {
    if (this.current === session) this.current = null;
    const {view, cleanup} = session;
    session.view = null;
    session.cleanup = null;
    session.checked.clear();
    cleanup?.();
    view?.root.remove();
  }

  private listen(session: PickerSession, view: ProviderModelPickerView): () => void {
    const backdrop = (event: MouseEvent) => {
      if (event.target === session.view?.root) this.retire(session);
    };
    const edit = () => { session.customEditRevision++; };
    const enter = (event: KeyboardEvent) => {
      if (event.key === 'Enter' && this.view(session)) {
        event.preventDefault();
        void this.submitCustom();
      }
    };
    view.root.addEventListener('click', backdrop);
    view.customInput.addEventListener('input', edit);
    view.customInput.addEventListener('keydown', enter);
    const Observer = view.root.ownerDocument.defaultView?.MutationObserver;
    const observer = Observer ? new Observer(() => {
      if (!session.view?.root.isConnected) this.retire(session);
    }) : null;
    observer?.observe(view.root.ownerDocument.body, {childList: true, subtree: true});
    return () => {
      observer?.disconnect();
      view.root.removeEventListener('click', backdrop);
      view.customInput.removeEventListener('input', edit);
      view.customInput.removeEventListener('keydown', enter);
    };
  }
}

export function createProviderModelPicker(ports: ProviderModelPickerPorts): ProviderModelPicker {
  return new ProviderModelPicker(ports);
}
