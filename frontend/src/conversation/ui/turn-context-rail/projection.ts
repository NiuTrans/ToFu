/**
 * Snapshot normalization for the per-turn context rail. Accepts persisted,
 * legacy and live snapshot shapes and returns display facts only; no DOM,
 * globals or markup.
 * Entry points: projectTurnContextRail, normalizeTurnContextGit,
 * turnContextRootBranch, normalizeTurnContextChecklist.
 * Dependencies: contracts.
 */
import type {
  TurnContextBranch,
  TurnContextChecklist,
  TurnContextGit,
  TurnContextRailHost,
  TurnContextRailModel,
  TurnContextRoot,
  TurnContextTodo,
  TurnContextTodoStatus,
  TurnContextTool,
} from './contracts';

/* DOM bounds of one rail, unchanged from the retained renderer: a todo_write
 * plan paints at most this many lines, each line and branch name is clipped. */
const CHECKLIST_DISPLAY_ITEMS = 24;
const CHECKLIST_TEXT_CHARS = 240;
const BRANCH_NAME_CHARS = 80;

const TODO_STATUSES: ReadonlySet<string> = new Set(['completed', 'in_progress', 'blocked']);

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

const record = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null);

/** A full absolute path stacks four lines high in the rail; show its tail. */
function lastSegments(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts.length <= 2 ? path : parts.slice(-2).join('/');
}

function normalizeTools(values: unknown): TurnContextTool[] {
  if (!Array.isArray(values)) return [];
  const tools: TurnContextTool[] = [];
  for (const value of values) {
    const item = record(value);
    const label = item ? text(item.label) : text(value);
    if (label) tools.push({ label, tone: item ? text(item.tone) : '' });
  }
  return tools;
}

function normalizeLabels(values: ReadonlyArray<unknown>): string[] {
  return values.flatMap((value) => {
    const label = text(record(value)?.label ?? value);
    return label ? [label] : [];
  });
}

type RootFacts = Omit<TurnContextRoot, 'branch'>;

function normalizeRoots(values: unknown): RootFacts[] {
  if (!Array.isArray(values)) return [];
  const roots: RootFacts[] = [];
  for (const value of values) {
    const item = record(value);
    const path = item ? text(item.path) : text(value);
    const short = (item ? text(item.short) : '') || (path ? lastSegments(path) : '');
    if (!short && !path) continue;
    roots.push({ short, path, readOnly: Boolean(item && (item.ro === true || item.readOnly === true)) });
  }
  return roots;
}

export function normalizeTurnContextGit(value: unknown): TurnContextGit | null {
  const item = record(value);
  const branch = item ? text(item.branch).slice(0, BRANCH_NAME_CHARS) : '';
  if (!item || !branch) return null;
  const branches: TurnContextBranch[] = [];
  for (const row of Array.isArray(item.branches) ? item.branches : []) {
    const entry = record(row);
    const name = entry ? text(entry.branch).slice(0, BRANCH_NAME_CHARS) : '';
    if (!entry || !name) continue;
    branches.push({ path: text(entry.path), branch: name });
  }
  return { branch, branches: branches.length ? branches : [{ path: '', branch }] };
}

/** The branch one root was on: its own probe entry or, for the primary root,
 * a branch recorded without a path (snapshots that predate per-root probes).
 * An entry for a path the turn did not run in names no root. */
export function turnContextRootBranch(git: TurnContextGit | null, path: string, primary: boolean): string {
  if (!git) return '';
  const own = path ? git.branches.find((entry) => entry.path === path) : undefined;
  const recordedWithoutPath = primary ? git.branches.find((entry) => !entry.path) : undefined;
  return (own ?? recordedWithoutPath)?.branch ?? '';
}

export function normalizeTurnContextChecklist(value: unknown): TurnContextChecklist | null {
  if (!Array.isArray(value)) return null;
  const items: TurnContextTodo[] = [];
  let done = 0;
  let total = 0;
  for (const row of value) {
    const entry = record(row);
    const content = entry ? text(entry.content).slice(0, CHECKLIST_TEXT_CHARS) : '';
    if (!entry || !content) continue;
    const status = (TODO_STATUSES.has(entry.status as string)
      ? entry.status : 'pending') as TurnContextTodoStatus;
    total += 1;
    if (status === 'completed') done += 1;
    if (items.length < CHECKLIST_DISPLAY_ITEMS) items.push({ text: content, status });
  }
  if (!items.length) return null;
  const current = items.find((item) => item.status === 'in_progress')
    ?? items.find((item) => item.status !== 'completed')
    ?? items[items.length - 1];
  return { items, done, total, current };
}

/** Normalize one snapshot; null when it carries nothing worth a rail. */
export function projectTurnContextRail(
  snapshot: unknown,
  host: Pick<TurnContextRailHost, 'modelLabel' | 'currentGit'> = {},
): TurnContextRailModel | null {
  const source = record(snapshot);
  if (!source) return null;
  let tools = normalizeTools(source.tools);
  let modes: string[];
  if (Array.isArray(source.modes)) {
    modes = normalizeLabels(source.modes);
  } else {
    /* Snapshots sent before modes had their own field carried them inside
     * `tools` as tone:'mode' entries. */
    modes = tools.filter((tool) => tool.tone === 'mode').map((tool) => tool.label);
    tools = tools.filter((tool) => tool.tone !== 'mode');
  }
  const modelId = text(source.model);
  const roots = normalizeRoots(source.roots);
  /* A branch is probed from the workspace roots, so a turn without roots ran
   * on no branch. A turn sent before its workspace's branch was known (an
   * engine restart clears the scoped root registry) froze none: paint the
   * branch the workspace is on now. A recorded branch keeps its own history. */
  const git = roots.length
    ? normalizeTurnContextGit(source.git) ?? normalizeTurnContextGit(host.currentGit?.())
    : null;
  const checklist = normalizeTurnContextChecklist(source.todos);
  if (!modelId && !tools.length && !modes.length && !roots.length && !checklist) return null;
  return {
    modelId,
    modelLabel: modelId ? (text(host.modelLabel?.(modelId)) || modelId) : '',
    depth: text(source.depth),
    modes,
    tools,
    roots: roots.map((root, index) => ({ ...root, branch: turnContextRootBranch(git, root.path, index === 0) })),
    git,
    checklist,
  };
}
