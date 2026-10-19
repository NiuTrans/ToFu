/**
 * Conversation workspace authority: saved settings -> disposable local bar and
 * immutable send binding. Navigation never writes the process-global registry.
 * Retained adapters supply presentation ports only. A new draft owns a one-time
 * folder/default snapshot until its first send creates saved settings.
 */
export interface ProjectConversation {
  id?: string;
  projectPath?: string;
  projectPaths?: string[];
  readOnlyPaths?: string[];
}
export interface ConversationProjectBinding {
  projectPath: string;
  projectPaths: string[];
  readOnlyPaths: string[];
}

export interface ConversationDraftProjectBinding extends ConversationProjectBinding {
  folderId: string | null;
}
export interface ConversationDraftProjectOptions {
  folderId?: string | null;
  defaults?: ProjectConversation | null;
  /** Keep an explicit attachment/private choice when carrying unsent work. */
  preserveExplicit?: boolean;
}
/** An explicitly empty primary path detaches even if legacy extras remain. */
export function captureConversationProject(
  conv: ProjectConversation | null,
): ConversationProjectBinding {
  const primary = conv?.projectPath;
  const path = typeof primary === 'string' ? primary : '';
  const paths = path ? [...new Set([path, ...(conv?.projectPaths || [])]
    .filter((root): root is string => typeof root === 'string' && !!root))] : [];
  return {
    projectPath: path,
    projectPaths: paths,
    readOnlyPaths: [...new Set((conv?.readOnlyPaths || [])
      .filter(root => paths.includes(root)))],
  };
}

export function projectConversationWorkspace(conv: ProjectConversation | null) {
  const binding = captureConversationProject(conv);
  return {
    active: !!binding.projectPath,
    path: binding.projectPath,
    readOnly: binding.readOnlyPaths.includes(binding.projectPath),
    extraRoots: binding.projectPaths.slice(1).map(path => ({
      path, readOnly: binding.readOnlyPaths.includes(path),
    })),
    fileCount: 0, dirCount: 0, totalSize: 0, languages: {}, scanning: false,
    scanProgress: '', scanDetail: '', scannedAt: 0,
  };
}

/** Normalize one explicit picker response or rollback snapshot. */
export function captureProjectStatus(data: any): ConversationProjectBinding {
  const extras = Array.isArray(data?.extraRoots) ? data.extraRoots : [];
  return captureConversationProject({
    projectPath: data?.path || '',
    projectPaths: extras.map((root: any) => typeof root === 'string' ? root : root?.path),
    readOnlyPaths: [data?.readOnly ? data.path : '', ...extras
      .filter((root: any) => root?.readOnly).map((root: any) => root.path)].filter(Boolean),
  });
}

/** Global status may enrich matching roots, never select or append roots. */
export function projectStatusMatchesWorkspace(data: any, selection: any): boolean {
  if (!selection?.active || data?.path !== selection.path) return false;
  const roots = (items: any[]) => items.map(root =>
    typeof root === 'string' ? root : root?.path);
  const expected = roots(selection.extraRoots || []);
  const incoming = roots(data.extraRoots || []);
  return expected.length === incoming.length
    && expected.every((path, index) => path === incoming[index]);
}

interface ProjectPorts {
  activeId(): string | null;
  exists(id: string): boolean;
  apply(data: ReturnType<typeof projectConversationWorkspace>): void;
}
export function createConversationProjectController(ports: ProjectPorts) {
  let generation = 0;

  let draftWorkspace = captureConversationProject(null);
  let draftFolderId: string | null = null;
  // Transient provenance only: an empty explicit choice is not an unset default.
  let draftExplicit = false;
  function begin() {
    const token = ++generation;
    return () => token === generation;
  }
  function restore(conv: ProjectConversation | null, eligible = begin()) {
    const id = conv?.id ?? null;
    if (!eligible() || ports.activeId() !== id || (id && !ports.exists(id))) return;
    if (conv) {
      draftWorkspace = captureConversationProject(null);
      draftFolderId = null;
      draftExplicit = false;
    }
    // Boot/status refreshes of the null view cannot erase a live draft snapshot.
    ports.apply(projectConversationWorkspace(conv || draftWorkspace));
  }
  // Explicit choices and navigation share a fence, independent of input draft.
  function beginMutation() {
    const token = ++generation;
    const id = ports.activeId();
    return () => token === generation && ports.activeId() === id
      && (!id || ports.exists(id));
  }
  function startDraft(options: ConversationDraftProjectOptions = {}) {
    if (ports.activeId() !== null) return;
    ++generation;
    if (!options.preserveExplicit || !draftExplicit) {
      draftFolderId = options.folderId || null;
      draftWorkspace = captureConversationProject(options.defaults || null);
      draftExplicit = false;
    }
    ports.apply(projectConversationWorkspace(draftWorkspace));
  }
  function captureDraftIdentity(): ConversationDraftProjectBinding {
    return { folderId: draftFolderId, ...captureConversationProject(draftWorkspace) };
  }
  function chooseDraft(choice: ProjectConversation) {
    if (ports.activeId() !== null) return;
    ++generation;
    draftWorkspace = captureConversationProject(choice);
    draftExplicit = true;
    ports.apply(projectConversationWorkspace(draftWorkspace));
  }
  return {
    begin, beginMutation, restore, startDraft, chooseDraft, captureDraftIdentity,
    captureDraft: () => captureConversationProject(draftWorkspace),
  };
}

// Temporary hydration fences, not saved workspace authority. Entries are weak
// and removed when the last overlapping hydrate settles (including failures).
const hydrating = new WeakMap<ProjectConversation, {
  count: number; choice?: ConversationProjectBinding;
}>();

export function noteConversationProjectChoice(conv: ProjectConversation): void {
  const pending = hydrating.get(conv);
  if (pending) pending.choice = captureConversationProject(conv);
}
export function protectHydratingProjectSettings<T extends Record<string, unknown>>(
  conv: ProjectConversation, settings: T,
): T {
  const choice = hydrating.get(conv)?.choice;
  return choice ? { ...settings, ...choice } : settings;
}
export async function withProjectHydration<T>(
  conv: ProjectConversation, hydrate: () => Promise<T>,
): Promise<T> {
  const pending = hydrating.get(conv) || { count: 0 };
  pending.count += 1;
  hydrating.set(conv, pending);
  try { return await hydrate(); }
  finally {
    if (--pending.count === 0) hydrating.delete(conv);
  }
}
