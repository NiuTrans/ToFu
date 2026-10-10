/** Observe committed files and bounded authoritative discovery hints. No DOM,
 * output/prose scanning, PDF dependency or durable conversation state.
 */
import type { TurnState, TurnStateUpdate } from '../../core/turn-state';
import type { ProjectionTurn } from '../../core/turn-projection';
import type { TurnFileChange } from '../../api/conversation-sync.generated';
import { PAPER_SOURCE_SUFFIXES, PAPER_BUILD_LIMITS as limits } from '../../api/paper-build.generated';

export interface PaperPreviewChange {
  conversationId: string; changeId: string; source: 'local' | 'overleaf';
  path: string; file?: string; projectId?: string; mcpServer?: string; mainFile?: string;
  projectRoot?: string;
}
interface ChangePorts {
  changed(changes: PaperPreviewChange[]): void;
  activated(conversationId: string | null): void;

  discover?(conversationId: string, hint?: { path: string; projectRoot?: string }): void;
}
const suffixes = new Set<string>(PAPER_SOURCE_SUFFIXES);

type DiscoveryHint = { path: string; projectRoot?: string };
const texPath = (path: string): boolean => /\.(tex|ltx)$/i.test(path);
const boundedPath = (value: unknown): value is string => typeof value === 'string'
  && value.length > 0 && value.length <= 4096 && !/[\x00-\x1f]/.test(value);
const record = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;

function discoveryHints(turn: ProjectionTurn, cold = false): Map<string, DiscoveryHint | undefined> {
  const hints = new Map<string, DiscoveryHint | undefined>();
  const projection = turn.projection;
  const block = projection?.fileChanges;
  if (!block?.state || block.state === 'applied') {
    for (const file of (block?.files ?? projection?.modifiedFileList ?? []).slice(-limits.source_files)) {
      if ((!cold && file.changeId) || file.pending || file.ok === false || file.action === 'deleted'
        || file.source === 'overleaf' || !boundedPath(file.path) || !texPath(file.path)) continue;
      // Qualification is shared with real receipts, but no invented receipt escapes.
      const qualified = qualifyPath(file);
      if (!boundedPath(qualified.path) || (qualified.projectRoot && !boundedPath(qualified.projectRoot))) continue;
      hints.set(`file:${qualified.projectRoot ?? ''}:${qualified.path}`, qualified);
    }
  }
  for (const round of (projection?.toolRounds ?? []).slice(-limits.source_files)) {
    if (!round.toolCallId || round.rejection || round._rejected || round.result?.rejection
      || round.result?._rejected || round.result?.rejected
      || !['done', 'completed', 'success'].includes(round.status ?? '')
      || (round.result?.status && !['done', 'completed', 'success'].includes(round.result.status))) continue;
    let rawArgs = round.toolArgs;
    if (typeof rawArgs === 'string') {
      if (rawArgs.length > 16384) continue;
      try { rawArgs = JSON.parse(rawArgs); } catch { continue; }
    }
    const args = record(rawArgs);
    if (!args) continue;
    // Only structured result flags are inspected, never arbitrary tool output.
    const result = record(round.result?.content) ?? record(round.results?.[0]) ?? record(round.toolContent);
    if (result?.ok === false || result?.error || result?.notRun || result?.interrupted
      || result?.timedOut || (result?.exitCode !== undefined && result.exitCode !== 0 && result.exitCode !== '0')) continue;
    const key = `tool:${round.attemptId ?? ''}:${round.toolCallId}`;
    if (round.toolName === 'read_files') {
      const paths = Array.isArray(args.reads) ? args.reads.slice(0, 20).map(read => record(read)?.path) : [args.path];
      for (const path of paths) {
        if (boundedPath(path) && texPath(path)) hints.set(key, { path });
      }
    } else if (round.toolName === 'run_command' && typeof args.command === 'string'
      && args.command.length <= 16384 && args.background !== true
      && /(?:^|[;&|\n])\s*(?:unzip\b(?![^;&|\n]*\s-[^\s]*[ltZ])|tar\s+(?:--extract\b|-[a-zA-Z]*x[a-zA-Z]*\b|[a-zA-Z]*x[a-zA-Z]*\b))/.test(args.command)) {
      hints.set(key, boundedPath(args.working_dir) ? { path: args.working_dir } : undefined);
    }
  }
  return hints;
}

function qualifyPath(file: TurnFileChange): DiscoveryHint {
  const path = file.action === 'deleted' ? file.path.replace(/[^/\\]+$/, '') || '.' : file.path;
  const absolute = /^(?:[/\\]|[a-z]:[/\\])/i.test(path);
  const projectRoot = file.rootPath?.startsWith('remote:') ? file.rootPath : undefined;
  return { path: projectRoot ? path : !absolute && file.rootPath ? file.rootPath.replace(/[/\\]$/, '') + '/' + path
    : !absolute && file.root ? `${file.root}:${path}` : path, projectRoot };
}

function candidate(conversationId: string, file: TurnFileChange): PaperPreviewChange | null {
  if (!file.changeId || file.ok === false || file.pending || !suffixes.has(/\.[^.\/]+$/.exec(file.path)?.[0]?.toLowerCase() ?? '')) return null;
  const qualified = qualifyPath(file);
  return { conversationId, changeId: file.changeId, source: file.source ?? 'local',
    ...qualified,
    file: file.path, projectId: file.projectId, mcpServer: file.mcpServer, mainFile: file.mainFile };
}

export function createPaperPreviewChanges(ports: ChangePorts): {
  activateConversation(id: string | null): void;
  observe(id: string, state: TurnState, update?: TurnStateUpdate): void;
} {
  let active: string | null = null;
  let initialized = false;

  let observedUpdates = new WeakSet<TurnStateUpdate>();
  const coldDiscovery = (id: string, state: TurnState): void => {
    const turns = Object.values(state.turnsById).filter(turn => !!turn)
      .sort((a, b) => Number(b.ordinal) - Number(a.ordinal)).slice(0, limits.source_files);
    for (const turn of turns) {
      const hints = [...discoveryHints(turn, true).values()];
      if (hints.length) { ports.discover?.(id, hints.at(-1)); return; }
    }
  };
  const files = (turn?: ProjectionTurn): TurnFileChange[] => {
    const block = turn?.projection?.fileChanges;
    if (block?.state && block.state !== 'applied' && block.state !== 'undone') return [];
    return (block?.files ?? turn?.projection?.modifiedFileList ?? []).slice(0, limits.source_files)
      .filter(file => !!file.changeId && !file.pending && file.ok !== false);
  };
  const receiptKey = (turn: ProjectionTurn, file: TurnFileChange): string => {
    const block = turn.projection?.fileChanges;
    return `${file.changeId}\n${block?.commandId ?? ''}\n${block?.state ?? 'applied'}`;
  };
  return {
    activateConversation(id) {
      if (active === id) return;
      active = id; initialized = false;

      observedUpdates = new WeakSet();
      ports.activated(id);
    },
    observe(id, state, update) {
      if (active !== id) return;
      // Repainting has no new reducer transition. Cold hydration establishes a
      // baseline even if the empty store was bound before the snapshot arrived.
      if (!update) {
        if (!initialized && (Object.keys(state.turnsById).length > 0 || state.conversationRevision > 0)) {
          initialized = true;
          coldDiscovery(id, state);
        }
        return;
      }
      if (observedUpdates.has(update)) return;
      observedUpdates.add(update);
      if (!initialized) {
        if (update.action.type === 'snapshot') { initialized = true; coldDiscovery(id, state); return; }
        if (update.action.type !== 'command_response' && update.action.type !== 'event') return;
        initialized = true;
      }
      const previous = update.previousState;
      let previousNewest = -1;
      for (const turn of Object.values(previous.turnsById)) {
        if (turn) previousNewest = Math.max(previousNewest, Number(turn.ordinal));
      }
      const changes: PaperPreviewChange[] = [];

      let discover = false;
      let hint: DiscoveryHint | undefined;
      const currentTurns = Object.values(state.turnsById).filter((turn) => !!turn)
        .filter(turn => turn !== previous.turnsById[turn.turnId])
        .sort((a, b) => Number(a.ordinal) - Number(b.ordinal));
      for (const turn of currentTurns) {
        const prior = previous.turnsById[turn.turnId];
        if (!prior && Number(turn.ordinal) <= previousNewest) continue;

        const knownHints = prior ? discoveryHints(prior) : new Map();
        for (const [key, value] of discoveryHints(turn)) {
          if (!knownHints.has(key)) { discover = true; hint = value; }
        }
        const block = turn.projection?.fileChanges;
        if (block === prior?.projection?.fileChanges
          && turn.projection?.modifiedFileList === prior?.projection?.modifiedFileList) continue;
        const known = new Set(prior ? files(prior).map(file => receiptKey(prior, file)) : []);
        for (const file of files(turn)) {
          if (known.has(receiptKey(turn, file))) continue;
          const change = candidate(id, { ...file,
            changeId: block?.commandId || file.changeId,
            ...(block?.state === 'undone' ? { action: 'deleted' } : {}) });
          if (change) {
            changes.push(change);
            if (changes.length > limits.source_files) changes.shift();
          }
        }
      }
      // Consumers select the last receipt; keep TeX ahead of bibliography/assets.
      if (changes.length) ports.changed(changes.sort((a, b) => Number(texPath(a.file ?? a.path)) - Number(texPath(b.file ?? b.path))));
      if (discover && !changes.length) ports.discover?.(id, hint);
    },
  };
}
