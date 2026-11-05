/**
 * Per-turn context rail: the single presentation owner of a Turn's context
 * snapshot, beside the reading column (the rail) and under the message
 * header in panes too narrow for it (the fold). Ownership map: AGENTS.md.
 * Entry points: renderTurnContextRailHtml, renderTurnContextRootBranchHtml,
 * renderTurnContextChecklistRowHtml, projectTurnContextRail.
 * Dependencies: projection, markup, contracts.
 */
import type { TurnContextRailHost, TurnContextRailParts } from './contracts';
import {
  turnContextChecklistRowMarkup,
  turnContextRailMarkup,
  turnContextRootBranchMarkup,
} from './markup';
import {
  normalizeTurnContextChecklist,
  normalizeTurnContextGit,
  projectTurnContextRail,
  turnContextRootBranch,
} from './projection';

export type {
  TurnContextRailHost,
  TurnContextRailModel,
  TurnContextRailParts,
  TurnContextText,
} from './contracts';
export { projectTurnContextRail } from './projection';
export { TURN_CONTEXT_PREVIEW_PATHS, TURN_CONTEXT_PREVIEW_TOOLS } from './markup';

/** Escaped rail and fold markup for one snapshot, or null when it carries
 * nothing worth showing. The rail root is `.turn-ctx`, the fold `.tctx-fold`. */
export function renderTurnContextRailHtml(
  snapshot: unknown,
  host: TurnContextRailHost,
): TurnContextRailParts | null {
  const model = projectTurnContextRail(snapshot, host);
  return model ? turnContextRailMarkup(model, host) : null;
}

/** Live branch suffix of one workspace root line (`[data-tctx-root]`): its
 * `:branch` part (`[data-tctx-git]`), empty when that root has no branch.
 * `primary` marks the turn's first root, which owns a branch recorded
 * without a path. */
export function renderTurnContextRootBranchHtml(git: unknown, path: string, primary: boolean): string {
  return turnContextRootBranchMarkup(turnContextRootBranch(normalizeTurnContextGit(git), path, primary));
}

/** Live checklist row (`[data-tctx-todo]`); empty when there is no plan. */
export function renderTurnContextChecklistRowHtml(
  todos: unknown,
  host: Pick<TurnContextRailHost, 'text'>,
): string {
  return turnContextChecklistRowMarkup(normalizeTurnContextChecklist(todos), host.text);
}
