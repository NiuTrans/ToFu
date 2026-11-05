/** Tofu Studio as a game screen over real Headless runs. Composition root:
 * one stage (canvas world: the main office or a department room), the HUD,
 * the dialogue box and one docked window, all projected from confirmed backend
 * snapshots by studio/projection.ts. One visible-page read flight; writes are
 * explicit, idempotent and fenced by goal identity. Task execution, waits,
 * model routing and verdicts remain backend-owned.
 * Entry: mountStudio.
 */
import type { EvolutionApi } from './controller';
import type { CreateRunBody, EvolutionRun, EvolutionRunSummary, EvolutionFloor, EvolutionGatePending, HumanQuestion,
  EvolutionModel, GoalWorkspace, GoalCoordination, RoleModels, EvolutionRole, SwarmAgentStatus } from '../../api/evolution-runs';
import { proposalsPanelHtml, type GateReceipt } from './proposals';
import { esc, patch } from './components/ui';
import { t, type I18nKey, type I18nParamsFor } from '../../i18n';
import { mountStage } from './engine/stage';
import { createOfficeScene, type OfficeTarget } from './office-scene';
import { createPodScene } from './agent-pod';
import { mountOfficeRooms } from './office-rooms';
import { roomForRole, themeForRole, roleTitleKey } from './room-catalog';
import { followTaskActivity, type TaskActivityStream, type ToolActivity } from './task-stream';
import { mountGoalRoom } from './goal-room';
import { mountGoalConversation } from './conversation';
import { mountRoleModels } from './role-models';
import { isTerminalRun } from './goal-workbench';
import { mountExecutionEnvironment } from './studio/execution-environment';
import { projectStudio, studioEvents, actorStatusKey, statusKey, type StudioView, type ActorRole } from './studio/projection';
import { mountHud, type HudAction } from './studio/hud';
import { mountDialogue, portraitStyle } from './studio/dialogue';
import { mountWindows, type WindowKind } from './studio/windows';
import { glyph } from './studio/glyphs';
import { PLACES, type Place } from './office-layout';

type UnparameterizedStudioKey = {
  [K in Extract<I18nKey, `evolution.studio.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.studio.${string}`>];
const copy = (key: string) => t(`evolution.studio.${key}` as UnparameterizedStudioKey);
const text = (key: string) => esc(copy(key));
const ROLES: ActorRole[] = ['executor', 'reviewer', 'coordinator'];
const PLACE_KEYS: Record<string, Place> = {Digit1: 'library', Digit2: 'plan', Digit3: 'work', Digit4: 'review', Digit5: 'delivery'};

export function mountStudio(root: HTMLElement, ports: {
  api: EvolutionApi; path: string; navigate: (path: string) => void; openTask: (id: string) => void;
  /** Ensures and opens the owner's Router Watchdog; absent without the shell's Watchdog entry. */
  openWatchdog?: () => Promise<void>;
}) {
  let disposed = false, busy = false, flight: Promise<void> | undefined, timer = 0, path = ports.path;
  let runs: EvolutionRunSummary[] = [], active: EvolutionRun | undefined, workspace: GoalWorkspace | undefined;
  let floor: EvolutionFloor = {items: [], degraded: false}, proposals: EvolutionGatePending = {items: [], degraded: false};
  // Gate receipts live in state: polling re-projects the menu every few seconds.
  const gateReceipts = new Map<string, GateReceipt>();
  let coordination: GoalCoordination | undefined, coordinationError = '';
  let swarmAgents: SwarmAgentStatus[] = [], swarmKnown = false;
  let questions: HumanQuestion[] = [], models: Array<{model: EvolutionModel; label: string}> = [];
  let draftRoleModels: RoleModels = {}, pendingSubmit: {body: CreateRunBody; key: string} | undefined;
  let activityStream: TaskActivityStream | undefined, activityTaskId = '';
  let view: StudioView | undefined, motionPaused = false, room = '';
  const activeToolCalls = new Map<string, ToolActivity>();
  let activityRole: ActorRole | '' = '';
  const abort = new AbortController();
  const selectedId = () => {try {return path.split('/')[1] === 'runs' ? decodeURIComponent(path.split('/')[2] || '') : '';} catch {return '';}};
  const briefingRoute = () => path.split('/')[1] === 'start';

  root.classList.add('evo-workspace', 'evo-studio', 'evo-game');
  root.innerHTML = `<div class="evo-stage-host"></div><nav class="evo-rooms"></nav>
    <div class="evo-corner"><aside class="evo-member-card"></aside><section class="evo-dialogue"></section></div>
    <section class="evo-window"></section><div role="alert" class="evo-error" hidden></div>`;
  root.dataset.room = 'main';
  const q = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!;
  const error = q('.evo-error');

  // ── Stage and scenes ─────────────────────────────────────────────────────
  const stage = mountStage(q('.evo-stage-host'), {label: copy('office')});
  const office = createOfficeScene({copy, wake: () => stage.wake(), activate: target => onWorldTarget(target)});
  const pod = createPodScene({wake: () => stage.wake(), statusLabel: status => copy(['running', 'pending', 'completed', 'failed', 'cancelled', 'needs_attention'].includes(status) ? status : 'idle'),
    roleLabel: role => copy(roleTitleKey(role)), openMember: () => {if (rooms.taskId()) ports.openTask(rooms.taskId());}});
  stage.setScene(office.scene);
  const rooms = mountOfficeRooms(q('.evo-rooms'), q('.evo-member-card'), {copy, openTask: ports.openTask, select: id => showRoom(id)});
  function showRoom(id: string) {
    if (disposed) return;
    if (id === '') {if (room !== '') {room = '';stage.setScene(office.scene);}}
    else {
      const member = rooms.member();
      pod.update(member, {theme: member ? themeForRole(member.role) : id === 'general' ? 'general' : id as Parameters<typeof pod.update>[1]['theme'], accent: rooms.accent()});
      if (room !== id) {room = id;stage.setScene(pod.scene);}
    }
    root.dataset.room = room || 'main';stage.wake();
  }

  // ── HUD, dialogue and windows ────────────────────────────────────────────
  const hud = mountHud(root, {copy, act: (action, origin) => onHud(action, origin)});
  const dialogue = mountDialogue(q('.evo-dialogue'), {copy, openChat: () => openWindow('chat'),
    openRecord: () => openPlace('work'), openWatchdog: ports.openWatchdog,
    answer: async (questionId, response) => {if (!active) return;await ports.api.answerRun(active.run_id, questionId, response);await refreshAfterWrite();}});
  const windows = mountWindows(q('.evo-window'), {copy, fallbackFocus: () => q('.evo-stage'), changed: kind => {
    root.dataset.window = kind;journal.visibilityChanged();layoutInsets();
    if (kind !== 'actor') {office.select('');stage.wake();}
  }});
  const journalPane = document.createElement('div');
  const journal = mountGoalRoom(journalPane, {api: ports.api, copy, refresh: refreshAfterWrite, openTask: ports.openTask,
    stop: () => write('cancel'), reconcile: () => write('reconcile'), download, busy: () => busy,
    readOutcome: ports.api.getStageOutcome ? (id, taskId, offset, signal, sourceKind) => ports.api.getStageOutcome(id, taskId, offset, signal, sourceKind) : undefined,
    visible: () => windows.current() === 'journal' && !document.hidden,
    selected: place => {if (windows.current() === 'journal') windows.retitle(copy('place_' + place));}});
  windows.register('journal', journalPane, 'notebook');
  const chatPane = document.createElement('section');chatPane.className = 'evo-conversation';
  const conversation = mountGoalConversation(chatPane, {api: ports.api, copy, refresh: refreshAfterWrite, navigate: ports.navigate});
  windows.register('chat', chatPane, 'chat');
  const briefingPane = document.createElement('div');
  briefingPane.innerHTML = `<form class="evo-objective-form evo-briefing">
      <label class="evo-sr-only" for="evo-objective">${text('objective')}</label>
      <textarea id="evo-objective" name="objective" required maxlength="4000" rows="6" placeholder="${text('objectivePlaceholder')}"></textarea>
      <div class="evo-seeds" role="group" aria-label="${text('exampleTasks')}">${['research', 'benchmark', 'build'].map(key => `<button type="button" class="evo-seed" data-example="${key}">${glyph(key === 'research' ? 'lens' : key === 'benchmark' ? 'chart' : 'hammer')}<span>${text('example_' + key)}</span></button>`).join('')}</div>
      <details class="evo-briefing-settings"><summary>${glyph('wrench')}<span>${text('briefingSettings')}</span></summary>
        <label for="evo-model">${text('model')}</label><div class="ui-select"><select id="evo-model"><option value="">${text('automatic')}</option></select></div>
        <p class="evo-model-message" role="status"></p>
        <button type="button" class="evo-button" data-studio-action="role-models">${glyph('people')}<span>${text('roleModelTitle')}</span></button>
        <div class="evo-environment"></div></details>
      <button type="submit" class="evo-button evo-primary evo-dispatch">${glyph('flag')}<span>${text('dispatchMission')}</span></button></form>`;
  windows.register('briefing', briefingPane, 'plus');
  const objective = briefingPane.querySelector<HTMLTextAreaElement>('#evo-objective')!;
  const model = briefingPane.querySelector<HTMLSelectElement>('#evo-model')!;
  const dispatch = briefingPane.querySelector<HTMLButtonElement>('.evo-dispatch')!;
  const executionEnvironment = mountExecutionEnvironment(briefingPane.querySelector('.evo-environment')!, {copy, signal: abort.signal,
    load: signal => ports.api.listExecutorPlugins(signal)});
  const seeds = [...briefingPane.querySelectorAll<HTMLButtonElement>('.evo-seed')];
  const refreshSeeds = () => seeds.forEach(seed => {seed.disabled = !!objective.value.trim();});
  objective.addEventListener('input', refreshSeeds);refreshSeeds();
  const menuPane = document.createElement('div');menuPane.className = 'evo-menu';
  menuPane.innerHTML = `<section class="evo-menu-goals"><header><h3>${text('history')}</h3>
      <button type="button" class="evo-button evo-primary" data-studio-action="create">${glyph('plus')}<span>${text('newGoal')}</span></button></header><div class="evo-goal-list" role="list"></div></section>
    <section class="evo-menu-gate" hidden><h3>${text('proposalsTitle')}</h3><div class="evo-proposals-host"></div></section>
    <section class="evo-menu-floor" hidden><h3>${text('floorLive')}</h3><div class="evo-floor-host"></div></section>
    <section class="evo-menu-links"><h3>${text('navigation')}</h3><div class="evo-link-grid">
      <button type="button" class="evo-tile-link" data-studio-route="evolution/programs">${glyph('notebook')}<span>${text('programNotebook')}</span></button>
      <button type="button" class="evo-tile-link" data-studio-route="evolution/research">${glyph('flask')}<span>${text('research')}</span></button>
      <button type="button" class="evo-tile-link" data-studio-route="evolution/capabilities">${glyph('chart')}<span>${text('evidence')}</span></button>
      <a class="evo-tile-link" href="#/chat" data-workspace-link="chat">${glyph('home')}<span>${text('backToChat')}</span></a></div></section>
    <section class="evo-menu-settings"><h3>${text('settings')}</h3><div class="evo-button-row">
      <button type="button" class="evo-button" data-studio-action="motion" aria-pressed="false">${glyph('pause')}<span>${text('pauseMotion')}</span></button>
      <button type="button" class="evo-button" data-studio-action="coordinator-model">${glyph('people')}<span>${text('coordinatorModel')}</span></button>
      <button type="button" class="evo-button" data-studio-action="refresh">${glyph('refresh')}<span>${text('refresh')}</span></button>
      <button type="button" class="evo-button" data-studio-action="reset-view">${glyph('focus')}<span>${text('resetView')}</span></button></div></section>`;
  windows.register('menu', menuPane, 'menu');
  const actorPane = document.createElement('section');actorPane.className = 'evo-actor-sheet';
  actorPane.innerHTML = `<header class="evo-actor-head"><div class="evo-portrait" aria-hidden="true"></div>
      <div><span class="evo-chip evo-actor-state"></span><p class="evo-actor-activity" hidden>${glyph('wrench')}<code></code></p></div></header>
    <div class="evo-role-models"></div>`;
  windows.register('actor', actorPane, 'user');
  const askingRole = (): ActorRole => active?.status === 'reviewing' ? 'reviewer' : 'executor';
  const roleModels = mountRoleModels(actorPane.querySelector('.evo-role-models')!, {copy, api: ports.api, models: () => models,
    ready: () => windows.current() === 'briefing' || briefingRoute() || !selectedId() || !!active, current: () => briefingRoute() || windows.current() === 'briefing' ? undefined : active,
    draft: () => draftRoleModels, setDraft: value => {draftRoleModels = value;},
    refresh: refreshAfterWrite, openTask: ports.openTask, askHuman: () => dialogue.reveal(),
    attention: role => questions.length ? (askingRole() === role ? 'answer' : '')
      : role === 'executor' && view?.primary === 'stall' ? 'stall' : ''});
  // The sheet's header shows the actor's live state; the window title names it.
  function renderActorSheet() {
    const role = roleModels.role(), portrait = actorPane.querySelector<HTMLElement>('.evo-portrait')!;
    if (portrait.dataset.role !== role) {portrait.dataset.role = role;portrait.setAttribute('style', portraitStyle(role));}
    const state = actorPane.querySelector<HTMLElement>('.evo-actor-state')!;
    state.textContent = view ? copy(actorStatusKey(view, role)) : '';state.dataset.mode = view?.actors[role].mode || 'idle';
    const tool = activityRole === role ? [...activeToolCalls.values()].at(-1)?.tool || '' : '';
    const activity = actorPane.querySelector<HTMLElement>('.evo-actor-activity')!;
    activity.hidden = !tool;activity.querySelector('code')!.textContent = tool;
  }

  function openWindow(kind: WindowKind, origin?: Element | null, title?: string) {
    const titles: Record<WindowKind, string> = {journal: copy('place_' + journal.selected()), chat: copy('coordinator'),
      briefing: copy('newGoal'), menu: copy('navigation'), actor: copy('roleModelTitle')};
    windows.open(kind, title || titles[kind], origin);
    if (kind === 'briefing') objective.focus({preventScroll: true});
    if (kind === 'chat' && dialogue.unseenLine()) {dialogue.markSeen();render();}
  }
  function openPlace(place: Place, origin?: Element | null) {journal.open(place);openWindow('journal', origin, copy('place_' + place));}
  function configureRole(role: EvolutionRole, origin?: Element | null) {
    roleModels.open(role);renderActorSheet();openWindow('actor', origin, copy(role));
  }
  function showBriefing(origin?: Element | null) {openWindow('briefing', origin);refreshSeeds();}
  function onWorldTarget(target: OfficeTarget) {
    if (target.kind === 'place') {openPlace(target.place);return;}
    if (target.kind === 'actor') {
      if (target.role === 'coordinator') {openWindow('chat');return;}
      configureRole(target.role);return;
    }
    const agent = swarmAgents.find(item => item.id === target.id);
    if (agent) rooms.select(roomForRole(agent.role));
  }
  function onHud(action: HudAction, origin: HTMLElement) {
    if (action.startsWith('place:')) {openPlace(action.slice(6) as Place, origin);return;}
    if (action.startsWith('stage:')) {
      const stageId = action.slice(6);
      openPlace(stageId === 'plan' ? 'plan' : stageId === 'execute' ? 'work' : stageId === 'review' ? 'review' : 'delivery', origin);return;
    }
    if (action === 'journal') {openWindow('journal', origin);return;}
    if (action === 'menu') {windows.current() === 'menu' ? windows.close() : openWindow('menu', origin);return;}
    if (action === 'rooms') {root.dataset.roomsOpen = String(root.dataset.roomsOpen !== 'true');return;}
    if (action === 'chat') {openWindow('chat', origin);return;}
    if (action === 'create') {showBriefing(origin);return;}
    if (action === 'answer' || action === 'stall') {dialogue.reveal();return;}
    if (action === 'read') {openPlace('delivery', origin);return;}
    if (action === 'retry') prepareRetry(origin);
  }

  // ── Insets: the camera frames the room between the HUD bars and window ──
  function layoutInsets() {
    if (disposed) return;
    const box = root.getBoundingClientRect(), bars = hud.insets();
    const narrow = box.width < 760;
    const railBox = q('.evo-rooms').getBoundingClientRect();
    const windowBox = windows.element.hidden ? undefined : windows.element.getBoundingClientRect();
    // Overlays anchor to the measured bars: the hotbar grows when a primary action appears.
    root.style.setProperty('--evo-hud-top', `${bars.top}px`);root.style.setProperty('--evo-hud-bottom', `${bars.bottom}px`);
    stage.setInsets({
      top: bars.top, bottom: bars.bottom,
      left: narrow ? 0 : Math.max(0, railBox.right - box.left),
      right: narrow || !windowBox ? 0 : Math.max(0, box.right - windowBox.left),
    });
  }
  const insetObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(layoutInsets) : undefined;
  insetObserver?.observe(root);insetObserver?.observe(windows.element);
  insetObserver?.observe(q('.evo-hud-top'));insetObserver?.observe(q('.evo-hotbar'));

  // ── Projection → world, HUD, windows ────────────────────────────────────
  function placeLabels(next: StudioView): Record<Place, string> {
    const verdict = next.verdict ? ` · ${copy('verdict_' + next.verdict)}` : '';
    return {
      library: `${copy('place_library')}${next.counts.materials !== null ? ` · ${next.counts.materials}` : ''}`,
      plan: `${copy('place_plan')}${next.counts.steps !== null ? ` · ${next.counts.steps}` : ''}`,
      work: `${copy('place_work')} · ${copy('stageState_' + next.stages.execute)}`,
      review: `${copy('place_review')} · ${copy('stageState_' + next.stages.review)}${verdict}`,
      delivery: `${copy('place_delivery')}${next.deliverable ? ` · ${copy('deliverableReady')}` : ''}`,
    };
  }
  function render() {
    if (disposed) return;
    const loading = !!selectedId() && !active;
    const next = projectStudio({run: active, workspace, coordination, questions: questions.length, loading, agents: swarmAgents, agentsKnown: swarmKnown});
    for (const event of studioEvents(view, next)) hud.toast(event);
    view = next;
    dialogue.update(active, questions, coordination);
    if (windows.current() === 'chat') dialogue.markSeen();
    const labels = placeLabels(next);
    const counts: Record<Place, string> = {
      library: next.counts.materials ? String(next.counts.materials) : '', plan: next.counts.steps ? String(next.counts.steps) : '',
      work: next.counts.records ? String(next.counts.records) : '', review: next.verdict ? (next.verdict === 'pass' ? '✓' : next.verdict === 'fail' ? '✗' : '~') : '',
      delivery: next.deliverable ? '★' : '',
    };
    hud.update(next, {placeLabels: labels, placeCounts: counts, proposals: proposals.items.length, busy,
      chatAttention: next.coordinator === 'attention' || dialogue.unseenLine()});
    const names = Object.fromEntries(ROLES.map(role => [role, copy(role)])) as Record<ActorRole, string>;
    const attention: Place[] = [];
    if (next.deliverable && next.phase === 'completed') attention.push('delivery');
    if (next.primary === 'stall') attention.push('work');
    office.update({goalId: next.runId, view: next, materials: workspace?.materials || [],
      assignmentSeq: Math.max(0, ...(coordination?.updates || []).filter(item => item.kind === 'assignment' && item.target === 'executing').map(item => item.seq)),
      agents: swarmAgents, names, placeLabels: labels, attention,
      actorLabels: Object.fromEntries(ROLES.map(role => [role, `${names[role]} · ${copy(actorStatusKey(next, role))}`])) as Record<ActorRole, string>});
    rooms.update(swarmAgents, active?.status === 'executing' ? active.task_id : '', swarmKnown);
    if (room) showRoom(room);
    stage.wake();
    journal.update(active, workspace, coordination, loading);
    conversation.update(active, coordination, coordinationError);
    roleModels.sync();renderActorSheet();
    renderMenu();
    root.dataset.status = statusKey(next);
    syncActivityStream(active);
  }
  function renderMenu() {
    const list = active && !runs.some(run => run.run_id === active!.run_id) ? [active, ...runs] : runs;
    patch(menuPane.querySelector('.evo-goal-list')!, list.length ? list.map(run => `<a role="listitem" class="evo-goal-row" data-key="${esc(run.run_id)}" data-status="${esc(run.status)}" href="#/evolution/runs/${encodeURIComponent(run.run_id)}"${run.run_id === active?.run_id ? ' aria-current="true"' : ''}><i aria-hidden="true"></i><span>${esc(run.objective)}</span><small>${text('state_' + (projectStudio({run: run as EvolutionRun}).phase))}</small></a>`).join('') : '<p class="evo-empty-mark">—</p>');
    const gate = menuPane.querySelector<HTMLElement>('.evo-menu-gate')!;
    gate.hidden = !proposals.items.length && !gateReceipts.size && !proposals.degraded;
    patch(menuPane.querySelector('.evo-proposals-host')!, proposalsPanelHtml(proposals, gateReceipts));
    const floorHost = menuPane.querySelector<HTMLElement>('.evo-menu-floor')!;
    floorHost.hidden = !floor.items.length && !floor.degraded;
    patch(menuPane.querySelector('.evo-floor-host')!, floor.items.map(item => {
      const external = item.kind !== 'evolution';
      const href = external ? '#/chat' : `#/evolution/runs/${encodeURIComponent(item.run_id)}`;
      const conv = item.conversation_id ? ` data-studio-action="open-conv" data-conv="${esc(item.conversation_id)}"` : '';
      return `<a class="evo-floor-row" data-key="${esc(item.kind + item.run_id)}" data-kind="${esc(item.kind)}" href="${href}"${conv}><small>${text(item.kind === 'evolution' ? 'floorKindEvolution' : item.kind === 'flow' ? 'floorKindFlow' : 'floorKindGoal')}</small><span>${esc(item.title)}</span><em>${text(item.status)}</em></a>`;
    }).join('') + (floor.degraded ? '<p class="evo-empty-mark">!</p>' : ''));
    const motion = menuPane.querySelector<HTMLButtonElement>('[data-studio-action=motion]')!;
    motion.setAttribute('aria-pressed', String(motionPaused));
    motion.innerHTML = `${glyph(motionPaused ? 'play' : 'pause')}<span>${text(motionPaused ? 'resumeMotion' : 'pauseMotion')}</span>`;
  }

  // ── Read flight ─────────────────────────────────────────────────────────
  function showError(value: unknown) {
    if (disposed) return;
    error.textContent = value instanceof Error ? value.message : String(value || '');error.hidden = !value;
  }
  async function refresh() {
    if (disposed || document.hidden) return;
    if (flight) return flight;
    const requestedPath = path;
    flight = (async () => {
      try {
        const response = await ports.api.listRuns(abort.signal);
        if (disposed || path !== requestedPath) return;
        runs = response.runs.slice(0, 50).sort((a, b) => b.created_at - a.created_at);
        floor = response.floor || {items: [], degraded: false};
        proposals = response.proposals || {items: [], degraded: false};
        const id = briefingRoute() ? undefined : selectedId() || active?.run_id || runs.find(r => !isTerminalRun(r))?.run_id || runs[0]?.run_id;
        if (id) {
          const [detail, dialogueRead] = await Promise.all([ports.api.getRun(id, abort.signal),
            ports.api.getCoordination(id, abort.signal).then(value => ({value, error: ''}), reason => ({value: undefined, error: reason instanceof Error ? reason.message : String(reason)}))]);
          if (disposed || path !== requestedPath) return;
          // A navigation during the request must not replace the new selection.
          if (selectedId() && id !== selectedId()) return;
          active = detail.run;workspace = detail.workspace;questions = detail.pending_questions || [];
          coordination = dialogueRead.value;coordinationError = dialogueRead.error;
          // Only the executing task can own a swarm; fail-soft to an empty floor.
          const swarmTaskId = detail.run.status === 'executing' ? detail.run.task_id : '';
          const swarm = swarmTaskId ? await ports.api.getSwarmStatus(swarmTaskId, abort.signal).then(value => ({value}), () => ({value: undefined})) : {value: undefined};
          if (disposed || path !== requestedPath) return;
          swarmAgents = (swarm.value?.agents || []).slice(0, 8);
          swarmKnown = !swarmTaskId || Boolean(swarm.value && swarm.value.known !== false);
        } else {active = undefined;workspace = undefined;questions = [];coordination = undefined;coordinationError = '';swarmAgents = [];swarmKnown = true;}
        showError('');render();
      } catch (e) {if (!abort.signal.aborted) showError(e);}
    })().finally(() => {flight = undefined;schedule();});
    return flight;
  }
  async function refreshAfterWrite() {if (flight) await flight;await refresh();}
  function schedule() {
    clearTimeout(timer);timer = 0;
    if (!disposed && !document.hidden) timer = window.setTimeout(() => {void refresh();}, (active && !isTerminalRun(active)) || coordination?.coordinator.status === 'running' ? 3000 : 15000);
  }
  function visibility() {journal.visibilityChanged();if (document.hidden) {clearTimeout(timer);timer = 0;} else void refresh();}

  // Live tool chips decorate the polling authority: one SSE per active stage task.
  function closeActivityStream() {
    activityStream?.close();activityStream = undefined;activityTaskId = '';activityRole = '';activeToolCalls.clear();
    office.setToolBubble('executor', '');office.setToolBubble('reviewer', '');
    if (!disposed) renderActorSheet();
  }
  function syncActivityStream(run: EvolutionRun | undefined) {
    const liveTaskId = run && !isTerminalRun(run) && run.task_id && !run.compute_wait && !run.background_wait && (run.status === 'executing' || run.status === 'reviewing') ? run.task_id : '';
    if (liveTaskId === activityTaskId) return;
    closeActivityStream();
    if (!liveTaskId || !run) return;
    activityTaskId = liveTaskId;
    const role: ActorRole = run.status === 'executing' ? 'executor' : 'reviewer';activityRole = role;
    const project = () => {const current = [...activeToolCalls.values()].at(-1);office.setToolBubble(role, current?.tool || '');renderActorSheet();stage.wake();};
    activityStream = followTaskActivity(liveTaskId, {
      toolStart: activity => {activeToolCalls.set(activity.toolCallId || activity.tool, activity);project();},
      toolEnd: id => {if (id) activeToolCalls.delete(id);else activeToolCalls.clear();project();},
      done: () => closeActivityStream(),
    });
  }

  // ── Writes ───────────────────────────────────────────────────────────────
  function setBusy(value: boolean) {
    busy = value;executionEnvironment.setBusy(value);dispatch.disabled = value;objective.readOnly = value;model.disabled = value;
    dispatch.querySelector('span')!.textContent = copy(value ? 'starting' : 'dispatchMission');
    if (view) render();
  }
  async function onSubmit(event: Event) {
    const form = event.target as HTMLFormElement;
    if (!form.classList.contains('evo-objective-form') || !root.contains(form)) return;
    event.preventDefault();
    if (busy) return;
    let executionOptions: Pick<CreateRunBody, 'project_path' | 'executor_plugins'>;
    try {executionOptions = executionEnvironment.read();} catch (reason) {showError(reason instanceof Error ? reason.message : String(reason));return;}
    const body = {...executionOptions, objective: objective.value.trim(),
      ...(Object.keys(draftRoleModels).length ? {role_models: {...draftRoleModels}} : {}),
      ...(model.value ? {model: models[Number(model.value) - 1].model} : {})};
    if (!body.objective) return;
    if (!pendingSubmit || JSON.stringify(pendingSubmit.body) !== JSON.stringify(body)) pendingSubmit = {body, key: crypto.randomUUID()};
    const submittedPath = path;
    setBusy(true);showError('');
    try {
      const response = await ports.api.createRun(pendingSubmit.body, pendingSubmit.key);
      if (disposed) return;
      pendingSubmit = undefined;objective.value = '';refreshSeeds();
      if (path !== submittedPath) {await refresh();return;}
      windows.close();
      active = response.run;workspace = undefined;questions = [];coordination = undefined;coordinationError = '';swarmAgents = [];swarmKnown = false;render();
      ports.navigate(`evolution/runs/${encodeURIComponent(active.run_id)}`);
      await refresh();
    } catch (e) {showError(e);} finally {if (!disposed) setBusy(false);}
  }
  async function write(action: 'cancel' | 'reconcile') {
    if (!active || busy || disposed) return;
    const runId = active.run_id;
    setBusy(true);showError('');
    try {
      if (action === 'cancel') await ports.api.cancelRun(runId);else await ports.api.reconcileRun(runId);
      await refreshAfterWrite();
    } catch (e) {showError(e);} finally {if (!disposed) setBusy(false);}
  }
  function download() {
    if (!active?.result || active.compute_wait || disposed) return;
    const url = URL.createObjectURL(new Blob([active.result], {type: 'text/markdown;charset=utf-8'}));
    const anchor = document.createElement('a');anchor.href = url;anchor.download = `${active.run_id}.md`;anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  // Retry prepares a new authorization in the briefing; it never replays the
  // failed task. The user confirms with the dispatch button.
  function prepareRetry(origin?: Element | null) {
    if (!active || !isTerminalRun(active) || busy) return;
    objective.value = active.objective;draftRoleModels = {...active.role_models};refreshSeeds();
    const index = models.findIndex(entry => JSON.stringify(entry.model) === JSON.stringify(active?.model));
    model.value = index >= 0 ? String(index + 1) : '';
    showBriefing(origin);
  }
  async function onClick(event: Event) {
    const link = (event.target as Element).closest<HTMLAnchorElement>('a[href^="#/"]');
    if (link && root.contains(link) && link.closest('.evo-menu')) windows.close();
    const target = (event.target as Element).closest<HTMLElement>('[data-studio-action],[data-studio-route],[data-example]');
    if (!target || !root.contains(target)) return;
    if (target.dataset.studioRoute) {ports.navigate(target.dataset.studioRoute);return;}
    if (target.dataset.example) {
      // Fill only an empty draft. Selecting a seed never dispatches a task.
      if (!objective.value.trim()) {objective.value = copy('prompt_' + target.dataset.example);refreshSeeds();objective.focus();}
      return;
    }
    const action = target.dataset.studioAction;
    if (action === 'create') {showBriefing(target);return;}
    if (action === 'refresh') {void refresh();return;}
    if (action === 'motion') {
      // The root carries the pause so HUD animations still with the world.
      motionPaused = !motionPaused;root.dataset.motion = motionPaused ? 'paused' : 'live';
      stage.setMotionPaused(motionPaused);renderMenu();return;
    }
    if (action === 'reset-view') {stage.resetView();return;}
    if (action === 'role-models') {configureRole('executor', target);return;}
    if (action === 'coordinator-model') {configureRole('coordinator', target);return;}
    if (action === 'open-conv') {
      const conv = target.dataset.conv || '';const opener = (window as unknown as {openConversation?: (id: string) => unknown}).openConversation;
      if (conv && typeof opener === 'function') opener(conv);return;
    }
    if (!action?.startsWith('proposal-') || busy) return;
    const id = target.dataset.id || '';
    if (!id) return;
    target.setAttribute('disabled', '');
    try {
      if (action === 'proposal-dismiss') {gateReceipts.delete(id);renderMenu();return;}
      const pendingItem = proposals.items.find(item => item.mutation_id === id);
      const title = gateReceipts.get(id)?.title || pendingItem?.title || id;
      if (action === 'proposal-reject') {
        await ports.api.mutationAction(id, 'reject');
        if (!disposed) gateReceipts.set(id, {result: {mutation: {}, final_status: 'rejected'}, title});
      } else {
        const result = await ports.api.mutationAction(id, action === 'proposal-waiver' ? 'enable_waiver' : 'enable');
        if (!disposed) gateReceipts.set(id, {result: result as GateReceipt['result'], title});
      }
      if (!disposed) renderMenu();
      await refreshAfterWrite();
    } catch (e) {showError(e);} finally {if (!disposed) target.removeAttribute('disabled');}
  }
  // Game hotkeys never fire while the human is typing.
  function hotkey(event: KeyboardEvent) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest('input,textarea,select,[contenteditable="true"]')) return;
    if (event.key === 'Escape') {
      if (windows.current()) {windows.close();event.preventDefault();return;}
      if (root.contains(target) || target === document.body) {openWindow('menu');event.preventDefault();}
      return;
    }
    if (!(root.contains(target) || target === document.body)) return;
    const place = PLACE_KEYS[event.code];
    if (place && view?.runId) {openPlace(place);event.preventDefault();return;}
    if (event.code === 'KeyT' && view?.runId) {openWindow('chat');event.preventDefault();return;}
    if (event.code === 'KeyJ' && view?.runId) {openWindow('journal');event.preventDefault();return;}
    if (event.code === 'KeyN') {showBriefing();event.preventDefault();}
  }
  root.addEventListener('submit', onSubmit);root.addEventListener('click', onClick);
  document.addEventListener('keydown', hotkey);document.addEventListener('visibilitychange', visibility);
  if (briefingRoute()) showBriefing();
  render();layoutInsets();void refresh();
  void ports.api.listRunModels(abort.signal).then(response => {
    if (disposed) return;
    models = response.models.slice(0, 128);roleModels.refreshOptions();
    models.forEach((entry, i) => model.add(new Option(entry.label, String(i + 1))));
    if (!models.length) briefingPane.querySelector('.evo-model-message')!.textContent = copy('noModels');
  }).catch(() => {if (!disposed) briefingPane.querySelector('.evo-model-message')!.textContent = copy('modelsUnavailable');});
  return {
    updateRoute(next: string) {
      path = next;active = undefined;workspace = undefined;questions = [];coordination = undefined;coordinationError = '';swarmKnown = false;
      if (briefingRoute()) showBriefing();else if (windows.current() === 'briefing') windows.close();
      render();void (flight || Promise.resolve()).then(() => refresh());
    },
    /** Diagnostics for tests: current projection and stage metrics. */
    diagnostics() {return {view, stage: stage.metrics(), office: office.diagnostics()};},
    dispose() {
      disposed = true;closeActivityStream();insetObserver?.disconnect();
      windows.dispose();dialogue.dispose();hud.dispose();journal.dispose();conversation.dispose();roleModels.dispose();rooms.dispose();
      office.dispose();pod.dispose();stage.dispose();
      abort.abort();clearTimeout(timer);
      document.removeEventListener('keydown', hotkey);document.removeEventListener('visibilitychange', visibility);
      root.removeEventListener('submit', onSubmit);root.removeEventListener('click', onClick);
      delete root.dataset.window;delete root.dataset.room;delete root.dataset.motion;root.classList.remove('evo-studio', 'evo-game');
    },
  };
}
