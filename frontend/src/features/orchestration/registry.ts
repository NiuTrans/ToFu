/**
 * Module-private compatibility port for the incrementally typed
 * Orchestration owners. It replaces the file-scope habit of publishing
 * factories and contract helpers on ``window`` while the retained controller
 * is split into direct imports.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const orchestrationRegistry: any = Object.create(null);
