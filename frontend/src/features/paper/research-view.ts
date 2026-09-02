import { featureRegistry } from '../../feature-registry';
import { researchIsRunning, type ResearchStream } from './research-runtime';

type JsonObject = Record<string, unknown>;
type Translator = (key: string) => string;

type ResearchViewWindow = Window & {
  _researchStream?: ResearchStream | null;
  escapeHtml?: (value: unknown) => string;
  t?: Translator;
  renderMarkdown?: (markdown: string) => string;
  renderToolRoundsHTML?: (rounds: JsonObject[], running: boolean) => string;
  Api?: {
    research?: {
      list(limit: number): Promise<JsonObject>;
    };
  };
  _researchField?: typeof researchField;
  _researchScoresHtml?: typeof researchScoresHtml;
  _researchIdeasHtml?: typeof researchIdeasHtml;
  _researchRejectedHtml?: typeof researchRejectedHtml;
  _researchSurveyHtml?: typeof researchSurveyHtml;
  _researchCorpusHtml?: typeof researchCorpusHtml;
  _researchEvaluationHtml?: typeof researchEvaluationHtml;
  _researchUsageHtml?: typeof researchUsageHtml;
  _researchToolsHtml?: typeof researchToolsHtml;
  _renderRecentResearch?: typeof renderRecentResearch;
  _paintResearch?: typeof paintResearch;
  _showResearchLanding?: typeof showResearchLanding;
};

const RESEARCH_PHASES = ['harvest', 'survey', 'ideate', 'evaluate'] as const;

function globals(): ResearchViewWindow {
  return featureRegistry as unknown as ResearchViewWindow;
}

function escapeHtml(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function translator(): Translator {
  return globals().t ?? ((key) => key);
}

function object(value: unknown): JsonObject | null {
  return value && typeof value === 'object' ? value as JsonObject : null;
}

function array(value: unknown): JsonObject[] {
  return Array.isArray(value)
    ? value.filter((row): row is JsonObject => Boolean(row && typeof row === 'object'))
    : [];
}

export function researchField(label: string, value: unknown): string {
  if (!value) return '';
  return '<div class="pm-idea-field">'
    + '<span class="pm-idea-label">' + escapeHtml(label) + '</span>'
    + '<span class="pm-idea-value">' + escapeHtml(String(value)) + '</span>'
    + '</div>';
}

export function researchScoresHtml(scores: unknown, overall: unknown): string {
  const scoreRows = object(scores);
  if (!scoreRows) return '';
  let chips = Object.keys(scoreRows).map((axis) => (
    '<span class="pm-score-chip">' + escapeHtml(axis) + ' '
      + escapeHtml(String(scoreRows[axis])) + '</span>'
  )).join('');
  if (overall != null) {
    chips += '<span class="pm-score-chip is-overall">'
      + escapeHtml(String(overall)) + '</span>';
  }
  return '<div class="pm-idea-scores">' + chips + '</div>';
}

export function researchIdeasHtml(stream: ResearchStream, t: Translator): string {
  const ideas = stream.acceptedIdeas ?? [];
  if (!ideas.length) {
    return '<div class="pm-research-empty">'
      + escapeHtml(t('paper.research.noIdeas')) + '</div>';
  }
  const cards = ideas.map((idea) => (
    '<div class="pm-idea-card">'
      + '<div class="pm-idea-title">'
      + escapeHtml(idea.title || t('paper.research.untitled')) + '</div>'
      + researchField(t('paper.research.corpusAnchor'), idea.corpus_anchor_id)
      + researchField(t('paper.research.corpusDelta'), idea.corpus_delta)
      + researchField(t('paper.research.newInvariant'), idea.new_invariant)
      + researchField(t('paper.research.mechanism'), idea.core_mechanism)
      + researchField(t('paper.research.novelty'), idea.novelty_claim)
      + researchField(t('paper.research.prediction'), idea.falsifiable_prediction)
      + researchField(t('paper.research.whyNotAB'), idea.why_not_AB)
      + researchScoresHtml(idea.scores, idea.overall)
      + '</div>'
  )).join('');
  return '<div class="pm-research-section">'
    + '<div class="pm-research-section-title">'
    + escapeHtml(t('paper.research.acceptedTitle')) + '</div>'
    + cards + '</div>';
}

export function researchRejectedHtml(stream: ResearchStream, t: Translator): string {
  const rejected = stream.rejectedIdeas ?? [];
  if (!rejected.length) return '';
  let best: number | null = null;
  for (const row of rejected) {
    const score = Number(row.overall);
    if (Number.isFinite(score) && (best == null || score > best)) best = score;
  }
  const summary = t('paper.research.rejectedSummary')
    .replace('{n}', String(rejected.length))
    .replace('{best}', best == null ? '—' : String(best))
    .replace('{threshold}', stream.threshold == null ? '—' : String(stream.threshold));
  const rows = rejected.map((row) => (
    '<div class="pm-idea-card is-rejected">'
      + '<div class="pm-idea-title">'
      + escapeHtml(row.title || t('paper.research.untitled')) + '</div>'
      + researchField(t('paper.research.rejectReason'), row.reject_reason)
      + researchField(t('paper.research.rejectStage'), row.reject_stage)
      + researchScoresHtml(row.scores, row.overall)
      + '</div>'
  )).join('');
  return '<details class="pm-research-rejected"><summary>'
    + escapeHtml(summary) + '</summary>' + rows + '</details>';
}

export function researchSurveyHtml(stream: ResearchStream, t: Translator): string {
  let html = '';
  if (stream.surveyMd) {
    const markdown = globals().renderMarkdown;
    const content = markdown
      ? markdown(stream.surveyMd)
      : '<pre>' + escapeHtml(stream.surveyMd) + '</pre>';
    html += '<details class="pm-research-survey"><summary>'
      + escapeHtml(t('paper.research.surveyTitle')) + '</summary>'
      + '<div class="pm-research-md">' + content + '</div></details>';
  }
  const gaps = array(stream.openGaps?.open_gaps);
  if (gaps.length) {
    const items = gaps.map((gap) => (
      '<li class="pm-gap-item">'
        + '<span class="pm-gap-text">' + escapeHtml(gap.gap || '') + '</span>'
        + (gap.why_open
          ? '<span class="pm-gap-why">' + escapeHtml(gap.why_open) + '</span>'
          : '')
        + '</li>'
    )).join('');
    html += '<details class="pm-research-gaps"><summary>'
      + escapeHtml(t('paper.research.gapsTitle')) + '</summary>'
      + '<ul class="pm-gap-list">' + items + '</ul></details>';
  }
  return html;
}

export function researchCorpusHtml(stream: ResearchStream, t: Translator): string {
  const ids = stream.corpusIds ?? [];
  if (!ids.length) return '';
  const rows = ids.map((arxivId, index) => (
    '<button type="button" class="pm-research-corpus-paper" '
      + 'data-tofu-action="_openResearchCorpusPaper(' + index + ')">'
      + escapeHtml('arXiv:' + arxivId) + '</button>'
  )).join('');
  return '<details class="pm-research-corpus"><summary>'
    + escapeHtml(t('paper.research.corpusTitle').replace('{n}', String(ids.length)))
    + '</summary><div class="pm-research-corpus-list">' + rows + '</div></details>';
}

export function researchEvaluationHtml(stream: ResearchStream, t: Translator): string {
  const evaluation = stream.evaluation;
  if (!evaluation) return '';
  const score = Number(evaluation.overall_score);
  const hasScore = evaluation.overall_score != null && Number.isFinite(score);
  const verdict = hasScore
    ? score.toFixed(2) + ' / 5'
    : t('paper.research.evaluationUnavailable');
  const follow = evaluation.worth_following_up
    ? t('paper.research.followUpYes')
    : t('paper.research.followUpNo');
  const judges = t('paper.research.judgeConsensus')
    .replace('{n}', String(Number(evaluation.judge_count) || 0))
    .replace('{consensus}', String(evaluation.consensus || 'unavailable'));
  const scores = object(evaluation.scores);
  const scoreHtml = scores ? Object.keys(scores).map((axis) => (
    '<span class="pm-score-chip">'
      + escapeHtml(axis.replace(/_/g, ' ')) + ' '
      + escapeHtml(String(scores[axis])) + '</span>'
  )).join('') : '';
  const failureModes = Array.isArray(evaluation.failure_modes)
    ? evaluation.failure_modes : [];
  const failures = failureModes.length
    ? '<div class="pm-eval-line"><b>'
      + escapeHtml(t('paper.research.failureModes')) + '</b> '
      + escapeHtml(failureModes.join(', ')) + '</div>'
    : '';
  const changes = array(evaluation.recommended_changes).slice(0, 5).map((item) => (
    '<li><span class="pm-eval-priority">'
      + escapeHtml(String(item.priority || 'medium')) + '</span> '
      + escapeHtml(String(item.change || '')) + '</li>'
  )).join('');
  return '<section class="pm-research-evaluation'
    + (evaluation.degraded ? ' is-degraded' : '') + '">'
    + '<div class="pm-eval-head"><div><div class="pm-eval-kicker">'
    + escapeHtml(t('paper.research.evaluationTitle')) + '</div>'
    + '<div class="pm-eval-score">' + escapeHtml(verdict) + '</div></div>'
    + '<div class="pm-eval-follow">' + escapeHtml(follow) + '</div></div>'
    + '<div class="pm-eval-meta">' + escapeHtml(judges) + '</div>'
    + (evaluation.verdict
      ? '<div class="pm-eval-verdict">' + escapeHtml(evaluation.verdict) + '</div>'
      : '')
    + (scoreHtml ? '<div class="pm-idea-scores">' + scoreHtml + '</div>' : '')
    + failures
    + (changes
      ? '<div class="pm-eval-line"><b>'
        + escapeHtml(t('paper.research.recommendedChanges')) + '</b>'
        + '<ul class="pm-eval-changes">' + changes + '</ul></div>'
      : '')
    + '</section>';
}

function usageNumber(value: unknown): string {
  const number = Number(value) || 0;
  try { return number.toLocaleString(); } catch { return String(number); }
}

function usageStageRow(name: string, value: unknown, t: Translator): string {
  const row = object(value);
  if (!row || !Number(row.calls)) return '';
  return '<div class="pm-research-usage-row"><span>'
    + escapeHtml(t('paper.research.' + name)) + '</span><span>'
    + escapeHtml(t('paper.research.usageLine')
      .replace('{calls}', usageNumber(row.calls))
      .replace('{input}', usageNumber(row.prompt_tokens))
      .replace('{output}', usageNumber(row.completion_tokens))
      .replace('{cache}', usageNumber(row.cache_read_tokens)))
    + '</span></div>';
}

export function researchUsageHtml(stream: ResearchStream, t: Translator): string {
  const usage = stream.usage;
  const total = object(usage?.total);
  if (!total || !Number(total.calls)) return '';
  const incomplete = Number(total.unmetered_calls) > 0;
  const priceText = Number(total.priced_calls)
    ? (incomplete ? '≥' : (total.cost_estimated ? '≈' : ''))
      + '¥' + (Number(total.cost_cny) || 0).toFixed(4)
    : '—';
  const summary = t('paper.research.usageSummary')
    .replace('{calls}', usageNumber(total.calls))
    .replace('{input}', usageNumber(total.prompt_tokens))
    .replace('{output}', usageNumber(total.completion_tokens))
    .replace('{cost}', priceText);
  const forced = total.forced_final
    ? '<div class="pm-research-usage-note">'
      + escapeHtml(t('paper.research.usageForced')) + '</div>'
    : '';
  const missing = incomplete
    ? '<div class="pm-research-usage-note">'
      + escapeHtml(t('paper.research.usageIncomplete')
        .replace('{n}', usageNumber(total.unmetered_calls))) + '</div>'
    : '';
  const stages = object(usage?.stages);
  return '<details class="pm-research-usage"><summary>'
    + escapeHtml(t('paper.research.usageTitle')) + ' · ' + escapeHtml(summary)
    + '</summary><div class="pm-research-usage-body">'
    + usageStageRow('survey', stages?.survey, t)
    + usageStageRow('ideate', stages?.ideate, t)
    + usageStageRow('evaluate', stages?.evaluate, t)
    + forced + missing + '</div></details>';
}

export function researchToolsHtml(stream: ResearchStream): string {
  const render = globals().renderToolRoundsHTML;
  if (!stream.toolRounds.length || !render) return '';
  return '<div class="paper-report-tools pm-research-tools">'
    + render(stream.toolRounds, researchIsRunning(stream)) + '</div>';
}

export async function renderRecentResearch(): Promise<void> {
  const host = document.getElementById('paperRecentResearch');
  if (!host) return;
  const t = translator();
  let data: JsonObject;
  try {
    const list = globals().Api?.research?.list;
    if (!list) return;
    data = await list(20);
  } catch (error: unknown) {
    console.debug('[Research] recent list failed:', error);
    return;
  }
  const items = array(data.items);
  if (!items.length) {
    host.innerHTML = '';
    return;
  }
  const rows = items.map((item) => {
    const counts = t('paper.research.recentCounts')
      .replace('{accepted}', String(item.accepted || 0))
      .replace('{rejected}', String(item.rejected || 0));
    const direction = JSON.stringify(String(item.direction || '')).replace(/"/g, '&quot;');
    const lang = JSON.stringify(String(item.lang || 'en')).replace(/"/g, '&quot;');
    return '<button class="pm-recent-item" data-tofu-action="_restoreResearchFromStore('
      + direction + ',' + lang + ')">'
      + '<span class="pm-recent-dir">' + escapeHtml(item.direction) + '</span>'
      + '<span class="pm-recent-meta">' + escapeHtml(counts)
      + (item.degraded ? ' · ' + escapeHtml(t('paper.research.degraded')) : '')
      + '</span></button>';
  }).join('');
  host.innerHTML = '<div class="pm-recent-head">'
    + '<span class="pm-recent-title">'
    + escapeHtml(t('paper.research.recentTitle')) + '</span>'
    + '<span class="pm-recent-hint">'
    + escapeHtml(t('paper.research.recentHint')) + '</span></div>'
    + '<div class="pm-recent-list">' + rows + '</div>';
}

export function showResearchLanding(): void {
  const host = document.getElementById('researchViewer');
  if (!host) return;
  const t = translator();
  host.innerHTML = '<div class="research-landing">'
    + '<div class="research-landing-icon">'
    + '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/></svg>'
    + '</div>'
    + '<h3>' + escapeHtml(t('paper.research.entryTitle')) + '</h3>'
    + '<p>' + escapeHtml(t('paper.research.subtitle')) + '</p>'
    + '<div class="research-landing-row">'
    + '<input type="text" id="paperResearchInput" class="research-landing-input" placeholder="'
    + escapeHtml(t('paper.research.entryPlaceholder')) + '"'
    + ' data-tofu-action-keydown="if(event.key===\'Enter\')_submitResearchDirection()">'
    + '<button class="research-landing-btn" data-tofu-action="_submitResearchDirection()">'
    + '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>'
    + ' ' + escapeHtml(t('paper.research.startBtn')) + '</button>'
    + '</div>'
    // Past-run index is the ONLY way back to a finished run whose exact
    // wording the user no longer remembers (the direction hash is one-way).
    // Stays EMPTY when nothing was ever researched — no blank box.
    + '<div id="paperRecentResearch" class="pm-recent"></div>'
    + '</div>';
  renderRecentResearch().catch((error: unknown) => {
    console.debug('[Research] recent research render failed:', error);
  });
}

export function paintResearch(): void {
  const stream = globals()._researchStream;
  if (!stream) return;
  const viewer = document.getElementById('researchViewer');
  if (!viewer) return;
  const t = translator();
  const running = researchIsRunning(stream);
  const phaseIndex = RESEARCH_PHASES.indexOf(
    stream.phase as typeof RESEARCH_PHASES[number]);
  const steps = RESEARCH_PHASES.map((phase) => {
    const done = RESEARCH_PHASES.indexOf(phase) < phaseIndex;
    const active = phase === stream.phase;
    return '<div class="paper-step' + (active ? ' is-active' : '')
      + (done ? ' is-done' : '') + '">'
      + escapeHtml(t('paper.research.' + phase)) + '</div>';
  }).join('');
  const quality = stream.degraded
    ? '<div class="pm-quality is-degraded" role="alert">'
      + '<div class="pm-quality-title">'
      + escapeHtml(t('paper.research.degraded')) + '</div>'
      + '<div class="pm-quality-reason">'
      + escapeHtml(stream.degradedReason) + '</div></div>'
    : '';
  const body = running
    ? '<div class="pm-console-head">'
      + '<div class="pm-console-title">' + escapeHtml(t('paper.research.running')) + '</div>'
      + '<button class="pm-console-abort" data-tofu-action="_abortResearchJob()">'
      + escapeHtml(t('paper.research.abort')) + '</button></div>'
      + '<div class="paper-stepper">' + steps + '</div>'
      + '<div class="paper-media-activity" data-research-elapsed data-started-at="'
      + String(stream.startedAt || 0) + '"></div>'
      + researchToolsHtml(stream)
    : '<div class="pm-console-head"><div class="pm-console-title">'
      + escapeHtml(t('paper.research.finished')) + '</div></div>' + quality
      + '<div class="pm-research-tally">'
      + '<span>' + escapeHtml(String(stream.accepted)) + ' accepted</span>'
      + '<span>' + escapeHtml(String(stream.rejected)) + ' rejected</span>'
      + '<span>' + escapeHtml(String(stream.corpusSize)) + ' papers</span></div>'
      + researchUsageHtml(stream, t)
      + researchToolsHtml(stream)
      + researchCorpusHtml(stream, t)
      + researchEvaluationHtml(stream, t)
      + researchIdeasHtml(stream, t)
      + researchRejectedHtml(stream, t)
      + researchSurveyHtml(stream, t)
      + (stream.folderId
        ? '<button class="paper-retry-btn" data-tofu-action="_openResearchFolder()">'
          + escapeHtml(t('paper.research.openFolder')) + '</button>'
        : '');
  viewer.innerHTML = '<div class="pm-console" data-research-shell="1">'
    + '<div class="pm-studio-head"><div class="pm-studio-head-text">'
    + '<div class="pm-studio-title">' + escapeHtml(stream.direction) + '</div>'
    + '<div class="pm-studio-sub">' + escapeHtml(t('paper.research.subtitle'))
    + '</div></div></div>' + body + '</div>';
}

export function installResearchViewGlobals(): void {
  const target = globals();
  target._researchField = researchField;
  target._researchScoresHtml = researchScoresHtml;
  target._researchIdeasHtml = researchIdeasHtml;
  target._researchRejectedHtml = researchRejectedHtml;
  target._researchSurveyHtml = researchSurveyHtml;
  target._researchCorpusHtml = researchCorpusHtml;
  target._researchEvaluationHtml = researchEvaluationHtml;
  target._researchUsageHtml = researchUsageHtml;
  target._researchToolsHtml = researchToolsHtml;
  target._renderRecentResearch = renderRecentResearch;
  target._paintResearch = paintResearch;
  target._showResearchLanding = showResearchLanding;
}

installResearchViewGlobals();
