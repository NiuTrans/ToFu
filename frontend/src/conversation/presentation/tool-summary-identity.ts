/**
 * Exact identity and user-facing intent of one tool/program block.
 * Entry point: toolSummaryIdentity. Pure projection shared by native header
 * decoration and round search (`../ui/tool-summary.ts`, `../ui/round-outline.ts`)
 * and the live-status row (`view-model/live-turns.ts`).
 * Dependencies: view-model block types only; no DOM.
 */
import type { ConversationBlockViewModel } from './view-model/types';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === 'string' ? value : '';

export function toolSummaryIdentity(block: ConversationBlockViewModel): { name: string; target: string; intent: string } {
  if (block.kind !== 'tool' && block.kind !== 'program') return { name: '', target: '', intent: '' };
  const source = record(block.source);
  const round = record(block.round);
  let input = round.toolArgs ?? source.input;
  if (typeof input === 'string' && input.length <= 120_000) {
    try { input = JSON.parse(input); } catch { input = null; }
  }
  const args = record(input);
  const meta = record(Array.isArray(round.results) ? round.results[0] : null);
  const name = text(round.toolName) || text(source.name) || text(source.toolName);
  const paths = Array.isArray(args.paths) ? args.paths.slice(0, 3).map(text).join(', ') : '';
  const target = [args.path, args.file_path, paths, args.command, args.url, args.query, args.pattern,
    args.cwd, args.root, meta.command].map(text).find(Boolean) ?? '';
  const intent = text(args.description) || text(meta.description) || text(round.query) || name;
  return { name, target: target.slice(0, 2000), intent: intent.slice(0, 2000) };
}
