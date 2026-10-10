/** Single display owner for Turn acquisition facts and their context witnesses.
 * Never manufactures injected text or moves an existing chronological note.
 * Matching is conservative: ambiguous/retried witnesses use unpaired metadata.
 */
import type { TurnProvenance } from '../../api/conversation-sync.generated';
import { contextNotePolicy, isContextInjectionNote } from './context-note-grouping';
import type { ConversationBlockViewModel, SystemNoteBlockViewModel } from './view-model/types';

export function partitionContextProvenance(value: TurnProvenance): {
  context: TurnProvenance; actions: TurnProvenance;
} {
  const context: TurnProvenance = { blockId: value.blockId };
  const actions: TurnProvenance = { blockId: value.blockId };
  for (const field of ['memoryPrefetch', 'preferencesApplied', 'relatedConversations',
    'mcpToolsDelta', 'projectPathChange'] as const) {
    if (value[field]) context[field] = value[field];
  }
  if (value.mcpLoginHint) {
    if (!value.mcpLoginHint.phase || value.mcpLoginHint.phase === 'awaiting_approval') {
      actions.mcpLoginHint = value.mcpLoginHint;
    } else context.mcpLoginHint = value.mcpLoginHint;
  }
  if (value.preferencesLearned?.length) {
    const pending = value.preferencesLearned.filter((item) => item.pending);
    const settled = value.preferencesLearned.filter((item) => !item.pending);
    if (pending.length) actions.preferencesLearned = pending;
    if (settled.length) context.preferencesLearned = settled;
  }
  return { context, actions };
}

export function hasContextProvenance(value: TurnProvenance): boolean {
  return Object.keys(value).some((field) => field !== 'blockId');
}

function mayPair(field: string, value: unknown): boolean {
  if (field !== 'memoryPrefetch') return true;
  const memory = value as { phase?: string; selected?: number };
  // Selection completion is not injection; the note separately witnesses it.
  return memory?.phase === 'done' && Number(memory.selected) > 0;
}

/** Called after chronological grouping. A unique matching note owns the fact;
 * otherwise the first leading context disclosure holds it as metadata. With
 * no startup context run the provenance-only block remains the fallback. */
export function integrateContextProvenance(blocks: ConversationBlockViewModel[]): void {
  const provenanceIndex = blocks.findIndex((block) => block.kind === 'provenance');
  if (provenanceIndex < 0) return;
  const provenanceBlock = blocks[provenanceIndex];
  if (provenanceBlock.kind !== 'provenance') return;
  const { context, actions } = partitionContextProvenance(provenanceBlock.value);
  if (hasContextProvenance(actions)) {
    // An actionable prompt may not be swallowed by a collapsed metadata fold.
    blocks.splice(provenanceIndex + 1, 0, {
      ...provenanceBlock, blockId: `${provenanceBlock.blockId}:actions`, value: actions,
    });
  }
  const unmatched: TurnProvenance = { blockId: context.blockId };
  const notes: SystemNoteBlockViewModel[] = blocks.flatMap((block) => (
    block.kind === 'system-note-group' ? [...block.items]
      : block.kind === 'system-note' && isContextInjectionNote(block) ? [block] : []
  ));
  for (const [field, value] of Object.entries(context)) {
    if (field === 'blockId') continue;
    const candidates = notes.filter((note) => (
      contextNotePolicy(note)?.provenanceField === field
    ));
    if (candidates.length === 1 && mayPair(field, value)) {
      candidates[0].provenance = { blockId: context.blockId, [field]: value };
    } else Object.assign(unmatched, { [field]: value });
  }
  const firstContent = blocks.findIndex((block) => ![
    'provenance', 'attachments', 'origin', 'context',
  ].includes(block.kind));
  const leading = blocks[firstContent];
  if (hasContextProvenance(unmatched) && leading?.kind === 'system-note-group') {
    leading.provenance = unmatched;
  } else if (hasContextProvenance(unmatched) && leading?.kind === 'system-note'
      && isContextInjectionNote(leading)) {
    blocks[firstContent] = {
      blockId: leading.blockId, kind: 'system-note-group', source: leading.source,
      identitySource: leading.identitySource, items: [leading], provenance: unmatched,
    };
  } else if (hasContextProvenance(unmatched)) {
    // Provenance-only legacy turns retain a keyed native context disclosure.
    blocks[provenanceIndex] = { ...provenanceBlock, value: unmatched };
    return;
  }
  blocks.splice(provenanceIndex, 1);
}
