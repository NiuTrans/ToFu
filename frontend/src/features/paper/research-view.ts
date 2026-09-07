import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import { researchIsRunning, type ResearchStream } from './research-runtime';

import { loadResearchWorkspace, researchWorkspaceHtml } from './research-workspace';

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
  _researchPipelineHtml?: typeof researchPipelineHtml;
  _researchDeliverablesHtml?: typeof researchDeliverablesHtml;
  _copyResearchArtifact?: typeof copyResearchArtifact;
  _newResearchDirection?: typeof newResearchDirection;
  _fillResearchTemplate?: typeof fillResearchTemplate;

  _destroyResearchRuntime?: () => void;
  _renderRecentResearch?: typeof renderRecentResearch;
  _paintResearch?: typeof paintResearch;
  _showResearchLanding?: typeof showResearchLanding;
  _switchResearchTab?: typeof switchResearchTab;
};

const RESEARCH_PHASES = ['harvest', 'survey', 'ideate', 'evaluate', 'publish'] as const;
type ResearchPhase = typeof RESEARCH_PHASES[number];

const ICONS = {
  flask: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 2v7.5L4.7 20.6A1 1 0 0 0 5.6 22h12.8a1 1 0 0 0 .9-1.4L14 9.5V2"/><path d="M8.5 2h7M7.2 16h9.6"/></svg>',
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
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function researchStageIndex(phase: string): number {
  if (phase === 'done') return RESEARCH_PHASES.length;
  const index = RESEARCH_PHASES.indexOf(phase as ResearchPhase);
  return index < 0 ? 0 : index;
}

function researchStatusLabel(stream: ResearchStream, t: Translator): string {
  if (stream.status === 'error') return tr(t, 'paper.research.statusError', 'Needs attention');
  if (stream.status === 'aborted') return tr(t, 'paper.research.statusAborted', 'Stopped');
  if (stream.degraded) return tr(t, 'paper.research.statusDegraded', 'Delivered with warnings');
  if (researchIsRunning(stream)) return tr(t, 'paper.research.statusRunning', 'Pipeline running');
  return tr(t, 'paper.research.statusReady', 'Decision packet ready');
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
      ? tr(t, 'paper.research.runFailed', 'This run could not finish')
      : tr(t, 'paper.research.runStopped', 'This run was stopped'))
    + '</div>'
    + (stream.error
      ? '<div class="rs-alert-detail">' + escapeHtml(stream.error) + '</div>'
      : '')
    + '<button type="button" class="rs-btn is-secondary" data-tofu-action="_newResearchDirection()">'
    + escapeHtml(tr(t, 'paper.research.backToWorkbench', 'Start a new direction')) + '</button>'
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
    novelty: tr(t, 'paper.research.scoreNovelty', 'Novelty'),
    falsifiability: tr(t, 'paper.research.scoreFalsifiability', 'Falsifiability'),
    mechanism_depth: tr(t, 'paper.research.scoreMechanism', 'Mechanism'),
    value: tr(t, 'paper.research.scoreValue', 'Value'),
    survey_coverage: tr(t, 'paper.research.scoreCoverage', 'Coverage'),
    evidence_traceability: tr(t, 'paper.research.scoreTraceability', 'Traceability'),
    gap_specificity: tr(t, 'paper.research.scoreGap', 'Gap specificity'),
    synthesis_quality: tr(t, 'paper.research.scoreSynthesis', 'Synthesis'),
    idea_relevance: tr(t, 'paper.research.scoreRelevance', 'Relevance'),
    idea_mechanism_depth: tr(t, 'paper.research.scoreMechanism', 'Mechanism'),
    idea_falsifiability: tr(t, 'paper.research.scoreFalsifiability', 'Falsifiability'),
    gate_selectivity: tr(t, 'paper.research.scoreSelectivity', 'Selectivity'),
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
      + escapeHtml(tr(t, 'paper.research.overall', 'Overall')) + '</span><strong>'
      + escapeHtml(overallScore.toFixed(2)) + '</strong><small>/ 5</small></div>')
    + '</div>';
}

function ideaRank(idea: JsonObject): number {
  return number(idea.overall) ?? -1;
}

function researchIdeaBrief(idea: JsonObject, t: Translator): string {
  const fields: [string, unknown][] = [
    [tr(t, 'paper.research.mechanism', 'Mechanism'), idea.core_mechanism],
    [tr(t, 'paper.research.novelty', 'Novelty claim'), idea.novelty_claim],
    [tr(t, 'paper.research.prediction', 'Falsifiable prediction'), idea.falsifiable_prediction],
    [tr(t, 'paper.research.corpusAnchor', 'Corpus anchor'), idea.corpus_anchor_id],
    [tr(t, 'paper.research.corpusDelta', 'Delta from prior work'), idea.corpus_delta],
    [tr(t, 'paper.research.newInvariant', 'New invariant'), idea.new_invariant],
    [tr(t, 'paper.research.whyNotAB', 'Why this is not A+B'), idea.why_not_AB],
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
    + escapeHtml(tr(t, 'paper.research.ideaReviewTitle', 'Review board on this idea')) + '</span>'
    + (score == null ? '' : '<strong>' + escapeHtml(score.toFixed(2)) + '<small> / 5</small></strong>')
    + '</div>'
    + verdicts.map((verdict) => '<p>' + escapeHtml(verdict) + '</p>').join('')
    + (risks.length
      ? '<div class="rs-idea-risk"><span>' + escapeHtml(tr(t, 'paper.research.mainRisk', 'Main risk'))
        + '</span>' + escapeHtml(risks.join(' · ')) + '</div>'
      : '')
    + '</div>';
}

function researchIdeaCard(idea: JsonObject, index: number, t: Translator, stream: ResearchStream): string {
  const title = text(idea.title) || tr(t, 'paper.research.untitled', '(untitled)');
  const overall = number(idea.overall);
  const linkedGap = text(idea.linked_gap_id);
  const secondary = researchField(tr(t, 'paper.research.corpusAnchor', 'Corpus anchor'), idea.corpus_anchor_id)
    + researchField(tr(t, 'paper.research.corpusDelta', 'Delta from prior work'), idea.corpus_delta)
    + researchField(tr(t, 'paper.research.newInvariant', 'New invariant'), idea.new_invariant)
    + researchField(tr(t, 'paper.research.whyNotAB', 'Why this is not A+B'), idea.why_not_AB);
  return '<article class="rs-idea-card" data-research-idea="' + index + '">'
    + '<div class="rs-idea-card-head"><div class="rs-idea-rank">#' + (index + 1) + '</div>'
    + '<div class="rs-idea-heading"><h3>' + escapeHtml(title) + '</h3>'
    + '<div class="rs-idea-tags">'
    + (idea.kind ? '<span>' + escapeHtml(idea.kind) + '</span>' : '')
    + (linkedGap ? '<span>' + escapeHtml(linkedGap) + '</span>' : '')
    + '</div></div>'
    + (overall == null ? '' : '<div class="rs-idea-score"><strong>'
      + escapeHtml(overall.toFixed(2)) + '</strong><span>/5</span></div>')
    + '</div>'
    + '<div class="rs-idea-grid">'
    + researchField(tr(t, 'paper.research.mechanism', 'Mechanism'), idea.core_mechanism)
    + researchField(tr(t, 'paper.research.novelty', 'Novelty claim'), idea.novelty_claim)
    + researchField(tr(t, 'paper.research.prediction', 'Falsifiable prediction'), idea.falsifiable_prediction)
    + '</div>'
    + (secondary
      ? '<details class="rs-idea-more"><summary><span>'
        + escapeHtml(tr(t, 'paper.research.ideaMoreFields', 'Supporting arguments'))
        + '</span><span class="rs-details-toggle">＋</span></summary>'
        + '<div class="rs-idea-grid is-secondary">' + secondary + '</div></details>'
      : '')
    + researchIdeaReviewHtml(stream, idea, t)
    + researchScoresHtml(idea.scores, null, t)
    + '<div class="rs-idea-actions"><button type="button" class="rs-link-btn" '
    + 'data-tofu-action="_copyResearchArtifact(\'idea\',' + index + ',this)">'
    + ICONS.copy + escapeHtml(tr(t, 'paper.research.copyBrief', 'Copy experiment brief'))
    + '</button></div>'
    + '</article>';
}

export function researchIdeasHtml(stream: ResearchStream, t: Translator): string {
  const ideas = [...(stream.acceptedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
  if (!ideas.length) {
    return '<section class="rs-panel rs-ideas"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
      + escapeHtml(tr(t, 'paper.research.candidatesKicker', 'Candidate program')) + '</div><h2>'
      + escapeHtml(tr(t, 'paper.research.acceptedTitle', 'Ideas that cleared the gate')) + '</h2></div></div>'
      + '<div class="rs-empty"><strong>'
      + escapeHtml(tr(t, 'paper.research.noIdeasTitle', 'The gate rejected every candidate'))
      + '</strong><span>' + escapeHtml(tr(t, 'paper.research.noIdeas', 'No idea cleared the gate this run — an honest zero, not a failure.'))
      + '</span></div></section>';
  }
  return '<section class="rs-panel rs-ideas"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'paper.research.candidatesKicker', 'Candidate program')) + '</div><h2>'
    + escapeHtml(tr(t, 'paper.research.acceptedTitle', 'Ideas that cleared the gate')) + '</h2></div>'
    + '<div class="rs-panel-actions"><button type="button" class="rs-link-btn" '
    + 'data-tofu-action="_copyResearchArtifact(\'ideas\',0,this)">'
    + ICONS.copy + escapeHtml(tr(t, 'paper.research.copyAllBriefs', 'Copy all briefs'))
    + '</button><span class="rs-count">' + ideas.length + '</span></div></div>'
    + '<p class="rs-panel-intro">'
    + escapeHtml(tr(t, 'paper.research.candidatesHint', 'Ranked by the novelty gate. Open the mechanism, falsifier and evidence delta before committing compute.'))
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
  const summary = format(t, 'paper.research.rejectedSummary',
    '{n} rejected · best {best} / threshold {threshold}', {
      n: rejected.length,
      best: best == null ? '—' : best.toFixed(2),
      threshold: stream.threshold == null ? '—' : stream.threshold,
    });
  const rows = rejected.map((row, index) => (
    '<article class="rs-rejected-row"><span class="rs-rejected-index">' + (index + 1) + '</span>'
      + '<div><strong>' + escapeHtml(row.title || tr(t, 'paper.research.untitled', '(untitled)')) + '</strong>'
      + '<p>' + escapeHtml(row.reject_reason || tr(t, 'paper.research.rejectReasonUnknown', 'Did not clear the structural or novelty gate.')) + '</p>'
      + '<span>' + escapeHtml(row.reject_stage || '')
      + (number(row.overall) == null ? '' : ' · ' + escapeHtml((number(row.overall) ?? 0).toFixed(2)) + '/5')
      + '</span></div></article>'
  )).join('');
  return '<details class="rs-panel rs-rejections"><summary><span><b>'
    + escapeHtml(tr(t, 'paper.research.rejectionAudit', 'Rejection audit')) + '</b><small>'
    + escapeHtml(summary) + '</small></span><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-rejected-list">' + rows + '</div></details>';
}

function researchGapCards(stream: ResearchStream, t: Translator): string {
  const gaps = array(stream.openGaps?.open_gaps);
  if (!gaps.length) return '';
  return '<div class="rs-gap-grid">' + gaps.map((gap, index) => (
    '<article class="rs-gap-card"><div class="rs-gap-num">G' + String(index + 1).padStart(2, '0') + '</div>'
      + '<div><strong>' + escapeHtml(gap.gap || '') + '</strong>'
      + (gap.why_open ? '<p>' + escapeHtml(gap.why_open) + '</p>' : '')
      + (Array.isArray(gap.evidence) && gap.evidence.length
        ? '<div class="rs-gap-evidence">' + escapeHtml(tr(t, 'paper.research.evidence', 'Evidence'))
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
    + escapeHtml(tr(t, 'paper.research.evidenceKicker', 'Evidence synthesis')) + '</div><h2>'
    + escapeHtml(tr(t, 'paper.research.gapsTitle', 'Open-gap map')) + '</h2></div>'
    + (stream.surveyMd ? '<button type="button" class="rs-link-btn" data-tofu-action="_copyResearchArtifact(\'survey\',0,this)">'
      + ICONS.copy + escapeHtml(tr(t, 'paper.research.copySurvey', 'Copy survey')) + '</button>' : '')
    + '</div>' + gaps
    + (markdown ? '<details class="rs-survey"><summary><span>'
      + escapeHtml(tr(t, 'paper.research.surveyTitle', 'Full survey'))
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
    + escapeHtml(format(t, 'paper.research.corpusTitle', '{n} papers read in this run', { n: ids.length }))
    + '</span><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-paper-list">' + rows + '</div></details>';
}

function researchIdeaAssessmentsHtml(stream: ResearchStream, t: Translator): string {
  const rows = ideaAssessments(stream);
  if (!rows.length) return '';
  return '<div class="rs-review-ideas"><h3>'
    + escapeHtml(tr(t, 'paper.research.evaluationIdeasTitle', 'Challenges by idea')) + '</h3>'
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
          ? '<div class="rs-idea-risk"><span>' + escapeHtml(tr(t, 'paper.research.mainRisk', 'Main risk'))
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
  const judges = format(t, 'paper.research.judgeConsensus', '{n} judges · {consensus}', {
    n: number(evaluation.judge_count) ?? 0,
    consensus: evaluation.consensus || 'unavailable',
  });
  return '<section class="rs-panel rs-evaluation' + (evaluation.degraded ? ' is-degraded' : '') + '">'
    + '<div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'paper.research.evaluationKicker', 'Independent review board')) + '</div><h2>'
    + escapeHtml(tr(t, 'paper.research.evaluationTitle', 'Artifact quality review')) + '</h2></div>'
    + '<div class="rs-review-decision ' + (worthFollowing ? 'is-go' : 'is-hold') + '">'
    + escapeHtml(worthFollowing
      ? tr(t, 'paper.research.followUpYes', 'Worth a follow-up experiment')
      : tr(t, 'paper.research.followUpNo', 'Revise before spending compute')) + '</div></div>'
    + '<p class="rs-panel-intro">'
    + escapeHtml(format(t, 'paper.research.evaluationScope',
      'Review target: the whole decision packet — survey + {n} ranked ideas + the novelty gate. Per-idea challenges are listed below.', {
        n: stream.acceptedIdeas?.length || stream.accepted || 0,
      }))
    + '</p>'
    + '<div class="rs-review-grid"><div class="rs-review-score"><span>'
    + escapeHtml(tr(t, 'paper.research.overall', 'Overall')) + '</span><strong>'
    + escapeHtml(score == null ? '—' : score.toFixed(2)) + '</strong><small>/ 5</small>'
    + '<em>' + escapeHtml(judges) + '</em></div>'
    + '<div class="rs-review-main">'
    + (evaluation.verdict ? '<p class="rs-review-verdict">' + escapeHtml(evaluation.verdict) + '</p>' : '')
    + researchScoresHtml(evaluation.scores, null, t)
    + '</div></div>'
    + ((strengths.length || failureModes.length) ? '<div class="rs-review-columns">'
      + '<div><h3>' + escapeHtml(tr(t, 'paper.research.strengths', 'What survived scrutiny')) + '</h3><ul>'
      + strengths.map((item) => '<li>' + escapeHtml(item) + '</li>').join('') + '</ul></div>'
      + '<div><h3>' + escapeHtml(tr(t, 'paper.research.failureModes', 'Main failure modes')) + '</h3><ul>'
      + failureModes.map((item) => {
        const code = String(item);
        return '<li data-failure-mode="' + escapeHtml(code) + '">'
          + escapeHtml(code.replace(/_/g, ' ')) + '</li>';
      }).join('') + '</ul></div>'
      + '</div>' : '')
    + (changes.length ? '<div class="rs-review-actions"><h3>'
      + escapeHtml(tr(t, 'paper.research.recommendedChanges', 'Next revision queue')) + '</h3>'
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
    + escapeHtml(tr(t, 'paper.research.' + name, name)) + '</span><span>'
    + escapeHtml(format(t, 'paper.research.usageLine',
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
  const summary = format(t, 'paper.research.usageSummary',
    '{calls} calls · {input} input · {output} output · {cost}', {
      calls: usageNumber(total.calls), input: usageNumber(total.prompt_tokens),
      output: usageNumber(total.completion_tokens), cost: priceText,
    });
  const stages = object(usage?.stages);
  return '<details class="rs-usage"><summary><span>'
    + escapeHtml(tr(t, 'paper.research.usageTitle', 'Resource usage')) + '</span><small>'
    + escapeHtml(summary) + '</small><span class="rs-details-toggle">＋</span></summary>'
    + '<div class="rs-usage-body">'
    + usageStageRow('harvest', stages?.harvest, t)
    + usageStageRow('survey', stages?.survey, t)
    + usageStageRow('ideate', stages?.ideate, t)
    + usageStageRow('evaluate', stages?.evaluate, t)
    + (total.forced_final ? '<p>' + escapeHtml(tr(t, 'paper.research.usageForced', 'The adaptive guard finalized from collected evidence.')) + '</p>' : '')
    + (incomplete ? '<p>' + escapeHtml(format(t, 'paper.research.usageIncomplete',
      '{n} calls were not metered; the displayed cost is a lower bound.', { n: total.unmetered_calls })) + '</p>' : '')
    + '</div></details>';
}

export function researchToolsHtml(stream: ResearchStream): string {
  const render = globals().renderToolRoundsHTML;
  if (!stream.toolRounds.length || !render) return '';
  return '<section class="rs-panel rs-trace"><div class="rs-panel-head"><div><div class="rs-eyebrow">Execution trace</div><h2>Live evidence trail</h2></div></div>'
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
  const progress = researchIsRunning(stream)
    ? Math.max(6, Math.min(94, ((researchStageIndex(stream.phase) + 0.45) / RESEARCH_PHASES.length) * 100))
    : (stream.status === 'done' ? 100 : (researchStageIndex(stream.phase) / RESEARCH_PHASES.length) * 100);
  const steps = RESEARCH_PHASES.map((phase, index) => {
    const state = phaseState(stream, phase);
    return '<div class="rs-pipeline-step is-' + state + '">'
      + '<div class="rs-pipeline-node">' + (state === 'done' ? ICONS.check : String(index + 1)) + '</div>'
      + '<div><strong>' + escapeHtml(tr(t, 'paper.research.' + phase, phase)) + '</strong><span>'
      + escapeHtml(tr(t, 'paper.research.phase.' + phase, '')) + '</span></div></div>';
  }).join('');
  return '<section class="rs-pipeline" aria-label="'
    + escapeHtml(tr(t, 'paper.research.pipelineTitle', 'Research pipeline')) + '">'
    + '<div class="rs-pipeline-head"><div><span>'
    + escapeHtml(tr(t, 'paper.research.pipelineTitle', 'Research pipeline')) + '</span><strong>'
    + escapeHtml(researchStatusLabel(stream, t)) + '</strong></div>'
    + (researchElapsed(stream) ? '<time>' + escapeHtml(researchElapsed(stream)) + '</time>' : '')
    + '</div><div class="rs-pipeline-track"><span style="width:' + progress.toFixed(1) + '%"></span></div>'
    + '<div class="rs-pipeline-steps">' + steps + '</div></section>';
}

export function researchDeliverablesHtml(stream: ResearchStream, t: Translator): string {
  if (researchIsRunning(stream)) return '';
  const evaluation = stream.evaluation;
  const rows = [
    {
      className: 'is-candidate',
      label: tr(t, 'paper.research.deliverableCandidates', 'Candidate papers'),
      value: format(t, 'paper.research.deliverableCandidatesValue', '{n} ranked directions', { n: stream.accepted }),
      detail: tr(t, 'paper.research.deliverableCandidatesHint', 'Mechanism, novelty delta and falsifiable prediction'),
    },
    {
      className: 'is-evidence',
      label: tr(t, 'paper.research.deliverableEvidence', 'Evidence packet'),
      value: format(t, 'paper.research.deliverableEvidenceValue', '{papers} papers · {gaps} open gaps', {
        papers: stream.corpusSize || stream.corpusIds.length,
        gaps: array(stream.openGaps?.open_gaps).length,
      }),
      detail: tr(t, 'paper.research.deliverableEvidenceHint', 'Corpus, comparison survey and gap map'),
    },
    {
      className: evaluation?.worth_following_up ? 'is-go' : 'is-hold',
      label: tr(t, 'paper.research.deliverableDecision', 'Review decision'),
      value: evaluation?.worth_following_up
        ? tr(t, 'paper.research.followUpYes', 'Worth a follow-up experiment')
        : tr(t, 'paper.research.followUpNo', 'Revise before spending compute'),
      detail: number(evaluation?.overall_score) == null
        ? tr(t, 'paper.research.evaluationUnavailable', 'Review unavailable')
        : `${(number(evaluation?.overall_score) ?? 0).toFixed(2)} / 5`,
    },
  ];
  const usageTotal = object(stream.usage?.total);
  if (usageTotal && Number(usageTotal.calls)) {
    rows.push({
      className: 'is-usage',
      label: tr(t, 'paper.research.deliverableUsage', 'Resource usage'),
      value: format(t, 'paper.research.deliverableUsageValue', '{calls} calls · {cost}', {
        calls: usageNumber(usageTotal.calls),
        cost: usagePriceText(usageTotal),
      }),
      detail: tr(t, 'paper.research.deliverableUsageHint', 'Per-stage tokens and cache detail in the ledger'),
    });
  }
  return '<section class="rs-deliverables">' + rows.map((row) => (
    '<article class="rs-deliverable ' + row.className + '"><span>' + escapeHtml(row.label)
      + '</span><strong>' + escapeHtml(row.value) + '</strong><small>' + escapeHtml(row.detail) + '</small></article>'
  )).join('') + '</section>';
}

function recentDate(timestamp: unknown): string {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return '';
  try {
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' })
      .format(new Date(value < 1e12 ? value * 1000 : value));
  } catch {
    return '';
  }
}

export async function renderRecentResearch(): Promise<void> {
  const host = document.getElementById('paperRecentResearch');
  if (!host) return;
  const t = translator();
  host.innerHTML = '<div class="rs-recent-loading">'
    + escapeHtml(tr(t, 'paper.research.recentLoading', 'Loading research archive…')) + '</div>';
  let data: JsonObject;
  try {
    const list = globals().Api?.research?.list;
    if (!list) throw new Error('Research list unavailable');
    data = await list(20);
  } catch (error: unknown) {
    console.debug('[Research] recent list failed:', error);
    host.innerHTML = '<div class="rs-recent-error">'
      + escapeHtml(tr(t, 'paper.research.recentUnavailable', 'The research archive is temporarily unavailable.')) + '</div>';
    return;
  }
  const items = array(data.items);
  if (!items.length) {
    host.innerHTML = '<div class="rs-recent-empty">'
      + escapeHtml(tr(t, 'paper.research.recentEmpty', 'Completed runs will appear here as a reusable research archive.')) + '</div>';
    return;
  }
  const rows = items.map((item, index) => {
    const counts = format(t, 'paper.research.recentCounts', '{accepted} ideas · {rejected} rejected', {
      accepted: item.accepted || 0, rejected: item.rejected || 0,
    });
    const direction = JSON.stringify(String(item.direction || '')).replace(/"/g, '&quot;');
    const lang = JSON.stringify(String(item.lang || 'en')).replace(/"/g, '&quot;');
    return '<button type="button" class="rs-recent-item" data-tofu-action="_restoreResearchFromStore('
      + direction + ',' + lang + ')"><span class="rs-recent-index">'
      + String(index + 1).padStart(2, '0') + '</span><span class="rs-recent-copy"><strong>'
      + escapeHtml(item.direction) + '</strong><small>' + escapeHtml(counts)
      + (item.degraded ? ' · ' + escapeHtml(tr(t, 'paper.research.degraded', 'degraded')) : '')
      + '</small></span><span class="rs-recent-date">' + escapeHtml(recentDate(item.created_at))
      + '</span>' + ICONS.arrow + '</button>';
  }).join('');
  host.innerHTML = '<div class="rs-recent-head"><div><span>'
    + escapeHtml(tr(t, 'paper.research.recentTitle', 'Research archive')) + '</span><small>'
    + escapeHtml(tr(t, 'paper.research.recentHint', 'Reopen evidence and decisions without remembering the exact wording'))
    + '</small></div><span class="rs-recent-total">' + items.length + '</span></div>'
    + '<div class="rs-recent-list">' + rows + '</div>';
}

function landingCapability(
  numberText: string,
  title: string,
  detail: string,
): string {
  return '<article class="rs-capability"><span>' + escapeHtml(numberText) + '</span><div><strong>'
    + escapeHtml(title) + '</strong><p>' + escapeHtml(detail) + '</p></div></article>';
}

export function showResearchLanding(): void {
  const host = document.getElementById('researchViewer');
  if (!host) return;
  const t = translator();
  host.innerHTML = '<main class="rs-workbench rs-landing">'
    + '<section class="rs-hero"><div class="rs-hero-grid"></div><div class="rs-hero-copy">'
    + '<div class="rs-brand"><span class="rs-brand-icon">' + ICONS.flask + '</span><span>'
    + escapeHtml(tr(t, 'paper.research.workbenchLabel', 'Research Foundry')) + '</span></div>'
    + '<div class="rs-hero-kicker">' + escapeHtml(tr(t, 'paper.research.heroKicker', 'From question to a falsifiable paper thesis')) + '</div>'
    + '<h1>' + escapeHtml(tr(t, 'paper.research.heroTitle', 'Build a research program, not another list of ideas.')) + '</h1>'
    + '<p>' + escapeHtml(tr(t, 'paper.research.heroBody', 'Mine the literature, locate defensible gaps, pressure-test mechanisms and leave with ranked experiment briefs. Every decision stays traceable to the evidence that produced it.')) + '</p>'
    + '<div class="rs-direction-box"><label for="paperResearchInput">'
    + escapeHtml(tr(t, 'paper.research.directionLabel', 'Research direction')) + '</label>'
    + '<textarea id="paperResearchInput" class="rs-direction-input" rows="3" maxlength="2000" placeholder="'
    + escapeHtml(tr(t, 'paper.research.entryPlaceholder', 'Describe the unresolved mechanism, target domain, constraints, and the result that would change your mind.')) + '" '
    + 'data-tofu-action-keydown="if((event.metaKey||event.ctrlKey)&&event.key===\'Enter\')_submitResearchDirection()"></textarea>'
    + '<div class="rs-direction-actions"><span>'
    + escapeHtml(tr(t, 'paper.research.directionHint', 'Tip: include a failure mode or a few seed arXiv IDs in the direction.'))
    + '<kbd class="rs-kbd">⌘/Ctrl + Enter</kbd></span><button type="button" class="rs-start-btn" data-tofu-action="_submitResearchDirection()">'
    + '<span>' + escapeHtml(tr(t, 'paper.research.startBtn', 'Launch research pipeline')) + '</span>' + ICONS.arrow + '</button></div></div>'
    + '<div class="rs-templates"><span>'
    + escapeHtml(tr(t, 'paper.research.templatesLabel', 'No starting point? Begin from a template'))
    + '</span>'
    + [1, 2, 3].map((n) => '<button type="button" class="rs-template-chip" data-tofu-action="_fillResearchTemplate('
      + (n - 1) + ')">' + escapeHtml(tr(t, 'paper.research.template' + n, '')) + '</button>').join('')
    + '</div>'
    + '<div class="rs-hero-proof"><span>01</span><span></span><b>'
    + escapeHtml(tr(t, 'paper.research.heroProof', 'Checkpointed · inspectable · resumable')) + '</b></div>'
    + '</div><aside class="rs-capabilities"><div class="rs-capabilities-head"><span>'
    + escapeHtml(tr(t, 'paper.research.capabilitiesLabel', 'The operating model')) + '</span><strong>05</strong></div>'
    + landingCapability('01', tr(t, 'paper.research.harvest', 'Harvest'), tr(t, 'paper.research.capabilityHarvest', 'Build a bounded, reusable paper corpus.'))
    + landingCapability('02', tr(t, 'paper.research.survey', 'Survey'), tr(t, 'paper.research.capabilitySurvey', 'Compare methods and extract evidence-backed gaps.'))
    + landingCapability('03', tr(t, 'paper.research.ideate', 'Ideate'), tr(t, 'paper.research.capabilityIdeate', 'Generate mechanism-level deltas, not A+B combinations.'))
    + landingCapability('04', tr(t, 'paper.research.evaluate', 'Evaluate'), tr(t, 'paper.research.capabilityEvaluate', 'Use independent judges to expose weak claims.'))
    + landingCapability('05', tr(t, 'paper.research.publish', 'Package'), tr(t, 'paper.research.capabilityPublish', 'Produce ranked briefs, evidence and a revision queue.'))
    + '</aside></section>'
    + '<section id="paperRecentResearch" class="rs-recent"></section>'
    + '</main>';
  void renderRecentResearch();
  window.requestAnimationFrame(() => {
    (document.getElementById('paperResearchInput') as HTMLTextAreaElement | null)?.focus();
  });
}

export function fillResearchTemplate(index: number): void {
  const input = document.getElementById('paperResearchInput') as HTMLTextAreaElement | null;
  if (!input) return;
  const value = tr(translator(), 'paper.research.template' + (index + 1), '');
  if (!value) return;
  input.value = value;
  input.focus();
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
    if (idea) value = '# ' + (text(idea.title) || tr(t, 'paper.research.untitled', '(untitled)'))
      + '\n\n' + researchIdeaBrief(idea, t);
  }
  if (kind === 'ideas') {
    const ideas = [...(stream.acceptedIdeas ?? [])].sort((left, right) => ideaRank(right) - ideaRank(left));
    value = ideas.map((idea) => '# ' + (text(idea.title) || tr(t, 'paper.research.untitled', '(untitled)'))
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
    button.innerHTML = ICONS.check + escapeHtml(tr(t, 'paper.research.copied', 'Copied'));
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
  showResearchLanding();
}

// Tab selection survives re-paints (hydration repaints a finished run) and
// resets only when a different direction is shown.
let activeResearchTab = 'ideas';
let activeResearchDirection = '';

export function switchResearchTab(id: string): void {
  activeResearchTab = id;
  const shell = document.querySelector('[data-research-shell]');
  if (!shell) return;
  shell.querySelectorAll('[data-rs-tab]').forEach((node) => {
    node.classList.toggle('is-active', node.getAttribute('data-rs-tab') === id);
  });
  shell.querySelectorAll('[data-rs-panel]').forEach((node) => {
    node.classList.toggle('is-active', node.getAttribute('data-rs-panel') === id);
  });
}

function researchLedgerHtml(stream: ResearchStream, t: Translator): string {
  return '<section class="rs-panel rs-ledger"><div class="rs-panel-head"><div><div class="rs-eyebrow">'
    + escapeHtml(tr(t, 'paper.research.ledgerKicker', 'Reproducibility ledger')) + '</div><h2>'
    + escapeHtml(tr(t, 'paper.research.ledgerTitle', 'Corpus, execution and resource record')) + '</h2></div>'
    + (stream.folderId ? '<button type="button" class="rs-btn is-secondary" data-tofu-action="_openResearchFolder()">'
      + escapeHtml(tr(t, 'paper.research.openFolder', 'Open paper library')) + ICONS.external + '</button>' : '')
    + '</div>' + researchCorpusHtml(stream, t) + researchUsageHtml(stream, t) + '</section>';
}

function researchTabsHtml(stream: ResearchStream, t: Translator): string {
  if (activeResearchDirection !== stream.direction) {
    activeResearchDirection = stream.direction;
    activeResearchTab = 'ideas';
  }
  const tabs: Array<{ id: string; label: string; badge: string; content: string }> = [
    {
      id: 'ideas',
      label: tr(t, 'paper.research.tabIdeas', 'Ideas'),
      badge: stream.accepted ? String(stream.accepted) : '',
      content: researchIdeasHtml(stream, t) + researchRejectedHtml(stream, t),
    },
    {
      id: 'review',
      label: tr(t, 'paper.research.tabReview', 'Independent review'),
      badge: '',
      content: researchEvaluationHtml(stream, t),
    },
    {
      id: 'evidence',
      label: tr(t, 'paper.research.tabEvidence', 'Evidence & gaps'),
      badge: '',
      content: researchSurveyHtml(stream, t),
    },
    {
      id: 'ledger',
      label: tr(t, 'paper.research.tabLedger', 'Ledger & trace'),
      badge: '',
      content: researchLedgerHtml(stream, t) + researchToolsHtml(stream),
    },
    {
      id: 'workspace',
      label: tr(t, 'paper.research.tabWorkspace', 'Workspace'),
      badge: '',
      content: researchWorkspaceHtml(stream),
    },
  ].filter((tab) => tab.content);
  if (!tabs.some((tab) => tab.id === activeResearchTab)) {
    activeResearchTab = tabs[0]?.id ?? 'ideas';
  }
  const nav = tabs.map((tab) => '<button type="button" role="tab" class="rs-tab-btn'
    + (tab.id === activeResearchTab ? ' is-active' : '')
    + '" data-tofu-action="_switchResearchTab(\'' + tab.id + '\')" data-rs-tab="' + tab.id + '"><span>'
    + escapeHtml(tab.label) + '</span>'
    + (tab.badge ? '<b>' + escapeHtml(tab.badge) + '</b>' : '')
    + '</button>').join('');
  const panels = tabs.map((tab) => '<div class="rs-tab-panel'
    + (tab.id === activeResearchTab ? ' is-active' : '')
    + '" data-rs-panel="' + tab.id + '" role="tabpanel">' + tab.content + '</div>').join('');
  return '<div class="rs-tabs" role="tablist">' + nav + '</div><div class="rs-tab-panels">' + panels + '</div>';
}

function researchHeader(stream: ResearchStream, t: Translator): string {
  const statusClass = stream.status === 'error' ? 'is-error'
    : stream.degraded ? 'is-degraded'
      : researchIsRunning(stream) ? 'is-running' : 'is-ready';
  return '<header class="rs-run-head"><div class="rs-run-title"><button type="button" class="rs-back-btn" '
    + 'data-tofu-action="_newResearchDirection()" title="'
    + escapeHtml(tr(t, 'paper.research.backToWorkbench', 'Start a new direction')) + '">←</button>'
    + '<div><div class="rs-eyebrow">' + escapeHtml(tr(t, 'paper.research.activeProgram', 'Active research program'))
    + '</div><h1>' + escapeHtml(stream.direction) + '</h1><p>'
    + escapeHtml(tr(t, 'paper.research.subtitle', 'Literature → gap map → novelty gate → independent review'))
    + '</p></div></div><div class="rs-run-meta"><span class="rs-status ' + statusClass + '"><i></i>'
    + escapeHtml(researchStatusLabel(stream, t)) + '</span>'
    + (researchTimestamp(stream.startedAt) ? '<time>' + escapeHtml(researchTimestamp(stream.startedAt)) + '</time>' : '')
    + (researchIsRunning(stream) ? '<button type="button" class="rs-stop-btn" data-tofu-action="_abortResearchJob()">'
      + escapeHtml(tr(t, 'paper.research.abort', 'Stop run')) + '</button>' : '')
    + '</div></header>';
}

export function paintResearch(): void {
  const stream = globals()._researchStream;
  if (!stream) return;
  const viewer = document.getElementById('researchViewer');
  if (!viewer) return;
  const t = translator();
  const running = researchIsRunning(stream);
  const quality = stream.degraded
    ? '<section class="rs-alert is-warning" role="alert"><div class="rs-alert-title">'
      + escapeHtml(tr(t, 'paper.research.degraded', 'Delivered, but the pipeline was degraded')) + '</div>'
      + '<div class="rs-alert-detail">' + escapeHtml(stream.degradedReason || tr(t, 'paper.research.degradedFallback', 'Inspect the execution trace before using this artifact.')) + '</div></section>'
    : '';
  const liveStat = (label: string, value: string): string => '<div class="rs-live-stat"><span>'
    + escapeHtml(label) + '</span><strong>' + escapeHtml(value) + '</strong></div>';
  const runningBody = '<section class="rs-live-grid"><div class="rs-live-copy"><div class="rs-live-pulse"><i></i>'
    + escapeHtml(tr(t, 'paper.research.running', 'Researching…')) + '</div><h2>'
    + escapeHtml(tr(t, 'paper.research.runningTitle', 'The foundry is building your evidence base.')) + '</h2><p>'
    + escapeHtml(tr(t, 'paper.research.runningBody', 'The page updates as papers are harvested, compared and challenged. You can leave this page; the checkpointed job keeps running.'))
    + '</p></div><div class="rs-live-stat-stack">'
    + liveStat(tr(t, 'paper.research.currentPhase', 'Current phase'),
      tr(t, 'paper.research.' + (stream.phase || 'harvest'), stream.phase || 'harvest'))
    + liveStat(tr(t, 'paper.research.elapsed', 'Elapsed'), researchElapsed(stream) || '—')
    + liveStat(tr(t, 'paper.research.evidenceActions', 'Evidence actions'), String(stream.toolRounds.length))
    + '</div></section>';
  const finishedBody = researchDeliverablesHtml(stream, t)
    + researchFailureHtml(stream, t)
    + quality
    + researchTabsHtml(stream, t);
  viewer.innerHTML = '<main class="rs-workbench rs-console" data-research-shell="1">'
    + researchHeader(stream, t)
    + researchPipelineHtml(stream, t)
    + (running ? runningBody + researchToolsHtml(stream) : finishedBody)
    + '</main>';

  if (!running) void loadResearchWorkspace(stream.direction, stream.lang);
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
  target._renderRecentResearch = renderRecentResearch;
  target._paintResearch = paintResearch;
  target._showResearchLanding = showResearchLanding;
  target._switchResearchTab = switchResearchTab;
}

installResearchViewGlobals();
