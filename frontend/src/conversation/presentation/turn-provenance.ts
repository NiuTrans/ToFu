/**
 * Pure HTML presentation for the provenance attached to one projected Turn.
 *
 * Responsibility: render memory-prefetch, My Context, related-conversation,
 * learned-preference, and MCP-login facts from a Conversation Sync provenance
 * block. Entry points are returned by `createTurnProvenancePresentation`.
 * Dependencies: the generated translator, trusted shared-icon renderer, and
 * the shared HTML escaping policy. This module reads no DOM or browser state.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';

type UnknownRecord = Readonly<Record<string, unknown>>;
type IconHtml = (name: string, size?: number | string) => string;
type ProvenanceState = 'running' | 'done' | 'skipped' | 'failed';

type ProvenanceSegment = Readonly<{
  state?: ProvenanceState;
  segmentHtml: string;
  detailHtml: string;
}>;

export type TurnProvenancePresentation = Readonly<{
  inlineMarkdown(text: unknown): string;
  renderMcpLoginHintHtml(loginHint: unknown): string;
  renderTurnProvenanceHtml(provenance: unknown): string;
  renderPreferenceLearnedHtml(learned: unknown): string;
}>;

export type TurnProvenancePresentationDependencies = Readonly<{
  translate: Translator;
  iconHtml: IconHtml;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function records(value: unknown): readonly UnknownRecord[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is UnknownRecord => (
      item !== null && typeof item === 'object' && !Array.isArray(item)
    ));
}

function stringField(value: unknown, field: string): string {
  const candidate = record(value)[field];
  return typeof candidate === 'string' ? candidate : '';
}

function numberField(value: unknown, field: string): number {
  const candidate = record(value)[field];
  return typeof candidate === 'number' && Number.isFinite(candidate)
    ? candidate
    : 0;
}

/** Serialize one dynamic action argument without creating executable syntax. */
function actionStringArgument(value: string): string {
  const serialized = JSON.stringify(value)
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
  return escapeHtmlText(serialized);
}

function renderInlineMarkdown(text: unknown): string {
  return String(text ?? '').split(/(`[^`]+`)/g).map((part) => {
    if (part.length >= 2 && part.startsWith('`') && part.endsWith('`')) {
      return `<code>${escapeHtmlText(part.slice(1, -1))}</code>`;
    }
    return escapeHtmlText(part)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  }).join('');
}

export function createTurnProvenancePresentation(
  dependencies: TurnProvenancePresentationDependencies,
): TurnProvenancePresentation {
  const { translate, iconHtml } = dependencies;

  function memoryPrefetchSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const memoryPrefetch = record(value);
    const phase = stringField(memoryPrefetch, 'phase') || 'started';
    const selected = numberField(memoryPrefetch, 'selected');
    const candidates = numberField(memoryPrefetch, 'candidates');
    const totalMs = numberField(memoryPrefetch, 'totalMs');

    let icon = iconHtml('brain', 13);
    let state: ProvenanceState = 'running';
    let headline = '';
    let subline = '';
    if (phase === 'started') {
      const totalMemories = numberField(memoryPrefetch, 'totalMemories');
      headline = translate('memPrefetch.surfacing');
      subline = (totalMemories
        ? `${translate('memPrefetch.totalN', { n: totalMemories })} · `
        : '') + translate('memPrefetch.localLabel');
    } else if (phase === 'done') {
      state = 'done';
      if (selected === 0) {
        headline = translate('memPrefetch.none');
        subline = `${translate('memPrefetch.candidatesN', { n: candidates })} · ${
          translate('memPrefetch.localLabel')
        }`;
      } else {
        headline = translate(
          selected === 1 ? 'memPrefetch.prefetched' : 'memPrefetch.prefetchedN',
          { n: selected },
        );
        const parts = [];
        if (candidates) {
          parts.push(translate('memPrefetch.candidatesN', { n: candidates }));
        }
        parts.push(translate('memPrefetch.localLabel'));
        if (totalMs) {
          parts.push(`${translate('memPrefetch.totalLabel')} ${totalMs}ms`);
        }
        subline = parts.join(' · ');
      }
    } else if (phase === 'skipped') {
      state = 'skipped';
      headline = translate('memPrefetch.skipped');
      subline = stringField(memoryPrefetch, 'reason');
    } else if (phase === 'failed') {
      state = 'failed';
      icon = iconHtml('alertTriangle', 13);
      headline = translate('memPrefetch.failed');
      subline = stringField(memoryPrefetch, 'reason');
    } else {
      headline = translate('memPrefetch.generic');
      subline = phase;
    }

    let segmentLabel: string;
    if (state === 'running') {
      segmentLabel = translate('memPrefetch.tag');
    } else if (phase === 'done' && selected > 0) {
      segmentLabel = translate(
        selected === 1 ? 'memPrefetch.tagN' : 'memPrefetch.tagNs',
        { n: selected },
      );
    } else if (phase === 'done') {
      segmentLabel = translate('memPrefetch.tagNone');
    } else if (state === 'skipped') {
      segmentLabel = translate('memPrefetch.tagSkipped');
    } else if (state === 'failed') {
      segmentLabel = translate('memPrefetch.tagFailed');
    } else {
      segmentLabel = translate('memPrefetch.tag');
    }

    const segmentHtml = `<span class="tp-seg tp-seg-mem tp-${state}">${icon}`
      + `<span class="tp-label">${escapeHtmlText(segmentLabel)}</span>`
      + (state === 'running'
        ? '<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>'
        : '')
      + '</span>';

    let memoryList = '';
    const memories = records(memoryPrefetch.memories);
    if (phase === 'done' && selected > 0 && memories.length > 0) {
      const items = memories.map((memory) => {
        const name = escapeHtmlText(stringField(memory, 'name') || '?');
        const scope = escapeHtmlText(stringField(memory, 'scope'));
        const description = stringField(memory, 'description');
        return `<li><span class="mp-mem-name">${name}</span>`
          + (scope ? ` <span class="mp-mem-scope">${scope}</span>` : '')
          + (description
            ? `<div class="mp-mem-desc">${renderInlineMarkdown(description)}</div>`
            : '')
          + '</li>';
      }).join('');
      memoryList = `<ul class="mp-mem-list">${items}</ul>`;
    }
    const detailHtml = `<div class="tp-detail-row tp-detail-mem mp-${state}">`
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(headline)}</span>`
      + (subline ? `<span class="mp-sub">${escapeHtmlText(subline)}</span>` : '')
      + memoryList
      + '</div></div>';
    return { state, segmentHtml, detailHtml };
  }

  function preferencesAppliedSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const preferencesApplied = record(value);
    const items = Array.isArray(preferencesApplied.items)
      ? preferencesApplied.items
      : [];
    const count = items.length;
    const headline = count > 0
      ? translate('prefs.appliedN', { n: count })
      : translate('prefs.applied');
    const segmentLabel = count > 0
      ? translate(count === 1 ? 'prefs.tagN' : 'prefs.tagNs', { n: count })
      : translate('prefs.tagNone');
    const segmentHtml = '<span class="tp-seg tp-seg-prefs tp-done">'
      + iconHtml('sliders', 13)
      + `<span class="tp-label">${escapeHtmlText(segmentLabel)}</span></span>`;
    const preferenceList = count > 0
      ? `<ul class="mp-mem-list pa-list">${items.map((item) => (
        `<li>${renderInlineMarkdown(item)}</li>`
      )).join('')}</ul>`
      : '';
    const detailHtml = '<div class="tp-detail-row tp-detail-prefs pa-chip">'
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(headline)}</span>`
      + (numberField(preferencesApplied, 'chars')
        ? `<span class="mp-sub">${escapeHtmlText(translate('prefs.fromProfile'))}</span>`
        : '')
      + preferenceList
      + '</div></div>';
    return { state: 'done', segmentHtml, detailHtml };
  }

  function relatedConversationsSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const relatedConversations = record(value);
    const items = records(relatedConversations.items);
    const count = items.length || numberField(relatedConversations, 'count');
    if (count <= 0) return null;
    const label = translate(
      count === 1 ? 'relatedConvs.tagN' : 'relatedConvs.tagNs',
      { n: count },
    );
    const segmentHtml = '<span class="tp-seg tp-seg-convs tp-done">'
      + iconHtml('messageSquare', 13)
      + `<span class="tp-label">${escapeHtmlText(label)}</span></span>`;
    const rows = items.map((item) => {
      const conversationId = stringField(item, 'id');
      const title = escapeHtmlText(stringField(item, 'title') || '(untitled)');
      const summary = escapeHtmlText(stringField(item, 'summary'));
      const titleHtml = conversationId
        ? '<a class="rc-conv-link" href="#" '
          + 'data-tofu-action="event.stopPropagation();try{loadConversation('
          + `${actionStringArgument(conversationId)})}catch(e){};return false;">${title}</a>`
        : `<span class="rc-conv-title">${title}</span>`;
      return `<li>${titleHtml}`
        + (summary ? `<div class="rc-conv-summary">${summary}</div>` : '')
        + '</li>';
    }).join('');
    const conversationList = rows
      ? `<ul class="mp-mem-list rc-list">${rows}</ul>`
      : '';
    const detailHtml = '<div class="tp-detail-row tp-detail-convs mp-done">'
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(label)}</span>`
      + `<span class="mp-sub">${escapeHtmlText(translate('relatedConvs.sub'))}</span>`
      + conversationList
      + '</div></div>';
    return { state: 'done', segmentHtml, detailHtml };
  }

  function mcpToolsDeltaSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const delta = record(value);
    const namesOf = (field: string): readonly string[] => {
      const raw = delta[field];
      return Array.isArray(raw)
        ? raw.filter((name): name is string => typeof name === 'string' && !!name)
        : [];
    };
    const added = namesOf('added');
    const removed = namesOf('removed');
    if (!added.length && !removed.length) return null;
    const headlineParts: string[] = [];
    if (added.length) {
      headlineParts.push(added.length === 1
        ? translate('mcpDelta.added')
        : translate('mcpDelta.addedN', { n: added.length }));
    }
    if (removed.length) {
      headlineParts.push(removed.length === 1
        ? translate('mcpDelta.removed')
        : translate('mcpDelta.removedN', { n: removed.length }));
    }
    const segmentHtml = '<span class="tp-seg tp-seg-mcp tp-done">'
      + iconHtml('wrench', 13)
      + `<span class="tp-label">${escapeHtmlText(translate('mcpDelta.tag'))}</span></span>`;
    const rows = [
      ...added.map((name) => (
        `<li><span class="mp-mem-name">${escapeHtmlText(name)}</span>`
        + ` <span class="mp-mem-scope">${escapeHtmlText(translate('mcpDelta.addedTag'))}</span></li>`
      )),
      ...removed.map((name) => (
        `<li><span class="mp-mem-name">${escapeHtmlText(name)}</span>`
        + ` <span class="mp-mem-scope">${escapeHtmlText(translate('mcpDelta.removedTag'))}</span></li>`
      )),
    ].join('');
    const detailHtml = '<div class="tp-detail-row tp-detail-mcp mp-done">'
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(headlineParts.join(' · '))}</span>`
      + `<span class="mp-sub">${escapeHtmlText(translate('mcpDelta.sub'))}</span>`
      + `<ul class="mp-mem-list">${rows}</ul>`
      + '</div></div>';
    return { state: 'done', segmentHtml, detailHtml };
  }

  function projectPathChangeSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const change = record(value);
    const from = stringField(change, 'from');
    const to = stringField(change, 'to');
    if (!from && !to) return null;
    const segmentHtml = '<span class="tp-seg tp-seg-path tp-done">'
      + iconHtml('folder', 13)
      + `<span class="tp-label">${escapeHtmlText(translate('pathChange.tag'))}</span></span>`;
    const detailHtml = '<div class="tp-detail-row tp-detail-path mp-done">'
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(translate('pathChange.headline'))}</span>`
      + `<span class="mp-sub">${escapeHtmlText(from || '(none)')} → ${escapeHtmlText(to || '(none)')}</span>`
      + '</div></div>';
    return { state: 'done', segmentHtml, detailHtml };
  }
  function mcpLoginSegment(value: unknown): ProvenanceSegment | null {
    if (!value) return null;
    const loginHint = record(value);
    const phase = stringField(loginHint, 'phase') || 'awaiting_approval';
    if (phase === 'awaiting_approval') return null;
    const username = stringField(loginHint, 'username');

    let icon: string;
    let state: ProvenanceState;
    let headline: string;
    if (phase === 'approved') {
      icon = iconHtml('check', 13);
      state = 'done';
      headline = username
        ? `${translate('login.approved')} · ${username}`
        : translate('login.approved');
    } else if (phase === 'denied') {
      icon = iconHtml('ban', 13);
      state = 'failed';
      headline = translate('login.denied');
    } else if (phase === 'timeout') {
      icon = iconHtml('alarm', 13);
      state = 'failed';
      headline = translate('login.timeout');
    } else {
      icon = iconHtml('check', 13);
      state = 'done';
      headline = translate('login.finished');
    }

    let snippet = '';
    const rawSnippet = stringField(loginHint, 'snippet');
    if (rawSnippet && (phase === 'denied' || phase === 'timeout')) {
      let text = rawSnippet.trim();
      try {
        const withoutFence = text
          .replace(/^```(?:json)?\s*/, '')
          .replace(/\s*```$/, '');
        text = JSON.stringify(JSON.parse(withoutFence), null, 2);
      } catch {
        // A non-JSON diagnostic remains readable as escaped plain text.
      }
      snippet = `<pre class="mp-snippet">${escapeHtmlText(text)}</pre>`;
    }
    const segmentHtml = `<span class="tp-seg tp-seg-login tp-${state}">${icon}`
      + `<span class="tp-label">${escapeHtmlText(headline)}</span></span>`;
    const detailHtml = `<div class="tp-detail-row tp-detail-login mp-${state}">`
      + `<div class="mp-text"><span class="mp-headline">${escapeHtmlText(headline)}</span>`
      + snippet
      + '</div></div>';
    return { state, segmentHtml, detailHtml };
  }

  function preferencesLearnedSegment(value: unknown): ProvenanceSegment | null {
    const informational = records(value).filter((preference) => !preference.pending);
    if (informational.length === 0) return null;
    const count = informational.length;
    const segmentLabel = translate(
      count === 1 ? 'prefs.learnedTagN' : 'prefs.learnedTagNs',
      { n: count },
    );
    const segmentHtml = '<span class="tp-seg tp-seg-prefs-learned tp-done">'
      + iconHtml('check', 13)
      + `<span class="tp-label">${escapeHtmlText(segmentLabel)}</span></span>`;
    const rows = informational.map((preference) => {
      const lead = preference.kind === 'added'
        ? translate('prefs.added')
        : translate('prefs.learnedReinforced');
      const changeId = stringField(preference, 'change_id')
        || stringField(preference, 'id');
      const undo = changeId
        ? '<button class="pl-seg-undo" '
          + 'data-tofu-action="event.stopPropagation();undoContextChange(this,'
          + `${actionStringArgument(changeId)})">${escapeHtmlText(translate('prefs.undo'))}</button>`
        : '';
      return `<li><span class="pl-seg-lead">${escapeHtmlText(lead)}</span> `
        + `${renderInlineMarkdown(stringField(preference, 'summary'))}${undo}</li>`;
    }).join('');
    const detailHtml = '<div class="tp-detail-row tp-detail-prefs-learned mp-done">'
      + `<div class="mp-text"><span class="mp-headline">${
        escapeHtmlText(translate('prefs.learnedHeadline'))
      }</span>`
      + `<span class="mp-sub">${escapeHtmlText(translate('prefs.editInSettings'))}</span>`
      + `<ul class="mp-mem-list pa-list">${rows}</ul>`
      + '</div></div>';
    return { state: 'done', segmentHtml, detailHtml };
  }

  function renderMcpLoginHintHtml(value: unknown): string {
    if (!value) return '';
    const loginHint = record(value);
    const phase = stringField(loginHint, 'phase') || 'awaiting_approval';
    if (phase !== 'awaiting_approval') return '';
    const username = stringField(loginHint, 'username');
    const headline = username
      ? `${translate('login.awaiting')} · ${username}`
      : translate('login.awaiting');
    return '<div class="mem-prefetch-chip mp-running mp-login-hint">'
      + `<span class="mp-icon">${iconHtml('smartphone', 14)}</span>`
      + `<span class="mp-text"><span class="mp-headline">${escapeHtmlText(headline)}</span>`
      + `<span class="mp-sub">${escapeHtmlText(translate('login.awaitingSub'))}</span>`
      + '</span>'
      + '<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>'
      + '</div>';
  }

  function renderTurnProvenanceHtml(value: unknown): string {
    if (!value) return '';
    const provenance = record(value);
    const segments = [
      mcpLoginSegment(provenance.mcpLoginHint),
      mcpToolsDeltaSegment(provenance.mcpToolsDelta),
      projectPathChangeSegment(provenance.projectPathChange),
      memoryPrefetchSegment(provenance.memoryPrefetch),
      preferencesAppliedSegment(provenance.preferencesApplied),
      preferencesLearnedSegment(provenance.preferencesLearned),
      relatedConversationsSegment(provenance.relatedConversations),
    ].filter((segment): segment is ProvenanceSegment => segment !== null);
    if (segments.length === 0) return '';
    const running = segments.some((segment) => segment.state === 'running');
    const failed = segments.some((segment) => segment.state === 'failed');
    const stripState = failed ? 'tp-has-failed' : running ? 'tp-running' : 'tp-done';
    return `<div class="turn-prov ${stripState} tp-expandable" `
      + 'data-tofu-action="this.classList.toggle(\'tp-expanded\')">'
      + `<span class="tp-segs">${segments.map((segment) => segment.segmentHtml).join('')}</span>`
      + `<span class="tp-chevron">${iconHtml('chevronDown', 12)}</span>`
      + `<div class="tp-details">${segments.map((segment) => segment.detailHtml).join('')}</div>`
      + '</div>';
  }

  function renderPreferenceLearnedHtml(value: unknown): string {
    const pending = records(value).filter((preference) => Boolean(preference.pending));
    if (pending.length === 0) return '';
    const rows = pending.map((preference) => {
      const summary = escapeHtmlText(stringField(preference, 'summary'));
      const preferenceId = stringField(preference, 'id');
      const actionId = actionStringArgument(preferenceId);
      return `<div class="pl-row pl-pending" data-pref-id="${escapeHtmlText(preferenceId)}">`
        + `<span class="pl-lead">${iconHtml('lightbulb', 13)}</span>`
        + `<span class="pl-text">${escapeHtmlText(translate('prefs.learned'))} <b>${summary}</b>`
        + `<span class="pl-hint">${escapeHtmlText(translate('prefs.pendingHint'))}</span></span>`
        + '<span class="pl-actions">'
        + '<button class="pl-btn pl-confirm" data-tofu-action="resolvePreference(this,'
        + `${actionId},true)">${escapeHtmlText(translate('prefs.confirm'))}</button>`
        + '<button class="pl-btn pl-dismiss" data-tofu-action="resolvePreference(this,'
        + `${actionId},false)">${escapeHtmlText(translate('prefs.dismiss'))}</button>`
        + '</span></div>';
    }).join('');
    return `<div class="pref-learned-box">${rows}</div>`;
  }

  return Object.freeze({
    inlineMarkdown: renderInlineMarkdown,
    renderMcpLoginHintHtml,
    renderTurnProvenanceHtml,
    renderPreferenceLearnedHtml,
  });
}
