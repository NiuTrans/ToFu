import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import type { ResearchStream } from './research-runtime';

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
  workspace(direction: string, lang: string): Promise<JsonObject>;
  saveWorkspace(payload: JsonObject): Promise<JsonObject>;
  capabilities(): Promise<JsonObject>;
  scaffoldManuscript(payload: JsonObject): Promise<JsonObject>;
  sourceArchiveUrl(direction: string, lang: string): string;
};

type TasksApi = {
  start(kind: string, payload: JsonObject): Promise<JsonObject>;
  get(taskId: string): Promise<JsonObject | null>;
  abort(taskId: string): Promise<JsonObject | null>;
};

type ResearchWorkspaceWindow = Window & {
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
  _startResearchAction?: typeof startResearchAction;
  _abortResearchAction?: typeof abortResearchAction;
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
  selectedSourcePath: string;
  activeTask: JsonObject | null;
} = {
  direction: '', lang: 'en', workspace: null,
  catalog: { capabilities: [], tools: [] }, loading: false, error: '',
  selectedSourcePath: '', activeTask: null,
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
    stage: 'selection', selected_idea_id: '', hypothesis: '',
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
      engine: '', compiled_at: 0,
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
  copy.capability_bindings = [];
  document.querySelectorAll<HTMLSelectElement>('#researchWorkspace [data-rsw-binding]').forEach((node) => {
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
  if (typeof protocol.random_seeds_csv === 'string') {
    protocol.random_seeds = protocol.random_seeds_csv.split(',').map((item: string) => Number(item.trim())).filter(Number.isInteger);
    delete protocol.random_seeds_csv;
  }
  if (typeof protocol.stop_conditions_text === 'string') {
    protocol.stop_conditions = protocol.stop_conditions_text.split('\n').map((item: string) => item.trim()).filter(Boolean);
    delete protocol.stop_conditions_text;
  }
}

function ideaOptions(stream: ResearchStream): Array<[string, string]> {
  const candidates: Array<[string, string]> = ((stream.acceptedIdeas ?? []) as JsonObject[]).map((idea, index) => [
    text(idea.id || `idea-${index + 1}`), text(idea.title || `Candidate ${index + 1}`),
  ]);
  return [['', tr('paper.research.workspaceChooseIdea', 'Choose a candidate')], ...candidates];
}

function readiness(workspace: Workspace): Array<{ ok: boolean; label: string }> {
  const protocol = workspace.protocol || {};
  const manuscript = workspace.manuscript || {};
  return [
    { ok: Boolean(protocol.primary_metric && protocol.baseline && protocol.dataset && protocol.falsifier), label: tr('paper.research.gateProtocol', 'Falsification protocol is frozen') },
    { ok: workspace.runs.some((run) => run.status === 'passed' && (run.artifact_ref || run.artifact_refs?.length)), label: tr('paper.research.gateRun', 'A passing run has durable evidence') },
    { ok: workspace.claims.length > 0 && workspace.claims.every((claim) => claim.status === 'supported' && claim.evidence_refs?.length), label: tr('paper.research.gateClaims', 'Every claim points to evidence') },
    { ok: Boolean(manuscript.title && manuscript.abstract && manuscript.method && manuscript.results && manuscript.limitations && workspace.source_files.length), label: tr('paper.research.gateManuscript', 'Paper sections and source tree are complete') },
    { ok: workspace.compilation?.status === 'passing', label: tr('paper.research.gateCompile', 'Current source digest compiles') },
  ];
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
  return `<select class="rsw-input" data-rsw-binding="${escapeHtml(capability)}"><option value="">${escapeHtml(tr('paper.research.capabilityUnbound', 'Not bound'))}</option>${eligible.map((tool) => {
    const suggested = tool.suggested_capabilities?.some((row: JsonObject) => row.id === capability);
    const label = `${suggested ? '★ ' : ''}${tool.server} · ${tool.name.replace(`mcp__${tool.server}__`, '')}`;
    return `<option value="${escapeHtml(tool.name)}"${selected === tool.name ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('')}</select>`;
}

function capabilitiesHtml(workspace: Workspace): string {
  const capabilities = state.catalog.capabilities || [];
  if (!capabilities.length) return `<div class="rsw-empty">${escapeHtml(tr('paper.research.noCapabilities', 'No MCP research tools are connected. Local Agent actions remain available.'))}</div>`;
  return `<div class="rsw-capability-matrix">${capabilities.map((capability: JsonObject) => {
    const binding = bindingFor(workspace, capability.id);
    const selected = text(binding?.tool);
    const liveTool = (state.catalog.tools || []).find((row: JsonObject) => row.name === selected);
    const drifted = Boolean(binding?.schema_hash && liveTool?.schema_hash && binding.schema_hash !== liveTool.schema_hash);
    const detail = drifted
      ? tr('paper.research.capabilityDrift', 'Tool schema changed · review and save to rebind')
      : (capability.write ? tr('paper.research.capabilityWrite', 'May change an external system') : tr('paper.research.capabilityRead', 'Read-only observation'));
    return `<label class="rsw-capability-row ${drifted ? 'is-drifted' : ''}"><span><b>${escapeHtml(capability.id)}</b><small>${escapeHtml(detail)}</small></span>${capabilityOptions(capability.id, selected)}</label>`;
  }).join('')}</div>`;
}

function runsHtml(workspace: Workspace): string {
  if (!workspace.runs.length) return `<div class="rsw-empty">${escapeHtml(tr('paper.research.workspaceNoRuns', 'No runs recorded. Freeze the protocol before spending compute.'))}</div>`;
  return workspace.runs.map((run, index) => `<article class="rsw-record"><div class="rsw-record-head"><strong>${escapeHtml(text(run.label) || `Run ${index + 1}`)}</strong><span class="rsw-run-proof">${escapeHtml(text(run.task_id || 'manual'))} · ${(run.tool_receipts || []).length} receipts</span><button data-tofu-action="_removeResearchRun(${index})">×</button></div><div class="rsw-grid cols-3"><label>${escapeHtml(tr('paper.research.runLabel', 'Run label'))}${field(`runs.${index}.label`, run.label)}</label><label>${escapeHtml(tr('paper.research.runStatus', 'Status'))}${selectField(`runs.${index}.status`, run.status, [['planned','planned'],['running','running'],['passed','passed'],['failed','failed'],['inconclusive','inconclusive']])}</label><label>${escapeHtml(tr('paper.research.runArtifact', 'Primary evidence reference'))}${field(`runs.${index}.artifact_ref`, run.artifact_ref)}</label><label>${escapeHtml(tr('paper.research.runMetric', 'Metric'))}${field(`runs.${index}.metric`, run.metric)}</label><label>${escapeHtml(tr('paper.research.runBaseline', 'Baseline'))}${field(`runs.${index}.baseline`, run.baseline)}</label><label>${escapeHtml(tr('paper.research.runDelta', 'Delta'))}${field(`runs.${index}.delta`, run.delta)}</label><label>${escapeHtml(tr('paper.research.runBackend', 'Execution backend'))}${field(`runs.${index}.backend`, run.backend)}</label><label>${escapeHtml(tr('paper.research.runRemoteJob', 'Remote job ID'))}${field(`runs.${index}.remote_job_id`, run.remote_job_id)}</label></div><label>${escapeHtml(tr('paper.research.runNotes', 'Notes and failure analysis'))}${field(`runs.${index}.notes`, run.notes, true)}</label></article>`).join('');
}

function claimsHtml(workspace: Workspace): string {
  if (!workspace.claims.length) return `<div class="rsw-empty">${escapeHtml(tr('paper.research.workspaceNoClaims', 'No paper claims yet. Add only claims that point to run or literature evidence.'))}</div>`;
  return workspace.claims.map((claim, index) => `<article class="rsw-record"><div class="rsw-record-head"><strong>C${String(index + 1).padStart(2, '0')}</strong><button data-tofu-action="_removeResearchClaim(${index})">×</button></div><label>${escapeHtml(tr('paper.research.claimText', 'Paper claim'))}${field(`claims.${index}.text`, claim.text, true)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('paper.research.claimStatus', 'Evidence status'))}${selectField(`claims.${index}.status`, claim.status, [['draft','draft'],['supported','supported'],['contested','contested'],['rejected','rejected']])}</label><label>${escapeHtml(tr('paper.research.claimEvidence', 'Evidence references (comma-separated)'))}${field(`claims.${index}.evidence_refs_csv`, (claim.evidence_refs || []).join(', '))}</label></div></article>`).join('');
}

function visualsHtml(workspace: Workspace): string {
  const renderRows = (kind: 'figures' | 'tables', rows: JsonObject[]) => rows.map((row, index) => `<article class="rsw-visual"><div><span>${kind === 'figures' ? 'FIG' : 'TAB'} ${String(index + 1).padStart(2, '0')}</span><strong>${escapeHtml(text(row.title) || tr('paper.research.visualUntitled', 'Untitled visual'))}</strong>${selectField(`${kind}.${index}.status`, row.status, [['planned','planned'],['generated','generated'],['verified','verified'],['rejected','rejected']])}</div><label>${escapeHtml(tr('paper.research.visualCaption', 'Caption and evidence claim'))}${field(`${kind}.${index}.caption`, row.caption, true)}</label><div class="rsw-grid cols-3"><label>${escapeHtml(tr('paper.research.visualData', 'Data reference'))}${field(`${kind}.${index}.data_ref`, row.data_ref)}</label><label>${escapeHtml(tr('paper.research.visualScript', 'Rendering script'))}${field(`${kind}.${index}.script_ref`, row.script_ref)}</label><label>${escapeHtml(tr('paper.research.visualOutput', 'Output asset'))}${field(`${kind}.${index}.output_ref`, row.output_ref)}</label></div></article>`).join('');
  if (!workspace.figures.length && !workspace.tables.length) {
    return `<div class="rsw-empty">${escapeHtml(tr('paper.research.visualEmpty', 'No figure or table plan yet. Analyze evidence to create traceable visuals.'))}</div>`;
  }
  return `<div class="rsw-visual-grid">${renderRows('figures', workspace.figures)}${renderRows('tables', workspace.tables)}</div>`;
}

function actionConsoleHtml(workspace: Workspace): string {
  const active = state.activeTask;
  const actions: Array<[string, string, string]> = [
    ['experiment', tr('paper.research.actionExperiment', 'Run experiment'), tr('paper.research.actionExperimentHint', 'Execute the frozen falsifier and record receipts')],
    ['analyze', tr('paper.research.actionAnalyze', 'Analyze evidence'), tr('paper.research.actionAnalyzeHint', 'Ablations, uncertainty, figures and tables')],
    ['manuscript', tr('paper.research.actionManuscript', 'Write paper'), tr('paper.research.actionManuscriptHint', 'Turn supported claims into conference prose')],
    ['compile', tr('paper.research.actionCompile', 'Compile source'), tr('paper.research.actionCompileHint', 'Use the bound compiler on the current digest')],
    ['publish', tr('paper.research.actionPublish', 'Publish project'), tr('paper.research.actionPublishHint', 'Sync exact sources through the bound provider')],
  ];
  return `<div class="rsw-action-console"><div class="rsw-action-status ${active ? 'is-active' : ''}"><i></i><span>${escapeHtml(active ? `${active.action} · ${active.status}` : tr('paper.research.actionIdle', 'Agent ready · no action running'))}</span>${active ? `<button data-tofu-action="_abortResearchAction()">${escapeHtml(tr('paper.research.actionStop', 'Stop'))}</button>` : ''}</div><div class="rsw-action-track">${actions.map(([action, label, hint], index) => {
    const compileBlocked = action === 'compile' && !bindingFor(workspace, 'manuscript.compile');
    const publishBlocked = action === 'publish' && !bindingFor(workspace, 'publication.push');
    const disabled = Boolean(active || compileBlocked || publishBlocked);
    return `<button class="rsw-action" data-tofu-action="_startResearchAction('${action}',this)"${disabled ? ' disabled' : ''}><span>${String(index + 1).padStart(2, '0')}</span><b>${escapeHtml(label)}</b><small>${escapeHtml(compileBlocked || publishBlocked ? tr('paper.research.actionNeedsBinding', 'Bind the required capability first') : hint)}</small></button>`;
  }).join('')}</div></div>`;
}

function manuscriptFieldsHtml(workspace: Workspace): string {
  const manuscript = workspace.manuscript;
  return `<label>${escapeHtml(tr('paper.research.manuscriptTitle', 'Paper title'))}${field('manuscript.title', manuscript.title)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('paper.research.workspaceVenue', 'Target venue'))}${field('manuscript.venue', manuscript.venue)}</label><label>${escapeHtml(tr('paper.research.manuscriptKeywords', 'Keywords'))}${field('manuscript.keywords', manuscript.keywords)}</label></div><label>${escapeHtml(tr('paper.research.manuscriptAbstract', 'Abstract'))}${field('manuscript.abstract', manuscript.abstract, true, '', 5)}</label><div class="rsw-grid cols-2"><label>${escapeHtml(tr('paper.research.manuscriptIntroduction', 'Introduction and contributions'))}${field('manuscript.introduction', manuscript.introduction, true, '', 6)}</label><label>${escapeHtml(tr('paper.research.manuscriptRelated', 'Related work and novelty delta'))}${field('manuscript.related_work', manuscript.related_work, true, '', 6)}</label><label>${escapeHtml(tr('paper.research.manuscriptMethod', 'Method'))}${field('manuscript.method', manuscript.method, true, '', 7)}</label><label>${escapeHtml(tr('paper.research.manuscriptExperiments', 'Experimental setup'))}${field('manuscript.experiments', manuscript.experiments, true, '', 7)}</label><label>${escapeHtml(tr('paper.research.manuscriptResults', 'Results narrative'))}${field('manuscript.results', manuscript.results, true, '', 7)}</label><label>${escapeHtml(tr('paper.research.manuscriptLimitations', 'Limitations'))}${field('manuscript.limitations', manuscript.limitations, true, '', 7)}</label><label>${escapeHtml(tr('paper.research.manuscriptConclusion', 'Conclusion'))}${field('manuscript.conclusion', manuscript.conclusion, true, '', 5)}</label><label>${escapeHtml(tr('paper.research.manuscriptEthics', 'Ethics and broader impact'))}${field('manuscript.ethics', manuscript.ethics, true, '', 5)}</label></div>`;
}

function sourceEditorHtml(workspace: Workspace): string {
  const files = workspace.source_files || [];
  if (!files.length) return `<div class="rsw-source-empty"><p>${escapeHtml(tr('paper.research.sourceEmpty', 'No source tree yet. Create the conference scaffold after drafting the manuscript fields.'))}</p><button class="rs-btn" data-tofu-action="_scaffoldResearchManuscript(this)">${escapeHtml(tr('paper.research.sourceScaffold', 'Create LaTeX project'))}</button></div>`;
  let selectedIndex = Math.max(0, files.findIndex((row) => row.path === state.selectedSourcePath));
  if (selectedIndex < 0) selectedIndex = 0;
  const selected = files[selectedIndex];
  state.selectedSourcePath = text(selected.path);
  return `<div class="rsw-source-shell"><aside>${files.map((row, index) => `<button class="${index === selectedIndex ? 'is-current' : ''}" data-tofu-action="_selectResearchSource(${index})"><span>${escapeHtml(text(row.path).split('/').pop() || row.path)}</span><small>${escapeHtml(text(row.path))}</small></button>`).join('')}</aside><div class="rsw-source-editor"><div><code>${escapeHtml(selected.path)}</code><span>${escapeHtml(text(selected.sha256).slice(0, 12) || tr('paper.research.sourceUnsaved', 'unsaved'))}</span></div>${field(`source_files.${selectedIndex}.content`, selected.content, true, '', 22)}</div></div>`;
}

function outputHtml(workspace: Workspace): string {
  const gates = readiness(workspace);
  const ready = gates.every((gate) => gate.ok);
  const api = globals().Api?.research;
  const archive = api?.sourceArchiveUrl?.(state.direction, state.lang) || '#';
  return `<div class="rsw-output-grid"><div class="rsw-gates ${ready ? 'is-ready' : ''}"><strong>${escapeHtml(ready ? tr('paper.research.submissionReady', 'Submission gate passed') : tr('paper.research.submissionBlocked', 'Submission gate blocked'))}</strong>${gates.map((gate) => `<span class="${gate.ok ? 'is-ok' : ''}">${gate.ok ? '✓' : '○'} ${escapeHtml(gate.label)}</span>`).join('')}</div><div class="rsw-compiler"><strong>${escapeHtml(tr('paper.research.compilerTitle', 'Compile and publication receipts'))}</strong><span>${escapeHtml(`${workspace.compilation?.status || 'not_run'} · ${workspace.compilation?.engine || 'no compiler bound'}`)}</span><code>${escapeHtml(workspace.compilation?.detail || workspace.publication?.detail || 'No receipt')}</code><div><a class="rs-btn" href="${escapeHtml(archive)}">${escapeHtml(tr('paper.research.sourceDownload', 'Download source ZIP'))}</a>${workspace.publication?.project_url ? `<a class="rs-btn" href="${escapeHtml(workspace.publication.project_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(tr('paper.research.openPublished', 'Open published project'))}</a>` : ''}</div></div></div>`;
}

function workspaceBody(stream: ResearchStream, workspace: Workspace): string {
  return `<div class="rsw-head"><div><div class="rs-eyebrow">${escapeHtml(tr('paper.research.workspaceKicker', 'Research production workspace'))}</div><h2>${escapeHtml(tr('paper.research.workspaceTitle', 'From selected thesis to submission-ready source'))}</h2><p>${escapeHtml(tr('paper.research.workspaceHint', 'The notebook keeps tools, evidence and paper source on one revision line.'))}</p></div><div class="rsw-save"><span>r${workspace.revision}</span><button class="rs-btn" data-tofu-action="_saveResearchWorkspace(this)">${escapeHtml(tr('paper.research.workspaceSave', 'Save changes'))}</button></div></div>
  <nav class="rsw-stage">${['selection','experiment','evidence','writing','submission'].map((stage, index) => `<span class="${workspace.stage === stage ? 'is-current' : ''}">${index + 1}. ${escapeHtml(tr(`paper.research.stage.${stage}`, stage))}</span>`).join('')}</nav>
  <section class="rsw-section rsw-agent-section"><div class="rsw-section-head"><div><b>Agent</b><h3>${escapeHtml(tr('paper.research.actionTitle', 'Research action line'))}</h3></div></div>${actionConsoleHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>01</b><h3>${escapeHtml(tr('paper.research.workspaceSelection', 'Thesis and falsification contract'))}</h3></div></div><div class="rsw-grid cols-2"><label>${escapeHtml(tr('paper.research.workspaceCandidate', 'Promoted candidate'))}${selectField('selected_idea_id', workspace.selected_idea_id, ideaOptions(stream))}</label><label>${escapeHtml(tr('paper.research.workspaceHypothesis', 'Falsifiable hypothesis'))}${field('hypothesis', workspace.hypothesis, true)}</label></div><div class="rsw-grid cols-3"><label>${escapeHtml(tr('paper.research.protocolMetric', 'Primary metric'))}${field('protocol.primary_metric', workspace.protocol.primary_metric)}</label><label>${escapeHtml(tr('paper.research.protocolBaseline', 'Strongest baseline'))}${field('protocol.baseline', workspace.protocol.baseline)}</label><label>${escapeHtml(tr('paper.research.protocolDataset', 'Dataset / benchmark'))}${field('protocol.dataset', workspace.protocol.dataset)}</label><label>${escapeHtml(tr('paper.research.protocolFalsifier', 'Result that kills the hypothesis'))}${field('protocol.falsifier', workspace.protocol.falsifier)}</label><label>${escapeHtml(tr('paper.research.protocolSeeds', 'Random seeds'))}${field('protocol.random_seeds_csv', (workspace.protocol.random_seeds || []).join(', '))}</label><label>${escapeHtml(tr('paper.research.protocolEnvironment', 'Environment'))}${field('protocol.environment', workspace.protocol.environment)}</label></div><div class="rsw-grid cols-2"><label>${escapeHtml(tr('paper.research.protocolEvaluation', 'Evaluation protocol'))}${field('protocol.evaluation_protocol', workspace.protocol.evaluation_protocol, true)}</label><label>${escapeHtml(tr('paper.research.protocolStops', 'Stop conditions, one per line'))}${field('protocol.stop_conditions_text', (workspace.protocol.stop_conditions || []).join('\n'), true)}</label></div><label>${escapeHtml(tr('paper.research.protocolResources', 'Compute, time and data budget'))}${field('protocol.resources', workspace.protocol.resources)}</label><button class="rs-link-btn" data-tofu-action="_promoteResearchIdea()">${escapeHtml(tr('paper.research.promoteIdea', 'Use selected candidate as hypothesis'))}</button></section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>02</b><h3>${escapeHtml(tr('paper.research.capabilityTitle', 'Capability bindings'))}</h3></div><small>${escapeHtml(tr('paper.research.capabilityHint', 'Suggestions help discovery; only saved exact bindings grant Agent authority.'))}</small></div>${capabilitiesHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>03</b><h3>${escapeHtml(tr('paper.research.workspaceExperiments', 'Experiment operating log'))}</h3></div><button class="rs-btn" data-tofu-action="_addResearchRun()">+ ${escapeHtml(tr('paper.research.addRun', 'Add run'))}</button></div>${runsHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>04</b><h3>${escapeHtml(tr('paper.research.workspaceClaims', 'Claim → evidence ledger'))}</h3></div><button class="rs-btn" data-tofu-action="_addResearchClaim()">+ ${escapeHtml(tr('paper.research.addClaim', 'Add claim'))}</button></div>${claimsHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>05</b><h3>${escapeHtml(tr('paper.research.visualTitle', 'Evidence figures and tables'))}</h3></div></div>${visualsHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>06</b><h3>${escapeHtml(tr('paper.research.workspaceManuscript', 'Conference manuscript'))}</h3></div></div>${manuscriptFieldsHtml(workspace)}</section>
  <section class="rsw-section"><div class="rsw-section-head"><div><b>07</b><h3>${escapeHtml(tr('paper.research.workspaceLatex', 'Built-in LaTeX project'))}</h3></div><div class="rsw-section-actions"><button class="rs-btn" data-tofu-action="_scaffoldResearchManuscript(this)">${escapeHtml(tr('paper.research.sourceScaffold', 'Create missing files'))}</button><button class="rs-btn" data-tofu-action="_copyResearchLatex(this)">${escapeHtml(tr('paper.research.copyLatex', 'Copy current source'))}</button></div></div>${sourceEditorHtml(workspace)}</section>
  <section class="rsw-section rsw-output"><div class="rsw-section-head"><div><b>08</b><h3>${escapeHtml(tr('paper.research.submissionTitle', 'Submission gate'))}</h3></div></div>${outputHtml(workspace)}</section>`;
}

function render(): void {
  const host = document.getElementById('researchWorkspace');
  const stream = globals()._researchStream;
  if (!host || !stream) return;
  if (state.loading) { host.innerHTML = `<div class="rsw-loading">${escapeHtml(tr('paper.research.workspaceLoading', 'Loading production workspace…'))}</div>`; return; }
  if (state.workspace) {
    host.innerHTML = `${state.error ? `<div class="rs-alert is-error"><span class="rs-alert-detail">${escapeHtml(state.error)}</span></div>` : ''}${workspaceBody(stream, state.workspace)}`;
  }
}

export function researchWorkspaceHtml(stream: ResearchStream): string {
  return `<section id="researchWorkspace" class="rs-panel rsw-workspace" data-direction="${escapeHtml(stream.direction)}"><div class="rsw-loading">${escapeHtml(tr('paper.research.workspaceLoading', 'Loading production workspace…'))}</div></section>`;
}

export async function loadResearchWorkspace(direction: string, lang = 'en'): Promise<void> {
  const api = globals().Api?.research;
  if (!api?.workspace || !direction) return;
  if (state.loading || (state.direction === direction && state.lang === lang && state.workspace)) { render(); return; }
  state.direction = direction; state.lang = lang; state.loading = true;
  state.error = ''; state.workspace = null; state.catalog = { capabilities: [], tools: [] }; render();
  try {
    const [workspacePayload, capabilityPayload] = await Promise.all([
      api.workspace(direction, lang), api.capabilities?.().catch(() => null),
    ]);
    state.workspace = (workspacePayload?.workspace || empty(direction, lang)) as Workspace;
    state.catalog = capabilityPayload?.catalog || state.catalog;
    state.selectedSourcePath = text(state.workspace.source_files?.[0]?.path);
  } catch (error: unknown) {
    state.error = error instanceof Error ? error.message : 'Workspace unavailable';
    state.workspace = empty(direction, lang);
  } finally { state.loading = false; render(); }
}

async function commitWorkspace(): Promise<Workspace | null> {
  const api = globals().Api?.research;
  const workspace = collect();
  if (!api?.saveWorkspace || !workspace) return null;
  const payload = await api.saveWorkspace({
    direction: state.direction, lang: state.lang,
    expected_revision: workspace.revision, workspace,
  });
  state.workspace = (payload?.workspace || workspace) as Workspace;
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
  mutator(workspace); state.workspace = workspace; render();
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
    workspace.hypothesis = text(idea.falsifiable_prediction || idea.novelty_claim || idea.core_mechanism);
    if (!workspace.manuscript.title) workspace.manuscript.title = text(idea.title);
  });
}

export function selectResearchSource(index: number): void {
  const workspace = collect(); if (!workspace?.source_files[index]) return;
  state.workspace = workspace; state.selectedSourcePath = text(workspace.source_files[index].path); render();
}

export async function scaffoldResearchManuscript(button?: HTMLButtonElement): Promise<void> {
  const api = globals().Api?.research;
  const workspace = collect();
  if (!api?.scaffoldManuscript || !workspace) return;
  if (button) button.disabled = true;
  try {
    const payload = await api.scaffoldManuscript({
      direction: state.direction, lang: state.lang,
      expected_revision: workspace.revision, workspace,
    });
    state.workspace = payload.workspace as Workspace;
    state.selectedSourcePath = text(state.workspace.source_files?.[0]?.path);
    state.error = '';
  } catch (error: unknown) { state.error = error instanceof Error ? error.message : 'Scaffold failed'; }
  finally { if (button?.isConnected) button.disabled = false; render(); }
}

function actionNeedsConfirmation(action: string): boolean {
  return action !== 'manuscript';
}

async function pollAction(taskId: string): Promise<void> {
  const tasks = globals().Api?.tasks;
  if (!tasks?.get || state.activeTask?.id !== taskId) return;
  try {
    const snapshot = await tasks.get(taskId);
    if (!snapshot) throw new Error('Research action is no longer available');
    state.activeTask = { ...state.activeTask, status: snapshot.status || 'running' };
    if (snapshot.status === 'done') {
      state.workspace = (snapshot.result?.workspace || state.workspace) as Workspace;
      state.error = '';
      state.activeTask = null;
      render();
      return;
    }
    if (snapshot.status === 'error' || snapshot.status === 'aborted') {
      state.error = text(snapshot.error?.message || snapshot.error || `Action ${snapshot.status}`);
      state.activeTask = null;
      render();
      return;
    }
    render();
    window.setTimeout(() => { void pollAction(taskId); }, 1400);
  } catch (error: unknown) {
    state.error = error instanceof Error ? error.message : 'Action polling failed';
    state.activeTask = null; render();
  }
}

export async function startResearchAction(action: string, button?: HTMLButtonElement): Promise<void> {
  const tasks = globals().Api?.tasks;
  if (!tasks?.start || state.activeTask) return;
  if (actionNeedsConfirmation(action)) {
    const confirmed = window.confirm(tr('paper.research.actionConfirm', 'This action may run code or change the bound external system. Continue with the exact saved bindings?'));
    if (!confirmed) return;
  }
  if (button) button.disabled = true;
  try {
    const workspace = await commitWorkspace();
    if (!workspace) throw new Error('Save the workspace before running an action');
    const started = await tasks.start('research-action', {
      direction: state.direction, lang: state.lang, action,
      expected_revision: workspace.revision,
      confirm_external_writes: actionNeedsConfirmation(action),
    });
    const taskId = text(started?.taskId);
    if (!taskId) throw new Error('Research action returned no task ID');
    state.activeTask = { id: taskId, action, status: 'pending' };
    state.error = ''; render();
    void pollAction(taskId);
  } catch (error: unknown) {
    state.error = error instanceof Error ? error.message : 'Action failed to start'; render();
  } finally { if (button?.isConnected) button.disabled = false; }
}

export async function abortResearchAction(): Promise<void> {
  const task = state.activeTask;
  const tasks = globals().Api?.tasks;
  if (!task || !tasks?.abort) return;
  await tasks.abort(task.id);
  state.activeTask = { ...task, status: 'stopping' }; render();
}

export function copyResearchLatex(button?: HTMLButtonElement): void {
  const workspace = collect(); if (!workspace) return;
  const selected = workspace.source_files.find((row) => row.path === state.selectedSourcePath);
  const source = selected?.content || researchWorkspaceLatex(workspace);
  const write = globals()._safeClipboardWrite;
  const promise = typeof write === 'function' ? Promise.resolve(write(source)) : navigator.clipboard.writeText(source);
  void promise.then(() => { if (button) button.textContent = tr('paper.research.copied', 'Copied'); }).catch((error) => console.debug('[Research] LaTeX copy failed', error));
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
  target._startResearchAction = startResearchAction;
  target._abortResearchAction = abortResearchAction;
  target._scaffoldResearchManuscript = scaffoldResearchManuscript;
  target._selectResearchSource = selectResearchSource;
}
installResearchWorkspaceGlobals();
