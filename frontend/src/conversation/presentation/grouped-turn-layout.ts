/**
 * Grouped Turn reading layout. An assistant Turn reads as separate zones:
 * leading context, ONE reasoning disclosure, ONE tool panel, the prose, then
 * trailing receipts. The panel holds every tool call plus the process events
 * between them (failures, retries, interventions, injections) in authored
 * order, so a failure sits directly below the call it followed. The Turn's
 * own outcome (accepted work, a terminal failure) is never hidden inside the
 * panel. Zone membership is presentation policy only: order inside a zone is
 * the projection's block order, and Turn authority is never rewritten.
 *
 * Entry points: groupedTurnLayout, groupedTurnEntries, turnLayoutZone,
 * interimNarrationIds.
 * Dependencies: view-model types and thinking merge, tool attention, round
 * identity/state. DOM-free; memoized per immutable Turn view model.
 */
import type {
  ActivityEventBlockViewModel,
  ConversationBlockKind,
  ConversationBlockViewModel,
  ConversationTurnViewModel,
  ThinkingBlockViewModel,
} from './view-model/types';
import { mergeThinkingBlocks } from './view-model/block-coalescing';
import { hasAcceptedWorkUnit, toolRoundAttention } from './tool-execution-groups';
import { roundIdentityOf, toolBlockActivityState } from './round-outline';

export type TurnLayoutZone = 'lead' | 'thinking' | 'process' | 'content' | 'trail';

const ZONE_BY_KIND: Readonly<Record<ConversationBlockKind, TurnLayoutZone>> = {
  origin: 'lead',
  context: 'lead',
  attachments: 'lead',
  provenance: 'lead',
  thinking: 'thinking',
  tool: 'process',
  program: 'process',
  injections: 'process',
  'system-note': 'process',
  'system-note-group': 'process',
  'activity-event': 'process',
  'activity-event-group': 'process',
  'rolled-back': 'process',
  text: 'content',
  compaction: 'content',
  'proposed-plan': 'content',
  'file-changes': 'trail',
  'plan-execution': 'trail',
  'image-generation': 'trail',
  artifacts: 'trail',
  'autopilot-run-notice': 'trail',
  'queue-status': 'trail',
  'live-status': 'trail',
};

/** Accepted deliverables and the Turn's terminal failure stay outside the panel. */
export function turnLayoutZone(block: ConversationBlockViewModel): TurnLayoutZone {
  if ((block.kind === 'tool' || block.kind === 'program') && hasAcceptedWorkUnit(block.round)) {
    return 'trail';
  }
  if (block.kind === 'activity-event' && block.terminalError) return 'trail';
  return ZONE_BY_KIND[block.kind];
}

export interface ToolPanelSummary {
  readonly toolCount: number;
  /** Distinct model rounds; equals toolCount when any call lacks round identity. */
  readonly roundCount: number;
  readonly errorCount: number;
  /** A call is executing now. */
  readonly active: boolean;
  /** State without a reader choice: folded only for a completed reply with
   * its own explicit answer and nothing left to act on. */
  readonly defaultOpen: boolean;
}

export interface GroupedTurnLayout {
  readonly lead: ReadonlyArray<ConversationBlockViewModel>;
  readonly thinking: ThinkingBlockViewModel | null;
  readonly process: ReadonlyArray<ConversationBlockViewModel>;
  readonly content: ReadonlyArray<ConversationBlockViewModel>;
  readonly trail: ReadonlyArray<ConversationBlockViewModel>;
  /** Present only when the process zone holds a tool or program call. */
  readonly panel: ToolPanelSummary | null;
}

/** Surface-owned panel header entry; never a Turn fact or renderer block. */
export interface ToolPanelEntry {
  readonly kind: 'tool-panel';
  readonly blockId: string;
  readonly toolCount: number;
  readonly roundCount: number;
  readonly errorCount: number;
  readonly active: boolean;
  readonly open: boolean;
}

export type GroupedTurnEntry = ConversationBlockViewModel | ToolPanelEntry;

/** A contiguous run of entries the Surface windows on its own. `turn` is an
 * ungrouped Turn's whole list; a grouped reply windows its calls (`process`)
 * and its prose with the trailing receipts (`prose`) independently, so
 * neither crowds the other out of the viewport budget. The head before every
 * region (context, reasoning, panel header) is always mounted, so it never
 * blinks while a region follows its live tail. */
export type EntryRegionKind = 'turn' | 'process' | 'prose';

export interface EntryRegion {
  readonly kind: EntryRegionKind;
  readonly start: number;
  readonly end: number;
}

const regionCache = new WeakMap<ReadonlyArray<GroupedTurnEntry>, ReadonlyArray<EntryRegion>>();

/** Windowable regions of a mounted entry list; unknown lists window whole. */
export function entryRegions(entries: ReadonlyArray<GroupedTurnEntry>): ReadonlyArray<EntryRegion> {
  return regionCache.get(entries) ?? (entries.length ? [{ kind: 'turn', start: 0, end: entries.length }] : []);
}

/** Interim prose of a mounted reply: the contiguous run of round narration
 * that starts at its first interim text, so the run can share one bounded
 * reading well. Interim text is neither the settled answer nor the reply's
 * latest text: the latest stays outside while it may still stream (or become
 * the answer) and joins only once a later round's text follows it. The run
 * ends at the first other entry, so chronology is never reordered. */
export function interimNarrationIds(entries: ReadonlyArray<GroupedTurnEntry>): ReadonlySet<string> {
  let latestText = -1;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    if (entries[index].kind === 'text') { latestText = index; break; }
  }
  const interim = (entry: GroupedTurnEntry): boolean => (
    entry.kind === 'text' && !entry.deliverable && !entry.terminal);
  const ids = new Set<string>();
  let index = 0;
  while (index < latestText && !interim(entries[index])) index += 1;
  for (; index < latestText && interim(entries[index]); index += 1) ids.add(entries[index].blockId);
  return ids;
}

/* Backend block ids never use the `layout:` namespace. */
export const TOOL_PANEL_BLOCK_ID = 'layout:tool-panel';

type ToolLikeBlock = Extract<ConversationBlockViewModel, { kind: 'tool' | 'program' }>;

function toolValue(block: ToolLikeBlock): unknown {
  if (block.round) return block.round;
  const source = block.source as Record<string, unknown>;
  const result = source.result as Record<string, unknown> | undefined;
  return { ...source, status: source.status ?? result?.status };
}

function activityIsError(block: ActivityEventBlockViewModel): boolean {
  const entry = block.value;
  return Boolean(block.terminalError) || entry.status === 'failed' || entry.severity === 'error';
}

function processErrorCount(block: ConversationBlockViewModel): number {
  if (block.kind === 'activity-event') return activityIsError(block) ? 1 : 0;
  if (block.kind === 'activity-event-group') return block.items.filter(activityIsError).length;
  if (block.kind === 'tool' || block.kind === 'program') {
    return toolRoundAttention(toolValue(block)) === 'error' ? 1 : 0;
  }
  return 0;
}

function summarizePanel(
  turn: ConversationTurnViewModel,
  process: ReadonlyArray<ConversationBlockViewModel>,
): ToolPanelSummary | null {
  const tools = process.filter(
    (block): block is ToolLikeBlock => block.kind === 'tool' || block.kind === 'program',
  );
  if (!tools.length) return null;
  const rounds = new Set<string>();
  let identified = true;
  for (const block of tools) {
    const identity = roundIdentityOf(block.source) ?? roundIdentityOf(block.round);
    if (identity) rounds.add(identity.key);
    else identified = false;
  }
  const states = tools.map(toolBlockActivityState);
  const unresolved = tools.some((block, index) => {
    const attention = toolRoundAttention(toolValue(block));
    return attention === 'active' || attention === 'interactive'
      || states[index] === 'active' || states[index] === 'background';
  });
  // Without an explicit answer the calls are the reply's content, so they stay
  // in view; so does any unfinished, waiting or unresolved work.
  const answered = turn.blocks.some((block) => block.kind === 'text'
    && (block.terminal || block.deliverable) && Boolean(block.markdown.trim()));
  return {
    toolCount: tools.length,
    roundCount: identified ? rounds.size : tools.length,
    errorCount: process.reduce((total, block) => total + processErrorCount(block), 0),
    active: states.includes('active'),
    defaultOpen: turn.status !== 'completed' || !answered || unresolved
      || Boolean(turn.commandPending) || Boolean(turn.metadata.waitingOn),
  };
}

const layouts = new WeakMap<ConversationTurnViewModel, GroupedTurnLayout | null>();

/** Zones of an assistant Turn; null for input Turns, which keep their order. */
export function groupedTurnLayout(turn: ConversationTurnViewModel): GroupedTurnLayout | null {
  if (layouts.has(turn)) return layouts.get(turn)!;
  let layout: GroupedTurnLayout | null = null;
  if (turn.role === 'assistant') {
    const zones: Record<TurnLayoutZone, ConversationBlockViewModel[]> = {
      lead: [], thinking: [], process: [], content: [], trail: [],
    };
    for (const block of turn.blocks) zones[turnLayoutZone(block)].push(block);
    const thinking = zones.thinking as ThinkingBlockViewModel[];
    layout = {
      lead: zones.lead,
      thinking: thinking.length ? mergeThinkingBlocks(thinking) : null,
      process: zones.process,
      content: zones.content,
      trail: zones.trail,
      panel: summarizePanel(turn, zones.process),
    };
  }
  layouts.set(turn, layout);
  return layout;
}

const entryCache = new WeakMap<GroupedTurnLayout, Partial<Record<'open' | 'closed',
  ReadonlyArray<GroupedTurnEntry>>>>();

/** Mounted order for one panel state. A closed panel mounts none of its calls. */
export function groupedTurnEntries(
  layout: GroupedTurnLayout,
  open: boolean,
): ReadonlyArray<GroupedTurnEntry> {
  const cached = entryCache.get(layout) ?? {};
  const state = open ? 'open' : 'closed';
  const hit = cached[state];
  if (hit) return hit;
  const entries: GroupedTurnEntry[] = [...layout.lead];
  if (layout.thinking) entries.push(layout.thinking);
  const regions: EntryRegion[] = [];
  let proseStart: number;
  if (layout.panel) {
    const { toolCount, roundCount, errorCount, active } = layout.panel;
    entries.push({
      kind: 'tool-panel', blockId: TOOL_PANEL_BLOCK_ID,
      toolCount, roundCount, errorCount, active, open,
    });
    if (open && layout.process.length) {
      regions.push({ kind: 'process', start: entries.length, end: entries.length + layout.process.length });
      entries.push(...layout.process);
    }
    proseStart = entries.length;
  } else {
    // Without calls there is no panel: inline process rows read with the prose.
    proseStart = entries.length;
    entries.push(...layout.process);
  }
  // Receipts close the prose and window with it: a following reader sees
  // them at the tail, and they never add to a Turn scrolled away from.
  entries.push(...layout.content, ...layout.trail);
  if (entries.length > proseStart) regions.push({ kind: 'prose', start: proseStart, end: entries.length });
  cached[state] = entries;
  entryCache.set(layout, cached);
  regionCache.set(entries, regions);
  return entries;
}
