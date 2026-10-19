/** Confirmed sidebar presets and mutation sequencing. No DOM/network at import.
 * The retained folder list remains the one projection; ports replace it only
 * after authoritative ACK. In-flight list reads cannot roll back local ACKs.
 * Per-folder serialization is transient and releases on either outcome.
 */
export interface FolderPreset {
  id: string; name: string; color?: string; revision?: number;
  projectPath?: string; projectPaths?: string[]; readOnlyPaths?: string[];
  [key: string]: unknown;
}
export interface FolderWorkspace {
  projectPath: string; projectPaths: string[]; readOnlyPaths: string[];
}
export function folderPresetWorkspace(folder: Partial<FolderPreset> | null | undefined): FolderWorkspace {
  const raw = Array.isArray(folder?.projectPaths) ? folder.projectPaths
    : folder?.projectPath ? [folder.projectPath] : [];
  const paths = [...new Set(raw.filter((path): path is string => typeof path === 'string')
    .map(path => path.trim()).filter(Boolean))];
  return { projectPath: paths[0] || '', projectPaths: paths,
    readOnlyPaths: paths.filter(path => folder?.readOnlyPaths?.includes(path)) };
}
export function createFolderPresetMutations(ports: {
  list: () => FolderPreset[];
  replace: (rows: FolderPreset[]) => void;
  create: (name: string, color: string, roots: string[], readonly: string[]) => Promise<FolderPreset>;
  update: (id: string, updates: Record<string, unknown>) => Promise<FolderPreset>;

  remove?: (id: string) => Promise<boolean>;
}) {
  const pending = new Map<string, Promise<unknown>>();
  let revision = 0;
  function install(row: FolderPreset) {
    if (!row || typeof row.id !== 'string' || !row.id) throw new Error('Invalid confirmed folder response');
    const rows = ports.list();
    const existing = rows.find(item => item.id === row.id);
    if (existing && Number(existing.revision || 0) > Number(row.revision || 0)) return existing;
    ports.replace(existing ? rows.map(item => item.id === row.id ? row : item) : [...rows, row]);
    revision++;
    return row;
  }
  function reconcile(rows: FolderPreset[], startedRevision: number) {
    if (startedRevision !== revision) return false;
    ports.replace(rows.map(row => {
      const current = ports.list().find(item => item.id === row.id);
      return current && Number(current.revision || 0) > Number(row.revision || 0) ? current : row;
    }));
    return true;
  }
  return {
    revision: () => revision,
    invalidate: () => { revision++; },
    reconcile,
    async loadConfirmed(fetch: () => Promise<FolderPreset[]>) {
      // An invalidated single flight includes one fresh read, not a discarded
      // refresh intent. Bound immediate retries under continuous remote writes.
      for (let attempt = 0; attempt < 4; attempt++) {
        const started = revision;
        const rows = await fetch();
        if (!Array.isArray(rows)) throw new Error('Invalid folder list');
        if (reconcile(rows, started)) return ports.list();
      }
      throw new Error('Folder catalog changed repeatedly; retry refresh');
    },
    async create(name: string, color: string, roots: string[] = [], readonly: string[] = []) {
      return install(await ports.create(name, color, [...roots], [...readonly]));
    },
    remove(id: string) {
      const prior = pending.get(id) || Promise.resolve();
      const flight = prior.catch(() => undefined).then(async () => {
        if (!ports.remove || !await ports.remove(id)) throw new Error('Folder delete was not confirmed');
        ports.replace(ports.list().filter(row => row.id !== id));
        revision++;
        return true;
      });
      pending.set(id, flight);
      const release = () => { if (pending.get(id) === flight) pending.delete(id); };
      void flight.then(release, release);
      return flight;
    },
    update(id: string, updates: Record<string, unknown>) {
      const captured = structuredClone(updates);
      const prior = pending.get(id) || Promise.resolve();
      const flight = prior.catch(() => undefined).then(async () => {
        const current = ports.list().find(row => row.id === id);
        if (!current) throw new Error('Folder no longer exists');
        const result = await ports.update(id, { ...captured,
          expectedRevision: captured.expectedRevision ?? current.revision ?? 0 });
        // A concurrent delete is not undone by a late save response.
        return ports.list().some(row => row.id === id) ? install(result) : result;
      });
      pending.set(id, flight);
      const release = () => { if (pending.get(id) === flight) pending.delete(id); };
      void flight.then(release, release);
      return flight;
    },
  };
}
