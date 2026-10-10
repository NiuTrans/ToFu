import { paperCards, bindPaperCards, destroyLiteratureGraph } from './live-library';
import { openResearchRecord, resumeSavedResearch, startResearchJob } from '../controllers/runtime';
import { _i18nLang } from '../../../../i18n';
import { patchResearchDom } from './stable-dom';
import { researchNumber, researchText } from '../../../paper/research-cycle';
import { featureRegistry } from '../../../../feature-registry';
import { escapeHtml } from '../../../../html-safety';
import { researchIsRunning, type ResearchStream } from '../controllers/runtime';

import { loadResearchWorkspace, researchWorkspaceHtml, setResearchManuscriptVisible } from '../controllers/workspace';

type JsonObject = Record<string, unknown>;
type Translator = (key: string) => string;

type ResearchViewWindow = Window & {
  _researchStream?: ResearchStream | null;
  t?: Translator;
  renderMarkdown?: (markdown: string) => string;
  renderToolRoundsHTML?: (rounds: JsonObject[], running: boolean) => string;
  _safeClipboardWrite?: (text: string) => Promise<unknown> | unknown;
  Api?: {
    research?: {
      list(limit: number): Promise<JsonObject>;
      records?(limit: number): Promise<JsonObject>;
    };
  };
  _restoreResearchFromStore?: (direction: string, lang: string) => Promise<unknown>;
  _renderResearchSidebar?: typeof renderResearchSidebar;
  _researchField?: typeof researchField;
  _researchScoresHtml?: typeof researchScoresHtml;
  _researchIdeasHtml?: typeof researchIdeasHtml;
  _researchRejectedHtml?: typeof researchRejectedHtml;
  _researchSurveyHtml?: typeof researchSurveyHtml;
  _researchCorpusHtml?: typeof researchCorpusHtml;
  _researchEvaluationHtml?: typeof researchEvaluationHtml;
  _researchUsageHtml?: typeof researchUsageHtml;
  _researchToolsHtml?: typeof researchToolsHtml;
  _researchPipelineHtml?: typeof researchPipelineHtml;
  _researchDeliverablesHtml?: typeof researchDeliverablesHtml;
  _copyResearchArtifact?: typeof copyResearchArtifact;
  _newResearchDirection?: typeof newResearchDirection;
  _fillResearchTemplate?: typeof fillResearchTemplate;

  _destroyResearchRuntime?: () => void;
  _paintResearch?: typeof paintResearch;
  _showResearchLanding?: typeof showResearchLanding;
  _switchResearchTab?: typeof switchResearchTab;
  _filterResearchThread?: typeof filterResearchThread;
};

const RESEARCH_PHASES = ['harvest', 'survey', 'ideate', 'evaluate', 'publish'] as const;
type ResearchPhase = typeof RESEARCH_PHASES[number];

const ICONS = {
  arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-7-7 7 7-7 7"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  external: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3h6v6m0-6-9 9"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
};

function globals(): ResearchViewWindow {
  return featureRegistry as unknown as ResearchViewWindow;
}

function translator(): Translator {
  // An installed translator deliberately echoes an unknown key. Preserve that
  // signal so i18n tests and production diagnostics can see missing coverage;
  // use English fallbacks only when the translation service itself is absent.
  return globals().t ?? (() => '');
}

function tr(t: Translator, key: string, fallback: string): string {
  return t(key) || fallback;
}

function format(t: Translator, key: string, fallback: string, values: JsonObject): string {
  let value = tr(t, key, fallback);
  for (const [name, replacement] of Object.entries(values)) {
    value = value.replaceAll(`{${name}}`, String(replacement ?? ''));
  }
  return value;
}

function object(value: unknown): JsonObject | null {
  return value && typeof value === 'object' ? value as JsonObject : null;
}

function array(value: unknown): JsonObject[] {
  return Array.isArray(value)
    ? value.filter((row): row is JsonObject => Boolean(row && typeof row === 'object'))
    : [];
}

function text(value: unknown): string {
  return value == null ? '' : String(value).trim();
}

function number(value: unknown): number | null {
  return researchNumber(value);
}

function researchStageIndex(phase: string): number {
  if (phase === 'done') return RESEARCH_PHASES.length;
  const index = RESEARCH_PHASES.indexOf(phase as ResearchPhase);
  return index < 0 ? 0 : index;
}

function researchStatusLabel(stream: ResearchStream, t: Translator): string {
  if (stream.reconnecting) return tr(t, 'evolution.research.reconnecting', 'Reconnecting — last confirmed state');
  if (stream.cancelling && researchIsRunning(stream)) return tr(t, 'evolution.research.cancelling', 'Stop requested — awaiting confirmation');
  if (stream.status === 'submitting') return tr(t, 'evolution.research.submitting', 'Submitting…');
  if (stream.status === 'error') return tr(t, 'evolution.research.statusError', 'Needs attention');
  if (stream.status === 'aborted') return tr(t, 'evolution.research.statusAborted', 'Stopped');
  if (stream.researchCycle?.status) return cycleStatus(stream.researchCycle.status, t);
  if (stream.degraded) return tr(t, 'evolution.research.statusDegraded', 'Delivered with warnings');
  if (researchIsRunning(stream)) return tr(t, 'evolution.research.statusRunning', 'Pipeline running');
  return tr(t, 'evolution.research.statusReady', 'Decision packet ready');
}

function researchTimestamp(timestamp: unknown): string {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return '';
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }).format(new Date(value));
  } catch {
    return '';
  }
}

function researchElapsed(stream: ResearchStream): string {
  if (!stream.startedAt) return '';
  const end = researchIsRunning(stream) ? Date.now() : (stream.lastEventAt || Date.now());
  const seconds = Math.max(0, Math.round((end - stream.startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m ${String(rest).padStart(2, '0')}s`;
}

function researchFailureHtml(stream: ResearchStream, t: Translator): string {
  if (stream.status !== 'error' && stream.status !== 'aborted') return '';
  const isError = stream.status === 'error';
  return '<section class="rs-alert ' + (isError ? 'is-error' : 'is-stopped') + '" role="alert">'
    + '<div class="rs-alert-title">'
    + escapeHtml(isError
      ? tr(t, 'evolution.research.runFailed', 'This run could not finish')
      : tr(t, 'evolution.research.runStopped', 'This run was stopped'))
    + '</div>'
    + (stream.error
      ? '<div class="rs-alert-detail">' + escapeHtml(stream.error) + '</div>'
      : '')
    + (stream.failedStage ? '<div class="rs-alert-detail">' + escapeHtml(tr(t, 'evolution.research.library.failedStage', 'Failed stage')) + ': ' + escapeHtml(stream.failedStage) + '</div>' : '')
    + (stream.surveyBatchFailures?.length ? '<details class="rs-alert-detail"><summary>'
      + escapeHtml(tr(t, 'evolution.research.evidence', 'Evidence')) + ' · ' + stream.surveyBatchFailures.length + '</summary>'
      + stream.surveyBatchFailures.slice(0, 20).map(row => '<p>' + escapeHtml(text(row.error))
        + '<br>' + escapeHtml(Array.isArray(row.paper_ids) ? row.paper_ids.slice(0, 25).join(', ') : '') + '</p>').join('') + '</details>' : '')
    + '<button type="button" id="researchRetryDirection" class="rs-btn is-secondary">' + escapeHtml(tr(t, 'evolution.research.library.retry', 'Retry this direction (new run)')) + '</button>'
    + '<button type="button" class="rs-btn is-secondary" data-tofu-action="_newResearchDirection()">'
    + escapeHtml(tr(t, 'evolution.research.backToWorkbench', 'Start a new direction')) + '</button>'
    + '</section>';
}

function textList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => text(item)).filter(Boolean) : [];
}

function researchFieldValue(value: unknown): string {
  const raw = text(value);
  if (!raw) return '';
  // Ideate used to let models prefix inapplicable fields with "N/A (kind):"
  // even when substantive text followed; stored artifacts still carry it.
  const stripped = raw.replace(/^[Nn]\/?[Aa](?:\s*\([^)]*\))?\s*[:：]\s*/, '');
  if (!stripped || /^[Nn]\/?[Aa]\.?$/.test(stripped) || stripped === '不适用') return '';
  return stripped;
}

export function researchField(label: string, value: unknown): string {
  const cleaned = researchFieldValue(value);
  if (!cleaned) return '';
  return '<div class="rs-idea-field">'
    + '<div class="rs-idea-label">' + escapeHtml(label) + '</div>'
    + '<div class="rs-idea-value">' + escapeHtml(cleaned) + '</div>'
    + '</div>';
}

function scoreLabel(axis: string, t: Translator): string {
  const known: Record<string, string> = {
    novelty: tr(t, 'evolution.research.scoreNovelty', 'Novelty'),
    falsifiability: tr(t, 'evolution.research.scoreFalsifiability', 'Falsifiability'),
    mechanism_depth: tr(t, 'evolution.research.scoreMechanism', 'Mechanism'),
    value: tr(t, 'evolution.research.scoreValue', 'Value'),
    survey_coverage: tr(t, 'evolution.research.scoreCoverage', 'Coverage'),
    evidence_traceability: tr(t, 'evolution.research.scoreTraceability', 'Traceability'),
    gap_specificity: tr(t, 'evolution.research.scoreGap', 'Gap specificity'),
    synthesis_quality: tr(t, 'evolution.research.scoreSynthesis', 'Synthesis'),
    idea_relevance: tr(t, 'evolution.research.scoreRelevance', 'Relevance'),
    idea_mechanism_depth: tr(t, 'evolution.research.scoreMechanism', 'Mechanism'),
    idea_falsifiability: tr(t, 'evolution.research.scoreFalsifiability', 'Falsifiability'),
    gate_selectivity: tr(t, 'evolution.research.scoreSelectivity', 'Selectivity'),
  };
  return known[axis] || axis.replace(/_/g, ' ');
}

export function researchScoresHtml(
  scores: unknown,
  overall: unknown,
  t: Translator = translator(),
): string {
  const scoreRows = object(scores);
  const rows = scoreRows ? Object.keys(scoreRows).filter((axis) => number(scoreRows[axis]) != null) : [];
  if (!rows.length && number(overall) == null) return '';
  const chips = rows.map((axis) => {
    const score = number(scoreRows?.[axis]) ?? 0;
    const width = Math.max(0, Math.min(100, score * 20));
    return '<div class="rs-score" title="' + escapeHtml(scoreLabel(axis, t)) + ' ' + escapeHtml(score.toFixed(1)) + ' / 5">'
      + '<div class="rs-score-label"><span>' + escapeHtml(scoreLabel(axis, t)) + '</span>'
      + '<strong>' + escapeHtml(score.toFixed(1)) + '</strong></div>'
      + '<div class="rs-score-track"><span style="width:' + width + '%"></span></div>'
      + '</div>';
  }).join('');
  const overallScore = number(overall);
  return '<div class="rs-scores">' + chips
    + (overallScore == null ? '' : '<div class="rs-score-overall"><span>'
      + escapeHtml(tr(t, 'evolution.research.overall', 'Overall')) + '</span><strong>'
      + escapeHtml(overallScore.toFixed(2)) + '</strong><small>/ 5</small></div>')
    + '</div>';
}

function ideaRank(idea: JsonObject): number {
  return number(idea.overall) ?? -1;
}

function researchIdeaBrief(idea: JsonObject, t: Translator): string {
  const fields: [string, unknown][] = [
    [tr(t, 'evolution.research.mechanism', 'Mechanism'), idea.core_mechanism],
    [tr(t, 'evolution.research.novelty', 'Novelty claim'), idea.novelty_claim],
    [tr(t, 'evolution.research.prediction', 'Falsifiable prediction'), idea.falsifiable_prediction],
    [tr(t, 'evolution.research.corpusAnchor', 'Corpus anchor'), idea.corpus_anchor_id],
    [tr(t, 'evolution.research.corpusDelta', 'Delta from prior work'), idea.corpus_delta],
    [tr(t, 'evolution.research.newInvariant', 'New invariant'), idea.new_invariant],
    [tr(t, 'evolution.research.whyNotAB', 'Why this is not A+B'), idea.why_not_AB],
  ];
  return fields.filter(([, value]) => text(value)).map(([label, value]) => (
    `## ${label}\n${text(value)}`
  )).join('\n\n');
}

function ideaAssessments(stream: ResearchStream): JsonObject[] {
  return array(object(stream.evaluation)?.idea_assessments);
}

function ideaAssessmentFor(stream: ResearchStream, title: string): JsonObject | null {
  const wanted = title.trim().toLowerCase();
  if (!wanted) return null;
  return ideaAssessments(stream).find((row) => text(row.idea).toLowerCase() === wanted) ?? null;
}

function researchIdeaReviewHtml(stream: ResearchStream, idea: JsonObject, t: Translator): string {
  const assessment = ideaAssessmentFor(stream, text(idea.title));
  if (!assessment) return '';
  const score = number(assessment.score);
  const verdicts = textList(assessment.verdicts);
  const risks = textList(assessment.main_risks);
  return '<div class="rs-idea-review"><div class="rs-idea-review-head"><span>'
    + escapeHtml(tr(t, 'evolution.research.ideaReviewTitle', 'Review board on this idea')) + '</span>'
    + (score == null ? '' : '<strong>' + escapeHtml(score.toFixed(2)) + '<small> / 5</small></strong>')
    + '</div>'
    + verdicts.map((verdict) => '<p>' + escapeHtml(verdict) + '</p>').join('')
    + (risks.length
      ? '<div class="rs-idea-risk"><span>' + escapeHtml(tr(t, 'evolution.research.mainRisk', 'Main risk'))
        + '</span>' + escapeHtml(risks.join(' · ')) + '</div>'
      : '')
    + '</div>';
}

function researchIdeaCard(idea: JsonObject, index: number, t: Translator, stream: ResearchStream): string {
  const title = text(idea.title) || tr(t, 'evolution.research.untitled', '(untitled)');
  const overall = number(idea.overall);
  const linkedGap = text(idea.linked_gap_id);
  const secondary = researchField(tr(t, 'evolution.research.corpusAnchor', 'Corpus anchor'), idea.corpus_anchor_id)
    + researchField(tr(t, 'evolution.research.corpusDelta', 'Delta from prior work'), idea.corpus_delta)
    + researchField(tr(t, 'evolution.research.newInvariant', 'New invariant'), idea.new_invariant)
    + researchField(tr(t, 'evolution.research.whyNotAB', 'Why this is not A+B'), idea.why_not_AB);
  return '<article class="rs-idea-card" data-research-idea="' + index + '">'
    + '<div class="rs-idea-card-head"><div class="rs-idea-rank">#' + (index + 1) + '</div>'
    + '<div class="rs-idea-heading"><h3>' + escapeHtml(title) + '</h3>'
    + '<div class="rs-idea-tags">'
    + (idea.kind ? '<span>' + escapeHtml(idea.kind) + '</span>' : '')
    + (linkedGap ? '<span>' + escapeHtml(linkedGap) + '</span>' : '')

    + (number(idea.idea_version) == null ? '' : '<span>v' + escapeHtml(number(idea.idea_version)) + '</span>')
    + '</div></div>'
    + (overall == null ? '' : '<div class="rs-idea-score"><strong>'
      + escapeHtml(overall.toFixed(2)) + '</strong><span>/5</span></div>')
    + '</div>'
    + '<div class="rs-idea-grid">'
    + researchField(tr(t, 'evolution.research.mechanism', 'Mechanism'), idea.core_mechanism)
    + researchField(tr(t, 'evolution.research.novelty', 'Novelty claim'), idea.novelty_claim)
    + researchField(tr(t, 'evolution.research.prediction', 'Falsifiable prediction'), idea.falsifiable_prediction)
    + '</div>'
    + (secondary
      ? '<details class="rs-idea-more"><summary><span>'
        + escapeHtml(tr(t, 'evolution.research.ideaMoreFields', 'Supporting arguments'))
        + '</span><span class="rs-details-toggle">＋</span></summary>'
        + '<div class="rs-idea-grid is-secondary">' + secondary + '</div></details>'
      : '')
    + researchIdeaReviewHtml(stream, idea, t)
    + (researchScoresHtml(idea.scores, null, t) ? '<details class="rs-idea-more"><summary>'
      + escapeHtml(tr(t, 'evolution.research.reviewScores', 'Review scores (supporting signal)'))
      + '</summary>' + researchScoresHtml(idea.scores, null, t) + '</details>' : '')
    + '<div class="rs-idea-actions"><button type="button" class="rs-link-btn" '
    + 'data-tofu-action="_copyResearchArtifact(\'idea\',' + index + ',this)">'
    + ICONS.copy + escapeHtml(tr(t, 'evolution.research.copyBrief', 'Copy experiment brief'))
    + '</button></div>'
    + '</article>';
}

function cycleStatus(status: string, t: Translator): string {
  const labels: Record<string, string> = {
    reviewing: 'Reviewing', revising: 'Revising', complete: 'Review complete',
    unresolved: 'Unresolved issues', budget_exhausted: 'Revision budget exhausted',
    no_change: 'No substantive change', error: 'Revision interrupted',
    resolved: 'Resolved',
  };
  return labels[status] ? tr(t, 'evolution.research.cycle.' + status, labels[status]) : status;
}

export function researchCycleHtml(stream: ResearchStream, t: Translator): string {
  const cycle = stream.researchCycle;
  if (!cycle) return '';
  const issueHtml = cycle.issues.slice(0, 24).map((issue) => '<article class="rs-review-action"><div><strong>'
    + escapeHtml(researchText(issue.change)) + '</strong><small>'
    + escapeHtml([researchText(issue.idea_id), researchText(issue.target), researchText(issue.priority),
      cycleStatus(researchText(issue.status), t)].filter(Boolean).join(' · ')) + '</small>'
    + researchField(tr(t, 'evolution.research.evidence', 'Evidence'), researchText(issue.evidence))
    + researchField(tr(t, 'evolution.research.resolutionEvidence', 'Resolution evidence'), researchText(issue.resolution_evidence))
    + '</div></article>').join('');
  const history = cycle.history.slice(-12).map((entry) => {
    const ideas = object(entry.ideas);
    const versions = [...array(ideas?.accepted), ...array(ideas?.rejected)];
    return '<details class="rs-idea-more"><summary>'
      + escapeHtml(tr(t, 'evolution.research.revision', 'Revision')) + ' ' + escapeHtml(number(entry.revision) ?? '—')
      + '</summary>' + versions.map((idea) => '<p>' + escapeHtml(researchText(idea.title) || researchText(idea.idea_id))
        + ' · v' + escapeHtml(number(idea.idea_version) ?? '—')
        + '</p>' + researchField(tr(t, 'evolution.research.mechanism', 'Mechanism'), researchText(idea.core_mechanism))
        + researchField(tr(t, 'evolution.research.prediction', 'Falsifiable prediction'), researchText(idea.falsifiable_prediction))).join('') + '</details>';
  }).join('');
  return '<section class="rs-panel rs-cycle"><div class="rs-panel-head"><h2>'
    + escapeHtml(tr(t, 'evolution.research.cycleTitle', 'Decision & revision')) + '</h2><span>'
    + escapeHtml(cycleStatus(cycle.status, t)) + '</span></div>'
    + (cycle.revision_count == null ? '' : '<p>' + escapeHtml(format(t, 'evolution.research.revisionCount', '{n} revisions', { n: cycle.revision_count })) + '</p>')
    + researchField(tr(t, 'evolution.research.nextAction', 'Next action'), cycle.next_action)
    + (issueHtml ? '<details class="rs-idea-more" open><summary>' + escapeHtml(tr(t, 'evolution.research.issues', 'Issues'))
      + ' · ' + cycle.issues.length + '</summary>' + issueHtml + '</details>' : '')
    + (history ? '<details class="rs-idea-more"><summary>' + escapeHtml(tr(t, 'evolution.research.versions', 'Version history')) + '</summary>' + history + '</details>' : '')
    + '</section>';
}

export function researchIdeasHtml(stream: ResearchStream, t: Translator): string {
  const ideas = [...(stream.acceptedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
  if (!ideas.length) {
    const failedWithoutVerdict = ['error', 'aborted', 'interrupted'].includes(stream.status)
      && !stream.gateReached && !(stream.rejectedIdeas || []).length;
    const interrupted = failedWithoutVerdict || !stream.verdictAvailable;
    return '<section class="rs-panel rs-ideas"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
      + escapeHtml(tr(t, 'evolution.research.candidatesKicker', 'Candidate program')) + '</div><h2>'
      + escapeHtml(tr(t, 'evolution.research.acceptedTitle', 'Ideas that cleared the gate')) + '</h2></div></div>'
      + '<div class="rs-empty"><strong>'
      + escapeHtml(failedWithoutVerdict
        ? tr(t, 'evolution.research.noIdeasRunFailedTitle', 'Run interrupted — no gate verdict')
        : interrupted ? tr(t, 'evolution.research.library.noVerdict', 'No verdict available')
        : tr(t, 'evolution.research.noIdeasTitle', 'The gate rejected every candidate'))
      + '</strong><span>' + escapeHtml(failedWithoutVerdict
        ? tr(t, 'evolution.research.noIdeasRunFailed', 'The pipeline stopped before producing a gate verdict. See the failure details above.')
        : interrupted ? tr(t, 'evolution.research.library.noVerdictHint', 'No completed gate verdict has been saved. An empty list is not a rejection verdict.')
        : tr(t, 'evolution.research.noIdeas', 'No idea cleared the gate this run — an honest zero, not a failure.'))
      + '</span></div></section>';
  }
  return '<section class="rs-panel rs-ideas"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'evolution.research.candidatesKicker', 'Candidate program')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.acceptedTitle', 'Ideas that cleared the gate')) + '</h2></div>'
    + '<div class="rs-panel-actions"><button type="button" class="rs-link-btn" '
    + 'data-tofu-action="_copyResearchArtifact(\'ideas\',0,this)">'
    + ICONS.copy + escapeHtml(tr(t, 'evolution.research.copyAllBriefs', 'Copy all briefs'))
    + '</button><span class="rs-count">' + ideas.length + '</span></div></div>'
    + '<p class="rs-panel-intro">'
    + escapeHtml(tr(t, 'evolution.research.candidatesHint', 'Ranked by the novelty gate. Open the mechanism, falsifier and evidence delta before committing compute.'))
    + '</p><div class="rs-idea-list">'
    + ideas.map((idea, index) => researchIdeaCard(idea, index, t, stream)).join('')
    + '</div></section>';
}

export function researchRejectedHtml(stream: ResearchStream, t: Translator): string {
  const rejected = [...(stream.rejectedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
  if (!rejected.length) return '';
  const best = rejected.reduce<number | null>((highest, row) => {
    const score = number(row.overall);
    return score != null && (highest == null || score > highest) ? score : highest;
  }, null);
  const summary = format(t, 'evolution.research.rejectedSummary',
    '{n} rejected · best {best} / threshold {threshold}', {
      n: rejected.length,
      best: best == null ? '—' : best.toFixed(2),
      threshold: stream.threshold == null ? '—' : stream.threshold,
    });
  const rows = rejected.map((row, index) => (
    '<article class="rs-rejected-row"><span class="rs-rejected-index">' + (index + 1) + '</span>'
      + '<div><strong>' + escapeHtml(row.title || tr(t, 'evolution.research.untitled', '(untitled)')) + '</strong>'
      + '<p>' + escapeHtml(row.reject_reason || tr(t, 'evolution.research.rejectReasonUnknown', 'Did not clear the structural or novelty gate.')) + '</p>'
      + '<span>' + escapeHtml(row.reject_stage || '')
      + (number(row.overall) == null ? '' : ' · ' + escapeHtml((number(row.overall) ?? 0).toFixed(2)) + '/5')
      + '</span></div></article>'
  )).join('');
  return '<details class="rs-panel rs-rejections"><summary><span><b>'
    + escapeHtml(tr(t, 'evolution.research.rejectionAudit', 'Rejection audit')) + '</b><small>'
    + escapeHtml(summary) + '</small></span><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-rejected-list">' + rows + '</div></details>';
}

function researchGapCards(stream: ResearchStream, t: Translator): string {
  const gaps = array(stream.openGaps?.open_gaps);
  if (!gaps.length) return '';
  return '<div class="rs-gap-grid">' + gaps.map((gap, index) => (
    '<article class="rs-gap-card"><div class="rs-gap-num">G' + String(index + 1).padStart(2, '0') + '</div>'
      + '<div><strong>' + escapeHtml(researchText(gap.gap) || researchText(gap.title) || researchText(gap.description) || tr(t, 'evolution.research.gapMissing', 'Gap description unavailable')) + '</strong>'
      + (researchText(gap.description) && (researchText(gap.gap) || researchText(gap.title))
        ? '<p>' + escapeHtml(researchText(gap.description)) + '</p>' : '')
      + (researchText(gap.why_open) || researchText(gap.rationale) ? '<p>' + escapeHtml(researchText(gap.why_open) || researchText(gap.rationale)) + '</p>' : '')
      + (Array.isArray(gap.evidence) && gap.evidence.length
        ? '<div class="rs-gap-evidence">' + escapeHtml(tr(t, 'evolution.research.evidence', 'Evidence'))
          + ': ' + escapeHtml(gap.evidence.join(', ')) + '</div>'
        : '')
      + '</div></article>'
  )).join('') + '</div>';
}

export function researchSurveyHtml(stream: ResearchStream, t: Translator): string {
  const gaps = researchGapCards(stream, t);
  const markdown = stream.surveyMd
    ? (globals().renderMarkdown
      ? globals().renderMarkdown?.(stream.surveyMd) || ''
      : '<pre>' + escapeHtml(stream.surveyMd) + '</pre>')
    : '';
  if (!gaps && !markdown) return '';
  return '<section class="rs-panel rs-evidence"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'evolution.research.evidenceKicker', 'Evidence synthesis')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.gapsTitle', 'Open-gap map')) + '</h2></div>'
    + (stream.surveyMd ? '<button type="button" class="rs-link-btn" data-tofu-action="_copyResearchArtifact(\'survey\',0,this)">'
      + ICONS.copy + escapeHtml(tr(t, 'evolution.research.copySurvey', 'Copy survey')) + '</button>' : '')
    + '</div>' + gaps
    + (markdown ? '<details class="rs-survey"><summary><span>'
      + escapeHtml(tr(t, 'evolution.research.surveyTitle', 'Full survey'))
      + '</span><span class="rs-details-toggle">＋</span></summary>'
      + '<div class="rs-markdown">' + markdown + '</div></details>' : '')
    + '</section>';
}

export function researchCorpusHtml(stream: ResearchStream, t: Translator): string {
  const ids = stream.corpusIds ?? [];
  if (!ids.length) return '';
  const rows = ids.map((arxivId, index) => (
    '<button type="button" class="rs-paper-chip" data-tofu-action="_openResearchCorpusPaper(' + index + ')">'
      + '<span>arXiv</span><strong>' + escapeHtml(arxivId) + '</strong>' + ICONS.external + '</button>'
  )).join('');
  return '<details class="rs-corpus"><summary><span>'
    + escapeHtml(format(t, 'evolution.research.corpusTitle', '{n} papers indexed in this run', { n: ids.length }))
    + '</span><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-paper-list">' + rows + '</div></details>';
}

function researchIdeaAssessmentsHtml(stream: ResearchStream, t: Translator): string {
  const rows = ideaAssessments(stream);
  if (!rows.length) return '';
  return '<div class="rs-review-ideas"><h3>'
    + escapeHtml(tr(t, 'evolution.research.evaluationIdeasTitle', 'Challenges by idea')) + '</h3>'
    + rows.map((row) => {
      const score = number(row.score);
      const verdicts = textList(row.verdicts);
      const risks = textList(row.main_risks);
      return '<article class="rs-review-idea"><div class="rs-review-idea-head"><strong>'
        + escapeHtml(text(row.idea)) + '</strong>'
        + (score == null ? '' : '<span>' + escapeHtml(score.toFixed(2)) + ' / 5</span>')
        + '</div>'
        + verdicts.map((verdict) => '<p>' + escapeHtml(verdict) + '</p>').join('')
        + (risks.length
          ? '<div class="rs-idea-risk"><span>' + escapeHtml(tr(t, 'evolution.research.mainRisk', 'Main risk'))
            + '</span>' + escapeHtml(risks.join(' · ')) + '</div>'
          : '')
        + '</article>';
    }).join('') + '</div>';
}
export function researchEvaluationHtml(stream: ResearchStream, t: Translator): string {
  const evaluation = stream.evaluation;
  if (!evaluation) return '';
  const score = number(evaluation.overall_score);
  const worthFollowing = evaluation.worth_following_up === true;
  const strengths = Array.isArray(evaluation.strengths) ? evaluation.strengths : [];
  const failureModes = Array.isArray(evaluation.failure_modes) ? evaluation.failure_modes : [];
  const changes = array(evaluation.recommended_changes).slice(0, 6);
  const judges = format(t, 'evolution.research.judgeConsensus', '{n} judges · {consensus}', {
    n: number(evaluation.judge_count) ?? 0,
    consensus: evaluation.consensus || 'unavailable',
  });
  return '<section class="rs-panel rs-evaluation' + (evaluation.degraded ? ' is-degraded' : '') + '">'
    + '<div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'evolution.research.evaluationKicker', 'Independent review board')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.evaluationTitle', 'Artifact quality review')) + '</h2></div>'
    + '<div class="rs-review-decision ' + (worthFollowing ? 'is-go' : 'is-hold') + '">'
    + escapeHtml(worthFollowing
      ? tr(t, 'evolution.research.followUpYes', 'Worth a follow-up experiment')
      : tr(t, 'evolution.research.followUpNo', 'Revise before spending compute')) + '</div></div>'
    + '<p class="rs-panel-intro">'
    + escapeHtml(format(t, 'evolution.research.evaluationScope',
      'Review target: the whole decision packet — survey + {n} ranked ideas + the novelty gate. Per-idea challenges are listed below.', {
        n: stream.acceptedIdeas?.length || stream.accepted || 0,
      }))
    + '</p>'
    + '<div class="rs-review-grid"><div class="rs-review-score"><span>'
    + escapeHtml(tr(t, 'evolution.research.overall', 'Overall')) + '</span><strong>'
    + escapeHtml(score == null ? '—' : score.toFixed(2)) + '</strong><small>/ 5</small>'
    + '<em>' + escapeHtml(judges) + '</em></div>'
    + '<div class="rs-review-main">'
    + (evaluation.verdict ? '<p class="rs-review-verdict">' + escapeHtml(evaluation.verdict) + '</p>' : '')
    + researchScoresHtml(evaluation.scores, null, t)
    + '</div></div>'
    + ((strengths.length || failureModes.length) ? '<div class="rs-review-columns">'
      + '<div><h3>' + escapeHtml(tr(t, 'evolution.research.strengths', 'What survived scrutiny')) + '</h3><ul>'
      + strengths.map((item) => '<li>' + escapeHtml(item) + '</li>').join('') + '</ul></div>'
      + '<div><h3>' + escapeHtml(tr(t, 'evolution.research.failureModes', 'Main failure modes')) + '</h3><ul>'
      + failureModes.map((item) => {
        const code = String(item);
        return '<li data-failure-mode="' + escapeHtml(code) + '">'
          + escapeHtml(code.replace(/_/g, ' ')) + '</li>';
      }).join('') + '</ul></div>'
      + '</div>' : '')
    + (changes.length ? '<div class="rs-review-actions"><h3>'
      + escapeHtml(tr(t, 'evolution.research.recommendedChanges', 'Next revision queue')) + '</h3>'
      + changes.map((item, index) => '<div class="rs-review-action"><span>'
        + escapeHtml(String(index + 1).padStart(2, '0')) + '</span><div><strong>'
        + escapeHtml(item.change || '') + '</strong><small>'
        + escapeHtml([item.target, item.priority].filter(Boolean).join(' · ')) + '</small>'
        + (item.evidence ? '<p>' + escapeHtml(item.evidence) + '</p>' : '') + '</div></div>').join('')
      + '</div>' : '')
    + researchIdeaAssessmentsHtml(stream, t)
    + '</section>';
}

function usageNumber(value: unknown): string {
  const parsed = Number(value) || 0;
  try { return parsed.toLocaleString(); } catch { return String(parsed); }
}

function usagePriceText(total: JsonObject): string {
  const incomplete = Number(total.unmetered_calls) > 0;
  if (!Number(total.priced_calls)) return '—';
  return (incomplete ? '≥' : (total.cost_estimated ? '≈' : ''))
    + '¥' + (Number(total.cost_cny) || 0).toFixed(4);
}

function usageStageRow(name: string, value: unknown, t: Translator): string {
  const row = object(value);
  if (!row || !Number(row.calls)) return '';
  return '<div class="rs-usage-row"><span>'
    + escapeHtml(tr(t, 'evolution.research.' + name, name)) + '</span><span>'
    + escapeHtml(format(t, 'evolution.research.usageLine',
      '{calls} calls · {input} input · {output} output · {cache} cached', {
        calls: usageNumber(row.calls), input: usageNumber(row.prompt_tokens),
        output: usageNumber(row.completion_tokens), cache: usageNumber(row.cache_read_tokens),
      })) + '</span></div>';
}

export function researchUsageHtml(stream: ResearchStream, t: Translator): string {
  const usage = stream.usage;
  const total = object(usage?.total);
  if (!total || !Number(total.calls)) return '';
  const incomplete = Number(total.unmetered_calls) > 0;
  const priceText = usagePriceText(total);
  const summary = format(t, 'evolution.research.usageSummary',
    '{calls} calls · {input} input · {output} output · {cost}', {
      calls: usageNumber(total.calls), input: usageNumber(total.prompt_tokens),
      output: usageNumber(total.completion_tokens), cost: priceText,
    });
  const stages = object(usage?.stages);
  return '<details class="rs-usage"><summary><span>'
    + escapeHtml(tr(t, 'evolution.research.usageTitle', 'Resource usage')) + '</span><small>'
    + escapeHtml(summary) + '</small><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-usage-body">'
    + usageStageRow('harvest', stages?.harvest, t)
    + usageStageRow('survey', stages?.survey, t)
    + usageStageRow('ideate', stages?.ideate, t)
    + usageStageRow('evaluate', stages?.evaluate, t)
    + (total.forced_final ? '<p>' + escapeHtml(tr(t, 'evolution.research.usageForced', 'The adaptive guard finalized from collected evidence.')) + '</p>' : '')
    + (incomplete ? '<p>' + escapeHtml(format(t, 'evolution.research.usageIncomplete',
      '{n} calls were not metered; the displayed cost is a lower bound.', { n: total.unmetered_calls })) + '</p>' : '')
    + '</div></details>';
}

export function researchToolsHtml(stream: ResearchStream): string {
  const render = globals().renderToolRoundsHTML;
  if (!stream.toolRounds.length || !render) return '';
  const t = translator();
  return '<section class="rs-panel rs-trace"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'evolution.research.traceKicker', 'Execution trace')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.traceTitle', 'Live evidence trail')) + '</h2></div></div>'
    + '<div class="paper-report-tools rs-tool-rounds">'
    + render(stream.toolRounds, researchIsRunning(stream)) + '</div></section>';
}

function phaseState(stream: ResearchStream, phase: ResearchPhase): 'done' | 'active' | 'pending' | 'error' {
  const current = researchStageIndex(stream.phase);
  const index = RESEARCH_PHASES.indexOf(phase);
  if ((stream.status === 'error' || stream.status === 'aborted') && index === current) return 'error';
  if (researchIsRunning(stream) && index === current) return 'active';
  if (!researchIsRunning(stream) && stream.status === 'done') return 'done';
  if (index < current) return 'done';
  return 'pending';
}

export function researchPipelineHtml(stream: ResearchStream, t: Translator): string {
  const steps = RESEARCH_PHASES.map((phase, index) => {
    const state = phaseState(stream, phase);
    return '<div class="rs-pipeline-step is-' + state + '">'
      + '<div class="rs-pipeline-node">' + (state === 'done' ? ICONS.check : String(index + 1)) + '</div>'
      + '<div><strong>' + escapeHtml(tr(t, 'evolution.research.' + phase, phase)) + '</strong><span>'
      + escapeHtml(tr(t, 'evolution.research.phase.' + phase, '')) + '</span></div></div>';
  }).join('');
  return '<section class="rs-pipeline" aria-label="'
    + escapeHtml(tr(t, 'evolution.research.pipelineTitle', 'Research pipeline')) + '">'
    + '<div class="rs-pipeline-head"><div><span>'
    + escapeHtml(tr(t, 'evolution.research.pipelineTitle', 'Research pipeline')) + '</span><strong>'
    + escapeHtml(researchStatusLabel(stream, t)) + '</strong></div>'
    + (researchElapsed(stream) ? '<time>' + escapeHtml(researchElapsed(stream)) + '</time>' : '')
    + '</div>'
    + '<div class="rs-pipeline-steps">' + steps + '</div></section>';
}

export function researchDeliverablesHtml(stream: ResearchStream, t: Translator): string {
  if (researchIsRunning(stream)) return '';
  const evaluation = stream.evaluation;
  const rows = [
    {
      className: 'is-candidate',
      label: tr(t, 'evolution.research.deliverableCandidates', 'Candidate papers'),
      value: format(t, 'evolution.research.deliverableCandidatesValue', '{n} ranked directions', { n: stream.accepted }),
      detail: tr(t, 'evolution.research.deliverableCandidatesHint', 'Mechanism, novelty delta and falsifiable prediction'),
    },
    {
      className: 'is-evidence',
      label: tr(t, 'evolution.research.deliverableEvidence', 'Evidence packet'),
      value: format(t, 'evolution.research.deliverableEvidenceValue', '{papers} papers · {gaps} open gaps', {
        papers: stream.corpusSize || stream.corpusIds.length,
        gaps: array(stream.openGaps?.open_gaps).length,
      }),
      detail: tr(t, 'evolution.research.deliverableEvidenceHint', 'Corpus, comparison survey and gap map'),
    },
    {
      className: evaluation?.worth_following_up ? 'is-go' : 'is-hold',
      label: tr(t, 'evolution.research.deliverableDecision', 'Review decision'),
      value: evaluation?.worth_following_up
        ? tr(t, 'evolution.research.followUpYes', 'Worth a follow-up experiment')
        : tr(t, 'evolution.research.followUpNo', 'Revise before spending compute'),
      detail: number(evaluation?.overall_score) == null
        ? tr(t, 'evolution.research.evaluationUnavailable', 'Review unavailable')
        : `${(number(evaluation?.overall_score) ?? 0).toFixed(2)} / 5`,
    },
  ];
  const usageTotal = object(stream.usage?.total);
  if (usageTotal && Number(usageTotal.calls)) {
    rows.push({
      className: 'is-usage',
      label: tr(t, 'evolution.research.deliverableUsage', 'Resource usage'),
      value: format(t, 'evolution.research.deliverableUsageValue', '{calls} calls · {cost}', {
        calls: usageNumber(usageTotal.calls),
        cost: usagePriceText(usageTotal),
      }),
      detail: tr(t, 'evolution.research.deliverableUsageHint', 'Per-stage tokens and cache detail in the ledger'),
    });
  }
  return '<section class="rs-deliverables">' + rows.map((row) => (
    '<article class="rs-deliverable ' + row.className + '"><span>' + escapeHtml(row.label)
      + '</span><strong>' + escapeHtml(row.value) + '</strong><small>' + escapeHtml(row.detail) + '</small></article>'
  )).join('') + '</section>';
}

/* ── Sidebar record rail ─────────────────────────────────────────────
   Research mode swaps the sidebar conversation list for this rail: current
   program card on top, every past exploration below. Mirrors the paper
   library overlay; the classes live in 03-paper-reader.css. */

function researchUiLabel(zh: string, en: string): string {
  return _i18nLang === 'zh' ? zh : en;
}

const RECORD_STATUS: Record<string, { dot: string; zh: string; en: string }> = {
  running: { dot: 'is-running', zh: '后台运行中', en: 'Running' },
  pending: { dot: 'is-pending', zh: '等待执行', en: 'Pending' },
  interrupted: { dot: 'is-interrupted', zh: '已中断 · 记录已保存', en: 'Interrupted · saved' },
  done: { dot: 'is-done', zh: '已完成', en: 'Completed' },
  error: { dot: 'is-error', zh: '执行失败 · 记录已保存', en: 'Failed · saved' },
  aborted: { dot: 'is-aborted', zh: '已停止', en: 'Stopped' },
  degraded: { dot: 'is-degraded', zh: '已完成 · 需复核', en: 'Completed · review needed' },
};

function sidebarDate(timestamp: unknown): string {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return '';
  try {
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' })
      .format(new Date(value < 1e12 ? value * 1000 : value));
  } catch {
    return '';
  }
}

function renderResearchSidebarCurrent(host: HTMLElement | null): void {
  if (!host) return;
  const stream = globals()._researchStream;
  if (!stream || !stream.direction) {
    host.innerHTML = '';
    return;
  }
  const statusClass = researchIsRunning(stream) ? 'is-running'
    : stream.status === 'interrupted' ? 'is-interrupted'
      : stream.status === 'error' || stream.status === 'aborted' ? 'is-error'
        : 'is-done';
  host.innerHTML = '<div class="research-sidebar-current-icon">'
    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2v7.5L4.7 20.6A1 1 0 0 0 5.6 22h12.8a1 1 0 0 0 .9-1.4L14 9.5V2"/><path d="M8.5 2h7M7.2 16h9.6"/></svg></div>'
    + '<div class="research-sidebar-current-meta"><span class="research-sidebar-current-title">'
    + escapeHtml(stream.direction) + '</span>'
    + '<span class="research-sidebar-current-status ' + statusClass + '"><i></i>'
    + escapeHtml(researchStatusLabel(stream, translator())) + '</span></div>';
}

export async function renderResearchSidebar(): Promise<void> {
  const list = document.getElementById('researchRecordList');
  if (!list) return;
  renderResearchSidebarCurrent(document.getElementById('researchSidebarCurrent'));
  const stream = globals()._researchStream;
  const count = document.getElementById('researchRecordCount');
  const t = translator();
  const note = (message: string): void => {
    list.innerHTML = '<div class="research-record-note">' + escapeHtml(message) + '</div>';
    if (count) count.textContent = '';
  };
  const itemHtml = (dot: string, title: string, meta: string, date: string, active: boolean): string =>
    '<button type="button" class="conv-item research-record-item' + (active ? ' active' : '') + '" title="' + escapeHtml(title) + '">'
    + '<span class="research-record-dot' + (dot ? ' ' + dot : '') + '"></span>'
    + '<span class="research-record-info"><span class="conv-title">' + escapeHtml(title) + '</span>'
    + '<span class="research-record-meta"><span>' + escapeHtml(meta) + '</span>'
    + (date ? '<time>' + escapeHtml(date) + '</time>' : '') + '</span></span></button>';
  note(tr(t, 'evolution.research.recentLoading', 'Loading research archive…'));
  const api = globals().Api?.research;
  try {
    // Resumable task-registry records first; the durable direction archive is
    // the fallback for builds without the records endpoint.
    if (api?.records) {
      const data = await api.records(50);
      if (!list.isConnected) return;
      if (data.ok !== false) {
        const rows = array(data.items);
        if (rows.length) {
          list.innerHTML = rows.map((row) => {
            const status = RECORD_STATUS[text(row.status)]
              ?? { dot: '', zh: text(row.status), en: text(row.status) };
            const taskId = text(row.task_id);
            return itemHtml(status.dot, text(row.direction) || taskId,
              researchUiLabel(status.zh, status.en), sidebarDate(row.updated_at),
              Boolean(stream?.taskId && stream.taskId === taskId));
          }).join('');
          list.querySelectorAll<HTMLButtonElement>('.research-record-item').forEach((button, index) => {
            button.onclick = () => { void openResearchRecord(text(rows[index].task_id)); };
          });
          if (count) count.textContent = String(rows.length);
          return;
        }
      }
    }
    if (!api?.list) throw new Error('Research list unavailable');
    const data = await api.list(20);
    if (!list.isConnected) return;
    const items = array(data.items);
    if (!items.length) {
      note(researchUiLabel('还没有探索记录 — 从上方的新方向开始。',
        'No exploration records yet — start a new direction above.'));
      return;
    }
    list.innerHTML = items.map((item) => {
      const counts = format(t, 'evolution.research.recentCounts', '{accepted} ideas · {rejected} rejected', {
        accepted: item.accepted || 0, rejected: item.rejected || 0,
      });
      return itemHtml('is-done', text(item.direction), counts, sidebarDate(item.created_at),
        Boolean(stream?.direction && stream.direction === text(item.direction)));
    }).join('');
    list.querySelectorAll<HTMLButtonElement>('.research-record-item').forEach((button, index) => {
      button.onclick = () => {
        void globals()._restoreResearchFromStore?.(text(items[index].direction), text(items[index].lang) || 'en');
      };
    });
    if (count) count.textContent = String(items.length);
  } catch (error: unknown) {
    console.debug('[Research] sidebar records failed:', error);
    note(tr(t, 'evolution.research.recentUnavailable', 'The research archive is temporarily unavailable.'));
  }
}

// paintResearch runs on every poll tick; the current card is cheap enough to
// refresh every time, but the record list only refetches on identity/status
// transitions so a long run cannot hammer the registry endpoint.
let sidebarRecordsKey = '';

function refreshResearchSidebarRail(stream: ResearchStream): void {
  renderResearchSidebarCurrent(document.getElementById('researchSidebarCurrent'));
  const key = `${stream.taskId || ''}:${stream.status}:${stream.direction}`;
  if (key === sidebarRecordsKey) return;
  sidebarRecordsKey = key;
  void renderResearchSidebar();
}

export function showResearchLanding(): void {
  const host = document.getElementById('researchViewer');
  if (!host) return;
  const t = translator();
  host.innerHTML = '<main class="rs-workbench rs-landing">'
    + '<section class="rs-composer">'
    + '<textarea id="paperResearchInput" class="rs-direction-input" rows="3" maxlength="2000" aria-label="'
    + escapeHtml(tr(t, 'evolution.research.directionLabel', 'Research direction')) + '" placeholder="'
    + escapeHtml(tr(t, 'evolution.research.entryPlaceholder', 'Describe the unresolved mechanism, target domain, constraints, and the result that would change your mind.')) + '" '
    + 'data-tofu-action-keydown="if((event.metaKey||event.ctrlKey)&&event.key===\'Enter\')_submitResearchDirection()"></textarea>'
    + '<div class="rs-composer-foot"><span class="rs-composer-hint">'
    + escapeHtml(tr(t, 'evolution.research.directionHint', 'Tip: include a failure mode or a few seed arXiv IDs in the direction.'))
    + '<kbd class="rs-kbd">⌘/Ctrl + Enter</kbd></span>'
    + '<span class="rs-composer-side"><span class="rs-char-count" id="researchCharCount"></span>'
    + '<label class="rs-harvest-picker" for="researchHarvestN"><span>'
    + escapeHtml(tr(t, 'evolution.research.corpusSize', 'Corpus size')) + '</span><select id="researchHarvestN">'
    + [40, 60, 100, 150, 200].map(n => '<option value="' + n + '"' + (n === 60 ? ' selected' : '') + '>'
      + n + '</option>').join('') + '</select></label>'
    + '<button type="button" class="rs-start-btn" data-tofu-action="_submitResearchDirection()">'
    + '<span>' + escapeHtml(tr(t, 'evolution.research.startBtn', 'Launch research pipeline')) + '</span>' + ICONS.arrow + '</button></span></div></section>'
    + '<section class="rs-templates"><span class="rs-section-label">'
    + escapeHtml(tr(t, 'evolution.research.templatesLabel', 'No starting point? Begin from a template'))
    + '</span><div class="rs-template-grid">'
    + [1, 2, 3].map((n) => '<button type="button" class="rs-template-card" data-tofu-action="_fillResearchTemplate('
      + (n - 1) + ')"><span class="rs-template-num">' + String(n).padStart(2, '0') + '</span><p>'
      + escapeHtml(tr(t, 'evolution.research.template' + n, '')) + '</p>' + ICONS.arrow + '</button>').join('')
    + '</div></section>'
    + '</main>';
  renderResearchSidebarCurrent(document.getElementById('researchSidebarCurrent'));
  wireResearchLandingComposer(host);
  window.requestAnimationFrame(() => {
    (document.getElementById('paperResearchInput') as HTMLTextAreaElement | null)?.focus();
  });
}

function wireResearchLandingComposer(host: HTMLElement): void {
  const input = host.querySelector<HTMLTextAreaElement>('#paperResearchInput');
  const counter = host.querySelector<HTMLElement>('#researchCharCount');
  if (!input) return;
  const autosize = (): void => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 260) + 'px';
    if (counter) {
      counter.textContent = input.value.length
        ? `${input.value.length} / ${input.maxLength}` : '';
    }
  };
  input.addEventListener('input', autosize);
  autosize();
}

export function fillResearchTemplate(index: number): void {
  const input = document.getElementById('paperResearchInput') as HTMLTextAreaElement | null;
  if (!input) return;
  const value = tr(translator(), 'evolution.research.template' + (index + 1), '');
  if (!value) return;
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

export function copyResearchArtifact(kind: string, index: number, button?: HTMLElement): void {
  const stream = globals()._researchStream;
  if (!stream) return;
  const t = translator();
  let value = '';
  if (kind === 'survey') value = stream.surveyMd || '';
  if (kind === 'idea') {
    const ideas = [...(stream.acceptedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
    const idea = ideas[index];
    if (idea) value = '# ' + (text(idea.title) || tr(t, 'evolution.research.untitled', '(untitled)'))
      + '\n\n' + researchIdeaBrief(idea, t);
  }
  if (kind === 'ideas') {
    const ideas = [...(stream.acceptedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
    value = ideas.map((idea) => '# ' + (text(idea.title) || tr(t, 'evolution.research.untitled', '(untitled)'))
      + '\n\n' + researchIdeaBrief(idea, t)).join('\n\n---\n\n');
  }
  if (!value) return;
  const write = globals()._safeClipboardWrite;
  const promise = typeof write === 'function'
    ? Promise.resolve(write(value))
    : (navigator.clipboard?.writeText ? navigator.clipboard.writeText(value) : Promise.reject());
  void promise.then(() => {
    if (!button) return;
    const old = button.innerHTML;
    button.innerHTML = ICONS.check + escapeHtml(tr(t, 'evolution.research.copied', 'Copied'));
    window.setTimeout(() => { if (button.isConnected) button.innerHTML = old; }, 1500);
  }).catch((error: unknown) => console.debug('[Research] copy failed:', error));
}

export function newResearchDirection(): void {
  const state = globals();
  // Reuse the runtime owner so an active push subscription and poll timer do
  // not survive behind the landing page. The server job itself remains
  // checkpointed/resumable; this only detaches this browser surface.
  state._destroyResearchRuntime?.();
  state._researchStream = null;
  destroyLiteratureGraph();
  sidebarRecordsKey = '';
  showResearchLanding();
}

// Thread kinds in pipeline order; the run view renders one chronological
// entry per kind and the filter chips spotlight a single kind. Surface and
// filter selection survive re-paints (hydration repaints a finished run) and
// reset only when a different direction is shown.
const RESEARCH_THREAD_KINDS = ['papers', 'evidence', 'ideas', 'review', 'ledger'] as const;
type ResearchThreadKind = (typeof RESEARCH_THREAD_KINDS)[number];
let activeResearchTab = 'thread';
let activeResearchFilter = 'all';
let activeResearchDirection = '';

export function switchResearchTab(id: string): void {
  if (!['thread', 'workspace', 'paper'].includes(id)) return;
  activeResearchTab = id;
  const shell = document.querySelector('[data-research-shell]');
  if (!shell) return;
  shell.setAttribute('data-research-page', id);
  shell.querySelectorAll('[data-rs-tab]').forEach((node) => {
    const selected = node.getAttribute('data-rs-tab') === id;
    node.classList.toggle('is-active', selected);
    node.setAttribute('aria-pressed', String(selected));
  });
  const thread = shell.querySelector<HTMLElement>('[data-rs-thread]');
  if (thread) thread.hidden = id !== 'thread';
  const workspace = shell.querySelector<HTMLElement>('[data-rs-panel="workspace"]');
  if (workspace) workspace.hidden = id === 'thread';
  setResearchManuscriptVisible(id === 'paper');
}

export function filterResearchThread(kind: string): void {
  if (kind !== 'all' && !(RESEARCH_THREAD_KINDS as readonly string[]).includes(kind)) return;
  activeResearchFilter = kind;
  const shell = document.querySelector('[data-research-shell]');
  if (!shell) return;
  if (activeResearchTab !== 'thread') switchResearchTab('thread');
  shell.setAttribute('data-research-filter', kind);
  shell.querySelectorAll('[data-rs-filter]').forEach((node) => {
    const selected = node.getAttribute('data-rs-filter') === kind;
    node.classList.toggle('is-active', selected);
    node.setAttribute('aria-pressed', String(selected));
  });
}

function researchLedgerHtml(stream: ResearchStream, t: Translator): string {
  return '<section class="rs-panel rs-ledger"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'evolution.research.ledgerKicker', 'Reproducibility ledger')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.ledgerTitle', 'Corpus, execution and resource record')) + '</h2></div>'
    + (stream.folderId ? '<button type="button" class="rs-btn is-secondary" data-tofu-action="_openResearchFolder()">'
      + escapeHtml(tr(t, 'evolution.research.openFolder', 'Open paper library')) + ICONS.external + '</button>' : '')
    + '</div>' + researchCorpusHtml(stream, t) + researchUsageHtml(stream, t) + '</section>';
}

function researchThreadBadge(kind: ResearchThreadKind, stream: ResearchStream): string {
  if (kind === 'papers') return stream.papers?.length ? String(stream.papers.length) : '';
  if (kind === 'evidence') {
    const gaps = array(stream.openGaps?.open_gaps).length;
    return gaps ? String(gaps) : '';
  }
  if (kind === 'ideas') return stream.accepted ? String(stream.accepted) : '';
  if (kind === 'review') {
    const score = number(stream.evaluation?.overall_score);
    return score == null ? '' : score.toFixed(1);
  }
  return '';
}

function researchNavHtml(kinds: ResearchThreadKind[], stream: ResearchStream, t: Translator): string {
  const chip = (id: string, label: string, badge: string): string => '<button type="button" class="rs-chip'
    + (id === activeResearchFilter ? ' is-active' : '') + '" aria-pressed="' + String(id === activeResearchFilter)
    + '" data-tofu-action="_filterResearchThread(\'' + id + '\')" data-rs-filter="' + id + '"><span>'
    + escapeHtml(label) + '</span>' + (badge ? '<b>' + escapeHtml(badge) + '</b>' : '') + '</button>';
  const chips = chip('all', tr(t, 'evolution.research.filterAll', 'All'), '')
    + kinds.map((kind) => chip(kind, tr(t, 'evolution.research.kind' + kind[0].toUpperCase() + kind.slice(1), kind),
      researchThreadBadge(kind, stream))).join('');
  const tabs = [
    { id: 'thread', label: tr(t, 'evolution.research.tabThread', 'Thread') },
    { id: 'workspace', label: tr(t, 'evolution.research.tabExperiment', 'Experiment') },
    { id: 'paper', label: tr(t, 'evolution.research.tabPaper', 'Paper') },
  ].map((tab) => '<button type="button" class="rs-tab-btn'
    + (tab.id === activeResearchTab ? ' is-active' : '') + '" aria-pressed="' + String(tab.id === activeResearchTab)
    + '" data-tofu-action="_switchResearchTab(\'' + tab.id + '\')" data-rs-tab="' + tab.id + '"><span>'
    + escapeHtml(tab.label) + '</span></button>').join('');
  return '<nav class="rs-nav"><div class="rs-chip-group" role="group">' + chips
    + '</div><div class="rs-tab-group" role="group">' + tabs + '</div></nav>';
}

function researchThreadEntry(kind: string, content: string, live = false): string {
  return '<div class="rs-thread-entry' + (live ? ' is-live' : '') + '" data-rs-kind="' + kind + '">'
    + '<div class="rs-thread-rail"><i></i></div><div class="rs-thread-body">' + content + '</div></div>';
}

function researchThreadHtml(stream: ResearchStream, t: Translator, runningBody: string): { html: string; kinds: ResearchThreadKind[] } {
  const entries: Array<[ResearchThreadKind, string]> = [
    ['papers', paperCards(stream, t)],
    ['evidence', researchSurveyHtml(stream, t)],
    ['ideas', researchCycleHtml(stream, t) + researchIdeasHtml(stream, t) + researchRejectedHtml(stream, t)],
    ['review', researchEvaluationHtml(stream, t)],
    ['ledger', researchDeliverablesHtml(stream, t) + researchLedgerHtml(stream, t) + researchToolsHtml(stream)],
  ];
  const kinds: ResearchThreadKind[] = [];
  let html = '';
  entries.forEach(([kind, content]) => {
    if (!content) return;
    kinds.push(kind);
    html += researchThreadEntry(kind, content);
  });
  if (researchIsRunning(stream)) html += researchThreadEntry('live', runningBody, true);
  return { html, kinds };
}

function researchHeader(stream: ResearchStream, t: Translator): string {
  const statusClass = stream.status === 'error' ? 'is-error'
    : stream.degraded || (stream.researchCycle?.status && stream.researchCycle.status !== 'complete') ? 'is-degraded'
      : researchIsRunning(stream) ? 'is-running' : 'is-ready';
  return '<header class="rs-run-head"><div class="rs-run-title"><button type="button" class="rs-back-btn" '
    + 'data-tofu-action="_newResearchDirection()" title="'
    + escapeHtml(tr(t, 'evolution.research.backToWorkbench', 'Start a new direction')) + '">←</button>'
    + '<div><div class="rs-eyebrow">' + escapeHtml(tr(t, 'evolution.research.activeProgram', 'Active research program'))
    + '</div><h1>' + escapeHtml(stream.direction) + '</h1><p>'
    + escapeHtml(tr(t, 'evolution.research.subtitle', 'Literature → gap map → novelty gate → independent review'))
    + '</p></div></div><div class="rs-run-meta"><span role="status" class="rs-status ' + statusClass + '"><i></i>'
    + escapeHtml(stream.status === 'interrupted' ? (_i18nLang === 'zh' ? '已中断 · 记录已保存' : 'Interrupted · saved') : researchStatusLabel(stream, t)) + '</span>'
    + (stream.status === 'interrupted' ? '<button type="button" id="researchResumeSaved" class="rs-btn">' + (_i18nLang === 'zh' ? '从已保存阶段继续' : 'Resume saved stages') + '</button>' : '')
    + (researchTimestamp(stream.startedAt) ? '<time>' + escapeHtml(researchTimestamp(stream.startedAt)) + '</time>' : '')
    + (researchIsRunning(stream) && stream.taskId && !stream.cancelling ? '<button type="button" class="rs-stop-btn" data-tofu-action="_abortResearchJob()">'
      + escapeHtml(tr(t, 'evolution.research.abort', 'Stop run')) + '</button>' : '')
    + '</div></header>';
}

export function paintResearch(): void {
  const stream = globals()._researchStream;
  if (!stream) return;
  const viewer = document.getElementById('researchViewer');
  if (!viewer) return;
  const t = translator();
  const quality = stream.degraded
    ? '<section class="rs-alert is-warning" role="alert"><div class="rs-alert-title">'
      + escapeHtml(tr(t, 'evolution.research.degraded', 'Delivered, but the pipeline was degraded')) + '</div>'
      + '<div class="rs-alert-detail">' + escapeHtml(stream.degradedReason || tr(t, 'evolution.research.degradedFallback', 'Inspect the execution trace before using this artifact.')) + '</div></section>'
    : '';
  const liveStat = (label: string, value: string): string => '<div class="rs-live-stat"><span>'
    + escapeHtml(label) + '</span><strong>' + escapeHtml(value) + '</strong></div>';
  const runningBody = '<section class="rs-live-grid"><div class="rs-live-copy"><div class="rs-live-pulse"><i></i>'
    + escapeHtml(tr(t, 'evolution.research.running', 'Researching…')) + '</div><h2>'
    + escapeHtml(tr(t, 'evolution.research.runningTitle', 'The foundry is building your evidence base.')) + '</h2><p>'
    + escapeHtml(tr(t, 'evolution.research.runningBody', 'The page updates as papers are harvested, compared and challenged. You can leave this page; the checkpointed job keeps running.'))
    + '</p></div><div class="rs-live-stat-stack">'
    + liveStat(tr(t, 'evolution.research.currentPhase', 'Current phase'),
      tr(t, 'evolution.research.' + (stream.phase || 'harvest'), stream.phase || 'harvest'))
    + liveStat(tr(t, 'evolution.research.elapsed', 'Elapsed'), researchElapsed(stream) || '—')
    + (stream.progress ? Object.entries(stream.progress.counts).filter(([key, count]) => ['discovered', 'admitted', 'failed'].includes(key) && count !== null).map(([key, count]) =>
        liveStat(tr(t, 'evolution.research.count.' + key, key), String(count))).join('') : '')
    + liveStat(tr(t, 'evolution.research.evidenceActions', 'Evidence actions'), String(stream.toolRounds.length))
    + '</div></section>';
  const thread = researchThreadHtml(stream, t, runningBody);
  if (activeResearchDirection !== stream.direction) {
    activeResearchDirection = stream.direction;
    activeResearchTab = 'thread';
    activeResearchFilter = 'all';
  }
  if (activeResearchFilter !== 'all' && !thread.kinds.includes(activeResearchFilter as ResearchThreadKind)) {
    activeResearchFilter = 'all';
  }
  patchResearchDom(viewer, '<main class="rs-workbench rs-console" data-research-shell="1" data-research-page="'
    + activeResearchTab + '" data-research-filter="' + activeResearchFilter + '">'
    + researchHeader(stream, t)
    + researchPipelineHtml(stream, t)
    + (stream.commandError ? '<p role="alert" class="rs-alert is-error">' + escapeHtml(stream.commandError) + '</p>' : '')
    + researchFailureHtml(stream, t)
    + quality
    + researchNavHtml(thread.kinds, stream, t)
    + '<div class="rs-thread" data-rs-thread' + (activeResearchTab !== 'thread' ? ' hidden' : '') + '>' + thread.html + '</div>'
    + '<div class="rs-workspace-panel" data-rs-panel="workspace"' + (activeResearchTab === 'thread' ? ' hidden' : '') + '>'
    + researchWorkspaceHtml(stream) + '</div>'
    + '</main>');
  switchResearchTab(activeResearchTab);
  bindPaperCards(viewer, stream, paintResearch, t);
  const retry = viewer.querySelector<HTMLButtonElement>('#researchRetryDirection');
  if (retry) retry.onclick = () => { retry.disabled = true; void startResearchJob(stream.direction); };
  refreshResearchSidebarRail(stream);
  const resume = viewer.querySelector<HTMLButtonElement>('#researchResumeSaved');
  if (resume) resume.onclick = () => { resume.disabled = true; void resumeSavedResearch().finally(() => { resume.disabled = false; }); };

  void loadResearchWorkspace(stream.direction, stream.lang);
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
  target._researchPipelineHtml = researchPipelineHtml;
  target._researchDeliverablesHtml = researchDeliverablesHtml;
  target._copyResearchArtifact = copyResearchArtifact;
  target._newResearchDirection = newResearchDirection;
  target._fillResearchTemplate = fillResearchTemplate;
  target._renderResearchSidebar = renderResearchSidebar;
  target._paintResearch = paintResearch;
  target._showResearchLanding = showResearchLanding;
  target._switchResearchTab = switchResearchTab;
  target._filterResearchThread = filterResearchThread;
}

installResearchViewGlobals();
