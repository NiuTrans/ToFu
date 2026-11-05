/**
 * Pure HTML presentation for the provenance attached to one projected Turn.
 *
 * Responsibility: render memory-prefetch, My Context, related-conversation,
 * learned-preference, and MCP-login facts from a Conversation Sync provenance
 * block. Each field becomes one structured fact; context disclosures render
 * it as a receipt row or fold it into the note it witnesses. Entry points are
 * returned by `createTurnProvenancePresentation`.
 * Dependencies: the generated translator, trusted shared-icon renderer, and
 * the shared HTML escaping policy. This module reads no DOM or browser state.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';

type UnknownRecord = Readonly<Record<string, unknown>>;
type IconHtml = (name: string, size?: number | string) => string;
type ProvenanceState = 'running' | 'done' | 'skipped' | 'failed';

/** One acquisition fact. Text fields are plain (callers escape them);
 * `bodyHtml` is already-escaped list or snippet markup. */
export type ContextProvenanceFact = Readonly<{
  field: string;
  state: ProvenanceState;
  glyph: string;
  /** Short tag, e.g. "2 memories". */
  label: string;
  /** Listed items (memories, entries, tools); 0 when not a count. */
  count: number;
  /** Sentence form, e.g. "Prefetched 2 memories". */
  headline: string;
  /** Secondary facts: candidates, timing, a reason or a path move. */
  detail: string;
  /** One-line glimpse of the listed items. */
  peek: string;
  bodyHtml: string;
}>;

export type TurnProvenancePresentation = Readonly<{
  inlineMarkdown(text: unknown): string;
  renderMcpLoginHintHtml(loginHint: unknown): string;
  renderTurnProvenanceHtml(provenance: unknown): string;
  contextProvenanceFacts(provenance: unknown): readonly ContextProvenanceFact[];
  renderContextProvenanceHtml(provenance: unknown): string;
  renderPreferenceLearnedHtml(learned: unknown): string;
}>;

export type TurnProvenancePresentationDependencies = Readonly<{
  translate: Translator;
  iconHtml: IconHtml;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

/* Legacy class hooks per field, kept on receipt rows for styling and tests. */
const FIELD_CLASS: Readonly<Record<string, string>> = {
  memoryPrefetch: 'tp-seg-mem',
  preferencesApplied: 'tp-seg-prefs',
  preferencesLearned: 'tp-seg-prefs-learned',
  relatedConversations: 'tp-seg-convs',
  mcpToolsDelta: 'tp-seg-mcp',
  projectPathChange: 'tp-seg-path',
  mcpLoginHint: 'tp-seg-login',
};

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

/** Plain glimpse of inline-Markdown items: markers dropped, never parsed. */
function plainGlimpse(items: readonly string[]): string {
  return items.map((item) => item.replace(/[*`]/g, '').trim()).filter(Boolean).join(' · ');
}

export function createTurnProvenancePresentation(
  dependencies: TurnProvenancePresentationDependencies,
): TurnProvenancePresentation {
  const { translate, iconHtml } = dependencies;

  function memoryPrefetchFact(value: unknown): ContextProvenanceFact | null {
    if (!value) return null;
    const memoryPrefetch = record(value);
    const phase = stringField(memoryPrefetch, 'phase') || 'started';
    const selected = numberField(memoryPrefetch, 'selected');
    const candidates = numberField(memoryPrefetch, 'candidates');
    const totalMs = numberField(memoryPrefetch, 'totalMs');

    let glyph = 'brain';
    let state: ProvenanceState = 'running';
    let headline = '';
    let detail = '';
    if (phase === 'started') {
      const totalMemories = numberField(memoryPrefetch, 'totalMemories');
      headline = translate('memPrefetch.surfacing');
      detail = (totalMemories
        ? `${translate('memPrefetch.totalN', { n: totalMemories })} · `
        : '') + translate('memPrefetch.localLabel');
    } else if (phase === 'done') {
      state = 'done';
      if (selected === 0) {
        headline = translate('memPrefetch.none');
        detail = `${translate('memPrefetch.candidatesN', { n: candidates })} · ${
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
        detail = parts.join(' · ');
      }
    } else if (phase === 'skipped') {
      state = 'skipped';
      headline = translate('memPrefetch.skipped');
      detail = stringField(memoryPrefetch, 'reason');
    } else if (phase === 'failed') {
      state = 'failed';
      glyph = 'alertTriangle';
      headline = translate('memPrefetch.failed');
      detail = stringField(memoryPrefetch, 'reason');
    } else {
      headline = translate('memPrefetch.generic');
      detail = phase;
    }

    let label: string;
    if (state === 'running') {
      label = translate('memPrefetch.tag');
    } else if (phase === 'done' && selected > 0) {
      label = translate(
        selected === 1 ? 'memPrefetch.tagN' : 'memPrefetch.tagNs',
        { n: selected },
      );
    } else if (phase === 'done') {
      label = translate('memPrefetch.tagNone');
    } else if (state === 'skipped') {
      label = translate('memPrefetch.tagSkipped');
    } else if (state === 'failed') {
      label = translate('memPrefetch.tagFailed');
    } else {
      label = translate('memPrefetch.tag');
    }

    const memories = phase === 'done' && selected > 0
      ? records(memoryPrefetch.memories) : [];
    const bodyHtml = memories.length ? `<ul class="mp-mem-list">${memories.map((memory) => {
      const name = escapeHtmlText(stringField(memory, 'name') || '?');
      const scope = escapeHtmlText(stringField(memory, 'scope'));
      const description = stringField(memory, 'description');
      return `<li><span class="mp-mem-name">${name}</span>`
        + (scope ? ` <span class="mp-mem-scope">${scope}</span>` : '')
        + (description
          ? `<div class="mp-mem-desc">${renderInlineMarkdown(description)}</div>`
          : '')
        + '</li>';
    }).join('')}</ul>` : '';
    return {
      field: 'memoryPrefetch', state, glyph, label, headline, detail, bodyHtml,
      count: phase === 'done' ? selected : 0,
      peek: memories.map((memory) => stringField(memory, 'name')).filter(Boolean).join(' · '),
    };
  }

  function preferencesAppliedFact(value: unknown): ContextProvenanceFact | null {
    if (!value) return null;
    const preferencesApplied = record(value);
    const items = Array.isArray(preferencesApplied.items)
      ? preferencesApplied.items.map((item) => String(item ?? ''))
      : [];
    const count = items.length;
    return {
      field: 'preferencesApplied',
      state: 'done',
      glyph: 'sliders',
      label: count > 0
        ? translate(count === 1 ? 'prefs.tagN' : 'prefs.tagNs', { n: count })
        : translate('prefs.tagNone'),
      count,
      headline: count > 0
        ? translate('prefs.appliedN', { n: count })
        : translate('prefs.applied'),
      detail: numberField(preferencesApplied, 'chars') ? translate('prefs.fromProfile') : '',
      peek: plainGlimpse(items),
      bodyHtml: count > 0
        ? `<ul class="mp-mem-list pa-list">${items.map((item) => (
          `<li>${renderInlineMarkdown(item)}</li>`
        )).join('')}</ul>`
        : '',
    };
  }

  function relatedConversationsFact(value: unknown): ContextProvenanceFact | null {
    if (!value) return null;
    const relatedConversations = record(value);
    const items = records(relatedConversations.items);
    const count = items.length || numberField(relatedConversations, 'count');
    if (count <= 0) return null;
    const label = translate(
      count === 1 ? 'relatedConvs.tagN' : 'relatedConvs.tagNs',
      { n: count },
    );
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
    return {
      field: 'relatedConversations',
      state: 'done',
      glyph: 'messageSquare',
      label,
      count,
      headline: label,
      detail: translate('relatedConvs.sub'),
      peek: items.map((item) => stringField(item, 'title')).filter(Boolean).join(' · '),
      bodyHtml: rows ? `<ul class="mp-mem-list rc-list">${rows}</ul>` : '',
    };
  }

  function mcpToolsDeltaFact(value: unknown): ContextProvenanceFact | null {
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
    return {
      field: 'mcpToolsDelta',
      state: 'done',
      glyph: 'plug',
      label: translate('mcpDelta.tag'),
      count: added.length + removed.length,
      headline: headlineParts.join(' · '),
      detail: translate('mcpDelta.sub'),
      peek: [...added, ...removed].join(' · '),
      bodyHtml: `<ul class="mp-mem-list">${rows}</ul>`,
    };
  }

  function projectPathChangeFact(value: unknown): ContextProvenanceFact | null {
    if (!value) return null;
    const change = record(value);
    const from = stringField(change, 'from');
    const to = stringField(change, 'to');
    if (!from && !to) return null;
    const move = `${from || '(none)'} → ${to || '(none)'}`;
    return {
      field: 'projectPathChange',
      state: 'done',
      glyph: 'folder',
      label: translate('pathChange.tag'),
      count: 0,
      headline: translate('pathChange.headline'),
      detail: move,
      peek: move,
      bodyHtml: '',
    };
  }

  function mcpLoginFact(value: unknown): ContextProvenanceFact | null {
    if (!value) return null;
    const loginHint = record(value);
    const phase = stringField(loginHint, 'phase') || 'awaiting_approval';
    if (phase === 'awaiting_approval') return null;
    const username = stringField(loginHint, 'username');

    let glyph: string;
    let state: ProvenanceState;
    let headline: string;
    if (phase === 'approved') {
      glyph = 'check';
      state = 'done';
      headline = username
        ? `${translate('login.approved')} · ${username}`
        : translate('login.approved');
    } else if (phase === 'denied') {
      glyph = 'ban';
      state = 'failed';
      headline = translate('login.denied');
    } else if (phase === 'timeout') {
      glyph = 'alarm';
      state = 'failed';
      headline = translate('login.timeout');
    } else {
      glyph = 'check';
      state = 'done';
      headline = translate('login.finished');
    }

    let bodyHtml = '';
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
      bodyHtml = `<pre class="mp-snippet">${escapeHtmlText(text)}</pre>`;
    }
    return {
      field: 'mcpLoginHint', state, glyph, label: headline, headline,
      count: 0, detail: '', peek: '', bodyHtml,
    };
  }

  function preferencesLearnedFact(value: unknown): ContextProvenanceFact | null {
    const informational = records(value).filter((preference) => !preference.pending);
    if (informational.length === 0) return null;
    const count = informational.length;
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
    return {
      field: 'preferencesLearned',
      state: 'done',
      glyph: 'check',
      label: translate(
        count === 1 ? 'prefs.learnedTagN' : 'prefs.learnedTagNs',
        { n: count },
      ),
      count,
      headline: translate('prefs.learnedHeadline'),
      detail: translate('prefs.editInSettings'),
      peek: plainGlimpse(informational.map((preference) => stringField(preference, 'summary'))),
      bodyHtml: `<ul class="mp-mem-list pa-list">${rows}</ul>`,
    };
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

  function contextProvenanceFacts(value: unknown): readonly ContextProvenanceFact[] {
    const provenance = record(value);
    const builders = {
      mcpLoginHint: mcpLoginFact,
      mcpToolsDelta: mcpToolsDeltaFact,
      projectPathChange: projectPathChangeFact,
      memoryPrefetch: memoryPrefetchFact,
      preferencesApplied: preferencesAppliedFact,
      preferencesLearned: preferencesLearnedFact,
      relatedConversations: relatedConversationsFact,
    };
    return Object.entries(builders).flatMap(([field, build]) => {
      const fact = build(provenance[field]);
      return fact ? [fact] : [];
    });
  }

  /* A receipt is metadata about acquisition, not injected text: it never
   * opens on its own, and its list is visible as soon as the context fold is. */
  function receiptHtml(fact: ContextProvenanceFact): string {
    return `<div class="sw-note-provenance-item sw-note-receipt ${FIELD_CLASS[fact.field] ?? ''} tp-${fact.state}"`
      + ` data-provenance-field="${fact.field}" data-state="${fact.state}">`
      + '<div class="sw-note-item-head">'
      + `<span class="sw-note-sticker" data-glyph="${fact.glyph}" aria-hidden="true">${iconHtml(fact.glyph, 14)}</span>`
      + `<span class="sw-note-item-title">${escapeHtmlText(fact.headline)}</span>`
      + (fact.detail ? `<span class="sw-note-item-meta">${escapeHtmlText(fact.detail)}</span>` : '')
      + '</div>'
      + (fact.bodyHtml ? `<div class="sw-note-item-body">${fact.bodyHtml}</div>` : '')
      + '</div>';
  }

  function renderContextProvenanceHtml(value: unknown): string {
    return contextProvenanceFacts(value).map(receiptHtml).join('');
  }

  /** Provenance-only legacy turns still use the SAME context disclosure,
   * not a competing turn-level strip. Status is metadata, never proof that
   * failed/skipped acquisition was injected into the model. */
  function renderTurnProvenanceHtml(value: unknown): string {
    const facts = contextProvenanceFacts(value);
    if (!facts.length) return '';
    const state = facts.some((fact) => fact.state === 'failed')
      ? 'tp-has-failed' : facts.some((fact) => fact.state === 'running')
        ? 'tp-running' : 'tp-done';
    return `<details class="sw-inbox-row sw-system-note-row sw-note-group ${state}" data-context-provenance="true">`
      + '<summary class="ptool-line sw-inbox-row-header">'
      + `<span class="ptool-icon">${iconHtml('layers', 13)}</span>`
      + `<span class="ptool-text">${escapeHtmlText(translate('systemNote.groupLabel'))}</span>`
      + `<span class="sw-note-group-names">${escapeHtmlText(facts.map((fact) => fact.label).join(' · '))}</span>`
      + `<span class="sw-note-group-count">${escapeHtmlText(translate('systemNote.groupItemsTitle', { n: facts.length }))}</span>`
      + '</summary><div class="sw-inbox-row-body sw-note-group-body">'
      + `<div class="sw-note-receipts">${facts.map(receiptHtml).join('')}</div>`
      + '</div></details>';
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
    contextProvenanceFacts,
    renderContextProvenanceHtml,
    renderPreferenceLearnedHtml,
  });
}
