/**
 * Responsibility: own the bounded folder-name filter and render one folder-list projection.
 * Entry point: createProjectDirectoryBrowser.
 * Dependencies: injected copy, HTML escaping, and trusted presentation assets.
 */

export interface ProjectDirectoryRow {
  path: string;
  name: string;
  hasCode: boolean;
  hidden: boolean;
  itemCount: number;
}

export interface ProjectDirectoryBrowserData {
  dirs: readonly ProjectDirectoryRow[];
  filesCount: number;
  truncated: boolean;
}

export interface ProjectDirectoryBrowserAssets {
  codeFolder: string;
  plainFolder: string;
  deleteFolder: string;
  addFolder: string;
  folderChevron: string;
}

export interface ProjectDirectoryBrowserPorts {
  escapeHtml(value: unknown): string;
  translate(key: string, values?: Readonly<Record<string, unknown>>): string;
  assets: ProjectDirectoryBrowserAssets;
}

export interface ProjectDirectoryBrowser {
  setFilter(value: unknown): string;
  clearFilter(): void;
  resetForNavigation(currentPath: string, requestedPath: string): boolean;
  filterValue(): string;
  render(data: ProjectDirectoryBrowserData, addedPaths: readonly string[]): string;
}

// This is browser-owned transient input, not an invitation to retain an
// arbitrarily large pasted string for every repaint.
const FILTER_MAX_CODE_UNITS = 256;

const actionCall = (
  functionName: string,
  ...args: readonly string[]
): string => `${functionName}(${args.map((value) => JSON.stringify(value)).join(',')})`;

export const createProjectDirectoryBrowser = (
  ports: ProjectDirectoryBrowserPorts,
): ProjectDirectoryBrowser => {
  let filter = '';

  const setFilter = (value: unknown): string => {
    filter = String(value ?? '').slice(0, FILTER_MAX_CODE_UNITS);
    return filter;
  };

  const clearFilter = (): void => {
    filter = '';
  };

  const resetForNavigation = (currentPath: string, requestedPath: string): boolean => {
    if (!filter || currentPath === requestedPath) return false;
    clearFilter();
    return true;
  };

  const render = (
    data: ProjectDirectoryBrowserData,
    addedPaths: readonly string[],
  ): string => {
    const { escapeHtml, translate, assets } = ports;
    if (data.dirs.length === 0) {
      const count = data.filesCount
        ? ` · ${escapeHtml(translate('pm.filesCount', { n: data.filesCount }))}`
        : '';
      return `<div class="fb-state"><span>${escapeHtml(translate('pm.noSubdirs'))}${count}</span></div>`;
    }

    const rawQuery = filter.trim();
    const query = rawQuery.toLowerCase();
    const visibleDirectories = query
      ? data.dirs.filter((directory) => directory.name.toLowerCase().includes(query))
      : data.dirs;
    if (visibleDirectories.length === 0) {
      return `<div class="fb-state"><span>${escapeHtml(translate('pm.browseNoMatches', { q: rawQuery }))}</span></div>`;
    }

    const added = new Set(addedPaths);
    const rows = visibleDirectories.map((directory) => {
      const badge = directory.hasCode
        ? `<span class="folder-code-badge">${escapeHtml(translate('pm.codeBadge'))}</span>`
        : '';
      const hiddenClass = directory.hidden ? ' folder-hidden' : '';
      const addedClass = added.has(directory.path) ? ' folder-added' : '';
      const itemCount = directory.itemCount > 0
        ? `<span class="folder-item-count">${directory.itemCount > 100 ? '100+' : directory.itemCount}</span>`
        : '';
      const icon = directory.hasCode ? assets.codeFolder : assets.plainFolder;
      // Escape the complete action as an HTML attribute after JSON has encoded
      // its JS string arguments; folder quotes cannot break either grammar.
      const browseAction = escapeHtml(actionCall('browseDirectory', directory.path));
      const deleteAction = escapeHtml(
        `event.stopPropagation();${actionCall('mpDeleteFolder', directory.path, directory.name)}`,
      );
      const addAction = escapeHtml(
        `event.stopPropagation();${actionCall('mpAddBrowsedPath', directory.path)}`,
      );
      return `<div class="folder-item${hiddenClass}${addedClass}" data-dir-path="${escapeHtml(directory.path)}" data-tofu-action="${browseAction}" title="${escapeHtml(translate('pm.openTitle', { name: directory.name }))}"><span class="folder-icon">${icon}</span><span class="folder-name">${escapeHtml(directory.name)}</span>${badge}${itemCount}<button class="folder-del-btn" data-tofu-action="${deleteAction}" title="${escapeHtml(translate('pm.deleteFolder'))}">${assets.deleteFolder}</button><button class="folder-add-btn" data-tofu-action="${addAction}" title="${escapeHtml(translate('pm.addToWorkspace'))}">${assets.addFolder}</button>${assets.folderChevron}</div>`;
    }).join('');

    const truncation = data.truncated && !query
      ? `<div class="fb-state"><span>${escapeHtml(translate('pm.showingFirst', { n: data.dirs.length }))}</span></div>`
      : '';
    return rows + truncation;
  };

  return Object.freeze({
    setFilter,
    clearFilter,
    resetForNavigation,
    filterValue: () => filter,
    render,
  });
};
