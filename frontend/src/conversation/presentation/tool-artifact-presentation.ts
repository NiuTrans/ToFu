/**
 * Saved tool-output continuation copy over immutable projected rounds.
 * Entry points: renderToolArtifactHeaderHtml and renderToolArtifactRequestHtml.
 * Owns bounded argument/origin interpretation, not result parsing or settlement.
 * Dependencies: shared escaping and injected translator; no DOM or state.
 * The existing result disclosure owns technical scope and reader interaction.
 */
import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';

type RecordValue = Readonly<Record<string, unknown>>;
type ArtifactRequest = Readonly<{
  search: boolean;
  items: readonly RecordValue[];
  omitted: number;
  multipleResults: boolean;
  source: string;
  sourceRound: number | null;
  queries: readonly string[];
}>;

export const TOOL_ARTIFACT_PRESENTATION_LIMITS = Object.freeze({
  items: 16, argumentUnits: 32_768, queryUnits: 200, sourceUnits: 512,
  selectorUnits: 1024, referenceUnits: 96, historicalTitleUnits: 8192,
});
const LEGACY_TITLE = /^(Read|Search) compacted result(?: of R(\d+))?(?: · ([\s\S]*))?$/;
const GENERIC_TITLE = /^(?:Read|Search)(?: \d+)? saved tool results?(?:: [\s\S]*)?$/;

function record(value: unknown): RecordValue | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : null;
}
function field(value: RecordValue, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(value, key) ? value[key] : undefined;
}
function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function bounded(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function queryText(raw: unknown): string {
  const value = text(raw);
  const limit = TOOL_ARTIFACT_PRESENTATION_LIMITS.queryUnits;
  let shown = '';
  let count = 0;
  // Tool schemas count Unicode code points. Preserve significant whitespace.
  for (const point of value.slice(0, limit * 2 + 2)) {
    if (count++ === limit) return `${shown}…`;
    shown += point;
  }
  return shown;
}
function argumentsRecord(raw: unknown): RecordValue | null {
  if (typeof raw !== 'string') return record(raw);
  if (raw.length > TOOL_ARTIFACT_PRESENTATION_LIMITS.argumentUnits) return null;
  try { return record(JSON.parse(raw) as unknown); } catch { return null; }
}

/** Machine aliases help inspect requests, but never describe their purpose. */
function referenceLabel(raw: unknown): string {
  const value = text(raw);
  if (value.length > TOOL_ARTIFACT_PRESENTATION_LIMITS.referenceUnits) return '';
  const digest = /^tool-result:([0-9a-f]{64})$/.exec(value);
  if (digest) return `#${digest[1].slice(0, 12)}`;
  const alias = /^tr:([a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?):([0-9a-f]{8,64})$/.exec(value);
  return alias ? `${alias[1]} #${alias[2].slice(0, 12)}` : '';
}

function projectRequest(roundValue: unknown): ArtifactRequest | null {
  const round = record(roundValue);
  if (!round) return null;
  const name = field(round, 'toolName');
  if (name !== 'read_tool_artifact' && name !== 'search_tool_artifact') return null;
  const search = name === 'search_tool_artifact';
  const rawArgs = field(round, 'toolArgs');
  const args = argumentsRecord(rawArgs);

  if (rawArgs !== undefined && rawArgs !== null && !args) return null;
  const origin = record(field(round, '_artifactOrigin'));
  const title = text(field(round, 'query'));
  const legacy = title.length <= TOOL_ARTIFACT_PRESENTATION_LIMITS.historicalTitleUnits
    ? LEGACY_TITLE.exec(title) : null;
  // Sparse historical titles remain unchanged unless they match a known producer.
  if (!args && !origin && !legacy && (rawArgs !== undefined && rawArgs !== null
    || title.length > TOOL_ARTIFACT_PRESENTATION_LIMITS.historicalTitleUnits
    || !GENERIC_TITLE.test(title))) return null;
  const batch = args ? field(args, search ? 'searches' : 'reads') : undefined;
  if (batch !== undefined && (!Array.isArray(batch) || !batch.length)) return null;
  const candidates: readonly unknown[] = Array.isArray(batch) ? batch : args ? [args] : [];
  const items = candidates.slice(0, TOOL_ARTIFACT_PRESENTATION_LIMITS.items).map(record);
  if (items.some(item => !item)) return null;
  const validItems = items as RecordValue[];
  if (args && (!validItems.length || validItems.some(item => {
    const ref = text(field(item, 'artifact_ref'));
    return !ref || ref.length > TOOL_ARTIFACT_PRESENTATION_LIMITS.referenceUnits
      || (search && !queryText(field(item, 'query')));
  }))) return null;
  const refs = new Set(validItems.map(item => text(field(item, 'artifact_ref'))));
  // One round's provenance must not be attached to unrelated batch results.
  const originQueries = origin ? field(origin, 'queries') : undefined;
  const unknownBatch = !args && (Array.isArray(originQueries) && originQueries.length > 1
    || /^(?:Read|Search) (?:[2-9]|\d{2,}) saved tool results/.test(title.slice(0, 100)));
  const multipleResults = refs.size > 1 || candidates.length > items.length;
  const canShowOrigin = !multipleResults && !unknownBatch;
  const rawSource = origin ? text(field(origin, 'source')) : legacy?.[3] || '';
  const boundedSource = bounded(rawSource, TOOL_ARTIFACT_PRESENTATION_LIMITS.sourceUnits);
  const source = canShowOrigin && !/tool-result:|tr:|#[0-9a-f]{8,}/i.test(boundedSource)
    ? boundedSource : '';
  const rawRound = origin ? field(origin, 'sourceRound') : legacy?.[2] ? Number(legacy[2]) : null;
  const sourceRound = canShowOrigin && typeof rawRound === 'number'
    && Number.isSafeInteger(rawRound) && rawRound > 0 ? rawRound : null;
  const rawQueries: readonly unknown[] = search
    ? validItems.length ? validItems.map(item => field(item, 'query'))
      : Array.isArray(originQueries) && originQueries.length ? originQueries
        : origin ? [field(origin, 'query')] : []
    : [];
  // Slice before scanning; do not walk arbitrarily large historical arrays.
  const queries = rawQueries.slice(0, TOOL_ARTIFACT_PRESENTATION_LIMITS.items)
    .map(queryText).filter(Boolean);
  return {
    search, items: validItems, multipleResults, source, sourceRound, queries,
    omitted: Math.max(0, candidates.length - items.length,
      rawQueries.length - TOOL_ARTIFACT_PRESENTATION_LIMITS.items),
  };
}

/** Action first; source once; patterns explicitly labeled, not error badges. */
export function renderToolArtifactHeaderHtml(round: unknown, translate: Translator): string {
  const request = projectRequest(round);
  if (!request) return '';
  const title = request.search
    ? translate(request.multipleResults ? 'toolArtifact.searchPlural' : 'toolArtifact.search')
    : translate(request.multipleResults ? 'toolArtifact.readPlural' : 'toolArtifact.read');
  const source = request.source
    ? `<span class="ptool-artifact-source">${escapeHtmlText(translate('toolArtifact.source', { source: request.source }))}</span>`
    : '';
  const queries = request.queries.length
    ? `<span class="ptool-artifact-queries"><span class="ptool-artifact-label">${escapeHtmlText(translate('toolArtifact.queries'))}</span>${request.queries
      .map(query => `<code class="ptool-artifact-query">${escapeHtmlText(query)}</code>`).join('')}</span>` : '';
  const omitted = request.omitted
    ? `<span class="ptool-artifact-omitted">${escapeHtmlText(translate('toolArtifact.moreItems', { n: request.omitted }))}</span>` : '';
  return `<span class="ptool-artifact-header"><span class="ptool-artifact-title">${escapeHtmlText(title)}</span>${source}${queries}${omitted}</span>`;
}

function cursorLabel(raw: unknown, search: boolean, translate: Translator): string {
  if (raw === undefined || raw === null || raw === '' || raw === '0' || raw === 0) {
    return search ? translate('toolArtifact.fromStart') : translate('toolArtifact.byteOffset', { n: '0' });
  }
  if (search && ((typeof raw === 'string' && /^\d{1,15}$/.test(raw))
      || typeof raw === 'number')) return translate('toolArtifact.numericSearchCursor');
  if (!search && ((typeof raw === 'string' && /^(0|[1-9]\d{0,14})$/.test(raw))
    || (typeof raw === 'number' && Number.isSafeInteger(raw) && raw >= 0))) {
    return translate('toolArtifact.byteOffset', { n: Number(raw).toLocaleString('en-US') });
  }
  // Signed cursors are opaque and private: never print or infer their position.
  return translate(search ? 'toolArtifact.continueSearch' : 'toolArtifact.continueRead');
}

/** Raw result JSON stays in evidence. Only safe error copy reaches the summary. */
export function toolArtifactFailureText(raw: string, translate: Translator): string {
  const fallback = () => translate('toolArtifact.failureDetails');
  const safeLine = (value: string): string => {
    const shown = value.slice(0, 513).trim();
    // A prose diagnostic can quote private retrieval metadata too. Do not guess
    // how to redact arbitrary signed tokens; leave the whole line in evidence.
    if (/tool-result:|\btr:|\btrq\d+\.|nextCursor|resumeCursor|artifactRef|artifact_ref|\bcursor\b/i.test(shown)) return '';
    return bounded(shown, 512);
  };
  const prefix = raw.slice(0, TOOL_ARTIFACT_PRESENTATION_LIMITS.argumentUnits).trim();
  if (prefix.startsWith('{') || prefix.startsWith('[')) {
    if (raw.length > TOOL_ARTIFACT_PRESENTATION_LIMITS.argumentUnits) return fallback();
    let parsed: RecordValue | null;
    try { parsed = record(JSON.parse(raw) as unknown); } catch { return fallback(); }
    if (!parsed) return fallback();
    const items = field(parsed, 'items');
    const failures = Array.isArray(items)
      ? items.slice(0, TOOL_ARTIFACT_PRESENTATION_LIMITS.items).map(record)
        .filter((item): item is RecordValue => item !== null && field(item, 'status') === 'error')
      : [parsed];
    for (const failure of failures) {
      const error = record(field(failure, 'error'));
      if (!error) continue;
      const rawCode = text(field(error, 'code'));
      const code = /^[a-z][a-z0-9_.-]{0,79}$/i.test(rawCode) ? rawCode : '';
      const message = safeLine(text(field(error, 'message')) || text(field(error, 'detail')));
      if (code || message) return bounded([code, message].filter(Boolean).join(': '), 512);
    }
    return fallback();
  }
  const firstLine = prefix.split('\n', 1)[0];
  return safeLine(firstLine) || fallback();
}

/** Requested scope is technical evidence, never claimed returned coverage. */
export function renderToolArtifactRequestHtml(round: unknown, translate: Translator): string {
  const request = projectRequest(round);
  if (!request || (!request.items.length && !request.sourceRound)) return '';
  const origin = request.sourceRound
    ? `<p>${escapeHtmlText(translate('toolArtifact.sourceRound', { n: request.sourceRound }))}</p>` : '';
  const rows = request.items.map(item => {
    const reference = referenceLabel(field(item, 'artifact_ref'));
    const parts = [reference ? translate('toolArtifact.reference', { reference }) : '',
      cursorLabel(field(item, 'cursor'), request.search, translate)];
    const limit = field(item, 'limit');
    const selector = text(field(item, 'selector'));
    if (selector) parts.push(translate('toolArtifact.selector', {
      selector: bounded(selector, TOOL_ARTIFACT_PRESENTATION_LIMITS.selectorUnits),
    }));
    if (request.search) {
      parts.push(queryText(field(item, 'query')));
      if (typeof limit === 'number' && Number.isInteger(limit) && limit > 0 && limit <= 20) {
        parts.push(translate('toolArtifact.matchLimit', { n: limit }));
      }
    } else {
      if (typeof limit === 'number' && Number.isInteger(limit) && limit > 0 && limit <= 65536) {
        parts.push(translate('toolArtifact.byteLimit', { n: limit.toLocaleString('en-US') }));
      }
    }
    return `<li>${escapeHtmlText(parts.filter(Boolean).join(' · '))}</li>`;
  }).join('');
  const omitted = request.omitted
    ? `<p>${escapeHtmlText(translate('toolArtifact.moreItems', { n: request.omitted }))}</p>` : '';
  return `<div class="ptool-artifact-request"><span class="ptool-artifact-label">${escapeHtmlText(translate('toolArtifact.requestDetails'))}</span>${origin}${rows ? `<ol>${rows}</ol>` : ''}${omitted}</div>`;
}
