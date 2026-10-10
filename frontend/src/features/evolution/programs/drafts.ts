/** Owner-scoped unsent input recovery, never execution or evidence authority.
 * One atomic browser value per authenticated owner/application; explicit reads
 * and writes, no globals, timers, replay, truncation or silent eviction. */
export interface DraftAttempt {signature: string; key: string; createdId?: string}
export interface ProgramDraft {fields: string[]; attempt?: DraftAttempt; researchSource?: {task_id: string; idea_id: string}; seedObjective?: string}
export type DraftStatus = 'saved' | 'restored' | 'unavailable' | 'full';
export const PROGRAM_DRAFT_LIMITS = {entries: 64, bytes: 512 * 1024, fields: 32, fieldChars: 10000} as const;
type Drafts = Record<string, ProgramDraft>;

function draft(value: unknown): value is ProgramDraft {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as ProgramDraft;
  if (!Array.isArray(record.fields) || record.fields.length > PROGRAM_DRAFT_LIMITS.fields
      || record.fields.some(field => typeof field !== 'string' || field.length > PROGRAM_DRAFT_LIMITS.fieldChars)) return false;
  if (record.researchSource !== undefined && (!record.researchSource
      || typeof record.researchSource.task_id !== 'string' || !record.researchSource.task_id || record.researchSource.task_id.length > 160
      || typeof record.researchSource.idea_id !== 'string' || !record.researchSource.idea_id || record.researchSource.idea_id.length > 160)) return false;
  if (record.seedObjective !== undefined && (typeof record.seedObjective !== 'string' || record.seedObjective.length > 4000)) return false;
  const attempt = record.attempt;
  return attempt === undefined || (!!attempt && typeof attempt === 'object'
    && typeof attempt.signature === 'string' && attempt.signature.length <= 32000
    && typeof attempt.key === 'string' && attempt.key.length > 0 && attempt.key.length <= 128
    && (attempt.createdId === undefined || (typeof attempt.createdId === 'string' && attempt.createdId.length > 0 && attempt.createdId.length <= 256)));
}
const copy = (value: ProgramDraft): ProgramDraft => ({fields: [...value.fields],
  ...(value.researchSource ? {researchSource: {...value.researchSource}} : {}),
  ...(value.seedObjective !== undefined ? {seedObjective: value.seedObjective} : {}),
  ...(value.attempt ? {attempt: {...value.attempt}} : {})});
const key = (identity: readonly string[]): string => JSON.stringify(identity);
const size = (text: string): number => new TextEncoder().encode(text).byteLength;

export function createProgramDraftStore(ports: {
  owner: () => unknown; scope: string; storage: () => Storage | undefined;
}) {
  let boundOwner: number | undefined;
  let status: DraftStatus | undefined;
  function storageKey(): string | undefined {
    const owner = ports.owner();
    if (typeof owner !== 'number' || !Number.isSafeInteger(owner) || owner < 1
        || !ports.scope || ports.scope.length > 2048 || (boundOwner !== undefined && owner !== boundOwner)) return undefined;
    boundOwner = owner;
    return `tofu.exploration.drafts.v1:${encodeURIComponent(ports.scope)}:${owner}`;
  }
  function snapshot(): {storage: Storage; storageKey: string; drafts: Drafts} | undefined {
    status = undefined;
    try {
      const name = storageKey(), storage = ports.storage();
      if (!name || !storage) throw new Error('No authenticated browser draft scope');
      const raw = storage.getItem(name);
      if (!raw) return {storage, storageKey: name, drafts: Object.create(null) as Drafts};
      if (raw.length > PROGRAM_DRAFT_LIMITS.bytes || size(raw) > PROGRAM_DRAFT_LIMITS.bytes) throw new Error('Oversized draft envelope');
      const parsed = JSON.parse(raw) as {version?: unknown; drafts?: unknown};
      if (parsed?.version !== 1 || !parsed.drafts || typeof parsed.drafts !== 'object' || Array.isArray(parsed.drafts)) throw new Error('Invalid draft envelope');
      const entries = Object.entries(parsed.drafts);
      if (entries.length > PROGRAM_DRAFT_LIMITS.entries) throw new Error('Too many drafts');
      const values: Drafts = Object.create(null);
      for (const [identity, value] of entries) {
        const parts = JSON.parse(identity) as unknown;
        if (!Array.isArray(parts) || !parts.length || parts.length > 4
            || parts.some(part => typeof part !== 'string' || !part || part.length > 512) || !draft(value)) throw new Error('Invalid draft');
        values[identity] = copy(value);
      }
      return {storage, storageKey: name, drafts: values};
    } catch { status = 'unavailable'; return undefined; }
  }
  function publish(current: NonNullable<ReturnType<typeof snapshot>>): DraftStatus {
    const raw = JSON.stringify({version: 1, drafts: current.drafts});
    if (Object.keys(current.drafts).length > PROGRAM_DRAFT_LIMITS.entries || size(raw) > PROGRAM_DRAFT_LIMITS.bytes) return status = 'full';
    try {current.storage.setItem(current.storageKey, raw); return status = 'saved';}
    catch {return status = 'unavailable';}
  }
  return {
    status: () => status,
    read(identity: readonly string[]): ProgramDraft | undefined {
      const value = snapshot()?.drafts[key(identity)];
      if (!value) return undefined;
      status = 'restored'; return copy(value);
    },
    save(identity: readonly string[], value: ProgramDraft): DraftStatus {
      const current = snapshot();
      if (!current || !draft(value) || !identity.length || identity.length > 4
          || identity.some(part => typeof part !== 'string' || !part || part.length > 512)) return status = 'unavailable';
      current.drafts[key(identity)] = copy(value); return publish(current);
    },
    clear(identity: readonly string[], expected?: ProgramDraft): boolean {
      const current = snapshot(); if (!current) return false;
      const name = key(identity);
      // A late acknowledgement may not erase input edited by another tab or
      // a newer mounted form. Compare the complete submitted draft envelope.
      if (expected && JSON.stringify(current.drafts[name]) !== JSON.stringify(expected)) return false;
      delete current.drafts[name]; return publish(current) === 'saved';
    },
  };
}


export function researchProgrammePath(source: {task_id: string; idea_id: string}): string {
  const query = new URLSearchParams({research_task: source.task_id, research_idea: source.idea_id});
  return `evolution/programs/new?${query}`;
}

export function programmeCreationDraftIdentity(path: string): string[] {
  const [route, query] = path.split('?');
  const values = new URLSearchParams(query || '');
  const task = values.get('research_task'), idea = values.get('research_idea');
  return route === 'evolution/programs/new' && task && idea && task.length <= 160 && idea.length <= 160
    ? ['create', 'research', task, idea] : ['create'];
}
