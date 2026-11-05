/**
 * Escaped markup of the per-turn context rail, its narrow-pane fold line and
 * the two live parts (a workspace root's branch suffix, the checklist row).
 * Every section shares one anatomy: a heading (label, count, optional
 * disclosure) above its facts. Glyphs come from the shared icon registry; the
 * brand mark from the host. No DOM, globals or state.
 * Entry points: turnContextRailMarkup, turnContextRootBranchMarkup,
 * turnContextChecklistRowMarkup, TURN_CONTEXT_PREVIEW_TOOLS,
 * TURN_CONTEXT_PREVIEW_PATHS.
 * Dependencies: html-safety, icons, contracts.
 */
import { raw, safeHtml, type SafeHtmlOutput } from '../../../html-safety';
import { iconHtml } from '../../../icons';
import type {
  TurnContextChecklist,
  TurnContextRailHost,
  TurnContextRailModel,
  TurnContextRailParts,
  TurnContextRoot,
  TurnContextText,
  TurnContextTodo,
  TurnContextTodoStatus,
  TurnContextTool,
} from './contracts';

/** Facts shown before a section's disclosure: a glanceable preview, so a
 * large MCP catalog never turns the rail into a wall of chips. The rail's
 * card scrolls within itself once it outgrows its track or the pane. */
export const TURN_CONTEXT_PREVIEW_TOOLS = 4;
export const TURN_CONTEXT_PREVIEW_PATHS = 3;

/* The labels the composer snapshot records (info-rail.js _collectTools) are
 * a persisted vocabulary; unknown labels fall back to their tone's glyph. */
const TOOL_GLYPHS: Readonly<Record<string, string>> = {
  Search: 'search',
  'Search ×N': 'search',
  Fetch: 'download',
  Browser: 'compass',
  Desktop: 'monitor',
  'Code Exec': 'terminal',
  Memory: 'brain',
  'Image Gen': 'image',
  'Ask User': 'messageCircle',
  Translate: 'languages',
};
const TONE_GLYPHS: Readonly<Record<string, string>> = {
  search: 'search',
  net: 'compass',
  code: 'terminal',
  ai: 'sparkles',
  mcp: 'plug',
};
const MCP_PREFIX = 'MCP: ';

/* Checklist status marks, drawn at the line's text size. */
const TODO_MARKS: Readonly<Record<TurnContextTodoStatus, string>> = {
  completed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
  in_progress: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="5"/></svg>',
  blocked: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M6.5 6.5l11 11"/></svg>',
  pending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><circle cx="12" cy="12" r="8.5"/></svg>',
};

function glyph(name: string): SafeHtmlOutput | '' {
  const svg = iconHtml(name);
  return svg ? safeHtml`<span class="tctx-icon" aria-hidden="true">${raw(svg)}</span>` : '';
}

/* Tones become class names; persisted values outside the token grammar
 * cannot add classes. */
const toneClass = (tone: string): string => (/^[a-z][a-z0-9-]*$/.test(tone) ? tone : 'tool');

function heading(
  label: string,
  count: string,
  disclosure: SafeHtmlOutput | '' = '',
  countState = '',
): SafeHtmlOutput {
  const counter = count
    ? safeHtml`<span class="tctx-row-count${countState}">${count}</span>` : '';
  return safeHtml`<div class="tctx-row-heading"><span class="tctx-row-h">${label}${counter}</span>${disclosure}</div>`;
}

/* The disclosure stays on its heading while the facts expand below it, so it
 * never travels away from the reader's pointer or focus. info-rail.js owns
 * the delegated toggle. */
function disclosure(hidden: number, text: TurnContextText): SafeHtmlOutput | '' {
  if (!hidden) return '';
  return safeHtml`<button type="button" class="tctx-more" data-tctx-more="1" aria-expanded="false">${
    text('turnCtx.showMore', 'Show {count} more', { count: hidden })}</button>`;
}

function toolChip(tool: TurnContextTool): SafeHtmlOutput {
  const server = tool.label.startsWith(MCP_PREFIX);
  /* The plug glyph names the protocol; the chip spends its width on the server. */
  const label = server && tool.tone === 'mcp' ? tool.label.slice(MCP_PREFIX.length) : tool.label;
  const icon = TOOL_GLYPHS[tool.label] ?? TONE_GLYPHS[tool.tone] ?? (server ? 'plug' : 'wrench');
  return safeHtml`<span class="tctx-chip tctx-tone-${toneClass(tool.tone)}" title="${tool.label}">${
    glyph(icon)}<span class="tctx-chip-label">${label}</span></span>`;
}

function toolsRow(tools: ReadonlyArray<TurnContextTool>, text: TurnContextText): SafeHtmlOutput | '' {
  if (!tools.length) return '';
  const rest = tools.slice(TURN_CONTEXT_PREVIEW_TOOLS);
  const overflow = rest.length
    ? safeHtml`<span class="tctx-overflow" hidden>${rest.map(toolChip)}</span>` : '';
  return safeHtml`<div class="tctx-row tctx-tools-row">${
    heading(text('turnCtx.toolsLabel', 'Tools'), String(tools.length), disclosure(rest.length, text))
  }<div class="tctx-chips">${tools.slice(0, TURN_CONTEXT_PREVIEW_TOOLS).map(toolChip)}${overflow}</div></div>`;
}

/* A root's branch follows its path after a colon (`root:branch`). Each name is
 * one line-breaking unit (`.tctx-path-name`), so a pair too long for its line
 * breaks only after the colon. `data-tctx-root` keys the line for the live
 * branch patch. */
function rootLine(root: TurnContextRoot): SafeHtmlOutput {
  return safeHtml`<div class="tctx-path${root.readOnly ? ' tctx-path--readonly' : ''}" title="${
    root.path || root.short}" data-tctx-root="${root.path}">${glyph(root.readOnly ? 'lock' : 'folder')}<span class="tctx-path-text"><span class="tctx-path-name">${
    root.short || root.path}</span>${raw(turnContextRootBranchMarkup(root.branch))}</span></div>`;
}

function rootsRow(roots: ReadonlyArray<TurnContextRoot>, text: TurnContextText): SafeHtmlOutput | '' {
  if (!roots.length) return '';
  const rest = roots.slice(TURN_CONTEXT_PREVIEW_PATHS);
  const overflow = rest.length
    ? safeHtml`<div class="tctx-overflow" hidden>${rest.map(rootLine)}</div>` : '';
  return safeHtml`<div class="tctx-row tctx-roots-row">${
    heading(text('turnCtx.workspaceLabel', 'Workspace'), String(roots.length), disclosure(rest.length, text))
  }<div class="tctx-paths">${roots.slice(0, TURN_CONTEXT_PREVIEW_PATHS).map(rootLine)}${overflow}</div></div>`;
}

/** A root's branch after its path: the `:branch` suffix (`[data-tctx-git]`),
 * the live part info-rail.js replaces in place; empty without a branch. The
 * colon stays outside the branch's name unit, so it stays with the root when
 * the pair wraps. */
export function turnContextRootBranchMarkup(branch: string): string {
  return branch
    ? String(safeHtml`<span class="tctx-path-branch" data-tctx-git="1"><span class="tctx-path-sep">:</span><span class="tctx-path-name">${branch}</span></span>`)
    : '';
}

function todoLine(item: TurnContextTodo, current = false): SafeHtmlOutput {
  return safeHtml`<div class="tctx-todo-item tctx-todo-${item.status}${current ? ' tctx-todo-current' : ''}" title="${
    item.text}"><span class="tctx-todo-mark" aria-hidden="true">${raw(TODO_MARKS[item.status])}</span><span class="tctx-todo-text">${
    item.text}</span></div>`;
}

/** Checklist row: progress, the live edge of the plan, then the other items
 * in plan order. Completed items fade; blocked ones stay explicit. */
export function turnContextChecklistRowMarkup(
  checklist: TurnContextChecklist | null,
  text: TurnContextText,
): string {
  if (!checklist) return '';
  const { items, done, total, current } = checklist;
  const rest = items.filter((item) => item !== current);
  const progress = Math.round((done / total) * 100);
  const list = rest.length ? safeHtml`<div class="tctx-todo-list">${rest.map((item) => todoLine(item))}</div>` : '';
  return String(safeHtml`<div class="tctx-row tctx-todo-row" data-tctx-todo="1">${
    heading(text('turnCtx.todoLabel', 'Todos'), `${done}/${total}`, '', done === total ? ' is-done' : '')
  }<div class="tctx-todos"><span class="tctx-todo-progress" aria-hidden="true"><span class="tctx-todo-progress-fill" style="width:${
    progress}%"></span></span>${todoLine(current, true)}${list}</div></div>`);
}

function head(model: TurnContextRailModel, host: TurnContextRailHost): SafeHtmlOutput | '' {
  const logo = model.modelId ? host.brandIconHtml?.(model.modelId) ?? '' : '';
  const name = model.modelLabel
    ? safeHtml`<span class="tctx-model" title="${model.modelId}">${model.modelLabel}</span>` : '';
  const depth = model.depth ? safeHtml`<span class="tctx-depth">${model.depth}</span>` : '';
  const group = name || depth ? safeHtml`<span class="tctx-model-group">${name}${depth}</span>` : '';
  const modes = model.modes.map((mode) => safeHtml`<span class="tctx-mode-badge">${mode}</span>`);
  if (!logo && !group && !modes.length) return '';
  return safeHtml`<div class="tctx-head">${
    logo ? safeHtml`<span class="tctx-logo" aria-hidden="true">${raw(logo)}</span>` : ''}${group}${modes}</div>`;
}

/* The same facts as one line for panes that cannot grant the rail a track. */
function foldMarkup(model: TurnContextRailModel, text: TurnContextText): string {
  const facts = [
    model.modelLabel,
    model.depth,
    ...model.modes,
    model.tools.length ? text('turnCtx.toolCount', '{count} tools', { count: model.tools.length }) : '',
    model.roots.length ? text('turnCtx.workspaceCount', '{count} workspaces', { count: model.roots.length }) : '',
    model.git ? `⑂ ${model.git.branch}` : '',
    model.checklist
      ? text('turnCtx.todoCount', '{done}/{total} todos', { done: model.checklist.done, total: model.checklist.total })
      : '',
  ].filter(Boolean).join(' · ');
  /* The fold truncates on tight panes; its title keeps the whole line. */
  return String(safeHtml`<div class="tctx-fold" title="${facts}"><span class="tctx-fold-dot"></span><span>${facts}</span></div>`);
}

export function turnContextRailMarkup(
  model: TurnContextRailModel,
  host: TurnContextRailHost,
): TurnContextRailParts {
  const rows = [
    toolsRow(model.tools, host.text),
    rootsRow(model.roots, host.text),
    raw(turnContextChecklistRowMarkup(model.checklist, host.text)),
  ];
  return {
    fold: foldMarkup(model, host.text),
    rail: String(safeHtml`<div class="turn-ctx">${head(model, host)}${rows}</div>`),
  };
}
