/**
 * Responsibility: conversation-scoped project restore and draft/selection fencing.
 * Entry: createConversationProjectController; retained shell supplies UI/transport ports.
 * No persistence: navigation and failed restoration never edit durable bindings.
 */
interface ProjectConversation {
  id: string;
  projectPath?: string;
  projectPaths?: string[];
  readOnlyPaths?: string[];
}
interface ProjectPorts {
  activeId(): string | null;
  exists(id: string): boolean;
  hasDraft(): boolean;
  selection(): any;
  clear(): void;
  remote(conv: ProjectConversation, path: string): void;
  apply(data: any): void;
  setPaths(paths: string[], readOnly: string[], recent: string[]): Promise<any>;
  warn(message: string): void;
}
export function createConversationProjectController(ports: ProjectPorts) {
  let generation = 0;
  let preservedDraft = false;
  function begin() {
    const token = ++generation;
    const selection = ports.selection();
    const blocked = ports.hasDraft();
    preservedDraft = blocked;
    return () => token === generation && !blocked && !ports.hasDraft()
      && selection === ports.selection();
  }
  async function restore(conv: ProjectConversation | null, eligible = begin()) {
    if (!eligible()) return;
    const id = conv?.id ?? null;
    const current = () => ports.activeId() === id && (!id || ports.exists(id));
    if (!current()) return;
    const path = conv?.projectPath || '';
    if (!path) { ports.clear(); return; }
    if (path.startsWith('remote:') && path.slice(7).includes(':')) {
      ports.remote(conv!, path);
      return;
    }
    const paths = [...new Set([path, ...(conv?.projectPaths || [])].filter(Boolean))];
    const readOnly = (conv?.readOnlyPaths || []).filter(p => paths.includes(p));
    const selected = ports.selection();
    const extras = selected?.extraRoots || [];
    const selectedPaths = [selected?.path, ...extras.map((root: any) =>
      typeof root === 'string' ? root : root.path)].filter(Boolean);
    const selectedReadOnly = [selected?.readOnly ? selected.path : '',
      ...extras.filter((root: any) => root?.readOnly).map((root: any) => root.path),
    ].filter(Boolean);
    if (selected?.active && selected.path === path
        && selectedPaths.length === paths.length && paths.every(p => selectedPaths.includes(p))
        && selectedReadOnly.length === readOnly.length
        && readOnly.every(p => selectedReadOnly.includes(p))) return;
    // Clear only the disposable bar, never the conversation settings.
    ports.clear();
    const stillEligible = begin();
    try {
      const response = await ports.setPaths(paths, readOnly, [path]);
      const data = response ? await response.json().catch(() => ({})) : {};
      if (!current() || !stillEligible()) return;
      if (response?.ok && data.path === path) ports.apply({
        ...data,
        path,
        readOnly: readOnly.includes(path),
        extraRoots: paths.slice(1).map(root => ({
          ...(data.extraRoots || []).find((item: any) => item?.path === root),
          path: root,
          readOnly: readOnly.includes(root),
        })),
      });
      else ports.warn(`Saved project could not be restored: ${path}`);
    } catch (error) {
      if (current() && stillEligible()) ports.warn(`Project restore failed: ${String(error)}`);
    }
  }
  // User mutations may coexist with a draft, unlike automatic restore. Share
  // the same generation so navigation or a newer choice invalidates completion.
  function beginMutation() {
    const token = ++generation;
    const id = ports.activeId();
    const selected = ports.selection();
    return () => token === generation && ports.activeId() === id
      && (!id || ports.exists(id)) && ports.selection() === selected;
  }
  return { begin, beginMutation, restore, preservesDraft: () => preservedDraft };
}
