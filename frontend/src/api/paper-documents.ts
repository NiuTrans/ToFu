/** Owned TeX discovery/selection transport. Neither command starts compilation. */
import { request } from './transport';
import type { PaperDocumentV1, PaperDocumentDiscoveryRequestV1, PaperDocumentSelectionRequestV1,
  PaperDocumentDiscoveryResultV1, PaperDocumentOpenRequestV1, PaperDocumentOpenResultV1,
  PaperArxivImportRequestV1, PaperArxivImportResultV1,
  PaperDocumentSourceRequestV1, PaperDocumentSourceV1,
  PaperReadingStateRequestV1, PaperReadingStateResultV1 } from './paper-build.generated';
import type { PaperResearchReadingOpenRequestV1, PaperResearchReadingOpenResultV1 } from './paper-build.generated';
import { PAPER_BUILD_LIMITS as limits } from './paper-build.generated';

export type PaperDocumentDiscovery = PaperDocumentDiscoveryResultV1;
export function paperDocumentKey(value: PaperDocumentV1 | null | undefined): string {
  if (!value) return '';
  return JSON.stringify(value.source === 'local'
    ? [value.source, value.project_root, value.path, value.main_file]
    : [value.source, value.mcp_server, value.project_id, value.main_file]);
}
export function paperDocumentSelection(convId: string, value: PaperDocumentV1): PaperDocumentSelectionRequestV1 {
  return value.source === 'local'
    ? { conv_id: convId, path: value.path, project_root: value.project_root, main_file: value.main_file }
    : { conv_id: convId, source: value.source, project_id: value.project_id,
      mcp_server: value.mcp_server, main_file: value.main_file };
}
/** In-browser source edit (Track B). `base_sha256` fences lost-update races;
 * the server answers a 409-style problem with code 'source_conflict' when the
 * bytes drifted since the client last read them. Wire types come from the
 * generated paper-build contract; aliases keep call sites readable. */
export type { PaperDocumentSourceEditRequestV1, PaperProposalRequestV1 } from './paper-build.generated';
import type { PaperDocumentSourceEditRequestV1, PaperDocumentSourceEditV1,
  PaperProposalRequestV1, PaperProposalV1 } from './paper-build.generated';
export type PaperDocumentSourceEditResultV1 = PaperDocumentSourceEditV1;
export type { PaperProposalV1 } from './paper-build.generated';
export type PaperProposalEditV1 = PaperProposalV1['edits'][number];
export type PaperProposalStatusV1 = PaperProposalV1['status'];
export type PaperProposalEditStatusV1 = PaperProposalEditV1['status'];
/** Accept decisions report a per-hunk outcome list alongside the proposal. */
export type PaperProposalDecisionV1 = PaperProposalV1 & {
  results?: Array<{ index: number; ok: boolean; error?: string }>;
};

export interface PaperDocumentsApi {
  discover(body: PaperDocumentDiscoveryRequestV1, signal: AbortSignal): Promise<PaperDocumentDiscovery>;
  select(body: PaperDocumentSelectionRequestV1): Promise<{ selected: PaperDocumentV1 }>;
  importArxiv(body: PaperArxivImportRequestV1, signal: AbortSignal): Promise<PaperArxivImportResultV1>;
  readSource(body: PaperDocumentSourceRequestV1, signal: AbortSignal): Promise<PaperDocumentSourceV1>;
  editSource(body: PaperDocumentSourceEditRequestV1, signal: AbortSignal): Promise<PaperDocumentSourceEditResultV1>;
  getProposal(body: PaperProposalRequestV1, signal: AbortSignal): Promise<PaperProposalV1>;
  acceptProposal(body: PaperProposalRequestV1, signal: AbortSignal): Promise<PaperProposalDecisionV1>;
  rejectProposal(body: PaperProposalRequestV1, signal: AbortSignal): Promise<PaperProposalV1>;
  open(body: PaperDocumentOpenRequestV1, signal: AbortSignal): Promise<PaperDocumentOpenResultV1>;
  openResearch(body: PaperResearchReadingOpenRequestV1, signal: AbortSignal): Promise<PaperResearchReadingOpenResultV1>;
  saveReading(documentId: string, body: PaperReadingStateRequestV1): Promise<PaperReadingStateResultV1>;
}
export const paperDocumentsApi: PaperDocumentsApi = {
  discover: (json, signal) => request('/api/v4/paper/documents/discover', {
    method: 'POST', json, signal, convId: json.conv_id }),
  importArxiv: (json, signal) => request('/api/v4/paper/documents/arxiv/import', {
    method: 'POST', json, signal, convId: json.conv_id, timeout: 180000 }),
  readSource: (json, signal) => request('/api/v4/paper/documents/source', {
    method: 'POST', json, signal, timeout: 15000 }),
  editSource: (json, signal) => request('/api/v4/paper/documents/source/edit', {
    method: 'POST', json, signal, timeout: 30000 }),
  getProposal: (json, signal) => request('/api/v4/paper/proposals/get', {
    method: 'POST', json, signal, timeout: 15000 }),
  acceptProposal: (json, signal) => request('/api/v4/paper/proposals/accept', {
    method: 'POST', json, signal, timeout: 60000 }),
  rejectProposal: (json, signal) => request('/api/v4/paper/proposals/reject', {
    method: 'POST', json, signal, timeout: 15000 }),
  select: (json) => request('/api/v4/paper/documents/select', {
    method: 'POST', json, convId: json.conv_id }),
  open: (json, signal) => request('/api/v4/paper/documents/open', {
    method: 'POST', json, signal, convId: json.conv_id, timeout: limits.reading_save_timeout_ms }),
  openResearch: (json, signal) => request('/api/v4/paper/documents/research/open', {
    method: 'POST', json, signal, timeout: limits.reading_save_timeout_ms }),
  saveReading: (documentId, json) => request(`/api/v4/paper/documents/${encodeURIComponent(documentId)}/reading`, {
    method: 'POST', json, keepalive: true, timeout: limits.reading_save_timeout_ms }),
};
