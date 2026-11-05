/**
 * Display contracts of the per-turn context rail. Types only.
 * Entry points: TurnContextRailHost, TurnContextRailModel and its parts.
 * Dependencies: none.
 */

/** Localized copy: the host owns the catalog and the fallback wording. */
export type TurnContextText = (
  key: string,
  fallback: string,
  values?: Readonly<Record<string, string | number>>,
) => string;

/**
 * Services the rail borrows from its composition. Only `text` is required;
 * a host without the model catalog still renders every persisted fact.
 */
export interface TurnContextRailHost {
  text: TurnContextText;
  /** Display name of a model id from the model catalog. */
  modelLabel?(modelId: string): string;
  /** Trusted brand-mark markup for a model id (the model picker's owner). */
  brandIconHtml?(modelId: string): string;
  /** Branch the workspace is on now, for snapshots that predate the probe. */
  currentGit?(): unknown;
}

export interface TurnContextTool {
  label: string;
  tone: string;
}

export interface TurnContextRoot {
  /** Last path segments shown in the rail; the full path is one hover away. */
  short: string;
  path: string;
  readOnly: boolean;
  /** Branch checked out in this root when the turn ran; '' outside a repository. */
  branch: string;
}

/** One root's probed branch; `path` is '' in snapshots that predate per-root probes. */
export interface TurnContextBranch {
  path: string;
  branch: string;
}

export interface TurnContextGit {
  branch: string;
  branches: ReadonlyArray<TurnContextBranch>;
}

export type TurnContextTodoStatus = 'completed' | 'in_progress' | 'blocked' | 'pending';

export interface TurnContextTodo {
  text: string;
  status: TurnContextTodoStatus;
}

export interface TurnContextChecklist {
  /** Display-bounded prefix of the plan, in plan order. */
  items: ReadonlyArray<TurnContextTodo>;
  /** Progress over the whole plan, not only the displayed prefix. */
  done: number;
  total: number;
  /** The live edge of the plan, always on screen: in progress, else the first open item. */
  current: TurnContextTodo;
}

/** Normalized facts of one Turn's context snapshot. */
export interface TurnContextRailModel {
  modelId: string;
  modelLabel: string;
  depth: string;
  modes: ReadonlyArray<string>;
  tools: ReadonlyArray<TurnContextTool>;
  roots: ReadonlyArray<TurnContextRoot>;
  git: TurnContextGit | null;
  checklist: TurnContextChecklist | null;
}

/** The rail and its narrow-pane fold have different DOM homes. */
export interface TurnContextRailParts {
  fold: string;
  rail: string;
}
