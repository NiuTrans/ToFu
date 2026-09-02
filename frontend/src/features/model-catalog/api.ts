import { request } from '../../api/transport';
import type {
  ModelCatalogEnvelope,
  ModelCatalogPayload,
  ModelCatalogPutPayload,
} from './types';

export const MODEL_CATALOG_PATH = '/api/v1/model-catalog';

/** Pure CAS payload builder kept exported so the contract is testable. */
export function buildPutPayload(
  expectedRevision: number,
  catalog: ModelCatalogPayload,
): ModelCatalogPutPayload {
  return { expected_revision: expectedRevision, catalog };
}

export function fetchModelCatalog(): Promise<ModelCatalogEnvelope> {
  return request<ModelCatalogEnvelope>(MODEL_CATALOG_PATH);
}

export function putModelCatalog(
  expectedRevision: number,
  catalog: ModelCatalogPayload,
): Promise<ModelCatalogEnvelope> {
  return request<ModelCatalogEnvelope>(MODEL_CATALOG_PATH, {
    method: 'PUT',
    json: buildPutPayload(expectedRevision, catalog),
  });
}

/** A 409 means the caller's revision lost the compare-and-swap race. */
export function isModelCatalogConflict(error: unknown): boolean {
  const candidate = error as { status?: unknown } | null;
  return candidate?.status === 409;
}
