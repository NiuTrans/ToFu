/**
 * Pure presentation policy for tool-catalog, web, fetch, and vertical search.
 *
 * Responsibility: render bounded search-result HTML from projected tool-round
 * values without mutating those projections. Entry point:
 * `createToolSearchPresentation`. Dependencies: generated i18n, shared HTML
 * escaping, pure tool-family predicates, and the trusted shared-icon port.
 * The caller supplies already-rendered trusted row-header slots; this owner
 * reads no DOM, browser global, cache, or mutable runtime state.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import {
  isFetchToolRound,
  isSearchToolRound,
  isToolSearchRound,
} from './tool-round-presentation';

type UnknownRecord = Readonly<Record<string, unknown>>;
type MutableRecord = Record<string, unknown>;
type IconHtml = (
  name: string,
  size?: number | string,
  style?: string,
) => string;

export const TOOL_SEARCH_PRESENTATION_LIMITS = Object.freeze({
  toolCatalogRecordsScanned: 512,
  toolCatalogCards: 64,
  toolArgumentRows: 8,
  webResultRows: 100,
  verticalRecords: 64,
  verticalSourcesScanned: 256,
  verticalItemsScanned: 512,
  verticalItemsPerCard: 12,
  engineCount: 32,
  engineUrls: 512,
});

export type ToolSearchHeaderHtml = Readonly<{
  iconHtml: string;
  queryHtml: string;
  rightControlsHtml: string;
}>;

export type ToolSearchPresentation = Readonly<{
  renderSearchHtml(
    round: unknown,
    projectedResults: unknown,
    header: ToolSearchHeaderHtml,
  ): string;
}>;

export type ToolSearchPresentationDependencies = Readonly<{
  translate: Translator;
  iconHtml: IconHtml;
}>;

type VerticalAccumulator = {
  domain: string;
  query: string;
  sources: UnknownRecord[];
  items: MutableRecord[];
  seenSources: Set<string>;
  seenItems: Map<string, MutableRecord>;
};

type VerticalCollection = Readonly<{
  records: UnknownRecord[];
  omitted: boolean;
}>;

type VerticalMerge = Readonly<{
  verticals: VerticalAccumulator[];
  omitted: boolean;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function stringField(value: unknown, field: string): string {
  const candidate = record(value)[field];
  return typeof candidate === 'string' ? candidate : '';
}

function arrayLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function boundedRecordArray(value: unknown, limit: number): UnknownRecord[] {
  return Array.isArray(value) ? value.slice(0, limit).map(record) : [];
}

function displayNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function rankNumber(value: unknown): number {
  if (value === null || value === undefined || value === '') return -1;
  return Number(value) || -1;
}

function safeHttpUrl(value: unknown): string {
  const url = typeof value === 'string' ? value : '';
  return /^https?:\/\//i.test(url) ? url : '';
}

function cssToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9_-]/g, '-');
}

function verticalItemKey(item: UnknownRecord, fallbackIndex: number): string {
  const stable = stringField(item, 'url')
    || stringField(item, 'arxiv_id')
    || stringField(item, 'title');
  if (stable) return stable;
  try {
    return JSON.stringify(item) || `vertical-item-${fallbackIndex}`;
  } catch {
    return `vertical-item-${fallbackIndex}`;
  }
}

function renderVerticalDomainIcon(domain: string): string {
  const normalized = domain.toLowerCase();
  if (normalized === 'academic') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.5 3 3 6 3s6-1.5 6-3v-5"/></svg>';
  }
  if (normalized === 'code') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>';
  }
  if (normalized === 'finance') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 17 9 11 13 15 21 7"/><polyline points="14 7 21 7 21 14"/></svg>';
  }
  if (normalized === 'security') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-4z"/></svg>';
  }
  if (normalized === 'network') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/></svg>';
  }
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><circle cx="11" cy="11" r="3"/></svg>';
}

function renderVerticalCardHtml(
  vertical: VerticalAccumulator,
  iconHtml: IconHtml,
): string {
  if (!vertical.items.length) return '';
  const sourceLabels = vertical.sources.map((source) => (
    stringField(source, 'source') || stringField(source, 'type')
  )).filter(Boolean);
  const sourceLabel = sourceLabels.join(' · ');
  const query = vertical.query;
  const queryLabel = query
    ? ` · ${escapeHtmlText(query.slice(0, 60))}`
    : '';
  const visibleItems = vertical.items.slice(
    0,
    TOOL_SEARCH_PRESENTATION_LIMITS.verticalItemsPerCard,
  );
  const rowsHtml = visibleItems.map((item) => {
    const title = stringField(item, 'title') || '(untitled)';
    const url = safeHttpUrl(item.url);
    const titleHtml = url
      ? `<a href="${escapeHtmlText(url)}" target="_blank" rel="noopener">${
        escapeHtmlText(title)
      }</a>`
      : `<span>${escapeHtmlText(title)}</span>`;
    const metadataHtml: string[] = [];
    if (item.upvotes !== null && item.upvotes !== undefined && item.upvotes !== '') {
      metadataHtml.push(
        '<span class="vertical-meta-pill" title="Upvotes">'
        + `<span class="vertical-meta-icon">${
          iconHtml('arrowRight', 12, 'transform:rotate(-90deg)')
        }</span>${escapeHtmlText(item.upvotes)}</span>`,
      );
    }
    if (
      item.citations !== null
      && item.citations !== undefined
      && item.citations !== ''
    ) {
      metadataHtml.push(
        '<span class="vertical-meta-pill" title="Citations">'
        + `<span class="vertical-meta-icon">${iconHtml('refreshCw', 12)}</span>${
          escapeHtmlText(Number(item.citations).toLocaleString())
        }</span>`,
      );
    }
    if (item.year) {
      metadataHtml.push(
        `<span class="vertical-meta-pill">${escapeHtmlText(item.year)}</span>`,
      );
    }
    const arxivId = stringField(item, 'arxiv_id');
    if (arxivId) {
      metadataHtml.push(
        `<span class="vertical-meta-pill">arXiv:${
          escapeHtmlText(arxivId)
        }</span>`,
      );
    }
    const itemSource = stringField(item, 'source');
    if (itemSource && !sourceLabel.includes(itemSource)) {
      metadataHtml.push(
        `<span class="vertical-meta-pill">${
          escapeHtmlText(itemSource)
        }</span>`,
      );
    }
    const metadataBlockHtml = metadataHtml.length
      ? `<div class="vertical-row-meta">${metadataHtml.join('')}</div>`
      : '';
    const snippet = stringField(item, 'snippet');
    const snippetHtml = snippet
      ? `<div class="vertical-row-snippet">${escapeHtmlText(snippet)}</div>`
      : '';
    return `<div class="vertical-row">
       <div class="vertical-row-title">${titleHtml}</div>
       ${metadataBlockHtml}
       ${snippetHtml}
     </div>`;
  }).join('');
  const hiddenItems = Math.max(
    0,
    vertical.items.length
      - TOOL_SEARCH_PRESENTATION_LIMITS.verticalItemsPerCard,
  );
  const moreLabelHtml = hiddenItems > 0
    ? `<div class="vertical-card-more">… +${hiddenItems} more</div>`
    : '';
  const title = vertical.domain.charAt(0).toUpperCase()
    + vertical.domain.slice(1);
  return `<div class="vertical-card vertical-domain-${
    cssToken(vertical.domain)
  }">
       <div class="vertical-card-header">
         <span class="vertical-card-icon">${
           renderVerticalDomainIcon(vertical.domain)
         }</span>
         <span class="vertical-card-title">${escapeHtmlText(title)} sources</span>
         ${sourceLabel
           ? `<span class="vertical-card-sources">${
             escapeHtmlText(sourceLabel)
           }${queryLabel}</span>`
           : ''}
         <span class="vertical-card-count">${vertical.items.length}</span>
       </div>
       <div class="vertical-card-body">${rowsHtml}${moreLabelHtml}</div>
     </div>`;
}

export function createToolSearchPresentation(
  dependencies: ToolSearchPresentationDependencies,
): ToolSearchPresentation {
  const { translate, iconHtml } = dependencies;

  function renderToolCatalogHtml(
    round: UnknownRecord,
    results: readonly UnknownRecord[],
    projectedResultCount: number,
    header: ToolSearchHeaderHtml,
  ): string {
    if (!isToolSearchRound(round) || stringField(round, 'status') !== 'done') {
      return '';
    }
    const matches = results.filter(
      (item) => stringField(item, 'type') === 'tool_catalog_match',
    );
    const visibleMatches = matches.slice(
      0,
      TOOL_SEARCH_PRESENTATION_LIMITS.toolCatalogCards,
    );
    const totalCandidate = Number(round.toolSearchTotal);
    const total = Number.isFinite(totalCandidate)
      ? totalCandidate
      : matches.length;
    const foundText = translate('toolSearch.found', {
      total,
      shown: visibleMatches.length,
    });
    const moreTextHtml = round.toolSearchNextCursor
      ? `<span class="ptool-tool-search-more">${
        escapeHtmlText(translate('toolSearch.more'))
      }</span>`
      : '';
    const failOpenTextHtml = round.toolSearchFailOpen
      ? `<span class="ptool-badge ptool-badge-warn">${
        escapeHtmlText(translate('toolSearch.failOpen'))
      }</span>`
      : '';

    let cardsHtml = '';
    if (matches.length) {
      cardsHtml = visibleMatches.map((item) => {
        const argumentCount = arrayLength(item.arguments);
        const args = boundedRecordArray(
          item.arguments,
          TOOL_SEARCH_PRESENTATION_LIMITS.toolArgumentRows,
        );
        const visibleArgsHtml = args.map((argument) => {
          const required = Boolean(argument.required);
          const requiredMarkHtml = required
            ? '<span class="ptool-tool-arg-required">*</span>'
            : '';
          return `<span class="ptool-tool-arg${
            required ? ' is-required' : ''
          }"><code>${
            escapeHtmlText(stringField(argument, 'name') || 'arg')
          }</code>${requiredMarkHtml}<span>${
            escapeHtmlText(stringField(argument, 'type') || 'value')
          }</span></span>`;
        }).join('');
        const argumentOverflowHtml = argumentCount > args.length
          ? `<span class="ptool-tool-arg ptool-tool-arg-more">+${
            argumentCount - args.length
          }</span>`
          : '';
        const argumentsHtml = visibleArgsHtml || argumentOverflowHtml
          ? `<div class="ptool-tool-search-args">${
            visibleArgsHtml
          }${argumentOverflowHtml}</div>`
          : '';
        const snippet = stringField(item, 'snippet');
        const descriptionHtml = snippet
          ? `<div class="ptool-tool-search-desc">${
            escapeHtmlText(snippet)
          }</div>`
          : '';
        return `<div class="ptool-tool-search-card">
        <div class="ptool-tool-search-card-head">
          <code class="ptool-tool-search-name">${escapeHtmlText(
            stringField(item, 'toolName')
              || stringField(item, 'title')
              || 'tool',
          )}</code>
          <span class="ptool-tool-search-namespace">${escapeHtmlText(
            stringField(item, 'namespace') || 'general',
          )}</span>
        </div>
        ${descriptionHtml}${argumentsHtml}
      </div>`;
      }).join('');
    } else {
      cardsHtml = `<div class="ptool-tool-search-empty">${
        escapeHtmlText(translate('toolSearch.none'))
      }</div>`;
    }
    if (
      matches.length > visibleMatches.length
      || projectedResultCount > results.length
    ) {
      const boundedTotal = Number.isFinite(totalCandidate)
        ? total
        : Math.max(matches.length, projectedResultCount);
      cardsHtml += `<div class="ptool-tool-search-limit">${
        escapeHtmlText(translate('toolSearch.catalogLimit', {
          shown: visibleMatches.length,
          total: boundedTotal,
        }))
      }</div>`;
    }

    return `<div class="ptool-tool-search-block">
    <div class="ptool-line ptool-tool-search-header">
      <span class="ptool-icon">${header.iconHtml}</span>
      <span class="ptool-text">${header.queryHtml}</span>
      <span class="ptool-tool-search-count">${escapeHtmlText(foundText)}</span>
      ${failOpenTextHtml}${moreTextHtml}${header.rightControlsHtml}
    </div>
    <div class="ptool-tool-search-results">${cardsHtml}</div>
  </div>`;
  }

  function renderZeroResultHtml(
    round: UnknownRecord,
    header: ToolSearchHeaderHtml,
  ): string {
    const diagnostic = record(round.searchDiag);
    const reason = stringField(diagnostic, 'reason');
    let badgeText = 'no results';
    let badgeClassName = 'ptool-badge-warn';
    let detailHtml = '';
    if (round.searchDiag) {
      if (reason === 'network_error') {
        badgeText = 'network error';
        badgeClassName = 'ptool-badge-err';
        detailHtml = '<div class="ptool-search-diag">All search engines failed — server may have limited internet access.</div>';
      } else if (reason === 'partial_network_error') {
        const failedEngines = Object.keys(record(diagnostic.engine_errors))
          .join(', ') || 'some engines';
        badgeText = 'partial failure';
        badgeClassName = 'ptool-badge-warn';
        detailHtml = `<div class="ptool-search-diag">Network errors from ${
          escapeHtmlText(failedEngines)
        }; other engines returned no matches.</div>`;
      } else if (reason === 'exception') {
        badgeText = 'error';
        badgeClassName = 'ptool-badge-err';
        detailHtml = '<div class="ptool-search-diag">Search encountered an internal error.</div>';
      } else {
        badgeText = 'no matches';
        badgeClassName = 'ptool-badge-warn';
        detailHtml = '<div class="ptool-search-diag">All engines responded but found no matching results. Try different keywords.</div>';
      }
    }
    const cacheSource = stringField(round, 'cacheSource');
    const sourceBadgeHtml = cacheSource === 'prefetch' || cacheSource === 'cache'
      ? `<span class="ptool-badge" style="opacity:.55" title="Served from a ${
        cacheSource === 'prefetch' ? 'streaming prefetch' : 'dedup cache'
      } hit — the search ran earlier, not just now">${cacheSource}</span>`
      : '';
    return `<div class="ptool-line${detailHtml ? ' ptool-line-with-diag' : ''}">
         <span class="ptool-icon">${header.iconHtml}</span>
         <span class="ptool-text">${header.queryHtml}</span>
         <span class="ptool-badge ${badgeClassName}">${badgeText}</span>
         ${sourceBadgeHtml}
         ${header.rightControlsHtml}
         ${detailHtml}
       </div>`;
  }

  function renderResultItemHtml(result: UnknownRecord): string {
    let fetchedBadgeHtml = '';
    if (result.irrelevant) {
      fetchedBadgeHtml = '<span class="search-result-fetched" style="color:var(--text-muted);opacity:.6">irrelevant</span>';
    } else if (result.fetched) {
      const fetchedChars = displayNumber(result.fetchedChars);
      const fetchedText = fetchedChars
        ? `${fetchedChars > 1000
          ? `${Math.round(fetchedChars / 1000)}k`
          : fetchedChars} chars`
        : 'fetched';
      fetchedBadgeHtml = `<span class="search-result-fetched${
        stringField(result, 'source') === 'PDF' ? ' pdf' : ''
      }">${fetchedText}</span>`;
    }
    const rawUrl = stringField(result, 'url');
    const url = safeHttpUrl(rawUrl);
    const title = stringField(result, 'title');
    const titleHtml = url
      ? `<a href="${escapeHtmlText(url)}" target="_blank" rel="noopener">${
        escapeHtmlText(title)
      }</a>`
      : `<span>${escapeHtmlText(title)}</span>`;
    const snippet = stringField(result, 'snippet');
    return `<div class="search-result-item"><div class="search-result-title">${
      titleHtml
    }<span class="search-result-source">${
      escapeHtmlText(stringField(result, 'source'))
    }</span>${fetchedBadgeHtml}</div>${
      snippet
        ? `<div class="search-result-snippet">${escapeHtmlText(snippet)}</div>`
        : ''
    }${rawUrl
      ? `<div class="search-result-url">${escapeHtmlText(rawUrl)}</div>`
      : ''}</div>`;
  }

  function collectVerticalRecords(round: UnknownRecord): VerticalCollection {
    const collected: UnknownRecord[] = [];
    const seen = new Set<object>();
    let omitted = false;
    const visit = (value: unknown, depth: number): void => {
      if (depth > 8) {
        omitted = true;
        return;
      }
      if (collected.length >= TOOL_SEARCH_PRESENTATION_LIMITS.verticalRecords) {
        omitted = true;
        return;
      }
      const candidate = record(value);
      if (candidate === EMPTY_RECORD || seen.has(candidate)) return;
      seen.add(candidate);
      if (Array.isArray(candidate.batch)) {
        for (let index = 0; index < candidate.batch.length; index += 1) {
          if (
            collected.length
              >= TOOL_SEARCH_PRESENTATION_LIMITS.verticalRecords
          ) {
            omitted = true;
            break;
          }
          visit(candidate.batch[index], depth + 1);
        }
        return;
      }
      collected.push(candidate);
    };
    if (round.vertical) visit(round.vertical, 0);
    if (Array.isArray(round.verticals)) {
      for (let index = 0; index < round.verticals.length; index += 1) {
        if (
          collected.length >= TOOL_SEARCH_PRESENTATION_LIMITS.verticalRecords
        ) {
          omitted = true;
          break;
        }
        visit(round.verticals[index], 0);
      }
    }
    return { records: collected, omitted };
  }

  function mergeVerticalRecords(
    verticals: readonly UnknownRecord[],
  ): VerticalMerge {
    const byDomain = new Map<string, VerticalAccumulator>();
    let remainingSourceBudget = TOOL_SEARCH_PRESENTATION_LIMITS
      .verticalSourcesScanned;
    let remainingItemBudget = TOOL_SEARCH_PRESENTATION_LIMITS.verticalItemsScanned;
    let omitted = false;
    for (const vertical of verticals) {
      const domain = stringField(vertical, 'domain') || 'vertical';
      let accumulator = byDomain.get(domain);
      if (!accumulator) {
        accumulator = {
          domain,
          query: stringField(vertical, 'query'),
          sources: [],
          items: [],
          seenSources: new Set(),
          seenItems: new Map(),
        };
        byDomain.set(domain, accumulator);
      } else if (!accumulator.query) {
        accumulator.query = stringField(vertical, 'query');
      }
      const sources = Array.isArray(vertical.sources) ? vertical.sources : [];
      for (let index = 0; index < sources.length; index += 1) {
        if (remainingSourceBudget <= 0) {
          omitted = true;
          break;
        }
        remainingSourceBudget -= 1;
        const source = record(sources[index]);
        const key = `${stringField(source, 'source') || stringField(source, 'type')}|${
          stringField(source, 'identifier')
        }`;
        if (!accumulator.seenSources.has(key)) {
          accumulator.seenSources.add(key);
          accumulator.sources.push(source);
        }
      }
      const items = Array.isArray(vertical.items) ? vertical.items : [];
      for (let index = 0; index < items.length; index += 1) {
        if (remainingItemBudget <= 0) {
          omitted = true;
          break;
        }
        remainingItemBudget -= 1;
        const item = record(items[index]);
        const key = verticalItemKey(item, accumulator.items.length);
        const previous = accumulator.seenItems.get(key);
        if (!previous) {
          const clone = { ...item };
          accumulator.seenItems.set(key, clone);
          accumulator.items.push(clone);
        } else {
          if (rankNumber(item.upvotes) > rankNumber(previous.upvotes)) {
            previous.upvotes = item.upvotes;
          }
          if (rankNumber(item.citations) > rankNumber(previous.citations)) {
            previous.citations = item.citations;
          }
        }
      }
    }
    const merged = [...byDomain.values()];
    for (const vertical of merged) {
      vertical.items.sort((left, right) => (
        rankNumber(right.upvotes) - rankNumber(left.upvotes)
        || rankNumber(right.citations) - rankNumber(left.citations)
      ));
    }
    return { verticals: merged, omitted };
  }

  function renderEngineBreakdownHtml(
    round: UnknownRecord,
    finalResultCount: number,
    headerIconHtml: string,
  ): string {
    const breakdown = record(round.engineBreakdown);
    if (breakdown === EMPTY_RECORD) return '';
    const allEngines = Object.keys(breakdown);
    if (!allEngines.length) return '';
    const totalRaw = allEngines.reduce((total, engine) => (
      total + (Array.isArray(breakdown[engine]) ? breakdown[engine].length : 0)
    ), 0);
    const visibleEngines = allEngines.slice(
      0,
      TOOL_SEARCH_PRESENTATION_LIMITS.engineCount,
    );
    let remainingUrlBudget = TOOL_SEARCH_PRESENTATION_LIMITS.engineUrls;
    const enginesHtml = visibleEngines.map((engine) => {
      const urlCount = arrayLength(breakdown[engine]);
      const visibleUrls = boundedRecordArray(
        breakdown[engine],
        remainingUrlBudget,
      );
      remainingUrlBudget -= visibleUrls.length;
      const urlsHtml = visibleUrls.map((urlRecord) => {
        const rawUrl = stringField(urlRecord, 'url');
        const url = safeHttpUrl(rawUrl);
        const title = stringField(urlRecord, 'title') || rawUrl;
        const titleHtml = url
          ? `<a href="${escapeHtmlText(url)}" target="_blank" rel="noopener">${
            escapeHtmlText(title)
          }</a>`
          : `<span>${escapeHtmlText(title)}</span>`;
        return `<div class="eb-url-item">${titleHtml}<div class="eb-url-text">${
          escapeHtmlText(rawUrl)
        }</div></div>`;
      }).join('');
      return `<div class="eb-engine"><div class="eb-engine-name">${
        escapeHtmlText(engine)
      } <span class="eb-engine-count">(${urlCount})</span></div><div class="eb-engine-urls">${
        urlsHtml
      }</div></div>`;
    }).join('');
    const omitted = allEngines.length > visibleEngines.length
      || totalRaw > TOOL_SEARCH_PRESENTATION_LIMITS.engineUrls;
    const limitHtml = omitted
      ? `<div class="eb-limit">${
        escapeHtmlText(translate('toolSearch.engineLimit'))
      }</div>`
      : '';
    return `<div class="eb-section">
          <button type="button" class="eb-toggle" aria-expanded="false" data-tofu-action="event.stopPropagation();this.parentElement.classList.toggle('eb-expanded');this.setAttribute('aria-expanded',String(this.parentElement.classList.contains('eb-expanded')))"><span class="eb-icon">${
            headerIconHtml
          }</span>Engine Sources <span class="eb-total">${totalRaw} raw → ${
            finalResultCount
          } final</span><span class="eb-arrow">${
            iconHtml('chevronDown', 16, 'transform:rotate(-90deg)')
          }</span></button>
          <div class="eb-content">${enginesHtml}${limitHtml}</div>
        </div>`;
  }

  function renderWebSearchHtml(
    round: UnknownRecord,
    results: readonly UnknownRecord[],
    projectedResultCount: number,
    header: ToolSearchHeaderHtml,
  ): string {
    if (!isSearchToolRound(round) && !isFetchToolRound(round)) return '';
    if (!results.length) return renderZeroResultHtml(round, header);

    const visibleResults = results.slice(
      0,
      TOOL_SEARCH_PRESENTATION_LIMITS.webResultRows,
    );
    const queryOrder: string[] = [];
    const byQuery = new Map<string, UnknownRecord[]>();
    for (const result of visibleResults) {
      const key = stringField(result, '_q');
      if (!byQuery.has(key)) {
        byQuery.set(key, []);
        queryOrder.push(key);
      }
      byQuery.get(key)?.push(result);
    }
    const multipleQueries = queryOrder.filter(Boolean).length > 1;
    const resultItemsHtml = multipleQueries
      ? queryOrder.map((key) => {
        const group = byQuery.get(key) || [];
        const groupItemsHtml = group.map(renderResultItemHtml).join('');
        const groupHeaderHtml = key
          ? `<div class="search-query-group-header"><span class="search-query-group-icon">${
            iconHtml('search', 13)
          }</span><span class="search-query-group-q">${
            escapeHtmlText(key)
          }</span><span class="search-query-group-count">${
            group.length
          }</span></div>`
          : '';
        return `<div class="search-query-group">${
          groupHeaderHtml
        }${groupItemsHtml}</div>`;
      }).join('')
      : visibleResults.map(renderResultItemHtml).join('');
    const resultLimitHtml = projectedResultCount > visibleResults.length
      ? `<div class="ptool-search-result-limit">${
        escapeHtmlText(translate('toolSearch.resultLimit', {
          shown: visibleResults.length,
          total: projectedResultCount,
        }))
      }</div>`
      : '';

    const verticalCollection = collectVerticalRecords(round);
    const verticalMerge = mergeVerticalRecords(verticalCollection.records);
    const verticalHtml = verticalMerge.verticals
      .map((vertical) => renderVerticalCardHtml(vertical, iconHtml))
      .join('');
    const verticalLimitHtml = verticalCollection.omitted || verticalMerge.omitted
      ? `<div class="vertical-result-limit">${
        escapeHtmlText(translate('toolSearch.verticalLimit'))
      }</div>`
      : '';
    const engineBreakdownHtml = renderEngineBreakdownHtml(
      round,
      projectedResultCount,
      header.iconHtml,
    );
    const verticalBadgeHtml = verticalCollection.records.length
      ? (() => {
        const domains = [...new Set(verticalCollection.records.map(
          (vertical) => stringField(vertical, 'domain'),
        ).filter(Boolean))];
        return `<span class="ptool-badge vertical-badge" title="Vertical domain data">vertical: ${
          escapeHtmlText(domains.join(' · ') || 'auto')
        }</span>`;
      })()
      : '';
    return `<div class="ptool-results-block" data-rn="${
      escapeHtmlText(round.roundNum)
    }">
         <div class="ptool-line ptool-results-header" role="button" tabindex="0" aria-expanded="false" data-tofu-action="if(event.target.closest('.ri-tool-anchor'))return;event.stopPropagation();this.parentElement.classList.toggle('expanded');this.setAttribute('aria-expanded',String(this.parentElement.classList.contains('expanded')))">
           <span class="ptool-icon">${header.iconHtml}</span>
           <span class="ptool-text">${header.queryHtml}</span>
           ${verticalBadgeHtml}
           <span class="ptool-badge ptool-badge-info">${projectedResultCount} result${
             projectedResultCount !== 1 ? 's' : ''
           }</span>
           ${header.rightControlsHtml}
           <span class="ptool-results-toggle">${iconHtml('chevronDown', 16)}</span>
         </div>
         <div class="ptool-results-content">${
           verticalHtml
         }${verticalLimitHtml}${resultItemsHtml}${resultLimitHtml}${engineBreakdownHtml}</div>
       </div>`;
  }

  function renderSearchHtml(
    roundValue: unknown,
    projectedResults: unknown,
    header: ToolSearchHeaderHtml,
  ): string {
    const round = record(roundValue);
    const projectedResultCount = arrayLength(projectedResults);
    const scanLimit = isToolSearchRound(round)
      ? TOOL_SEARCH_PRESENTATION_LIMITS.toolCatalogRecordsScanned
      : TOOL_SEARCH_PRESENTATION_LIMITS.webResultRows;
    const results = boundedRecordArray(projectedResults, scanLimit);
    return renderToolCatalogHtml(round, results, projectedResultCount, header)
      || renderWebSearchHtml(round, results, projectedResultCount, header);
  }

  return Object.freeze({ renderSearchHtml });
}
