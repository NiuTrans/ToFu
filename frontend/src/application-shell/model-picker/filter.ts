/** One render-local search index. Each query visits each row once and each fold/section member once; no selector scans or interpolated selectors. */
export interface SearchRow { element: HTMLElement; haystack: string; section: string }
export interface SearchFold { element: HTMLElement; control: HTMLElement; rows: SearchRow[] }
export function createModelPickerFilter() {
  const rows: SearchRow[] = [], folds: SearchFold[] = [], labels = new Map<string, HTMLElement>();
  let lastQuery: string | null = null;
  return {
    addRow(row: SearchRow) { rows.push(row); },
    addFold(fold: SearchFold) { folds.push(fold); },
    addLabel(section: string, element: HTMLElement) { labels.set(section, element); },
    apply(value: string): number {
      const query = String(value || '').trim().toLowerCase();
      if (query === lastQuery) return 0;
      lastQuery = query;
      const visibleSections = new Set<string>(), visibleRows = new Set<SearchRow>();
      let visits = 0;
      for (const row of rows) {
        visits++; const visible = !query || row.haystack.includes(query);
        const display = visible ? '' : 'none'; if (row.element.style.display !== display) row.element.style.display = display;
        if (visible) { visibleRows.add(row); visibleSections.add(row.section); }
      }
      for (const fold of folds) {
        let visible = false; for (const row of fold.rows) { visits++; if (visibleRows.has(row)) visible = true; }
        fold.element.classList.toggle('search-open', !!query && visible);
        fold.control.setAttribute('aria-expanded', String(query ? visible : fold.element.classList.contains('open')));
        if (fold.control.classList.contains('ps-dd-expander')) fold.control.style.display = query ? 'none' : '';
      }
      for (const [section, label] of labels) label.style.display = !query || visibleSections.has(section) ? '' : 'none';
      return visits;
    },
    firstVisible() { return rows.find(row => row.element.style.display !== 'none')?.element; },
  };
}
export type ModelPickerFilter = ReturnType<typeof createModelPickerFilter>;
