/** Notebook adapter for the shared terminal reader. One lazy content island;
 * receipt-bound explicit GETs only, no timers, writes or durable evidence cache.
 */
import type { EvolutionRun, EvolutionSourceKind, EvolutionSourceReference } from '../../../api/evolution-runs';
import type { ExplorationReceipt } from '../../../api/evolution-programs';
import { t, type I18nKey, type I18nParamsFor } from '../../../i18n';
import { createButton } from '../../../ui';
import { mountOutcomeReader, type OutcomeRead } from '../studio/outcome-reader';

type UnparameterizedStudioKey = {
  [K in Extract<I18nKey, `evolution.studio.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.studio.${string}`>];
const copy = (key: string) => t(`evolution.studio.${key}` as UnparameterizedStudioKey);
function reference(value: EvolutionSourceReference | undefined, kind: EvolutionSourceKind) {
  return value && typeof value.task_id === 'string' && !!value.task_id
    && /^[0-9a-f]{64}$/.test(value.source_sha256) && Number.isSafeInteger(value.total_chars)
    && value.total_chars >= 0 && (value.source_kind === undefined || value.source_kind === kind) ? {...value} : undefined;
}
export function mountProgramSources(host: HTMLElement, ports: {
  read?: OutcomeRead; visible: () => boolean; fallbackFocus: () => void;
}) {
  let owner = '', disposed = false;
  let reader: ReturnType<typeof mountOutcomeReader> | undefined;
  host.hidden = true;host.dataset.programSourceReader = 'true';
  return {
    owner(id: string) {if (id !== owner) {owner = id;reader?.reset();}},
    controls(record: Pick<EvolutionRun, 'run_id' | 'result' | 'result_source' | 'tool_evidence_source' | 'artifact_evidence_source' | 'background_evidence_source'> & Partial<Pick<ExplorationReceipt, 'program_id'>>, offset?: number) {
      if (disposed || !owner || (record.program_id && record.program_id !== owner)) return undefined;
      const references = {deliverable: reference(record.result_source, 'deliverable'),
        tool_evidence: reference(record.tool_evidence_source, 'tool_evidence'),
        artifact_evidence: reference(record.artifact_evidence_source, 'artifact_evidence'),
        background_evidence:reference(record.background_evidence_source,'background_evidence')};
      if (!references.deliverable && !references.tool_evidence && !references.artifact_evidence && !references.background_evidence) return undefined;
      const actions = document.createElement('div');actions.className = 'exp-source-actions';
      const note = document.createElement('p');note.className = 'exp-muted';
      note.textContent = t(`evolution.program.${ports.read ? 'sourcePreview' : 'sourceUnavailable'}`);
      actions.append(note);
      if (!ports.read) {
        const link = document.createElement('a');link.className = 'exp-link';
        link.href = `#/evolution/runs/${encodeURIComponent(record.run_id)}`;link.textContent = t('evolution.program.openRoom');actions.append(link);
        return actions;
      }
      const captured = owner;
      for (const kind of ['deliverable', 'tool_evidence', 'artifact_evidence', 'background_evidence'] as const) {
        const source = references[kind];if (!source) continue;
        const button = createButton({label: copy(kind === 'deliverable' ? 'sourceOpen' : kind==='background_evidence'?'sourceBackgroundEvidence':kind === 'artifact_evidence' ? 'sourceArtifacts' : 'sourceToolEvidence'), size: 'sm', onClick: () => {
          if (disposed || captured !== owner || ports.visible() === false) return;
          reader ||= mountOutcomeReader(host, {copy, read: ports.read!, visible: ports.visible, fallbackFocus: ports.fallbackFocus});
          // References for a different task require their own explicit opener.
          const expected = Object.fromEntries(Object.entries(references).filter(([, value]) => value?.task_id === source.task_id));
          reader.select(record.run_id, source.task_id, false, button, {kind, references: expected, offset});
        }});
        button.dataset.programSource = kind;actions.append(button);
      }
      return actions;
    },
    visibilityChanged() {reader?.visibilityChanged(ports.visible());},
    dispose() {disposed = true;reader?.dispose();host.replaceChildren();host.hidden = true;},
  };
}
