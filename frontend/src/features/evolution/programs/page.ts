/** Exploration notebook: stable input islands, authoritative progress and paged
 * evidence. Shared controls come from ui/. Writes are explicit and fenced by
 * route epoch; the read controller owns visibility, cancellation and polling. */
import '../../../styles/application/26-evolution-programs.css';
import { t, type I18nKey } from '../../../i18n';
import { createButton, createField } from '../../../ui';
import type { CreateProgramBody, ExplorationReceipt, ExplorationStep, ProgramAction, ReplanProposal } from '../../../api/evolution-programs';
import { createProgramController, emptyProgramState, type ProgramApi, type ProgramState } from './controller';
import { resolveBrowserLocalStorage } from '../../../core/browser-storage';
import { createProgramDraftStore, programmeCreationDraftIdentity, type DraftAttempt, type ProgramDraft } from './drafts';
import { mountProgramSources } from './source-reader';
import { mountCurrentExperiment } from './current-experiment';
import { programLimitHint } from './limits';
import { mountExecutionOptions, executionPolicyText } from './execution-options';
import { mountHypothesisComparison } from './hypothesis-comparison';
import { mountExperimentCharts } from './experiment-charts';
import { mountResourceLauncher } from '../../../rich-response/resource-launcher';
import { richChartText } from '../evidence-charts';
import { evidenceCharts } from '../evidence-charts';
import '../../../styles/application/26-evolution-observations.css';
import '../../../styles/application/03-data-chart.css';

const copy = (key: string) => t(`evolution.program.${key}` as Extract<I18nKey, `evolution.program.${string}`>);
function node<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text?: string): HTMLElementTagNameMap[K] {
  const result = document.createElement(tag); result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
const lines = (value: string) => value.split('\n').map(item => item.trim()).filter(Boolean);
const issue = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
function navigationState(button: HTMLButtonElement, unavailable: boolean) {
  // A focused read control remains in the keyboard order at a page boundary.
  // Native disabled would blur it during polling or after admitting a page.
  button.setAttribute('aria-disabled', String(unavailable));
}
function reconcile(host: HTMLElement, children: HTMLElement[]) {
  children.forEach((child, index) => {
    if (host.children[index] !== child) host.insertBefore(child, host.children[index] || null);
  });
  while (host.children.length > children.length) host.lastElementChild?.remove();
}
function field(form: HTMLFormElement, name: string, label: string, hint = '', rows = 0, limit = 4000) {
  const control = rows ? node('textarea') : node('input');
  control.name = name; control.className = 'ui-input'; control.maxLength = limit;
  if (control instanceof HTMLTextAreaElement) control.rows = rows;
  form.append(createField({label, hint, control, controlId: `exp-${name}`}).el);
  return control;
}
const blank = (text: string) => node('p', 'exp-muted', text);
function checksList(checks: string[]): HTMLUListElement {
  const list = node('ul', 'exp-checks'); checks.forEach(check => list.append(node('li', '', check))); return list;
}
function hypothesisDetails(step: ExplorationStep): HTMLDetailsElement | null {
  if (!step.hypothesis) return null;
  const details = node('details', 'exp-hypothesis');
  details.append(node('summary', '', copy('hypothesis.title')), blank(copy('hypothesis.note')));
  const fields = node('dl');
  for (const key of ['problem', 'audience', 'baseline', 'scenario', 'mechanism', 'expected_value', 'falsifier', 'assumptions', 'evidence_refs'] as const) {
    fields.append(node('dt', '', copy('hypothesis.' + key)));
    const value = step.hypothesis[key];
    const content = node('dd');
    content.append(Array.isArray(value) ? checksList(value) : node('p', '', value));
    fields.append(content);
  }
  details.append(fields); return details;
}

function replanDetails(proposal: ReplanProposal): HTMLDetailsElement {
  const details = node('details', 'exp-hypothesis exp-replan');
  details.append(node('summary', '', copy('replanProposal')), blank(copy('replanPending')),
    node('p', '', `${copy('recoveryOf')}: ${proposal.failed_sequence}`), node('p', '', proposal.reason));
  for (const step of proposal.steps) {
    details.append(node('h4', '', step.title), blank(step.instruction), checksList(step.checks));
    const hypothesis = hypothesisDetails(step); if (hypothesis) details.append(hypothesis);
  }
  return details;
}

export function mountProgramWorkbench(root: HTMLElement, ports: {
  api: ProgramApi; path: string; navigate: (path: string) => void; openTask: (id: string) => void;
  draftOwner?: () => unknown; draftScope?: string; draftStorage?: Storage;
}) {
  let state: ProgramState = emptyProgramState();
  let path = ports.path, selectedId = '', epoch = 0, disposed = false, busy = false;
  let pending: {signature: string; key: string; createdId?: string} | undefined;
  const expansionRequests = new Map<string, {signature: string; key: string}>();
  let modelSignature = '';
  let programSignature = '';

  let frontierSignature = '';
  let outputSignature = '';
  const programButtons = new Map<string, HTMLButtonElement>();
  const receiptRows = new Map<number, HTMLDetailsElement>();
  const memoryRows = new Map<string, {signature: string; element: HTMLDetailsElement}>();
  const questionForms = new Map<string, HTMLFormElement>();
  const questionDraftSavers = new Map<string, () => ProgramDraft>();
  const expansionDrafts = new Map<string, string[]>();
  const drafts = createProgramDraftStore({owner: ports.draftOwner || (() => null),
    scope: ports.draftScope || '', storage: () => ports.draftStorage || resolveBrowserLocalStorage(window)});
  root.classList.add('evo-workspace', 'evo-programs');
  root.replaceChildren();

  const sidebar = node('aside', 'exp-sidebar');
  const brand = node('a', 'exp-brand', 'Tofu'); brand.href = '#/evolution';
  sidebar.append(brand, node('p', 'exp-kicker', copy('title')));
  const newButton = createButton({label: copy('new'), variant: 'primary', onClick: () => ports.navigate('evolution/programs/new')});
  sidebar.append(newButton, node('h2', 'exp-sidebar-heading', copy('programs')));
  const catalog = node('nav', 'exp-catalog'); catalog.setAttribute('aria-label', copy('programs')); sidebar.append(catalog);
  const catalogPager = node('nav', 'exp-catalog-pager'); catalogPager.setAttribute('aria-label', copy('catalogNavigation'));
  const catalogPrevious = createButton({label: copy('catalogPrevious'), size: 'sm', onClick: () => controller.catalogPage('previous')});
  const catalogNext = createButton({label: copy('catalogNext'), size: 'sm', onClick: () => controller.catalogPage('next')});
  const catalogFirst = createButton({label: copy('catalogFirst'), size: 'sm', onClick: () => controller.catalogPage('first')});
  catalogPager.append(catalogPrevious, catalogNext, catalogFirst); sidebar.append(catalogPager);
  const nav = node('nav', 'exp-secondary-nav');
  for (const [href, label] of [['#/chat', copy('chat')], ['#/evolution/runs', copy('executionStudio')]]) {
    const link = node('a', '', label); link.href = href; nav.append(link);
  }
  sidebar.append(nav);
  const main = node('main', 'evo-main exp-main');
  const head = node('header', 'exp-page-head');
  const heading = node('div'); heading.append(node('p', 'exp-kicker', copy('title')));
  const title = node('h1', 'exp-title'); title.tabIndex = -1;
  const status = node('p', 'exp-status'); status.setAttribute('role', 'status'); heading.append(title, status);
  const actions = node('div', 'exp-actions'); head.append(heading, actions);
  const refreshButton = createButton({label: copy('refresh'), size: 'sm', onClick: () => void controller.refresh()});
  actions.append(refreshButton);
  const actionButtons = new Map<ProgramAction, HTMLButtonElement>();
  for (const [action, label, hint] of [
    ['start', 'resume', 'resume'], ['pause', 'pause', 'pauseNote'], ['retry', 'retry', 'retryNote'], ['cancel', 'cancel', 'cancelNote'],
  ] as const) {
    const button = createButton({label: copy(label), title: copy(hint), size: 'sm',
      variant: action === 'cancel' ? 'danger' : action === 'start' ? 'primary' : 'secondary',
      onClick: () => void command(action)});
    button.dataset.programAction = action; actions.append(button); actionButtons.set(action, button);
  }
  const notice = node('p', 'exp-notice'); notice.setAttribute('role', 'status');
  const errorBox = node('section', 'exp-error'); errorBox.hidden = true;
  errorBox.append(node('p', '', copy('readFailed')));
  const errorDetail = node('details'); errorDetail.append(node('summary', '', copy('technical')));
  const errorText = node('pre'); errorDetail.append(errorText); errorBox.append(errorDetail);
  errorBox.append(createButton({label: copy('retryRead'), size: 'sm', onClick: () => void controller.refresh()}));
  main.append(head, notice, errorBox);
  const draftNotice = node('p', 'exp-muted'); draftNotice.setAttribute('role', 'status'); draftNotice.hidden = true;
  draftNotice.dataset.draftNotice = 'true'; main.append(draftNotice);
  type DraftControl = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
  let researchSource: CreateProgramBody['research_source'];
  const creationIdentity = (route: string) => {
    const hash = window.location.hash.replace(/^#\/?/, '');
    return programmeCreationDraftIdentity(hash.split('?')[0] === route.split('?')[0] ? hash : route);
  };
  let creatorIdentity = creationIdentity(ports.path);
  function draftStatus() {
    const status = drafts.status();
    if (status) {draftNotice.hidden = false; draftNotice.textContent = copy('draft.' + status);}
  }
  function restoreDraft(identity: string[], controls: DraftControl[]): ProgramDraft | undefined {
    const saved = drafts.read(identity);
    if (!saved) {
      if (drafts.status() === 'unavailable' && typeof ports.draftOwner?.() === 'number') draftStatus();
      return undefined;
    }
    if (identity[0] === 'create' && saved.fields.length === 0 && saved.researchSource) {
      saved.fields = controls.map(control => control.name === 'objective' ? saved.seedObjective || '' : control.value);
    }
    if (identity[0] === 'create') researchSource = saved.researchSource ? {...saved.researchSource} : undefined;
    if (identity[0] === 'create' && [5, 11, 14].includes(saved.fields.length) && controls.length === 15) {
      saved.fields.push(...controls.slice(saved.fields.length).map(control => control.value));
    }
    if (saved.fields.length !== controls.length) {draftNotice.hidden = false; draftNotice.textContent = copy('draft.unavailable'); return undefined;}
    try {
      controls.forEach((control, index) => {
        if (control.name === 'model' && saved.fields[index]) {
          const value: unknown = JSON.parse(saved.fields[index]!);
          if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid stored model');
        }
      });
    } catch {draftNotice.hidden = false; draftNotice.textContent = copy('draft.unavailable'); return undefined;}
    controls.forEach((control, index) => {
      if (control instanceof HTMLSelectElement && saved.fields[index]
          && ![...control.options].some(option => option.value === saved.fields[index])) {
        control.append(new Option(copy('unavailableModel'), saved.fields[index]));
      }
      control.value = saved.fields[index]!;
    });
    draftStatus(); return saved;
  }
  function saveDraft(identity: string[], controls: DraftControl[], attempt?: DraftAttempt): ProgramDraft {
    const saved: ProgramDraft = {fields: controls.map(control => control.value), ...(attempt ? {attempt: {...attempt}} : {}),
      ...(identity[0] === 'create' && researchSource ? {researchSource: {...researchSource}} : {})};
    if (disposed) return saved;
    if ((identity[0] === 'create' ? saved.fields.slice(0, 3).some(Boolean) || saved.fields.slice(5).some(Boolean) : saved.fields.some(Boolean)) || attempt) {drafts.save(identity, saved); draftStatus();}
    else drafts.clear(identity);
    return saved;
  }

  const creator = node('section', 'exp-creator');
  creator.append(node('h2', '', copy('empty')));
  const form = node('form', 'exp-create-form');
  const objective = field(form, 'objective', copy('objective'), '', 5); objective.required = true;
  objective.placeholder = copy('objectivePlaceholder');
  const acceptance = field(form, 'acceptance', copy('acceptance'), copy('acceptanceHint'), 4, 10000);
  const project = field(form, 'project', copy('project'), copy('projectHint'), 0, 4096);
  const model = node('select'); model.name = 'model'; model.className = 'ui-input';
  form.append(createField({label: copy('model'), control: model, controlId: 'exp-model'}).el);
  const replanPolicy = node('select'); replanPolicy.name = 'auto-replan'; replanPolicy.className = 'ui-input';
  replanPolicy.append(new Option(copy('replanAuto'), 'auto'), new Option(copy('replanManual'), 'manual'));
  form.append(createField({label: copy('replanPolicy'), hint: copy('replanPolicyHint'),
    control: replanPolicy, controlId: 'exp-replan-policy'}).el);
  const executionOptions = mountExecutionOptions(form, copy);

  const pacing = node('select'); pacing.name = 'advance-mode';
  pacing.append(new Option(copy('pacingContinuous'), ''), new Option(copy('pacingCheckpoint'), 'checkpoint'));
  form.append(createField({label: copy('pacing'), hint: copy('pacingHint'), control: pacing, controlId: 'exp-advance-mode'}).el);
  const submit = createButton({label: copy('start'), variant: 'primary', type: 'submit'});
  const formStatus = node('p', 'exp-form-status'); formStatus.setAttribute('role', 'status');
  const savePlan = createButton({label: copy('savePlan'), onClick: () => void create(false)});
  const createActions = node('div', 'ui-row'); createActions.append(savePlan, submit);
  form.append(createActions, blank(copy('preparationHint')), formStatus); creator.append(form); main.append(creator);
  const createControls = [objective, acceptance, project, model, replanPolicy, ...executionOptions.controls, pacing];
  const sourceNote = blank(copy('researchSourceNote')); sourceNote.dataset.researchSourceNote = 'true';
  form.prepend(sourceNote);
  const createDefaults = createControls.map(control => control.value);
  const initialCreator = restoreDraft(creatorIdentity, createControls);
  pending = initialCreator?.attempt;
  let creatorRestored = !!initialCreator, creatorTouched = false;
  const saveCreatorDraft = () => {creatorTouched = true; return saveDraft(creatorIdentity, createControls, pending);};
  form.addEventListener('input', saveCreatorDraft); form.addEventListener('change', saveCreatorDraft);

  const detail = node('div', 'exp-detail'); detail.hidden = true;

  const controlRoom = node('section', 'ui-stack exp-control-room');
  const nextAction = node('p'); nextAction.setAttribute('role', 'status'); nextAction.dataset.nextAction = 'true';
  const pacingStatus = node('p'); pacingStatus.dataset.pacingStatus = 'true';
  const changePacing = createButton({label: copy('pacingCheckpoint'), size: 'sm', onClick: () => void setPacing()});
  changePacing.dataset.programPacing = 'true';
  controlRoom.append(node('h2', '', copy('controlRoom')), nextAction, pacingStatus, changePacing);
  const attention = node('section', 'exp-attention'); attention.hidden = true;
  const attentionNote = blank(copy('attentionNote'));
  attentionNote.setAttribute('role', 'status');
  attention.append(node('h2', '', copy('attention')), attentionNote);
  const attentionDetails = node('details'); attentionDetails.append(node('summary', '', copy('technical')));
  const attentionText = node('pre'); attentionDetails.append(attentionText); attention.append(attentionDetails);
  const layout = node('div', 'exp-layout');
  const trajectory = node('div', 'exp-trajectory');
  const current = node('section', 'exp-current');
  const currentExperiment = mountCurrentExperiment(current, {copy, hypothesis: hypothesisDetails, replan: replanDetails, openTask: ports.openTask});
  const original = node('section', 'exp-original');
  const objectiveDetails = node('details', 'exp-objective');
  objectiveDetails.append(node('summary', '', copy('objective')));
  const objectiveText = node('p', 'exp-objective-text');
  objectiveText.tabIndex = 0; objectiveText.setAttribute('role', 'region');
  objectiveText.setAttribute('aria-label', copy('objective')); objectiveDetails.append(objectiveText);
  const originalChecks = node('div');
  const originalPolicy = blank('');
  const policyDetails = node('details', 'exp-execution-options');
  policyDetails.append(node('summary', '', copy('frozenExecutionOptions')));
  const policyText = node('p', 'exp-policy-summary'); policyDetails.append(policyText);
  const projectHeading = node('h4', '', copy('project'));
  const projectPath = node('p', 'exp-project-path');
  original.append(node('h2', '', copy('original')), blank(copy('originalNote')), objectiveDetails,
    originalChecks, originalPolicy, policyDetails, projectHeading, projectPath);
  let objectiveProgramId = '';
  const queue = node('section', 'exp-queue');
  trajectory.append(current, original, queue);
  const memory = node('section', 'exp-memory');
  memory.append(node('h2', '', copy('history')), blank(copy('historyNote')));
  const counts = node('p', 'exp-memory-count'); memory.append(counts);
  const pager = node('div', 'exp-pager');
  const older = createButton({label: copy('older'), size: 'sm', onClick: () => {
    if (older.getAttribute('aria-disabled') !== 'true') controller.page(Math.max(0, state.historyAfter - 20));
  }});
  const newer = createButton({label: copy('newer'), size: 'sm', onClick: () => {
    if (newer.getAttribute('aria-disabled') !== 'true') controller.page(state.history?.next_cursor ?? undefined);
  }});
  const latest = createButton({label: copy('latest'), size: 'sm', onClick: () => {
    if (latest.getAttribute('aria-disabled') !== 'true') controller.page();
  }});
  pager.append(older, newer, latest); memory.append(pager);
  const ledger = node('div', 'exp-ledger'); memory.append(ledger);
  const lookupHistory = node('div', 'exp-ledger'); lookupHistory.dataset.semanticMemory = 'true'; memory.append(lookupHistory);
  layout.append(trajectory, memory);
  const human = node('section', 'exp-questions');
  const output = node('section', 'exp-output');
  const sourceHost = node('section');
  const sources = mountProgramSources(sourceHost, {
    read: ports.api.getStageOutcome ? (id, task, offset, signal, kind) => ports.api.getStageOutcome!(id, task, offset, signal, kind) : undefined,
    visible: () => !disposed && !document.hidden, fallbackFocus: () => title.focus({preventScroll: true}),
  });
  const deliveryWarning = blank(''); deliveryWarning.dataset.deliveryWarning = 'true';
  deliveryWarning.setAttribute('role', 'status'); deliveryWarning.hidden = true;
  const telemetry = node('p', 'exp-telemetry');
  const comparisonHost = node('section');
  const comparison = mountHypothesisComparison(comparisonHost, {copy, sources: receipt => sources.controls(receipt)});

  const expandHost = node('details', 'exp-expansion'); expandHost.append(node('summary', '', copy('extend')), blank(copy('extendHint')));
  const expandForm = node('form', 'exp-expand-form');
  const expandTitle = field(expandForm, 'experiment-title', copy('experimentTitle'), '', 0, 200);
  const instruction = field(expandForm, 'instruction', copy('instruction'), '', 4, 2400);
  const experimentChecks = field(expandForm, 'experiment-checks', copy('experimentChecks'), '', 3, 10000);
  [expandTitle, instruction, experimentChecks].forEach(control => control.required = true);
  const saveExperiment = createButton({label: copy('saveExperiment'), type: 'submit', variant: 'primary'});
  const expandStatus = node('p'); expandStatus.setAttribute('role', 'status');
  expandForm.append(saveExperiment, expandStatus); expandHost.append(expandForm);
  const expansionControls = [expandTitle, instruction, experimentChecks];
  const saveExpansionDraft = () => {if (selectedId) saveDraft(['expand', selectedId], expansionControls, expansionRequests.get(selectedId));};
  expandForm.addEventListener('input', saveExpansionDraft); expandForm.addEventListener('change', saveExpansionDraft);
  const observations=document.createElement('section');const charts=mountExperimentCharts(observations);
  const resourceHost=node('section','exp-resources');
  const resources=mountResourceLauncher(resourceHost,richChartText);
  detail.append(controlRoom, resourceHost, attention, observations, comparisonHost, layout, human, output, sourceHost, expandHost, telemetry); main.append(detail);
  root.append(sidebar, main);

  const controller = createProgramController(ports.api, {
    update(next) { state = next; render(); }, visible: () => !disposed && !document.hidden,
    schedule: (callback, delay) => window.setTimeout(callback, delay), cancel: id => window.clearTimeout(id),
  });
  const showWriteError = (reason: unknown, target: HTMLElement = notice) => {
    if (!disposed) { target.textContent = issue(reason); target.setAttribute('role', 'alert'); }
  };
  function setBusy(value: boolean) {
    busy = value;
    submit.disabled = value; savePlan.disabled = value; saveExperiment.disabled = value;
    render();
    submit.textContent = copy(value ? 'starting' : 'start');
  }
  async function command(action: ProgramAction) {
    if (busy || !selectedId || !state.program) return;
    if (['start', 'resume', 'retry'].includes(action) && !state.program.current && programLimitHint(state.program)) return;
    const captured = epoch, id = selectedId;
    // Only this explicit control grants continuation. Creator ACK retries keep
    // using start, whose initial-activation fence cannot approve a checkpoint.
    const program = state.program;
    const legacyActivationAmbiguous = program.started_at === undefined
      && (program.status !== 'ready' || program.current !== null || program.settled_through > 0);
    const commandAction = action === 'start' && (program.started_at != null || legacyActivationAmbiguous) ? 'resume' : action;
    setBusy(true); notice.textContent = copy('commandPending');
    try {
      await ports.api.commandProgram(id, commandAction);
      if (!disposed && captured === epoch) { notice.textContent = copy('commandSaved'); await controller.afterWrite(); }
    } catch (reason) { if (!disposed && captured === epoch) showWriteError(reason); }
    finally { if (!disposed) { setBusy(false); render(); } }
  }
  async function setPacing() {
    const program = state.program;
    if (busy || !program || !ports.api.setProgramPacing || ['completed', 'cancelled'].includes(program.status)) return;
    const captured = epoch;
    setBusy(true); notice.textContent = copy('commandPending');
    try {
      await ports.api.setProgramPacing(program.program_id, program.advance_mode === 'checkpoint' ? 'continuous' : 'checkpoint');
      if (!disposed && captured === epoch) {notice.textContent = copy('pacingSaved'); await controller.afterWrite();}
    } catch (reason) {if (!disposed && captured === epoch) showWriteError(reason);}
    finally {if (!disposed) setBusy(false);}
  }
  form.addEventListener('submit', event => {event.preventDefault(); void create(true);});
  async function create(launchAfterSave: boolean) {
    if (busy || !form.reportValidity()) return;
    const requirements = lines(acceptance.value);
    if (requirements.length > 16 || requirements.some(item => item.length > 600)) {
      formStatus.textContent = copy('invalidAcceptance'); return;
    }
    let policy: Partial<CreateProgramBody>;
    try {policy = executionOptions.read();} catch (reason) {formStatus.textContent = issue(reason); return;}
    const body: CreateProgramBody = {objective: objective.value.trim(), auto_replan: replanPolicy.value === 'auto', ...policy,
      ...(pacing.value === 'checkpoint' ? {advance_mode: 'checkpoint' as const} : {}),
      ...(requirements.length ? {acceptance: requirements} : {}),
      ...(project.value.trim() ? {project_path: project.value.trim()} : {}),
      ...(model.value ? {model: JSON.parse(model.value)} : {})};
    if (researchSource) body.research_source = {...researchSource};
    const signature = JSON.stringify(body), captured = epoch, submittedIdentity = [...creatorIdentity];
    // A lost ACK resolves the original request even if its model disappeared;
    // current backend admission still owns any new task dispatch.
    if (pending?.signature !== signature && !executionOptions.modelsAvailable()) {
      formStatus.textContent = copy('executionModelUnavailable'); return;
    }
    if (!pending || pending.signature !== signature) pending = {signature, key: crypto.randomUUID()};
    const request = pending; const submittedDraft = saveCreatorDraft();
    setBusy(true); formStatus.textContent = copy(launchAfterSave ? 'starting' : 'savingPlan');
    try {
      if (!request.createdId) {
        request.createdId = (await ports.api.createProgram(body, request.key)).program.program_id;
        const stored = drafts.read(submittedIdentity);
        if (stored?.attempt?.key === request.key && JSON.stringify(stored.fields) === JSON.stringify(submittedDraft.fields)) {
          submittedDraft.attempt = {...request}; drafts.save(submittedIdentity, submittedDraft);
        }
      }
      const program = (await ports.api.getProgram(request.createdId)).program;
      const neverActivated = program.started_at === null || (program.started_at === undefined
        && program.status === 'ready' && program.settled_through === 0 && !program.current);
      if (launchAfterSave && neverActivated && (program.status === 'ready' || program.status === 'paused')) {
        await ports.api.commandProgram(request.createdId, 'start');
      }
      if (!disposed && captured === epoch) {
        formStatus.textContent = copy('saved'); pending = undefined;
        drafts.clear(submittedIdentity, submittedDraft);
        if (JSON.stringify(createControls.map(control => control.value)) === JSON.stringify(submittedDraft.fields)) {
          [objective, acceptance, project].forEach(control => {control.value = '';});
        }
        ports.navigate(`evolution/programs/${encodeURIComponent(request.createdId)}`);
      }
    } catch (reason) { if (!disposed && captured === epoch) showWriteError(reason, formStatus); }
    finally { if (!disposed) setBusy(false); }
  }
  expandForm.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !selectedId || !state.program || !expandForm.reportValidity()) return;
    const checks = lines(experimentChecks.value);
    if (!checks.length || checks.length > 16 || checks.some(item => item.length > 600)) {
      expandStatus.textContent = copy('invalidChecks'); return;
    }
    const step: ExplorationStep = {title: expandTitle.value.trim(), kind: 'experiment', instruction: instruction.value.trim(), checks};
    const id = selectedId, captured = epoch, signature = id + JSON.stringify(step);
    let request = expansionRequests.get(id);
    if (!request || request.signature !== signature) {
      request = {signature, key: crypto.randomUUID()}; expansionRequests.set(id, request);
    }
    const submittedDraft = saveDraft(['expand', id], expansionControls, request);
    setBusy(true); expandStatus.textContent = copy('saving');
    try {
      await ports.api.appendProgramSteps(id, [step], request.key);
      drafts.clear(['expand', id], submittedDraft);
      if (!disposed && captured === epoch) {
        expandStatus.textContent = copy('saved'); expansionRequests.delete(id); expansionDrafts.delete(id);
        if (JSON.stringify(expansionControls.map(control => control.value)) === JSON.stringify(submittedDraft.fields)) expansionControls.forEach(control => control.value = '');
        await controller.afterWrite();
      }
    } catch (reason) { if (!disposed && captured === epoch) showWriteError(reason, expandStatus); }
    finally { if (!disposed) setBusy(false); }
  });

  function receiptRow(receipt: ExplorationReceipt): HTMLDetailsElement {
    const row = node('details', 'exp-receipt'); row.dataset.sequence = String(receipt.sequence); row.dataset.status = receipt.outcome;
    const summary = node('summary');
    summary.append(node('span', 'exp-sequence', String(receipt.sequence).padStart(2, '0')),
      node('span', 'exp-receipt-title', receipt.step.title), node('span', 'exp-receipt-outcome', copy('state.' + receipt.outcome)));
    row.append(summary);
    row.addEventListener('toggle', () => {
      if (!row.open || row.childElementCount > 1) return;
      const body = node('div', 'exp-receipt-body'); body.append(node('h3', '', copy('result')),
        node('pre', 'exp-result', receipt.result || copy('resultEmpty')));
      const visual=evidenceCharts(root.ownerDocument,receipt.result);if(visual)body.insertBefore(visual,body.children[1]!);
      const sourceActions = sources.controls(receipt);if (sourceActions) body.append(sourceActions);
      const hypothesis = hypothesisDetails(receipt.step); if (hypothesis) body.append(hypothesis);
      if (receipt.replan) body.append(replanDetails(receipt.replan));
      if (receipt.actuals_complete === false) body.append(blank(copy('usageIncomplete')));
      body.append(node('h3', '', copy('review')));
      const checks = receipt.review?.requirement_results || [];
      if (!checks.length) body.append(blank(copy('reviewEmpty')));
      const list = node('ul', 'exp-checks');
      const requirements = receipt.step.kind === 'final' ? [...(state.program?.acceptance || []), ...receipt.step.checks] : receipt.step.checks;
      checks.forEach(check => {
        const index = Number(check.requirement_id.split('-')[1]) - 1;
        const item = node('li'); item.append(node('strong', '', requirements[index] || check.requirement_id),
          node('span', 'exp-check-outcome', copy('outcome.' + check.outcome)), node('p', '', check.evidence_ref)); list.append(item);
      });
      body.append(list);
      if (receipt.error) {
        const why = node('details'); why.append(node('summary', '', copy('technical')), node('pre', 'exp-result', receipt.error)); body.append(why);
      }
      const link = node('a', 'exp-link', copy('openRoom')); link.href = `#/evolution/runs/${encodeURIComponent(receipt.run_id)}`; body.append(link);
      row.append(body);
    });
    return row;
  }
  function renderQuestions() {
    const keep = new Set<string>(); const forms: HTMLFormElement[] = [];
    for (const question of state.questions) {
      const id = `${selectedId}:${state.run?.run_id || ''}:${question.guidance_id}`; keep.add(id);
      let questionForm = questionForms.get(id);
      if (!questionForm) {
        questionForm = node('form', 'exp-question'); questionForm.dataset.question = question.guidance_id;
        questionForm.append(node('h2', '', copy('questions')));
        const fields = question.questions.length ? question.questions : [{question: question.question, options: question.options}];
        const draftIdentity = ['answer', selectedId, state.run?.run_id || '', question.guidance_id];
        const answers = fields.map((entry, index) => {
          const control = field(questionForm!, `answer-${question.guidance_id}-${index}`, entry.question, '', 2, 6000); control.required = true;
          for (const option of entry.options || []) questionForm!.append(createButton({label: option, size: 'sm', onClick: () => { control.value = option; saveAnswerDraft(); control.focus(); }}));
          return control;
        });
        restoreDraft(draftIdentity, answers);
        const saveAnswerDraft = () => saveDraft(draftIdentity, answers);
        questionDraftSavers.set(id, saveAnswerDraft);
        questionForm.addEventListener('input', saveAnswerDraft); questionForm.addEventListener('change', saveAnswerDraft);
        const send = createButton({label: copy('answer'), type: 'submit', variant: 'primary'});
        const answerStatus = node('p'); answerStatus.setAttribute('role', 'status'); questionForm.append(send, answerStatus);
        const runId = state.run?.run_id || '';
        questionForm.addEventListener('submit', async event => {
          event.preventDefault(); if (send.disabled || !questionForm!.reportValidity()) return;
          const captured = epoch; send.disabled = true;
          const submittedDraft = saveAnswerDraft();
          try {
            await ports.api.answerRun(runId, question.guidance_id, question.questions.length
              ? JSON.stringify(answers.map((answer, index) => ({index, answer: answer.value.trim()}))) : answers[0]!.value.trim());
            drafts.clear(draftIdentity, submittedDraft);
            if (JSON.stringify(answers.map(control => control.value)) === JSON.stringify(submittedDraft.fields)) answers.forEach(control => control.value = '');
            if (!disposed && captured === epoch) await controller.afterWrite();
          } catch (reason) { if (!disposed && captured === epoch) showWriteError(reason, answerStatus); }
          finally { if (!disposed) send.disabled = false; }
        });
        questionForms.set(id, questionForm);
      }
      forms.push(questionForm);
    }
    questionForms.forEach((_form, key) => { if (!keep.has(key)) {questionForms.delete(key); questionDraftSavers.delete(key);} });
    reconcile(human, forms); human.hidden = forms.length === 0;
  }
  function render() {
    sourceNote.hidden = !researchSource;
    if (disposed) return;
    if (!creatorRestored && !creatorTouched) {
      const saved = restoreDraft(creatorIdentity, createControls);
      if (saved) {creatorRestored = true; pending = saved.attempt;}
    }
    const keep = new Set<string>(); const buttons: HTMLButtonElement[] = [];
    const dates = new Intl.DateTimeFormat(document.documentElement.lang || undefined, {dateStyle: 'short', timeStyle: 'medium'});
    state.programs.forEach(program => {
      keep.add(program.program_id);
      let button = programButtons.get(program.program_id);
      if (!button) {
        button = createButton({label: program.objective, variant: 'ghost', onClick: () => ports.navigate(`evolution/programs/${encodeURIComponent(program.program_id)}`)});
        const metadata = node('span', 'exp-program-meta');
        metadata.append(node('span', 'exp-program-state'), document.createTextNode(' · '), node('time'));
        button.replaceChildren(node('span', 'exp-program-label', program.objective), metadata);
        button.classList.add('exp-program-link'); button.dataset.programId = program.program_id; programButtons.set(program.program_id, button);
      }
      if (button.firstElementChild!.textContent !== program.objective) button.firstElementChild!.textContent = program.objective;
      const status = button.querySelector<HTMLElement>('.exp-program-state')!;
      const stateLabel = copy('state.' + program.status);
      if (status.textContent !== stateLabel) status.textContent = stateLabel;
      const created = button.querySelector('time')!;
      const date = new Date(program.created_at * 1000);
      const validDate = Number.isFinite(date.getTime());
      created.hidden = !validDate;
      if (validDate) {
        created.dateTime = date.toISOString();
        const label = dates.format(date);
        if (created.textContent !== label) created.textContent = label;
      }
      button.title = stateLabel;
      button.setAttribute('aria-current', String(selectedId === program.program_id)); buttons.push(button);
    });
    programButtons.forEach((_button, key) => { if (!keep.has(key)) programButtons.delete(key); });
    reconcile(catalog, buttons);
    catalogPager.setAttribute('aria-busy', String(state.loading));
    navigationState(catalogPrevious, state.catalogPending || !state.catalogPrevious);
    navigationState(catalogNext, state.catalogPending || !state.catalogNext);
    navigationState(catalogFirst, state.catalogPending || (!state.catalogPrevious && !state.catalogNext));
    const nextModelSignature = JSON.stringify(state.models);
    if (nextModelSignature !== modelSignature || !model.childElementCount) {
      modelSignature = nextModelSignature; const saved = model.value, savedLabel = model.selectedOptions[0]?.textContent || ''; 
      model.replaceChildren(new Option(copy('automatic'), ''));
      state.models.forEach(option => model.append(new Option(option.label, JSON.stringify(option.model))));
      if (saved && ![...model.options].some(option => option.value === saved)) {
        const unavailable = new Option(copy('unavailableModel') + ': ' + savedLabel, saved);
        unavailable.disabled = true; model.append(unavailable);
      }
      model.value = saved;
    }
    executionOptions.refreshModels(state.models);
    errorBox.hidden = state.errors.length === 0; errorText.textContent = state.errors.join('\n');
    creator.hidden = !!selectedId; detail.hidden = !state.program || !selectedId;
    const program = state.program;
    title.textContent = program?.objective || copy(selectedId ? 'loading' : 'creating');
    status.textContent = program ? copy('state.' + program.status) : copy(selectedId ? 'loading' : 'subtitle');
    const limit = program ? programLimitHint(program) : null;
    actionButtons.forEach((button, action) => {
      button.hidden = !program || !(action === 'start' ? ['ready', 'paused'].includes(program.status)
        : action === 'pause' ? program.status === 'active'
        : action === 'retry' ? program.status === 'needs_attention' && !program.current
        : !['completed', 'cancelled'].includes(program.status));
      const blocked = !!limit && !program?.current && (action === 'start' || action === 'resume' || action === 'retry');
      button.disabled = busy || !program || blocked;
      button.title = copy(blocked ? limit! : action === 'start' ? 'resume' : action + 'Note');
    });
    currentExperiment.update(program, state.run, state.runError);
    resources.update(state.run?.run_id?{runId:state.run.run_id}:undefined);
    if (!program) return;
    comparison.update(program, state.history?.experiments || []);
    const awaitingHuman = state.questions.length > 0;
    const nextKey = limit || (awaitingHuman ? 'nextActionQuestions'
      : program.status === 'ready' ? 'nextActionReady'
      : program.status === 'paused' ? (program.current ? 'nextActionDraining' : 'nextActionCheckpoint')
      : program.status === 'needs_attention' ? 'nextActionAttention'
      : program.status === 'completed' ? 'nextActionCompleted'
      : program.status === 'cancelled' ? 'nextActionCancelled' : 'nextActionActive');
    nextAction.textContent = copy(nextKey);
    pacingStatus.textContent = `${copy('pacing')}: ${copy(program.advance_mode === 'checkpoint' ? 'pacingCheckpoint' : 'pacingContinuous')}`;
    changePacing.hidden = !ports.api.setProgramPacing || ['completed', 'cancelled'].includes(program.status);
    changePacing.disabled = busy;
    changePacing.textContent = copy(program.advance_mode === 'checkpoint' ? 'switchContinuous' : 'switchCheckpoint');
    const frozenPolicy = executionPolicyText(program, state.models, copy);
    if (policyText.textContent !== frozenPolicy) policyText.textContent = frozenPolicy;
    if (objectiveProgramId !== program.program_id) {
      objectiveProgramId = program.program_id; objectiveDetails.open = false; policyDetails.open = false;
    }
    if (objectiveText.textContent !== program.objective) objectiveText.textContent = program.objective;
    attention.hidden = program.status !== 'needs_attention' && !limit;
    attentionNote.textContent = copy(limit || 'attentionNote'); attentionText.textContent = program.error;
    const signature = JSON.stringify([program.status, program.execution_options, program.auto_replan, program.replan_proposal, program.current?.step, program.acceptance, program.queue]);
    if (signature !== programSignature) {
      programSignature = signature;
      originalChecks.replaceChildren(checksList(program.acceptance));
      originalPolicy.textContent = copy(program.auto_replan ? 'replanAuto' : 'replanManual');
      const path = program.execution_options?.project_path || '';
      projectHeading.hidden = projectPath.hidden = !path; projectPath.textContent = path;
    }
    const upcoming = program.current ? program.queue.slice(1) : program.queue;
    const nextFrontierSignature = JSON.stringify([program.program_id, upcoming]);
    if (nextFrontierSignature !== frontierSignature) {
      frontierSignature = nextFrontierSignature;
      queue.replaceChildren(node('h2', '', copy('next')));
      if (!upcoming.length) queue.append(blank(copy('queueEmpty')));
      const list = node('ol', 'exp-frontier');
      upcoming.forEach(step => {
        const item = node('li'); item.append(node('span', 'exp-phase', copy('phase.' + step.kind)), node('strong', '', step.title));

        const protocol = node('details', 'exp-protocol'); protocol.dataset.protocol = step.kind;
        const instruction = node('pre', 'exp-result', step.instruction); instruction.tabIndex = 0;
        protocol.append(node('summary', '', copy('protocol')), instruction,
          node('h3', '', copy('experimentChecks')), checksList(step.checks));
        item.append(protocol);
        const hypothesis = hypothesisDetails(step); if (hypothesis) item.append(hypothesis);
        list.append(item);
      });
      queue.append(list);
    }
    counts.textContent = `${copy('savedCount')}: ${program.settled_through.toLocaleString()}`;
    pager.setAttribute('aria-busy', String(state.loading));
    ledger.setAttribute('aria-busy', String(state.historyPending));
    navigationState(older, state.historyPending || state.historyAfter === 0);
    navigationState(newer, state.historyPending || state.history?.next_cursor == null);
    navigationState(latest, state.historyPending);
    const receipts = state.history?.experiments || [];
    charts.update(program, receipts);
    const sequences = new Set(receipts.map(receipt => receipt.sequence));
    receiptRows.forEach((_row, sequence) => { if (!sequences.has(sequence)) receiptRows.delete(sequence); });
    if (receipts.length) reconcile(ledger, receipts.map(receipt => {
      let row = receiptRows.get(receipt.sequence); if (!row) { row = receiptRow(receipt); receiptRows.set(receipt.sequence, row); } return row;
    }));
    else if (!ledger.querySelector('.exp-muted')) ledger.replaceChildren(blank(state.loading ? copy('loading') : copy('historyEmpty')));
    const lookups = state.memories?.entries || [];
    const memoryFocus = lookupHistory.contains(document.activeElement) ? document.activeElement as HTMLElement : null;
    const lookupChildren: HTMLElement[] = [node('h3', '', copy('memoryLookups')), blank(copy('memoryLookupsNote'))];
    if (state.memoryError) lookupChildren.push(blank(copy('memoryUnavailable')));
    else if (!lookups.length) lookupChildren.push(blank(copy('memoryEmpty')));
    const memoryKeys = new Set<string>();
    for (const lookup of lookups) {
      memoryKeys.add(lookup.key);
      const signature = JSON.stringify(lookup);
      const existing = memoryRows.get(lookup.key);
      if (existing?.signature === signature) { lookupChildren.push(existing.element); continue; }
      const detail = node('details', 'exp-hypothesis'); detail.append(node('summary', '', lookup.query));
      detail.open = existing?.element.open || false;
      detail.append(blank(`${copy('memoryCoverage')}: ${lookup.scanned_sources} · ${copy('tokens')}: ${lookup.actuals_complete ? '' : '≥ '}${lookup.actuals.tokens.toLocaleString()}`),
        blank(copy('memoryCostNote')));
      if (lookup.source_kind && lookup.source_kind !== 'receipt') detail.append(blank(t(lookup.source_kind === 'deliverable'
        ? 'evolution.studio.sourceOriginal' : lookup.source_kind === 'artifact_evidence' ? 'evolution.studio.sourceArtifacts' : lookup.source_kind==='background_evidence' ? 'evolution.studio.sourceBackgroundEvidence' : 'evolution.studio.sourceToolEvidence')));
      if (!lookup.matches.length) detail.append(blank(copy('memoryNoMatches')));
      for (const match of lookup.matches) {
        const source = lookup.source_chunks.find(chunk => chunk.candidate_id === match.candidate_id);
        if (!source) continue;
        detail.append(node('blockquote', '', match.quote), blank(match.reason));
        const open = createButton({label: `${copy('memoryOpenSource')} #${source.sequence}`, size: 'sm',
          onClick: () => controller.page(source.sequence - 1)});
        detail.append(open);
        if (source.source_kind && source.source_ref && source.run_id) {
          const record = {program_id: program.program_id, run_id: source.run_id, result: '',
            ...(source.source_kind === 'deliverable' ? {result_source: source.source_ref} : source.source_kind==='artifact_evidence' ? {artifact_evidence_source:source.source_ref} : source.source_kind==='background_evidence' ? {background_evidence_source:source.source_ref} : {tool_evidence_source: source.source_ref})};
          detail.append(blank(`${t('evolution.studio.sourceRange')}: ${source.offset + 1}–${source.end}`));
          const original = sources.controls(record, source.offset);
          if (original) detail.append(original);
        }
      }
      memoryRows.set(lookup.key, {signature, element: detail}); lookupChildren.push(detail);
    }
    memoryRows.forEach((_row, key) => { if (!memoryKeys.has(key)) memoryRows.delete(key); });
    reconcile(lookupHistory, lookupChildren);
    if (memoryFocus?.isConnected && document.activeElement !== memoryFocus) memoryFocus.focus({preventScroll: true});
    expandHost.hidden = ['completed', 'cancelled'].includes(program.status) || program.current?.step.kind === 'final';
    const finalReceipt = state.finalReceipt;
    const result = state.run?.result || finalReceipt?.result || '';
    const resultRecord = state.run?.result || !finalReceipt ? state.run : finalReceipt;
    const nextOutputSignature = JSON.stringify([program.status, result, resultRecord?.run_id, resultRecord?.result_source, resultRecord?.tool_evidence_source, resultRecord?.artifact_evidence_source]);
    if (nextOutputSignature !== outputSignature) {
      outputSignature = nextOutputSignature;
      output.replaceChildren(node('h2', '', copy(program.status === 'completed' ? 'delivery' : 'latestWork')),
        result ? node('pre', 'exp-result', result) : blank(copy('noOutput')), deliveryWarning);
      const visual=evidenceCharts(root.ownerDocument,result);if(visual)output.insertBefore(visual,output.children[1]!);
      const sourceActions = resultRecord && sources.controls(resultRecord);if (sourceActions) output.append(sourceActions);
    }
    // Read status changes leave the confirmed content island and selection intact.
    deliveryWarning.hidden = !state.deliveryError;
    if (state.deliveryError) deliveryWarning.textContent = copy(finalReceipt ? 'deliveryRetained' : 'deliveryUnavailable');
    const lower = program.actuals_complete === false ? '≥ ' : '';
    telemetry.textContent = `${copy('tokens')}: ${lower}${program.actuals.tokens.toLocaleString()} · ${copy('calls')}: ${lower}${program.actuals.tool_calls.toLocaleString()} — ${copy(program.actuals_complete === false ? 'usageIncomplete' : 'usageNote')}`;
    renderQuestions();
  }
  function updateRoute(next: string) {
    const parts = next.split('?')[0]!.split('/'); let id = '';
    if (!parts[2] || parts[2] === 'new') {
      const nextIdentity = creationIdentity(next);
      if (JSON.stringify(nextIdentity) !== JSON.stringify(creatorIdentity)) {
        saveCreatorDraft(); creatorIdentity = nextIdentity; researchSource = undefined;
        createControls.forEach((control,index) => {control.value = createDefaults[index]!;});
        pending = restoreDraft(creatorIdentity, createControls)?.attempt;
        creatorRestored = !!drafts.read(creatorIdentity); creatorTouched = false;
      }
    }
    try { id = parts[1] === 'programs' && parts[2] !== 'new' ? decodeURIComponent(parts[2] || '') : ''; }
    catch { notice.textContent = copy('readFailed'); }
    if (selectedId && selectedId !== id) {
      expansionDrafts.delete(selectedId); expansionDrafts.set(selectedId, [expandTitle.value, instruction.value, experimentChecks.value]);
      while (expansionDrafts.size > 20) {
        const oldest = expansionDrafts.keys().next().value!; expansionDrafts.delete(oldest); expansionRequests.delete(oldest);
      }
    }
    if (selectedId !== id) {
      const saved = expansionDrafts.get(id) || ['', '', ''];
      [expandTitle, instruction, experimentChecks].forEach((control, index) => control.value = saved[index]!);
      if (id) {
        const attempt = restoreDraft(['expand', id], expansionControls)?.attempt;
        if (attempt) expansionRequests.set(id, attempt);
      }
      receiptRows.clear(); memoryRows.clear(); questionForms.clear(); questionDraftSavers.clear(); ledger.replaceChildren(); lookupHistory.replaceChildren(); human.replaceChildren(); programSignature = ''; outputSignature = '';
    }
    path = next; selectedId = id; sources.owner(id); comparison.owner(id); epoch += 1; notice.textContent = ''; controller.select(id); render();
  }
  const visible = () => {controller.visibilityChanged();sources.visibilityChanged();}; document.addEventListener('visibilitychange', visible);
  const preserveDrafts = () => {saveCreatorDraft(); saveExpansionDraft(); questionDraftSavers.forEach(save => save());};
  window.addEventListener('pagehide', preserveDrafts);
  updateRoute(path); void controller.refresh();
  return {updateRoute, refresh: controller.refresh, dispose() {
    preserveDrafts(); window.removeEventListener('pagehide', preserveDrafts);
    disposed = true; epoch += 1; controller.dispose();resources.dispose();sources.dispose();currentExperiment.dispose();comparison.dispose(); document.removeEventListener('visibilitychange', visible);
    expansionDrafts.clear(); expansionRequests.clear(); questionForms.clear(); questionDraftSavers.clear(); receiptRows.clear(); memoryRows.clear(); programButtons.clear();
    root.replaceChildren(); root.classList.remove('evo-programs');
  }};
}
