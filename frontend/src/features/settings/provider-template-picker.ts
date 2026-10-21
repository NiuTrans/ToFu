/* Provider template catalog: injected API/presentation ports, one dialog and one
 * request flight. Server categories and seed metadata are hints, not availability. */
export interface ProviderTemplate {
  key: string;
  name: string;
  brand?: string;
  category?: string;
  base_url?: string;
  catalog_mode?: string;
  catalog_source?: { url?: string; checked_at?: string };
  protocol?: string;
  /** Named serving endpoints of one gateway. A request name pinned in
   *  ``request_faces`` is reachable ONLY through its face's URL/headers. */
  faces?: Record<string, {
    base_url?: string;
    protocol?: string;
    extra_headers?: Record<string, string>;
    responses_profile?: string;
    inference_profile?: unknown;
  }>;
  offering_recipes?: {
    model_id?: string;
    request_ids?: string[];
    request_faces?: Record<string, string>;
  }[];
}
type Category = 'official' | 'relay' | 'local' | 'other';
const categories: Category[] = ['official', 'relay', 'local', 'other'];
export function templateCategory(template: ProviderTemplate): Category {
  return categories.includes(template.category as Category) ? template.category as Category : 'other';
}
export function selectableTemplate(template: ProviderTemplate): boolean {
  return !!template.key && ((template.offering_recipes?.length || 0) > 0 ||
    (templateCategory(template) !== 'local' && template.catalog_mode === 'discovery'));
}
function safeUrl(value?: string): URL | null {
  try {
    const url = new URL(value || '');
    return ['https:', 'http:'].includes(url.protocol) ? url : null;
  } catch { return null; }
}
export function templateMatches(template: ProviderTemplate, query: string): boolean {
  const text = [template.name, template.key, safeUrl(template.base_url)?.host,
    ...(template.offering_recipes || []).flatMap(row => [row.model_id, ...(row.request_ids || [])])]
    .join(' ').toLocaleLowerCase();
  return query.trim().toLocaleLowerCase().split(/\s+/).every(word => text.includes(word));
}
interface Ports {
  document: Document;
  translate: (key: string) => string;
  load: () => Promise<unknown>;
  select: (key: string) => void;
  // Presentation only: SVG comes from the immutable application brand registry.
  iconHtml?: (brand: string) => string;
  discover?: (key: string, apiKey: string, current: () => boolean) => Promise<void>;
}
export function createProviderTemplatePicker(ports: Ports) {
  const { document: doc, translate: t } = ports;
  let closeView: (() => void) | null = null;
  let generation = 0;
  let flight: Promise<unknown> | null = null;

  function close() { closeView?.(); }
  function toggle(trigger: HTMLElement) {
    if (closeView) { close(); return; }
    const epoch = ++generation;
    const overlay = doc.createElement('div');
    overlay.className = 'stg-provider-catalog-overlay';
    const dialog = doc.createElement('section');
    dialog.id = 'stgTemplateMenu';
    dialog.className = 'stg-provider-catalog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'stgTemplateTitle');
    const node = (tag: string, text: string, className = '') => {
      const element = doc.createElement(tag);
      element.textContent = text;
      element.className = className;
      return element;
    };
    const button = (text: string, action: () => void) => {
      const element = doc.createElement('button');
      element.type = 'button';
      element.textContent = text;
      element.onclick = action;
      return element;
    };
    const header = node('header', '', 'stg-provider-catalog-header');
    const title = node('h2', t('providerCatalog.title'));
    title.id = 'stgTemplateTitle';
    header.append(title, button(t('providerCatalog.close'), close));
    const note = node('p', t('providerCatalog.notice'), 'stg-provider-catalog-note');
    note.id = 'stgTemplateNotice';
    dialog.setAttribute('aria-describedby', note.id);
    const search = doc.createElement('input');
    search.type = 'search';
    search.placeholder = t('providerCatalog.search');
    search.setAttribute('aria-label', t('providerCatalog.search'));
    const filters = node('div', '', 'stg-provider-catalog-filters');
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', t('providerCatalog.categories'));
    const status = node('p', '');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const results = node('div', '', 'stg-provider-catalog-results');
    dialog.append(header, note, search, filters, status, results);
    overlay.append(dialog);
    doc.body.append(overlay);
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-controls', dialog.id);
    trigger.setAttribute('aria-haspopup', 'dialog');
    let rows: ProviderTemplate[] = [];
    let category: Category | 'all' = 'all';
    let loading = false;
    const current = () => generation === epoch && overlay.isConnected;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); close();
      } else if (event.key === 'Tab') {
        const items = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]'))
          .filter(item => !item.hidden && !item.closest('[hidden]'));
        const first = items[0]; const last = items[items.length - 1];
        if (event.shiftKey && (doc.activeElement === first || !dialog.contains(doc.activeElement))) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (doc.activeElement === last || !dialog.contains(doc.activeElement))) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    const onOutside = (event: Event) => {
      const target = event.target as Node | null;
      if (target && !event.composedPath().includes(dialog) && !dialog.contains(target) && !trigger.contains(target)) close();
    };
    const onLanguage = () => close();
    doc.addEventListener('keydown', onKey, true);
    doc.addEventListener('click', onOutside);
    doc.defaultView?.addEventListener('tofu:language-change', onLanguage);
    // Also release listeners when a parent settings island is torn down.
    const Observer = doc.defaultView?.MutationObserver;
    const observer = Observer ? new Observer(() => {
      if (!trigger.isConnected || !overlay.isConnected) close();
    }) : null;
    observer?.observe(doc.body, { childList: true, subtree: true });
    closeView = () => {
      ++generation;
      closeView = null;
      observer?.disconnect();
      doc.removeEventListener('keydown', onKey, true);
      doc.removeEventListener('click', onOutside);
      doc.defaultView?.removeEventListener('tofu:language-change', onLanguage);
      for (const input of Array.from(dialog.querySelectorAll<HTMLInputElement>('input[type=password]'))) input.value = '';
      overlay.remove();
      trigger.setAttribute('aria-expanded', 'false');
      trigger.removeAttribute('aria-controls');
      if (trigger.isConnected) trigger.focus();
      rows = [];
    };
    search.focus();

    function showDiscovery(row: ProviderTemplate) {
      search.hidden = filters.hidden = true;
      results.replaceChildren();
      status.textContent = row.name;
      const form = doc.createElement('form');
      const label = doc.createElement('label');
      label.textContent = t('providerCatalog.apiKey');
      const key = doc.createElement('input');
      key.type = 'password'; key.autocomplete = 'off'; key.required = true;
      key.setAttribute('aria-label', t('providerCatalog.apiKey'));
      label.append(key);
      const submit = button(t('providerCatalog.discoverAdd'), () => undefined);
      submit.type = 'submit';
      const back = button(t('providerCatalog.back'), () => {
        key.value = ''; search.hidden = filters.hidden = false; render(); search.focus();
      });
      form.append(node('p', t('providerCatalog.discoveryNotice')),
        node('p', row.base_url || ''), label, submit, back);
      results.append(form); key.focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (submit.disabled || !key.value.trim()) return;
        if (!ports.discover) { status.textContent = t('providerCatalog.discoveryFailed'); return; }
        let secret = key.value.trim(); key.value = '';
        submit.disabled = back.disabled = key.disabled = true;
        status.textContent = t('providerCatalog.discovering');
        try {
          await ports.discover(row.key, secret, current);
          if (current()) close();
        } catch {
          // Never echo upstream messages: a remote error may contain secrets.
          if (current()) status.textContent = t('providerCatalog.discoveryFailed');
        } finally {
          secret = '';
          if (current()) { submit.disabled = back.disabled = key.disabled = false; key.focus(); }
        }
      };
    }

    function render() {
      if (!current() || loading) return;
      const matches = rows.filter(row => templateMatches(row, search.value));
      // Keep filter nodes stable so keyboard focus survives selection/search.
      for (const filter of Array.from(filters.querySelectorAll<HTMLButtonElement>('button'))) {
        const key = filter.dataset.category as Category | 'all';
        const count = matches.filter(row => key === 'all' || templateCategory(row) === key).length;
        filter.textContent = `${t('providerCatalog.' + key)} (${count})`;
        filter.setAttribute('aria-pressed', String(category === key));
      }
      results.replaceChildren();
      const visible = matches.filter(row => category === 'all' || templateCategory(row) === category);
      status.textContent = visible.length ? `${visible.length} ${t('providerCatalog.results')}` :
        t(rows.length ? 'providerCatalog.noMatches' : 'providerCatalog.empty');
      for (const key of categories) {
        const group = visible.filter(row => templateCategory(row) === key);
        if (!group.length) continue;
        const section = node('section', '', 'stg-provider-catalog-group');
        section.append(node('h3', `${t('providerCatalog.' + key)} (${group.length})`));
        const grid = node('div', '', 'stg-provider-catalog-grid');
        for (const row of group) {
          // Compact tile: brand mark + name only. Seed metadata still powers
          // search; the endpoint host stays available on the tooltip.
          const choose = button(row.name || row.key, () => {
            if (row.catalog_mode === 'discovery' && !row.offering_recipes?.length) showDiscovery(row);
            else { close(); ports.select(row.key); }
          });
          choose.dataset.tplKey = row.key;
          choose.className = 'stg-provider-catalog-tile';
          const host = safeUrl(row.base_url)?.host;
          choose.title = host ? `${row.name || row.key} · ${host}` : row.name || row.key;
          const label = doc.createElement('span');
          label.className = 'stg-provider-catalog-name';
          label.textContent = choose.textContent;
          choose.textContent = '';
          if (ports.iconHtml) {
            const badge = doc.createElement('span');
            badge.setAttribute('aria-hidden', 'true');
            badge.innerHTML = ports.iconHtml(row.brand || row.key);
            choose.append(badge);
          }
          choose.append(label);
          grid.append(choose);
        }
        section.append(grid); results.append(section);
      }
    }
    for (const key of ['all', ...categories] as const) {
      const filter = button('', () => { category = key; render(); });
      filter.dataset.category = key;
      filters.append(filter);
    }
    search.oninput = render;
    async function load() {
      if (loading || !current()) return;
      loading = true;
      dialog.setAttribute('aria-busy', 'true');
      status.textContent = t('providerCatalog.loading');
      results.replaceChildren();
      try {
        // A reopened view waits for the obsolete request, then fetches a fresh
        // snapshot. Many rapid toggles produce at most one trailing request.
        if (flight) await flight.catch(() => undefined);
        if (!current()) return;
        const request = Promise.resolve().then(ports.load);
        flight = request;
        let value: unknown;
        try { value = await request; }
        finally { if (flight === request) flight = null; }
        if (!current()) return;
        if (!Array.isArray(value)) throw new Error('Invalid template catalog');
        rows = value.slice(0, 256).filter((row): row is ProviderTemplate => !!row && typeof row === 'object' &&
          typeof row.key === 'string' && typeof row.name === 'string' &&
          (!row.offering_recipes || (Array.isArray(row.offering_recipes) &&
            row.offering_recipes.length <= 1024 && row.offering_recipes.every((recipe: { model_id?: unknown; request_ids?: unknown }) =>
              !!recipe && typeof recipe.model_id === 'string' && (!recipe.request_ids || Array.isArray(recipe.request_ids))))))
          .filter(selectableTemplate);
        loading = false;
        render();
      } catch {
        if (!current()) return;
        status.textContent = t('providerCatalog.failed');
        results.append(button(t('providerCatalog.retry'), () => void load()));
      } finally {
        loading = false;
        if (current()) dialog.setAttribute('aria-busy', 'false');
      }
    }
    // Labels remain useful during loading, including the separate local flow.
    render();
    void load();
  }
  return { toggle, destroy: close };
}
