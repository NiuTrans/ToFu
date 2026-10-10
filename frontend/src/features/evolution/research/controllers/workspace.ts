/** Research workspace owner: revision CAS, draft collection, action polling and
 * the visible manuscript lifecycle. Server responses own submission decisions;
 * rendering preserves active editors and the separately owned PDF subtree.
 */
import { createField, createButton } from '../../../../ui/index';
import { patchResearchDom } from '../components/stable-dom';
import { featureRegistry, readLiveRuntimeBinding } from '../../../../feature-registry';
import { escapeHtml } from '../../../../html-safety';
import { ResearchObservationRecovery } from '../components/observation-recovery';
import { ResearchHistory } from '../components/history';
import { ResearchManuscript } from './manuscript';
import { submissionReadiness, submissionGatesHtml, type SubmissionReadiness } from '../components/submission-gates';
import { createProgramDraftStore, researchProgrammePath, programmeCreationDraftIdentity } from '../../programs/drafts';
import { resolveBrowserLocalStorage } from '../../../../core/browser-storage';
import { goToWorkspace } from '../../../../application-shell/page-host';
import type { ResearchStream } from '../controllers/runtime';

type JsonObject = Record<string, any>;
type Workspace = JsonObject & {
  revision: number;
  direction: string;
  lang: string;
  stage: string;
  selected_idea_id: string;
  hypothesis: string;
  protocol: JsonObject;
  capability_bindings: JsonObject[];
  runs: JsonObject[];
  claims: JsonObject[];
  manuscript: JsonObject;
  source_files: JsonObject[];
  figures: JsonObject[];
  tables: JsonObject[];
  compilation: JsonObject;
  publication: JsonObject;
};

type ResearchApi = {
  observations(taskId: string, cursor: string, signal: AbortSignal): Promise<JsonObject>;
  workspace(direction: string, lang: string): Promise<JsonObject>;
  history(direction: string, lang: string, cursor: number, signal?: AbortSignal): Promise<JsonObject>;
  saveWorkspace(payload: JsonObject): Promise<JsonObject>;
  draftProtocol(payload: JsonObject): Promise<JsonObject>;
  capabilities(): Promise<JsonObject>;
  resumeAction?(taskId: string): Promise<JsonObject>;
  unfinishedActions?(direction: string, lang: string): Promise<JsonObject | null>;
  scaffoldManuscript(payload: JsonObject): Promise<JsonObject>;
  sourceArchiveUrl(direction: string, lang: string): string;
};

type TasksApi = {
  list?(kind: string, status: string, filters: JsonObject): Promise<JsonObject | null>;
  start(kind: string, payload: JsonObject): Promise<JsonObject>;
  get(taskId: string): Promise<JsonObject | null>;
  abort(taskId: string): Promise<JsonObject | null>;
};

type ResearchWorkspaceWindow = Window & {
  config?: { model?: string };
  Api?: { research?: ResearchApi; tasks?: TasksApi };
  t?: (key: string) => string;
  _researchStream?: ResearchStream | null;
  _safeClipboardWrite?: (value: string) => Promise<unknown> | unknown;
  _loadResearchWorkspace?: typeof loadResearchWorkspace;
  _saveResearchWorkspace?: typeof saveResearchWorkspace;
  _addResearchRun?: typeof addResearchRun;
  _removeResearchRun?: typeof removeResearchRun;
  _addResearchClaim?: typeof addResearchClaim;
  _removeResearchClaim?: typeof removeResearchClaim;
  _copyResearchLatex?: typeof copyResearchLatex;
  _promoteResearchIdea?: typeof promoteResearchIdea;
  _draftResearchProtocol?: typeof draftResearchProtocol;
  _autobindResearchCapabilities?: typeof autobindResearchCapabilities;
  _startResearchAction?: typeof startResearchAction;
  _abortResearchAction?: typeof abortResearchAction;
  _resumeResearchAction?: typeof resumeResearchAction;
  _scaffoldResearchManuscript?: typeof scaffoldResearchManuscript;
  _selectResearchSource?: typeof selectResearchSource;
};

const state: {
  direction: string;
  lang: string;
  workspace: Workspace | null;
  catalog: JsonObject;
  loading: boolean;
  error: string;
  notice: string;
  drafting: boolean;
  selectedSourcePath: string;
  activeTask: JsonObject | null;
  interruptedTask: JsonObject | null;
  resuming: boolean;
  actionDiscoveryBlocked: boolean;
  readiness: SubmissionReadiness | null;
  dirty: boolean;
} = {
  direction: '', lang: 'en', workspace: null,
  catalog: { capabilities: [], tools: [] }, loading: false, error: '',
  notice: '', drafting: false,
  selectedSourcePath: '', activeTask: null, interruptedTask: null, resuming: false, actionDiscoveryBlocked: false, readiness: null, dirty: false,
};

function globals(): ResearchWorkspaceWindow {
  return featureRegistry as unknown as ResearchWorkspaceWindow;
}

function tr(key: string, fallback: string): string {
  const value = globals().t?.(key);
  return typeof value === 'string' && value && value !== key ? value : fallback;
}

function text(value: unknown): string { return String(value ?? ''); }
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }

function empty(direction: string, lang: string): Workspace {
  return {
    contract_version: 'tofu.research-program/v1', revision: 0, direction, lang,
    stage: 'selection', selected_idea_id: '', selected_idea_source_task_id: '', hypothesis: '',
    protocol: {
      primary_metric: '', baseline: '', dataset: '', falsifier: '', resources: '',
      evaluation_protocol: '', environment: '', random_seeds: [], stop_conditions: [],
    },
    capability_bindings: [], runs: [], claims: [], figures: [], tables: [],
    manuscript: {
      title: '', venue: '', abstract: '', keywords: '', introduction: '',
      related_work: '', method: '', experiments: '', results: '', limitations: '',
      conclusion: '', ethics: '',
    },
    source_files: [],
    compilation: {
      mode: 'unconfigured', status: 'not_run', detail: '', source_digest: '',
      engine: '', compiled_at: 0, build: null,
    },
    publication: {
      provider: '', status: 'not_started', project_ref: '', project_url: '',
      source_digest: '', published_at: 0, detail: '',
    },
    updated_at: 0,
  };
}

function field(path: string, value: unknown, multiline = false, placeholder = '', rows = 3): string {
  const escaped = escapeHtml(text(value));
  const common = ` class="rsw-input" data-rsw-field="${escapeHtml(path)}" placeholder="${escapeHtml(placeholder)}"`;
  return multiline
    ? `<textarea${common} rows="${rows}">${escaped}</textarea>`
    : `<input${common} value="${escaped}">`;
}

function selectField(path: string, value: unknown, options: Array<[string, string]>): string {
  return `<select class="rsw-input" data-rsw-field="${escapeHtml(path)}">${options.map(([key, label]) =>
    `<option value="${escapeHtml(key)}"${text(value) === key ? ' selected' : ''}>${escapeHtml(label)}</option>`).join('')}</select>`;
}

function setPath(target: JsonObject, path: string, value: string): void {
  const parts = path.split('.');
  if (!parts[0]) return;
  let cursor = target;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index];
    const key: string | number = /^\d+$/.test(part) ? Number(part) : part;
    if (cursor[key] == null) cursor[key] = /^\d+$/.test(parts[index + 1]) ? [] : {};
    cursor = cursor[key];
  }
  const last = parts[parts.length - 1];
  cursor[/^\d+$/.test(last) ? Number(last) : last] = value;
}

function bindingFor(workspace: Workspace, capability: string): JsonObject | undefined {
  return (workspace.capability_bindings || []).find((row) => row.capability === capability && row.enabled !== false);
}

function collect(): Workspace | null {
  if (!state.workspace) return null;
  const copy = clone(state.workspace);
  document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('#researchWorkspace [data-rsw-field]').forEach((node) => {
    setPath(copy, node.dataset.rswField || '', node.value);
  });
  const sourceChanged = JSON.stringify((copy.source_files || []).map((row) => [row.path, row.content]))
    !== JSON.stringify((state.workspace.source_files || []).map((row) => [row.path, row.content]));
  if (sourceChanged && copy.compilation?.status === 'passing') {
    copy.compilation.status = 'not_run';
  }
  const prior = new Map((copy.capability_bindings || []).map((row) => [row.capability, row]));
  const controls = [...document.querySelectorAll<HTMLSelectElement>('#researchWorkspace [data-rsw-binding]')];
  const visibleCapabilities = new Set(controls.map(node => node.dataset.rswBinding));
  copy.capability_bindings = [...prior.values()].filter(row => !visibleCapabilities.has(row.capability));
  controls.forEach((node) => {
    const capability = node.dataset.rswBinding || '';
    if (!capability || !node.value) return;
    const current = prior.get(capability) || {};
    const toolContract = (state.catalog.tools || []).find((row: JsonObject) => row.name === node.value) || {};
    copy.capability_bindings.push({
      capability, provider: text(node.value).split('__')[1] || 'mcp', tool: node.value,
      schema_hash: text(toolContract.schema_hash),
      enabled: true, argument_defaults: current.argument_defaults || {},
      notes: current.notes || '',
    });
  });
  normalizeLists(copy);
  return copy;
}

function normalizeLists(workspace: Workspace): void {
  workspace.claims.forEach((claim) => {
    if (typeof claim.evidence_refs_csv === 'string') {
      claim.evidence_refs = claim.evidence_refs_csv.split(',').map((item: string) => item.trim()).filter(Boolean);
      delete claim.evidence_refs_csv;
    }
  });
  const protocol = workspace.protocol || {};
  if ('decision_direction' in protocol) {
    const direction = text(protocol.decision_direction);
    const threshold = text(protocol.decision_minimum_effect);
    const multiplier = text(protocol.decision_uncertainty_multiplier);
    if (!direction && !threshold && !multiplier) delete protocol.decision_rule;
    else protocol.decision_rule = { direction,
      minimum_effect: threshold ? Number(threshold) : null,
      uncertainty_multiplier: multiplier ? Number(multiplier) : null };
    delete protocol.decision_direction;
    delete protocol.decision_minimum_effect;
    delete protocol.decision_uncertainty_multiplier;
  }
  if (typeof protocol.random_seeds_csv === 'string') {
    protocol.random_seeds = protocol.random_seeds_csv.split(',').map((item: string) => Number(item.trim())).filter(Number.isInteger);
    delete protocol.random_seeds_csv;
  }
  if (typeof protocol.stop_conditions_text === 'string') {
    protocol.stop_conditions = protocol.stop_conditions_text.split('\n').map((item: string) => item.trim()).filter(Boolean);
    delete protocol.stop_conditions_text;
  }
}

function decisionRuleHtml(workspace: Workspace): string {
  const rule = workspace.protocol.decision_rule || {};
  const control = (key: string, label: string, value: unknown, choices?: Array<{value: string; label: string}>) => {
    const handle = createField({ label, value: value == null ? '' : String(value),
      inputType: choices ? undefined : 'number', choices, controlId: `research-rule-${key}` });
    handle.control.dataset.rswField = `protocol.decision_${key}`;
    if (!choices) handle.control.setAttribute('step', 'any');
    return handle.el.outerHTML;
  };
  return `<div class="ui-stack" data-rsw-decision-rule><h4>${escapeHtml(tr('evolution.research.ruleTitle', 'Freeze the numerical decision rule'))}</h4><p>${escapeHtml(tr('evolution.research.ruleHint', 'Choose before execution. Support requires the oriented effect minus the SE margin to exceed the threshold; refutation requires effect plus that margin below it. Equality is inconclusive. This is not a confidence interval or significance test. Changing the rule invalidates old evidence.'))}</p><div class="ui-grid">${control('direction', tr('evolution.research.ruleDirection', 'Metric direction'), rule.direction, [
    {value:'',label:tr('evolution.research.ruleChoose', 'Choose explicitly')},
    {value:'higher',label:tr('evolution.research.ruleHigher', 'Higher is better')},
    {value:'lower',label:tr('evolution.research.ruleLower', 'Lower is better')},
  ])}${control('minimum_effect', tr('evolution.research.ruleThreshold', 'Minimum oriented effect (negative permits noninferiority)'), rule.minimum_effect)}${control('uncertainty_multiplier', tr('evolution.research.ruleMargin', 'Positive SE multiplier'), rule.uncertainty_multiplier)}</div></div>`;
}

function decisionRuleError(workspace: Workspace, required = false): string {
  const rule = workspace.protocol.decision_rule;
  if (!rule && !required) return '';
  return rule && ['higher','lower'].includes(rule.direction)
    && typeof rule.minimum_effect === 'number' && Number.isFinite(rule.minimum_effect)
    && typeof rule.uncertainty_multiplier === 'number' && Number.isFinite(rule.uncertainty_multiplier)
    && rule.uncertainty_multiplier > 0 ? ''
    : tr('evolution.research.ruleInvalid', 'Before running an experiment, choose a metric direction, enter a finite effect threshold and a positive finite SE multiplier.');
}

function ideaOptions(stream: ResearchStream): Array<[string, string]> {
  const candidates: Array<[string, string]> = ((stream.acceptedIdeas ?? []) as JsonObject[]).map((idea, index) => [
    text(idea.id || `idea-${index + 1}`), text(idea.title || `Candidate ${index + 1}`),
  ]);
  return [['', tr('evolution.research.workspaceChooseIdea', 'Choose a candidate')], ...candidates];
}

function latexEscape(value: unknown): string {
  return text(value).replace(/\\/g, '\\textbackslash{}').replace(/([#$%&_{}])/g, '\\$1').replace(/\^/g, '\\textasciicircum{}').replace(/~/g, '\\textasciitilde{}');
}

export function researchWorkspaceLatex(workspace: Workspace): string {
  const main = (workspace.source_files || []).find((row) => row.path === 'main.tex');
  if (main?.content) return text(main.content);
  const manuscript = workspace.manuscript || {};
  return `\\documentclass[11pt]{article}\n\\usepackage{booktabs,graphicx,hyperref}\n\\title{${latexEscape(manuscript.title || 'Untitled research manuscript')}}\n\\begin{document}\n\\maketitle\n\\begin{abstract}\n${latexEscape(manuscript.abstract)}\n\\end{abstract}\n\\section{Method}\n${latexEscape(manuscript.method)}\n\\section{Results}\n${latexEscape(manuscript.results)}\n\\section{Limitations}\n${latexEscape(manuscript.limitations)}\n\\end{document}\n`;
}

function capabilityOptions(capability: string, selected: string): string {
  const tools = [...(state.catalog.tools || [])] as JsonObject[];
  const isWrite = Boolean((state.catalog.capabilities || []).find((row: JsonObject) => row.id === capability)?.write);
  const eligible = tools.filter((tool) => isWrite || tool.read_only);
  eligible.sort((left, right) => {
    const leftScore = left.suggested_capabilities?.find((row: JsonObject) => row.id === capability)?.score || 0;
    const rightScore = right.suggested_capabilities?.find((row: JsonObject) => row.id === capability)?.score || 0;
    return rightScore - leftScore || text(left.name).localeCompare(text(right.name));
  });
  return `<select class="rsw-input" data-rsw-binding="${escapeHtml(capability)}"><option value="">${escapeHtml(tr('evolution.research.capabilityUnbound', 'Not bound'))}</option>${eligible.map((tool) => {
    const suggested = tool.suggested_capabilities?.some((row: JsonObject) => row.id === capability);
    const label = `${suggested ? '✓ ' : ''}${tool.server} · ${tool.name.replace(`mcp__${tool.server}__`, '')}`;
    return `<option value="${escapeHtml(tool.name)}"${selected === tool.name ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('')}</select>`;
}

function capabilitiesHtml(workspace: Workspace): string {
  const capabilities = state.catalog.capabilities || [];
  if (!capabilities.length) return `<div class="rsw-empty">${escapeHtml(tr('evolution.research.noCapabilities', 'No MCP research tools are connected. Local Agent actions remain available.'))}</div>`;
  return `<div class="rsw-capability-matrix">${capabilities.map((capability: JsonObject) => {
    const binding = bindingFor(workspace, capability.id);
    const selected = text(binding?.tool);
    const liveTool = (state.catalog.tools || []).find((row: JsonObject) => row.name === selected);
    const drifted = Boolean(binding?.schema_hash && liveTool?.schema_hash && binding.schema_hash !== liveTool.schema_hash);
    const detail = drifted
      ? tr('evolution.research.capabilityDrift', 'Tool schema changed · review and save to rebind')
      : (capability.write ? tr('evolution.research.capabilityWrite', 'May change an external system') : tr('evolution.research.capabilityRead', 'Read-only observation'));
    return `<label class="rsw-capability-row ${drifted ? 'is-drifted' : ''}"><span><b>${escapeHtml(capability.id)}</b><small>${escapeHtml(detail)}</small></span>${capabilityOptions(capability.id, selected)}</label>`;
  }).join('')}</div>`;
}

function runsHtml(workspace: Workspace): string {
  if (!workspace.runs.length) return `<div class="rsw-empty">${escapeHtml(tr('evolution.research.workspaceNoRuns', 'No runs recorded. Freeze the protocol before spending compute.'))}</div>`;
  return workspace.runs.map((run, index) => `<article class="rsw-record"><div class="rsw-record-head"><strong>${escapeHtml(text(run.label) || `Run ${index + 1}`)}</strong><span class="rsw-run-proof">${escapeHtml(text(run.task_id || 'manual'))} · ${(Number(run.receipt_history?.count) || 0) + (run.tool_receipts || []).length} receipts</span><button data-tofu-action="_removeResearchRun(${index})">×</button></div><div class="rsw-grid cols-3"><label>${escapeHtml(tr('evolution.research.runLabel', 'Run label'))}${field(`runs.${index}.label`, run.label)}</label><label>${escapeHtml(tr('evolution.research.runStatus', 'Status'))}${selectField(`runs.${index}.status`, run.status, [['planned','planned'],['running','running'],['passed','passed'],['failed','failed'],['inconclusive','inconclusive']])}</label><label>${escapeHtml(tr('evolution.research.runArtifact', 'Primary evidence reference'))}${field(`runs.${index}.artifact_ref`, run.artifact_ref)}</label><label>${escapeHtml(tr('evolution.research.runMetric', 'Metric'))}${field(`runs.${index}.metric`, run.metric)}</label><label>${escapeHtml(tr('evolution.research.runBaseline', 'Baseline'))}${field(`runs.${index}.baseline`, run.baseline)}</label><label>${escapeHtml(tr('evolution.research.runDelta', 'Delta'))}${field(`runs.${index}.delta`, run.delta)}</label><label>${escapeHtml(tr('evolution.research.runBackend', 'Execution backend'))}${field(`runs.${index}.backend`, run.backend)}</label><label>${escapeHtml(tr('evolution.research.runRemoteJob', 'Remote job ID'))}${field(`runs.${index}.remote_job_id`, run.remote_job_id)}</label></div><label>${escapeHtml(tr('evolution.research.runNotes', 'Notes and failure analysis'))}${field(`runs.${index}.notes`, run.notes, true)}</label></article>`).join('');
}

function scientificDecisionHtml(workspace: Workspace): string {
  const decision = workspace.decision;
  const audit = workspace.manuscript_audit;
  if (!decision && !audit) return '';
  const actions = Array.isArray(decision?.next_actions) ? decision.next_actions : [];
  const checks = Array.isArray(audit?.checks) ? audit.checks : [];
  return `<div class="rsw-record"><strong>${escapeHtml(text(decision?.status || 'inconclusive'))}</strong><p>${escapeHtml(text(decision?.reason))}</p><small>${escapeHtml(text(decision?.scope))}</small>${actions.map((action: JsonObject) => `<details><summary>${escapeHtml(text(action.priority))}. ${escapeHtml(text(action.design))}</summary><p>${escapeHtml(text(action.decision_rule))}</p><p>${escapeHtml(text(action.expected_information))}</p><p>${escapeHtml(text(action.cost))}</p></details>`).join('')}<div class="rsw-gates">${checks.map((check: JsonObject) => `<span class="${check.ok === true ? 'is-ok' : ''}">${check.ok === true ? '✓' : '○'} ${escapeHtml(text(check.id))}</span>`).join('')}</div></div>`;
}

function claimsHtml(workspace: Workspace): string {
  if (!workspace.claims.length) return `<div class="rsw-empty">${escapeHtml(tr('evolution.research.workspaceNoClaims', 'No paper claims yet. Add only claims that point to run or literature evidence.'))}</div>`;
  return workspace.claims.map((claim, index) => `<article class="rsw-record"><div class="rsw-record-head"><strong>C${String(index + 1).padStart(2, '0')}</strong><button data-tofu-action="_removeResearchClaim(${index})">×</button></div><label>${escapeHtml(tr('evolution.research.claimText', 'Paper claim'))}${field(`claims.${index}.text`, claim.text, true)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.claimStatus', 'Evidence status'))}${selectField(`claims.${index}.status`, claim.status, [['draft','draft'],['supported','supported'],['contested','contested'],['rejected','rejected']])}</label><label>${escapeHtml(tr('evolution.research.claimEvidence', 'Evidence references (comma-separated)'))}${field(`claims.${index}.evidence_refs_csv`, (claim.evidence_refs || []).join(', '))}</label></div></article>`).join('');
}

function visualsHtml(workspace: Workspace): string {
  const renderRows = (kind: 'figures' | 'tables', rows: JsonObject[]) => rows.map((row, index) => `<article class="rsw-visual"><div><span>${kind === 'figures' ? 'FIG' : 'TAB'} ${String(index + 1).padStart(2, '0')}</span><strong>${escapeHtml(text(row.title) || tr('evolution.research.visualUntitled', 'Untitled visual'))}</strong>${selectField(`${kind}.${index}.status`, row.status, [['planned','planned'],['generated','generated'],['verified','verified'],['rejected','rejected']])}</div><label>${escapeHtml(tr('evolution.research.visualCaption', 'Caption and evidence claim'))}${field(`${kind}.${index}.caption`, row.caption, true)}</label><div class="rsw-grid cols-3"><label>${escapeHtml(tr('evolution.research.visualData', 'Data reference'))}${field(`${kind}.${index}.data_ref`, row.data_ref)}</label><label>${escapeHtml(tr('evolution.research.visualScript', 'Rendering script'))}${field(`${kind}.${index}.script_ref`, row.script_ref)}</label><label>${escapeHtml(tr('evolution.research.visualOutput', 'Output asset'))}${field(`${kind}.${index}.output_ref`, row.output_ref)}</label></div></article>`).join('');
  if (!workspace.figures.length && !workspace.tables.length) {
    return `<div class="rsw-empty">${escapeHtml(tr('evolution.research.visualEmpty', 'No figure or table plan yet. Analyze evidence to create traceable visuals.'))}</div>`;
  }
  return `<div class="rsw-visual-grid">${renderRows('figures', workspace.figures)}${renderRows('tables', workspace.tables)}</div>`;
}

function actionConsoleHtml(workspace: Workspace): string {
  const active = state.activeTask;
  const idleLabel = state.actionDiscoveryBlocked
    ? tr('evolution.research.actionChecking', 'Checking the state of existing actions…')
    : tr('evolution.research.actionIdle', 'Agent ready · no action running');
  const actions: Array<[string, string, string]> = [
    ['experiment', tr('evolution.research.actionExperiment', 'Run experiment'), tr('evolution.research.actionExperimentHint', 'Execute the frozen falsifier and record receipts')],
    ['analyze', tr('evolution.research.actionAnalyze', 'Analyze evidence'), tr('evolution.research.actionAnalyzeHint', 'Ablations, uncertainty, figures and tables')],
    ['manuscript', tr('evolution.research.actionManuscript', 'Write paper'), tr('evolution.research.actionManuscriptHint', 'Turn supported claims into conference prose')],
    ['compile', tr('evolution.research.actionCompile', 'Compile source'), tr('evolution.research.actionCompileHint', 'Build the saved manuscript with the application compiler')],
    ['publish', tr('evolution.research.actionPublish', 'Publish project'), tr('evolution.research.actionPublishHint', 'Sync exact sources through the bound provider')],
  ];
  let resume = '';
  if (state.interruptedTask && globals().Api?.research?.resumeAction) {
    const button = createButton({label: tr('evolution.research.actionResume', 'Continue recorded task'),
      variant: 'secondary', disabled: state.resuming || state.actionDiscoveryBlocked});
    button.dataset.tofuAction = '_resumeResearchAction(this)';
    resume = button.outerHTML;
  }
  return `<div class="rsw-action-console"><div class="rsw-action-status ${active ? 'is-active' : ''}"><i></i><span>${escapeHtml(active ? `${active.action} · ${active.status}` : idleLabel)}</span>${active ? `<button data-tofu-action="_abortResearchAction()">${escapeHtml(tr('evolution.research.actionStop', 'Stop'))}</button>` : resume}</div><div class="rsw-action-track">${actions.map(([action, label, hint], index) => {
    const compileBlocked = action === 'compile' && !workspace.source_files?.length;
    const publishBlocked = action === 'publish' && !bindingFor(workspace, 'publication.push');
    const disabled = Boolean(active || state.resuming || state.actionDiscoveryBlocked || compileBlocked || publishBlocked);
    const reason = compileBlocked ? tr('evolution.research.actionNeedsSource', 'Create manuscript sources first')
      : publishBlocked ? tr('evolution.research.actionNeedsBinding', 'Bind the required capability first') : hint;
    return `<button class="rsw-action" data-tofu-action="_startResearchAction('${action}',this)"${disabled ? ' disabled' : ''}><span>${String(index + 1).padStart(2, '0')}</span><b>${escapeHtml(label)}</b><small>${escapeHtml(reason)}</small></button>`;
  }).join('')}</div></div>`;
}

function manuscriptFieldsHtml(workspace: Workspace): string {
  const manuscript = workspace.manuscript;
  return `<label>${escapeHtml(tr('evolution.research.manuscriptTitle', 'Paper title'))}${field('manuscript.title', manuscript.title)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.workspaceVenue', 'Target venue'))}${field('manuscript.venue', manuscript.venue)}</label><label>${escapeHtml(tr('evolution.research.manuscriptKeywords', 'Keywords'))}${field('manuscript.keywords', manuscript.keywords)}</label></div><label>${escapeHtml(tr('evolution.research.manuscriptAbstract', 'Abstract'))}${field('manuscript.abstract', manuscript.abstract, true, '', 5)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.manuscriptIntroduction', 'Introduction and contributions'))}${field('manuscript.introduction', manuscript.introduction, true, '', 6)}</label><label>${escapeHtml(tr('evolution.research.manuscriptRelated', 'Related work and novelty delta'))}${field('manuscript.related_work', manuscript.related_work, true, '', 6)}</label><label>${escapeHtml(tr('evolution.research.manuscriptMethod', 'Method'))}${field('manuscript.method', manuscript.method, true, '', 7)}</label><label>${escapeHtml(tr('evolution.research.manuscriptExperiments', 'Experimental setup'))}${field('manuscript.experiments', manuscript.experiments, true, '', 7)}</label><label>${escapeHtml(tr('evolution.research.manuscriptResults', 'Results narrative'))}${field('manuscript.results', manuscript.results, true, '', 7)}</label><label>${escapeHtml(tr('evolution.research.manuscriptLimitations', 'Limitations'))}${field('manuscript.limitations', manuscript.limitations, true, '', 7)}</label><label>${escapeHtml(tr('evolution.research.manuscriptConclusion', 'Conclusion'))}${field('manuscript.conclusion', manuscript.conclusion, true, '', 5)}</label><label>${escapeHtml(tr('evolution.research.manuscriptEthics', 'Ethics and broader impact'))}${field('manuscript.ethics', manuscript.ethics, true, '', 5)}</label></div>`;
}

function sourceEditorHtml(workspace: Workspace): string {
  const files = workspace.source_files || [];
  if (!files.length) return `<div class="rsw-source-empty"><p>${escapeHtml(tr('evolution.research.sourceEmpty', 'No source tree yet. Create the conference scaffold after drafting the manuscript fields.'))}</p><button class="rs-btn" data-tofu-action="_scaffoldResearchManuscript(this)">${escapeHtml(tr('evolution.research.sourceScaffold', 'Create LaTeX project'))}</button></div>`;
  let selectedIndex = Math.max(0, files.findIndex((row) => row.path === state.selectedSourcePath));
  if (selectedIndex < 0) selectedIndex = 0;
  const selected = files[selectedIndex];
  state.selectedSourcePath = text(selected.path);
  return `<div class="rsw-source-shell"><aside>${files.map((row, index) => `<button class="${index === selectedIndex ? 'is-current' : ''}" data-tofu-action="_selectResearchSource(${index})"><span>${escapeHtml(text(row.path).split('/').pop() || row.path)}</span><small>${escapeHtml(text(row.path))}</small></button>`).join('')}</aside><div class="rsw-source-editor"><div><code>${escapeHtml(selected.path)}</code><span>${escapeHtml(text(selected.sha256).slice(0, 12) || tr('evolution.research.sourceUnsaved', 'unsaved'))}</span></div>${field(`source_files.${selectedIndex}.content`, selected.content, true, '', 22)}</div></div>`;
}

function outputHtml(workspace: Workspace): string {
  const ready = state.readiness?.ready === true;
  const api = globals().Api?.research;
  const archive = api?.sourceArchiveUrl?.(state.direction, state.lang) || '#';
  const status = workspace.compilation?.status || 'not_run';
  const statusLabel = status === 'passing' ? tr('evolution.research.compilePassed', 'Compiled')
    : status === 'failing' ? tr('evolution.research.compileFailed', 'Compilation failed')
    : tr('evolution.research.compileNotRun', 'Not compiled');
  const detail = workspace.compilation?.detail || workspace.publication?.detail || '';
  const diagnostics = detail ? `<details><summary>${escapeHtml(tr('evolution.research.executionDetails', 'View details'))}</summary><code>${escapeHtml(detail)}</code></details>` : '';
  return `<div class="rsw-output-grid"><div class="rsw-gates ${ready ? 'is-ready' : ''}">${submissionGatesHtml(state.readiness, tr)}</div><div class="rsw-compiler"><strong>${escapeHtml(tr('evolution.research.compilerTitle', 'Compile and publication receipts'))}</strong><span data-compile-status="${escapeHtml(status)}" title="${escapeHtml(workspace.compilation?.engine || '')}">${escapeHtml(statusLabel)}</span>${diagnostics}<div><a class="rs-btn" href="${escapeHtml(archive)}">${escapeHtml(tr('evolution.research.sourceDownload', 'Download source ZIP'))}</a>${workspace.publication?.project_url ? `<a class="rs-btn" href="${escapeHtml(workspace.publication.project_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(tr('evolution.research.openPublished', 'Open published project'))}</a>` : ''}</div></div></div>`;
}

function workspaceBody(stream: ResearchStream, workspace: Workspace): string {
  return `<div class="rsw-head"><div><div class="rs-eyebrow">${escapeHtml(tr('evolution.research.workspaceKicker', 'Research production workspace'))}</div><h2>${escapeHtml(tr('evolution.research.workspaceTitle', 'From selected thesis to submission-ready source'))}</h2><p>${escapeHtml(tr('evolution.research.workspaceHint', 'The notebook keeps tools, evidence and paper source on one revision line.'))}</p></div><div class="rsw-save"><span>r${workspace.revision}</span><button class="rs-btn" data-tofu-action="_saveResearchWorkspace(this)">${escapeHtml(tr('evolution.research.workspaceSave', 'Save changes'))}</button></div></div>
  <nav class="rsw-stage">${['selection','experiment','evidence','writing','submission'].map((stage, index) => `<span class="${workspace.stage === stage ? 'is-current' : ''}">${index + 1}. ${escapeHtml(tr(`evolution.research.stage.${stage}`, stage))}</span>`).join('')}</nav>
  <div id="researchManuscriptReaderHost" class="rsw-reader-host" data-rsw-area="paper"${workspace.compilation?.build ? '' : ' hidden'}></div>
  <section data-rsw-area="shared" class="rsw-section rsw-agent-section"><div class="rsw-section-head"><div><b>Agent</b><h3>${escapeHtml(tr('evolution.research.actionTitle', 'Research action line'))}</h3></div></div>${actionConsoleHtml(workspace)}</section>
  <section data-rsw-area="experiment" class="rsw-section"><div class="rsw-section-head"><div><b>01</b><h3>${escapeHtml(tr('evolution.research.workspaceSelection', 'Thesis and falsification contract'))}</h3></div></div><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.workspaceCandidate', 'Promoted candidate'))}${selectField('selected_idea_id', workspace.selected_idea_id, ideaOptions(stream))}</label><label>${escapeHtml(tr('evolution.research.workspaceHypothesis', 'Falsifiable hypothesis'))}${field('hypothesis', workspace.hypothesis, true)}</label></div><div class="rsw-draft-bar"><button class="rs-btn" data-tofu-action="_draftResearchProtocol(this)"${state.drafting ? ' disabled' : ''}>${escapeHtml(state.drafting ? tr('evolution.research.drafting', 'Drafting protocol…') : tr('evolution.research.draftProtocol', 'Draft protocol from candidate'))}</button></div><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.protocolMetric', 'Primary metric'))}${field('protocol.primary_metric', workspace.protocol.primary_metric)}</label><label>${escapeHtml(tr('evolution.research.protocolBaseline', 'Strongest baseline'))}${field('protocol.baseline', workspace.protocol.baseline)}</label><label>${escapeHtml(tr('evolution.research.protocolDataset', 'Dataset / benchmark'))}${field('protocol.dataset', workspace.protocol.dataset)}</label><label>${escapeHtml(tr('evolution.research.protocolFalsifier', 'Result that kills the hypothesis'))}${field('protocol.falsifier', workspace.protocol.falsifier)}</label></div>${decisionRuleHtml(workspace)}<details class="rsw-advanced"><summary>${escapeHtml(tr('evolution.research.advancedProtocol', 'Advanced protocol fields (optional)'))}</summary><div class="rsw-grid cols-3"><label>${escapeHtml(tr('evolution.research.protocolSeeds', 'Random seeds'))}${field('protocol.random_seeds_csv', (workspace.protocol.random_seeds || []).join(', '))}</label><label>${escapeHtml(tr('evolution.research.protocolEnvironment', 'Environment'))}${field('protocol.environment', workspace.protocol.environment)}</label><label>${escapeHtml(tr('evolution.research.protocolResources', 'Compute, time and data budget'))}${field('protocol.resources', workspace.protocol.resources)}</label></div><div class="rsw-grid cols-2"><label>${escapeHtml(tr('evolution.research.protocolEvaluation', 'Evaluation protocol'))}${field('protocol.evaluation_protocol', workspace.protocol.evaluation_protocol, true)}</label><label>${escapeHtml(tr('evolution.research.protocolStops', 'Stop conditions, one per line'))}${field('protocol.stop_conditions_text', (workspace.protocol.stop_conditions || []).join('\n'), true)}</label></div></details><button class="rs-link-btn" data-tofu-action="_promoteResearchIdea()">${escapeHtml(tr('evolution.research.promoteIdea', 'Use selected candidate as hypothesis'))}</button></section>
  <section data-rsw-area="experiment" class="rsw-section"><div class="rsw-section-head"><div><b>02</b><h3>${escapeHtml(tr('evolution.research.capabilityTitle', 'Capability bindings'))}</h3></div><div class="rsw-section-actions"><button class="rs-btn" data-tofu-action="_autobindResearchCapabilities()">${escapeHtml(tr('evolution.research.autobind', 'Accept suggested bindings'))}</button></div></div><small>${escapeHtml(tr('evolution.research.capabilityHint', 'Suggestions help discovery; only saved exact bindings grant Agent authority.'))}</small>${capabilitiesHtml(workspace)}</section>
  <section data-rsw-area="experiment" class="rsw-section"><div class="rsw-section-head"><div><b>03</b><h3>${escapeHtml(tr('evolution.research.workspaceExperiments', 'Experiment operating log'))}</h3></div><button class="rs-btn" data-tofu-action="_addResearchRun()">+ ${escapeHtml(tr('evolution.research.addRun', 'Add run'))}</button></div>${scientificDecisionHtml(workspace)}
<div id="researchObservationRecovery"></div>${runsHtml(workspace)}${workspace.history?.count ? '<div id="researchExperimentHistory"></div>' : ''}</section>
  <section data-rsw-area="experiment" class="rsw-section"><div class="rsw-section-head"><div><b>04</b><h3>${escapeHtml(tr('evolution.research.workspaceClaims', 'Claim → evidence ledger'))}</h3></div><button class="rs-btn" data-tofu-action="_addResearchClaim()">+ ${escapeHtml(tr('evolution.research.addClaim', 'Add claim'))}</button></div>${claimsHtml(workspace)}</section>
  <section data-rsw-area="experiment" class="rsw-section"><div class="rsw-section-head"><div><b>05</b><h3>${escapeHtml(tr('evolution.research.visualTitle', 'Evidence figures and tables'))}</h3></div></div>${visualsHtml(workspace)}</section>
  <section data-rsw-area="paper" class="rsw-section"><div class="rsw-section-head"><div><b>06</b><h3>${escapeHtml(tr('evolution.research.workspaceManuscript', 'Conference manuscript'))}</h3></div></div>${manuscriptFieldsHtml(workspace)}</section>
  <section data-rsw-area="paper" class="rsw-section"><div class="rsw-section-head"><div><b>07</b><h3>${escapeHtml(tr('evolution.research.workspaceLatex', 'Built-in LaTeX project'))}</h3></div><div class="rsw-section-actions"><button class="rs-btn" data-tofu-action="_scaffoldResearchManuscript(this)">${escapeHtml(tr('evolution.research.sourceScaffold', 'Create missing files'))}</button><button class="rs-btn" data-tofu-action="_copyResearchLatex(this)">${escapeHtml(tr('evolution.research.copyLatex', 'Copy current source'))}</button></div></div>${sourceEditorHtml(workspace)}</section>
  <section data-rsw-area="paper" class="rsw-section rsw-output"><div class="rsw-section-head"><div><b>08</b><h3>${escapeHtml(tr('evolution.research.submissionTitle', 'Submission gate'))}</h3></div></div>${outputHtml(workspace)}</section>`;
}

const history = new ResearchHistory();
const observations = new ResearchObservationRecovery();
let workspaceGeneration = 0;
let actionTimer: number | null = null;
let actionPollFlight: { taskId: string; generation: number } | null = null;
let localEdits = 0;
let workspaceHost: HTMLElement | null = null;
let manuscript: ResearchManuscript | null = null;
let manuscriptVisible = false;

function invalidateReadiness(): void {
  localEdits++; state.dirty = true; state.readiness = null;
  const gates = workspaceHost?.querySelector('.rsw-output .rsw-gates');
  if (gates) { gates.classList.remove('is-ready'); patchResearchDom(gates, submissionGatesHtml(null, tr)); }
}
function workspaceEdited(event: Event): void {
  if ((event.target as Element | null)?.matches('[data-rsw-field], [data-rsw-binding]')) invalidateReadiness();
}
function bindWorkspaceHost(host: HTMLElement | null): void {
  if (host === workspaceHost) return;
  workspaceHost?.removeEventListener('input', workspaceEdited);
  workspaceHost?.removeEventListener('change', workspaceEdited);
  workspaceHost = host;
  host?.addEventListener('input', workspaceEdited); host?.addEventListener('change', workspaceEdited);
}
function releaseManuscript(): void {
  manuscript?.hide();
}
function syncManuscript(): void {
  const host = document.getElementById('researchManuscriptReaderHost');
  const build = state.workspace?.compilation?.build;
  if (!manuscriptVisible || state.loading || !host || !build) { releaseManuscript(); return; }
  manuscript ??= new ResearchManuscript();
  manuscript.show(host, { direction: state.direction, lang: state.lang === 'zh' ? 'zh' : 'en' }, build);
}
export function setResearchManuscriptVisible(visible: boolean): void {
  manuscriptVisible = visible; syncManuscript();
}
export function destroyResearchWorkspace(): void {
  state.resuming = false;
  observations.dispose();
  history.dispose();
  workspaceGeneration += 1;
  if (actionTimer !== null) window.clearTimeout(actionTimer);
  actionTimer = null;
  const draft = collect();
  if (draft) state.workspace = draft;
  state.loading = false;
  manuscriptVisible = false; manuscript?.destroy(); manuscript = null; bindWorkspaceHost(null);
}

function appendProgrammeEntry(host: HTMLElement): void {
  const bar = host.querySelector('.rsw-draft-bar');
  if (!bar || bar.querySelector('[data-research-programme]')) return;
  const button = createButton({label: tr('evolution.research.continueProgramme', 'Continue in long-horizon programme'),
    variant: 'secondary', disabled: !state.workspace?.selected_idea_id
      || !['done', 'degraded'].includes(globals()._researchStream?.status || ''), onClick: () => {
      const workspace = collect(), stream = globals()._researchStream;
      if (!workspace || !stream?.taskId || !workspace.selected_idea_id) return;
      const source = {task_id: stream.taskId, idea_id: workspace.selected_idea_id};
      const path = researchProgrammePath(source), identity = programmeCreationDraftIdentity(path);
      const drafts = createProgramDraftStore({owner: () => readLiveRuntimeBinding('_currentUserId'), scope: location.pathname, storage: () => resolveBrowserLocalStorage(window)});
      if (!drafts.read(identity) && drafts.save(identity, {fields: [], researchSource: source, seedObjective: state.direction}) !== 'saved') {
        state.error = tr('evolution.research.programmeDraftUnavailable', 'Programme draft could not be retained'); render(); return;
      }
      goToWorkspace(path);
    }});
  button.dataset.researchProgramme = 'true'; bar.append(button);
}

function render(): void {
  const host = document.getElementById('researchWorkspace');
  const stream = globals()._researchStream;
  bindWorkspaceHost(host);
  if (!host || !stream) { observations.dispose(); history.dispose(); releaseManuscript(); return; }
  if (state.loading) { releaseManuscript(); host.innerHTML = `<div class="rsw-loading">${escapeHtml(tr('evolution.research.workspaceLoading', 'Loading production workspace…'))}</div>`; return; }
  if (state.workspace) {
    patchResearchDom(host, `${state.error ? `<div class="rs-alert is-error"><span class="rs-alert-detail">${escapeHtml(state.error)}</span></div>` : ''}${state.notice ? `<div class="rs-alert"><span class="rs-alert-detail">${escapeHtml(state.notice)}</span></div>` : ''}${workspaceBody(stream, state.workspace)}`);
    appendProgrammeEntry(host);
    const historyHost = host.querySelector<HTMLElement>('#researchExperimentHistory');
    const api = globals().Api?.research;
    if (historyHost && api?.history) history.sync(historyHost, state.workspace, api.history, tr);
    else history.dispose();
    const observationHost = host.querySelector<HTMLElement>('#researchObservationRecovery');
    if (observationHost && api?.observations) observations.sync(observationHost,
      JSON.stringify([state.direction, state.lang]), api.observations, tr);
    else observations.dispose();
    syncManuscript();
  }
}

export function researchWorkspaceHtml(stream: ResearchStream): string {
  return `<section id="researchWorkspace" class="rs-panel rsw-workspace" data-direction="${escapeHtml(stream.direction)}"><div class="rsw-loading">${escapeHtml(tr('evolution.research.workspaceLoading', 'Loading production workspace…'))}</div></section>`;
}

let actionDiscoveryGeneration = -1;
let actionDiscoveryFlight: number | null = null;
function applyOwnedActionLookup(direction: string, lang: string, pending: JsonObject | null | undefined,
    running: JsonObject | null | undefined, hasTaskList: boolean, durable?: JsonObject | null, hasDurable = false): void {
    state.actionDiscoveryBlocked = false;
    if (hasTaskList || hasDurable) {
      if ((hasTaskList && (!Array.isArray(pending?.tasks) || !Array.isArray(running?.tasks)))
          || (hasDurable && (!Array.isArray(durable?.tasks)
            || typeof durable?.complete !== 'boolean'
            || (durable?.complete === false && durable.tasks.length < 2)))) {
        state.actionDiscoveryBlocked = true;
        state.error = tr('evolution.research.actionLookupUnavailable', 'Cannot check running actions. Reopen this workspace before starting another action.');
      } else {
        const candidates = new Map<string, JsonObject>();
        for (const task of [...(durable?.tasks || []), ...(pending?.tasks || []), ...(running?.tasks || [])]) {
          if (task?.kind === 'research-action' && task.meta?.direction === direction
              && task.meta?.lang === lang && ['pending','running','interrupted','unknown'].includes(task.status)
              && typeof task.id === 'string' && task.id) candidates.set(task.id, task);
        }
        if (candidates.size === 1) {
          const task = [...candidates.values()][0];
          if (task.status === 'interrupted') {
            state.interruptedTask = {id: task.id, action: text(task.meta.action), status: task.status};
            state.activeTask = null;
            state.error = tr('evolution.research.actionInterrupted', 'The task was interrupted. Recorded results are retained.');
          } else if (task.status === 'unknown') {
            state.actionDiscoveryBlocked = true;
            state.error = tr('evolution.research.actionLookupUnavailable', 'Cannot check running actions. Reopen this workspace before starting another action.');
          } else {
            state.interruptedTask = null;
            state.activeTask = { id: task.id, action: text(task.meta.action), status: task.status };
          }
        } else if (candidates.size === 0 && hasDurable && durable?.complete === true) {
          state.interruptedTask = null;
        } else if (candidates.size > 1) {
          state.actionDiscoveryBlocked = true;
          state.error = tr('evolution.research.actionLookupAmbiguous', 'Multiple actions are running for this workspace. Check their task status before starting more work.');
        }
      }
    }
}

export async function loadResearchWorkspace(direction: string, lang = 'en'): Promise<void> {
  const api = globals().Api?.research;
  if (!api?.workspace || !direction) return;
  if (state.direction === direction && state.lang === lang && state.workspace) {
    if (document.querySelector('#researchWorkspace .rsw-loading')) render();

    if (actionDiscoveryGeneration !== workspaceGeneration) {
      const generation = workspaceGeneration;
      if (actionDiscoveryFlight === generation) return;
      actionDiscoveryFlight = generation;
      const tasks = globals().Api?.tasks;
      state.actionDiscoveryBlocked = true;
      render();
      const [pending, running, durable] = await Promise.all([
        tasks?.list?.('research-action', 'pending', { direction, lang, limit: 2 }).catch(() => null),
        tasks?.list?.('research-action', 'running', { direction, lang, limit: 2 }).catch(() => null),
        api.unfinishedActions?.(direction, lang).catch(() => null),
      ]);
      if (actionDiscoveryFlight === generation) actionDiscoveryFlight = null;
      if (generation !== workspaceGeneration) return;
      applyOwnedActionLookup(direction, lang, pending, running, Boolean(tasks?.list), durable, Boolean(api.unfinishedActions));
      actionDiscoveryGeneration = generation;
      render();
    }
    if (state.activeTask?.id && actionTimer === null) void pollAction(state.activeTask.id);
    return;
  }
  if (state.loading && state.direction === direction && state.lang === lang) return;
  observations.dispose();
  history.dispose();
  const generation = ++workspaceGeneration;
  if (actionTimer !== null) window.clearTimeout(actionTimer);
  actionTimer = null;
  state.activeTask = null;
  state.interruptedTask = null;
  state.resuming = false;
  state.actionDiscoveryBlocked = true;
  releaseManuscript();
  state.readiness = null; state.dirty = false;
  state.direction = direction; state.lang = lang; state.loading = true;
  state.error = ''; state.workspace = null; state.catalog = { capabilities: [], tools: [] }; render();
  try {
    const tasks = globals().Api?.tasks;
    const [workspacePayload, capabilityPayload, pending, running, durable] = await Promise.all([
      api.workspace(direction, lang), api.capabilities?.().catch(() => null),
      tasks?.list?.('research-action', 'pending', { direction, lang, limit: 2 }).catch(() => null),
      tasks?.list?.('research-action', 'running', { direction, lang, limit: 2 }).catch(() => null),
        api.unfinishedActions?.(direction, lang).catch(() => null),
    ]);
    if (generation !== workspaceGeneration) return;
    state.workspace = (workspacePayload?.workspace || empty(direction, lang)) as Workspace;
    state.readiness = submissionReadiness(workspacePayload?.readiness);
    state.catalog = capabilityPayload?.catalog || state.catalog;
    state.selectedSourcePath = text(state.workspace.source_files?.[0]?.path);
    applyOwnedActionLookup(direction, lang, pending, running, Boolean(tasks?.list), durable, Boolean(api.unfinishedActions));
    actionDiscoveryGeneration = generation;
  } catch (error: unknown) {
    if (generation !== workspaceGeneration) return;
    state.error = error instanceof Error ? error.message : 'Workspace unavailable';
    state.workspace = empty(direction, lang);
  } finally {
    if (generation === workspaceGeneration) {
      state.loading = false; render();
      const activeTask = state.activeTask as JsonObject | null;
      if (activeTask?.id) void pollAction(activeTask.id);
    }
  }
}

async function commitWorkspace(): Promise<Workspace | null> {
  const api = globals().Api?.research;
  const workspace = collect();
  if (!api?.saveWorkspace || !workspace) return null;
  const generation = workspaceGeneration;
  const ruleError = decisionRuleError(workspace);
  if (ruleError) throw new Error(ruleError);
  const edits = localEdits;
  const payload = await api.saveWorkspace({
    direction: state.direction, lang: state.lang,
    expected_revision: workspace.revision, workspace,
  });
  if (generation !== workspaceGeneration) return null;
  if (payload?.ok === false) throw new Error(text(payload.error || 'Save rejected'));
  state.workspace = (payload?.workspace || workspace) as Workspace;
  state.dirty = edits !== localEdits;
  state.readiness = state.dirty ? null : submissionReadiness(payload?.readiness);
  state.error = '';
  return state.workspace;
}

export async function saveResearchWorkspace(button?: HTMLButtonElement): Promise<void> {
  if (button) button.disabled = true;
  try { await commitWorkspace(); }
  catch (error: unknown) { state.error = error instanceof Error ? error.message : 'Save failed'; }
  finally { if (button?.isConnected) button.disabled = false; render(); }
}

function mutate(mutator: (workspace: Workspace) => void): void {
  const workspace = collect(); if (!workspace) return;
  mutator(workspace); state.workspace = workspace; invalidateReadiness(); render();
}

export function addResearchRun(): void {
  mutate((workspace) => workspace.runs.push({
    id: `run-${Date.now()}`, label: '', status: 'planned', metric: '',
    baseline: '', delta: '', artifact_ref: '', artifact_refs: [], backend: '',
    remote_job_id: '', spec_digest: '', task_id: '', notes: '',
    tool_receipts: [], started_at: 0, finished_at: 0, updated_at: 0,
  }));
}
export function removeResearchRun(index: number): void { mutate((workspace) => workspace.runs.splice(index, 1)); }
export function addResearchClaim(): void { mutate((workspace) => workspace.claims.push({ id: `claim-${Date.now()}`, text: '', status: 'draft', evidence_refs: [] })); }
export function removeResearchClaim(index: number): void { mutate((workspace) => workspace.claims.splice(index, 1)); }

export function promoteResearchIdea(): void {
  mutate((workspace) => {
    const stream = globals()._researchStream;
    const idea = (stream?.acceptedIdeas ?? []).find((item: JsonObject, index: number) => text(item.id || `idea-${index + 1}`) === workspace.selected_idea_id) as JsonObject | undefined;
    if (!idea) return;
    workspace.selected_idea_source_task_id = text(stream?.taskId);
    workspace.hypothesis = text(idea.falsifiable_prediction || idea.novelty_claim || idea.core_mechanism);
    if (!workspace.manuscript.title) workspace.manuscript.title = text(idea.title);
  });
}

export async function draftResearchProtocol(button?: HTMLButtonElement): Promise<void> {
  const api = globals().Api?.research;
  const workspace = collect();
  if (!api?.draftProtocol || !workspace) return;
  const ideaId = text(workspace.selected_idea_id);
  if (!ideaId) { state.error = tr('evolution.research.draftNeedIdea', 'Choose a promoted candidate first'); render(); return; }
  state.workspace = workspace;
  const generation = workspaceGeneration;
  state.drafting = true; state.error = ''; state.notice = '';
  if (button) button.disabled = true;
  render();
  try {
    const sourceTaskId = text(globals()._researchStream?.taskId);
    const payload = await api.draftProtocol({ direction: state.direction, lang: state.lang, idea_id: ideaId,
      ...(sourceTaskId ? { source_task_id: sourceTaskId } : {}),
    });
    if (generation !== workspaceGeneration) return;
    const draft = (payload?.draft || {}) as JsonObject;
    workspace.selected_idea_source_task_id = text(draft.source_task_id || sourceTaskId);
    workspace.hypothesis = text(draft.hypothesis) || workspace.hypothesis;
    workspace.protocol = { ...workspace.protocol, ...((draft.protocol || {}) as JsonObject) };
    state.workspace = workspace; invalidateReadiness();
    state.notice = tr('evolution.research.draftApplied', 'Draft applied — review the protocol, then save to freeze it.');
    state.error = '';
  } catch (error: unknown) { if (generation === workspaceGeneration) state.error = error instanceof Error ? error.message : 'Protocol draft failed'; }
  finally { if (generation === workspaceGeneration) { state.drafting = false; if (button?.isConnected) button.disabled = false; render(); } }
}

export function autobindResearchCapabilities(): void {
  const workspace = collect(); if (!workspace) return;
  const tools = (state.catalog.tools || []) as JsonObject[];
  let added = 0;
  ((state.catalog.capabilities || []) as JsonObject[]).forEach((capability) => {
    const id = text(capability.id);
    if (!id || bindingFor(workspace, id)) return;
    const eligible = tools.filter((tool) => Boolean(capability.write) || tool.read_only);
    let best: JsonObject | null = null;
    let bestScore = 0;
    eligible.forEach((tool) => {
      const suggestion = ((tool.suggested_capabilities || []) as JsonObject[]).find((row) => row.id === id);
      const score = Number(suggestion?.score || 0);
      if (suggestion && score > bestScore) { best = tool; bestScore = score; }
    });
    if (!best) return;
    const chosen: JsonObject = best;
    workspace.capability_bindings.push({
      capability: id, provider: text(chosen.name).split('__')[1] || 'mcp', tool: text(chosen.name),
      schema_hash: text(chosen.schema_hash), enabled: true, argument_defaults: {}, notes: 'suggested',
    });
    added += 1;
  });
  state.workspace = workspace; invalidateReadiness();
  state.notice = added
    ? tr('evolution.research.autobindDone', 'Suggested bindings applied — review, then save to grant authority.')
    : tr('evolution.research.autobindNone', 'No new suggestions to apply.');
  state.error = '';
  render();
}

export function selectResearchSource(index: number): void {
  const workspace = collect(); if (!workspace?.source_files[index]) return;
  state.workspace = workspace; state.selectedSourcePath = text(workspace.source_files[index].path); render();
}

export async function scaffoldResearchManuscript(button?: HTMLButtonElement): Promise<void> {
  const api = globals().Api?.research;
  const workspace = collect();
  if (!api?.scaffoldManuscript || !workspace) return;
  const generation = workspaceGeneration; const edits = localEdits;
  if (button) button.disabled = true;
  try {
    const payload = await api.scaffoldManuscript({
      direction: state.direction, lang: state.lang,
      expected_revision: workspace.revision, workspace,
    });
    if (generation !== workspaceGeneration) return;
    state.workspace = payload.workspace as Workspace;
    state.dirty = edits !== localEdits;
    state.readiness = state.dirty ? null : submissionReadiness(payload.readiness);
    state.selectedSourcePath = text(state.workspace.source_files?.[0]?.path);
    state.error = '';
  } catch (error: unknown) { if (generation === workspaceGeneration) state.error = error instanceof Error ? error.message : 'Scaffold failed'; }
  finally { if (generation === workspaceGeneration) { if (button?.isConnected) button.disabled = false; render(); } }
}

function actionNeedsConfirmation(action: string): boolean {
  return action !== 'manuscript';
}

async function pollAction(taskId: string): Promise<void> {
  const tasks = globals().Api?.tasks;
  if (!tasks?.get || state.activeTask?.id !== taskId) return;
  const generation = workspaceGeneration;
  if (actionPollFlight?.taskId === taskId && actionPollFlight.generation === generation) return;
  const flight = { taskId, generation };
  actionPollFlight = flight;
  try {
    const snapshot = await tasks.get(taskId);

    if (generation !== workspaceGeneration) return;
    if (state.activeTask?.id !== taskId) return;
    if (!snapshot) throw new Error('Research action is no longer available');
    if (state.activeTask?.action === 'experiment') observations.suggestTask(taskId);
    state.activeTask = { ...state.activeTask, status: snapshot.status || 'running' };
    if (snapshot.status === 'done' || snapshot.status === 'degraded') {
      if (!state.dirty) state.workspace = (snapshot.result?.workspace || state.workspace) as Workspace;
      state.readiness = state.dirty ? null : submissionReadiness(snapshot.result?.readiness);
      state.error = '';
      state.activeTask = null;
      render();
      return;
    }
    if (snapshot.status === 'error' || snapshot.status === 'aborted' || snapshot.status === 'interrupted') {
      state.interruptedTask = snapshot.status === 'interrupted'
        ? {id: taskId, action: state.activeTask?.action || snapshot.meta?.action, status: 'interrupted'} : null;
      state.error = snapshot.status === 'interrupted'
        ? tr('evolution.research.actionInterrupted',
            'The execution process ended and this task was interrupted. Recorded results are retained; the task has not resumed automatically.')
        : text(snapshot.error?.message || snapshot.error || `Action ${snapshot.status}`);
      state.activeTask = null;
      render();
      return;
    }
    render();
    actionTimer = window.setTimeout(() => { void pollAction(taskId); }, 1400);
  } catch (error: unknown) {
    if (generation !== workspaceGeneration) return;
    state.error = error instanceof Error ? error.message : 'Action polling failed';
    if (state.activeTask?.id !== taskId) return;
    render();
    actionTimer = window.setTimeout(() => { void pollAction(taskId); }, 3000);
  } finally { if (actionPollFlight === flight) actionPollFlight = null; }
}

export async function resumeResearchAction(button?: HTMLButtonElement): Promise<void> {
  const interrupted = state.interruptedTask, api = globals().Api?.research;
  if (!interrupted?.id || !api?.resumeAction || state.activeTask || state.resuming || state.actionDiscoveryBlocked) return;
  const generation = workspaceGeneration;
  state.workspace = collect();
  state.resuming = true; state.error = '';
  if (button) button.disabled = true;
  render();
  try {
    const response = await api.resumeAction(String(interrupted.id));
    if (generation !== workspaceGeneration) return;
    if (response.ok === false) throw new Error(text(response.error || 'Resume rejected'));
    const id = text(response.task_id || response.taskId || interrupted.id);
    state.interruptedTask = null;
    state.activeTask = {id, action: interrupted.action, status: 'pending'};
    await pollAction(id);
  } catch (error: unknown) {
    if (generation === workspaceGeneration) state.error = error instanceof Error ? error.message : 'Resume failed';
  } finally {
    if (generation === workspaceGeneration) { state.resuming = false; render(); }
  }
}

export async function startResearchAction(action: string, button?: HTMLButtonElement): Promise<void> {
  const tasks = globals().Api?.tasks;
  if (!tasks?.start || state.activeTask || state.resuming || state.actionDiscoveryBlocked) return;
  const selectedModel = String(globals().config?.model || '').trim();
  if (action === 'experiment') {
    const draft = collect();
    const error = draft ? decisionRuleError(draft, true) : '';
    if (error) { state.error = error; render(); return; }
  }
  if (actionNeedsConfirmation(action)) {
    const rule = action === 'experiment' ? collect()?.protocol.decision_rule : null;
    const ruleSummary = rule ? `\n${tr('evolution.research.ruleDirection', 'Metric direction')}: ${rule.direction === 'higher' ? tr('evolution.research.ruleHigher', 'Higher is better') : tr('evolution.research.ruleLower', 'Lower is better')}\n${tr('evolution.research.ruleThreshold', 'Minimum oriented effect (negative permits noninferiority)')}: ${rule.minimum_effect}\n${tr('evolution.research.ruleMargin', 'Positive SE multiplier')}: ${rule.uncertainty_multiplier}` : '';
    const confirmed = window.confirm((action === 'compile'
      ? tr('evolution.research.compileConfirm', 'Compile the saved manuscript with the application compiler?')
      : tr('evolution.research.actionConfirm', 'This action may run code or change the bound external system. Continue with the exact saved bindings?')) + ruleSummary);
    if (!confirmed) return;
  }
  if (button) button.disabled = true;
  const generation = workspaceGeneration;
  state.activeTask = { id: '', action, status: 'submitting' };
  try {
    const workspace = await commitWorkspace();
    if (!workspace) throw new Error('Save the workspace before running an action');
    const started = await tasks.start('research-action', {
      direction: state.direction, lang: state.lang, action,
      expected_revision: workspace.revision,
      confirm_external_writes: actionNeedsConfirmation(action),
      ...(action !== 'compile' && selectedModel ? { model: selectedModel } : {}),
    });
    if (generation !== workspaceGeneration) return;
    if (started?.ok === false) throw new Error(text(started.error || 'Start rejected'));
    const taskId = text(started?.taskId);
    if (!taskId) throw new Error('Research action returned no task ID');
    state.activeTask = { id: taskId, action, status: 'pending' };
    state.error = ''; render();
    void pollAction(taskId);
  } catch (error: unknown) {
    if (generation !== workspaceGeneration) return;
    state.activeTask = null;
    state.error = error instanceof Error ? error.message : 'Action failed to start'; render();
  } finally { if (button?.isConnected) button.disabled = false; }
}

export async function abortResearchAction(): Promise<void> {
  const task = state.activeTask;
  const tasks = globals().Api?.tasks;
  if (!task || !tasks?.abort) return;
  try {
    const response = await tasks.abort(task.id);
    if (response?.ok === false) throw new Error(text(response.error || 'Cancel rejected'));
    if (state.activeTask !== task) return;
    state.activeTask = { ...task, status: 'stopping' };
  } catch (error: unknown) { state.error = error instanceof Error ? error.message : 'Cancel failed'; }
  render();
}

export function copyResearchLatex(button?: HTMLButtonElement): void {
  const workspace = collect(); if (!workspace) return;
  const selected = workspace.source_files.find((row) => row.path === state.selectedSourcePath);
  const source = selected?.content || researchWorkspaceLatex(workspace);
  const write = globals()._safeClipboardWrite;
  const promise = typeof write === 'function' ? Promise.resolve(write(source)) : navigator.clipboard.writeText(source);
  void promise.then(() => { if (button) button.textContent = tr('evolution.research.copied', 'Copied'); }).catch((error) => console.debug('[Research] LaTeX copy failed', error));
}

export function installResearchWorkspaceGlobals(): void {
  const target = globals();
  target._loadResearchWorkspace = loadResearchWorkspace;
  target._saveResearchWorkspace = saveResearchWorkspace;
  target._addResearchRun = addResearchRun;
  target._removeResearchRun = removeResearchRun;
  target._addResearchClaim = addResearchClaim;
  target._removeResearchClaim = removeResearchClaim;
  target._copyResearchLatex = copyResearchLatex;
  target._promoteResearchIdea = promoteResearchIdea;
  target._draftResearchProtocol = draftResearchProtocol;
  target._autobindResearchCapabilities = autobindResearchCapabilities;
  target._startResearchAction = startResearchAction;
  target._abortResearchAction = abortResearchAction;
  target._resumeResearchAction = resumeResearchAction;
  target._scaffoldResearchManuscript = scaffoldResearchManuscript;
  target._selectResearchSource = selectResearchSource;
}
installResearchWorkspaceGlobals();
