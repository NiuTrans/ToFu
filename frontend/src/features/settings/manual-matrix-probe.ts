/** Manual matrix detection commands and start-error presentation.
 * Settings supplies its provider draft plus the last saved authority. Only
 * saved references and ephemeral wire names cross the API; no draft writes,
 * enablement changes, endpoints, or secrets belong to this owner. */
import type { ModelRoutingDocument, ProbeCellsStartRequest } from '../../api/api-v1-model-routing.generated';
import { apiFailure } from '../../api/errors';
import { t } from '../../i18n';

type Indexed<T> = { row: T };
export interface ManualMatrixContext {
  provider: { provider_id: string };
  access: { provider_access_id: string };
  offerings: Array<Indexed<{ offering_id: string }>>;
  connections: Array<Indexed<{ connection_id: string }>>;
  deployments: Array<Indexed<{ offering_id: string; connection_id: string; wire_model_id: string }>>;
  credentials: Array<Indexed<{ credential_id: string }>>;
}
interface ProbeOptions {
  attempts?: number;
  force?: boolean;
  only?: ProbeCellsStartRequest['only'] | null;
}

class ProbeSelectionError extends Error {
  constructor(readonly code: string) { super(code); }
}

export function buildManualMatrixProbeCommand(
  context: ManualMatrixContext | null,
  savedJson: string,
  options: ProbeOptions = {},
): ProbeCellsStartRequest {
  if (!context || !savedJson) throw new ProbeSelectionError('provider_probe_stale_selection');
  const saved = JSON.parse(savedJson) as ModelRoutingDocument;
  const accessId = context.access.provider_access_id;
  if (!saved.provider_accesses.some(row => row.provider_access_id === accessId
      && row.provider_id === context.provider.provider_id)) {
    throw new ProbeSelectionError('provider_probe_stale_selection');
  }
  const offeringIds = new Set(context.offerings.map(item => item.row.offering_id));
  const connectionIds = new Set(context.connections.map(item => item.row.connection_id));
  const savedOfferingIds = new Set(saved.offerings.filter(row => row.provider_access_id === accessId)
    .map(row => row.offering_id));
  const savedConnectionIds = new Set(saved.connections.filter(row => row.provider_access_id === accessId)
    .map(row => row.connection_id));
  // Include every request name, even for disabled offerings/deployments and
  // ungranted cells. The scope selects cells, not which targets are declared.
  const targets = context.deployments.map(({ row }) => {
    if (!offeringIds.has(row.offering_id) || !savedOfferingIds.has(row.offering_id)
        || !connectionIds.has(row.connection_id) || !savedConnectionIds.has(row.connection_id)
        || !row.wire_model_id.trim()) {
      throw new ProbeSelectionError('provider_probe_invalid_target');
    }
    return { offering_id: row.offering_id, connection_id: row.connection_id, wire_model_id: row.wire_model_id };
  });
  // Document order is UI column order, not lexical credential-id order.
  const credential_ids = context.credentials.map(({ row }) => row.credential_id);
  if (!targets.length || !credential_ids.length) throw new ProbeSelectionError('provider_probe_empty_scope');
  if (targets.length > 1024 || credential_ids.length > 128) {
    throw new ProbeSelectionError('provider_probe_invalid_target');
  }
  const command: ProbeCellsStartRequest = {
    targets, credential_ids, attempts: options.attempts || 3, force: !!options.force && !options.only,
  };
  if (options.only) {
    command.only = {};
    if (options.only.key_idxs) command.only.key_idxs = [...options.only.key_idxs];
    if (options.only.model_ids) command.only.model_ids = [...options.only.model_ids];
  }
  return command;
}

/** Return text only. Callers render it as text/escaped HTML, never raw markup.
 * Use public error detail, never stringify the response, raw cause or draft. */
export function manualMatrixProbeStartError(error: unknown): string {
  const failure = apiFailure(error);
  const kind = failure.problem?.code || failure.envelope?.kind || failure.code;
  let detail: string;
  switch (kind) {
    case 'provider_probe_stale_selection': detail = t('settings.matrixProbeStaleSelection', undefined); break;
    case 'provider_probe_invalid_target': detail = t('settings.matrixProbeInvalidTarget', undefined); break;
    case 'provider_probe_empty_scope': detail = t('settings.matrixProbeEmptyScope', undefined); break;
    default: {
      const publicDetail = failure.problem?.detail || failure.envelope?.detail;
      const message = failure.envelope?.message || failure.message;
      detail = [...new Set([message, publicDetail].filter((value): value is string => !!value))].join(' — ');
    }
  }
  return t('settings.matrixProbeNotStarted', undefined) + ': ' + detail;
}

export const manualMatrixProbe = Object.freeze({
  buildCommand: buildManualMatrixProbeCommand,
  startError: manualMatrixProbeStartError,
});
