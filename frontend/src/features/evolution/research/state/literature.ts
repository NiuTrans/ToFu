/** Bounded literature projection shared by live events, snapshots and saved runs. */
import type { JsonObject, ResearchStream } from './stream';

export type LiteratureSort = 'year-asc' | 'year-desc' | 'citations-asc' | 'citations-desc';
export const paperIdentity = (paper: JsonObject): string => String(paper.paperId || paper.arxivId || paper.doi || paper.semanticScholarId || '');

export function knownNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function sortedPapers(papers: JsonObject[], sort: LiteratureSort): JsonObject[] {
  const field = sort.startsWith('year') ? 'year' : 'citationCount';
  const direction = sort.endsWith('asc') ? 1 : -1;
  return [...papers].sort((a, b) => {
    const left = knownNumber(a[field]);
    const right = knownNumber(b[field]);
    if (left === null) return right === null ? 0 : 1;
    if (right === null) return -1;
    return direction * (left - right);
  });
}

export function mergeLiterature(stream: ResearchStream, payload: JsonObject): void {
  if (Array.isArray(payload.papers)) {
    const papers = stream.papers || (stream.papers = []);
    for (const raw of payload.papers.slice(0, 200)) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
      const paper = raw as JsonObject;
      if (!paperIdentity(paper)) continue;
      const previous = papers.find(row => ['paperId', 'arxivId', 'doi', 'semanticScholarId']
        .some(key => Boolean(paper[key] && row[key] === paper[key])));
      if (previous) {
        // A parser's sparse receipt is not authority to erase bibliometrics or
        // replace the discovery identity referenced by citation endpoints.
        const canonical = Array.isArray(payload.citation_edges) && paper.paperId ? paper.paperId : previous.paperId;
        const status = previous.status;
        Object.assign(previous, Object.fromEntries(Object.entries(paper)
          .filter(([, value]) => value !== null && value !== undefined && value !== '')));
        if (canonical) previous.paperId = canonical;
        if (status && status !== 'discovered' && status !== 'metadata_only'
            && (paper.status === 'discovered' || paper.status === 'metadata_only')) previous.status = status;
      } else if (papers.length < 200) papers.push({ ...paper });
    }
  }
  if (Array.isArray(payload.citation_edges)) {
    // Mirrors records.MAX_CITATION_EDGES: the graph surface renders up to
    // four validated cites edges per paper at the 200-paper ceiling.
    stream.citationEdges = payload.citation_edges.slice(0, 800).filter((raw): raw is JsonObject =>
      Boolean(raw && typeof raw === 'object' && raw.relation === 'cites'
        && typeof raw.from === 'string' && typeof raw.to === 'string' && raw.from !== raw.to));
  }
  if (Array.isArray(payload.source_statuses)) {
    stream.sourceStatuses = payload.source_statuses.slice(0, 64)
      .filter((raw): raw is JsonObject => Boolean(raw && typeof raw === 'object' && !Array.isArray(raw)));
  }
  if (Array.isArray(payload.survey_batch_failures)) {
    stream.surveyBatchFailures = payload.survey_batch_failures.slice(0, 200)
      .filter((raw): raw is JsonObject => Boolean(raw && typeof raw === 'object' && !Array.isArray(raw)));
  }
  if (typeof payload.error === 'string') stream.error = payload.error.slice(0, 2000);
  if (typeof payload.failed_stage === 'string') stream.failedStage = payload.failed_stage.slice(0, 80);
  if (payload.type === 'stage_failed' && typeof payload.stage === 'string') stream.failedStage = payload.stage.slice(0, 80);
}
