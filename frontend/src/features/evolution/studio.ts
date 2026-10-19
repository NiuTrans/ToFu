/** Interactive studio over real Headless runs. One visible-page read flight; office-scene owns bounded presentation animation.
 * Stable input islands preserve drafts; idempotency keys survive ambiguous POST retries.
 * Task execution, human waiting, model routing and verdicts remain backend-owned.
 */
import type { EvolutionApi } from './controller';
import type { CreateRunBody, EvolutionRun, EvolutionRunSummary, EvolutionFloor, EvolutionGatePending, HumanQuestion, EvolutionModel, GoalWorkspace, GoalCoordination, RoleModels, EvolutionRole, SwarmAgentStatus } from '../../api/evolution-runs';
import { proposalsPanelHtml, type GateReceipt } from './proposals';
import { esc, patch } from './components/ui';
import { t, type I18nKey, type I18nParamsFor } from '../../i18n';
import office from './assets/office-clear.webp';
import { mountOfficeScene } from './office-scene';
import { mountOfficeRooms } from './office-rooms';
import { mountOfficeLedger } from './office-ledger';
import { followTaskActivity, type TaskActivityStream, type ToolActivity } from './task-stream';
import { mountGoalRoom } from './goal-room';
import { mountGoalConversation } from './conversation';
import { mountRoleModels } from './role-models';

import { createButton } from '../../ui/index';
import { isTerminalRun, runStatusLabel } from './goal-workbench';
import { mountGoalCommandPanel } from './studio/goal-command-panel';
import { mountGoalRunInspector } from './studio/goal-run-inspector';
import { iconHtml } from '../../icons';
import { mountCommandCenter } from './studio/command-center';
import { mountExecutionEnvironment } from './studio/execution-environment';
type UnparameterizedStudioKey = {
  [K in Extract<I18nKey, `evolution.studio.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.studio.${string}`>];
const copy = (key: string) => t(`evolution.studio.${key}` as UnparameterizedStudioKey);
const terminal = isTerminalRun;
const text = (key: string) => esc(copy(key));

export function mountStudio(root: HTMLElement, ports: {
  api: EvolutionApi; path: string; navigate: (path: string) => void; openTask: (id: string) => void;
}) {
  let disposed = false, busy = false, flight: Promise<void> | undefined;
  let briefingOpen = ports.path.split('/')[1] === 'start';
  let runInspector: ReturnType<typeof mountGoalRunInspector> | undefined;
  let timer = 0, path = ports.path, runs: EvolutionRunSummary[] = [], active: EvolutionRun | undefined;
  let floor: EvolutionFloor = {items: [], degraded: false};
  let proposals: EvolutionGatePending = {items: [], degraded: false};
  // Gate receipts live in state, not the DOM: the polling refresh re-projects
  // the drawer, so a DOM-only receipt would be wiped mid-journey.
  const gateReceipts = new Map<string, GateReceipt>();
  let workspace: GoalWorkspace | undefined;
  let coordination:GoalCoordination|undefined,coordinationError='';
  let swarmAgents: SwarmAgentStatus[] = [];
  let swarmKnown = false;
  let questions: HumanQuestion[] = [], models: Array<{model: EvolutionModel;label: string}> = [];
  let draftRoleModels:RoleModels = {};
  let pendingSubmit: {body: CreateRunBody; key: string} | undefined;
  let activityStream: TaskActivityStream|undefined, activityTaskId = '';
  const activeToolCalls = new Map<string,ToolActivity>();
  const abort = new AbortController();
  root.classList.add('evo-workspace','evo-studio');
  root.innerHTML = `<nav class="evo-nav" aria-label="${text('navigation')}"><a href="#/evolution" class="evo-brand" aria-label="Tofu">TOFU<span>${text('brandNote')}</span></a><a href="#/evolution" aria-current="page">⌂　${text('studio')}</a><a href="#/evolution/runs">▤　${text('history')}</a><a href="#/evolution/research">⚗　${text('research')}</a><a href="#/evolution/capabilities">♧　${text('evidence')}</a><a href="#/chat" data-workspace-link="chat">←　${text('backToChat')}</a><p class="evo-nav-note">${text('navNote')}</p></nav><main class="evo-main"><header class="evo-page-head"><div><h1>${text('title')}</h1><p>${text('subtitle')}</p></div><button type="button" class="evo-button" data-studio-action="refresh">${text('refresh')}</button></header><div class="evo-goal-switcher"><label for="evo-goal">${text('currentGoal')}</label><select id="evo-goal"></select><a class="evo-button evo-primary" href="#/evolution/start">＋ ${text('newGoal')}</a></div><div role="alert" class="evo-error" hidden></div><div class="evo-studio-grid"><section class="evo-office" aria-label="${text('office')}"><img class="evo-office-backdrop" src="${office}" alt="" aria-hidden="true" decoding="async"><div class="evo-office-stage"><img src="${office}" alt="${text('officeAlt')}" width="1448" height="1086" decoding="async"><div class="evo-office-live"></div><div class="evo-office-objects"></div></div><div class="evo-place-shortcuts" role="group" aria-label="${text('exploreGoal')}"></div><div class="evo-office-controls"></div><footer class="evo-office-caption"><span>${text('officeCaption')}</span><span class="evo-office-state" role="status">${text('idle')}</span></footer></section><aside class="evo-studio-aside"><section id="evo-goal-panel" class="evo-goal-panel" aria-labelledby="evo-room-title" hidden></section><form class="evo-objective-form"><h2>${text('nextTask')}</h2><label class="evo-sr-only" for="evo-objective">${text('objective')}</label><textarea id="evo-objective" name="objective" required maxlength="4000" placeholder="${text('placeholder')}"></textarea><label for="evo-model">${text('model')}</label><select id="evo-model"><option value="">${text('automatic')}</option></select><p class="evo-model-message" role="status"></p><button type="submit" class="evo-button evo-primary">${text('start')} <span aria-hidden="true">↗</span></button></form><section class="evo-delivery"><h2>${text('delivery')}</h2><div class="evo-delivery-content"></div></section></aside></div><section class="evo-human" aria-live="polite"></section><section class="evo-run-detail"></section><section class="evo-proof"><div><h2>${text('proofTitle')}</h2><a href="#/evolution/capabilities">${text('seeEvidence')} →</a></div><p>${text('proofEmpty')}</p><small>${text('proofRule')}</small></section><section class="evo-run-history"><h2>${text('history')}</h2><div></div></section></main>`;
  const q = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!;
  // Secondary content is moved, not cloned: form drafts and delegated events retain one owner.
  function drawer(id:string,label:string,nodes:HTMLElement[]) {
    const element=document.createElement('wa-drawer');element.id=id;element.setAttribute('label',label);
    element.setAttribute('light-dismiss','');nodes.forEach(node=>element.append(node));root.append(element);return element;
  }
  const roomDrawer=drawer('evo-room-drawer',copy('exploreGoal'),[q('.evo-goal-panel')]);

  roomDrawer.classList.add('evo-workbench-drawer');
  const detailDrawer=drawer('evo-detail-drawer',copy('currentTask'),[q('.evo-human'),q('.evo-run-detail')]);

  detailDrawer.classList.add('evo-workbench-drawer');
  const menuDrawer=drawer('evo-menu-drawer',copy('navigation'),[q('.evo-goal-switcher'),q('.evo-nav'),q('[data-studio-action=refresh]'),q('.evo-proof'),q('.evo-run-history')]);
  const conversationHost=document.createElement('section');conversationHost.className='evo-conversation';
  const conversationDrawer=drawer('evo-conversation-drawer',copy('coordinator'),[conversationHost]);
  const proposalsHost=document.createElement('section');proposalsHost.className='evo-proposals-host';
  const proposalsDrawer=drawer('evo-proposals-drawer',copy('proposalsTitle'),[proposalsHost]);
  const floorHost=document.createElement('section');floorHost.className='evo-floor-host';
  const floorDrawer=drawer('evo-floor-drawer',copy('floorLive'),[floorHost]);
  const roleHost=document.createElement('section');roleHost.className='evo-role-models';
  const roleDrawer=drawer('evo-role-drawer',copy('roleModelTitle'),[roleHost]);
  const roomsDrawer=drawer('evo-rooms-drawer',copy('roomSwitch'),[]);
  roomsDrawer.setAttribute('placement','start');
  const drawers=[roomsDrawer,roomDrawer,detailDrawer,menuDrawer,conversationDrawer,proposalsDrawer,floorDrawer,roleDrawer];
  let drawerReturnFocus:HTMLElement|null=null;
  let briefingFocusPending=false;
  let focusReturnTimer=0;
  let revealSelectedRoom=false;
  function restoreDrawerFocus(event:Event){
    if(disposed)return;
    if(!drawers.some(item=>item===event.target))return;
    if(event.target===detailDrawer)runInspector?.visibilityChanged(false);
    // wa-after-hide precedes Lit's reflected attribute update. The live property
    // is already false; [open] would skip returning focus after a drawer handoff.
    if(!drawers.some(item=>item.open)){
      if(briefingFocusPending){
        briefingFocusPending=false;clearTimeout(focusReturnTimer);
        focusReturnTimer=window.setTimeout(()=>{focusReturnTimer=0;if(disposed)return;objective.disabled=false;dispatch.disabled=busy;if(briefingOpen&&!drawers.some(item=>item.open))center.focus();},0);
      }
      else if(drawerReturnFocus&&root.contains(drawerReturnFocus))drawerReturnFocus.focus({preventScroll:true});
    }

    if(event.target===roomsDrawer&&revealSelectedRoom){
      revealSelectedRoom=false;
      q('.evo-studio-grid').scrollIntoView({block:'start',behavior:'instant'});
    }
  }
  root.addEventListener('wa-after-hide',restoreDrawerFocus);
  function openDrawer(element:HTMLElement,label?:string){
    clearTimeout(focusReturnTimer);focusReturnTimer=0;
    objective.disabled=false;dispatch.disabled=busy;
    const focused=document.activeElement as HTMLElement|null;
    if(focused&&root.contains(focused)&&!focused.closest('wa-drawer'))drawerReturnFocus=focused;
    drawers.forEach(item=>item.toggleAttribute('open',item===element));
    runInspector?.visibilityChanged(element===detailDrawer&&!document.hidden);
    if(label)element.setAttribute('label',label);
    element.prepend(q('.evo-error'));
  }
  // The directory, mission header and tool shelf each have a fixed layout region.
  const dock=document.createElement('div');dock.className='evo-game-dock';
  dock.append(q('.evo-place-shortcuts'),q('.evo-office-controls'));
  const status=document.createElement('button');status.type='button';status.className='evo-status-button';status.dataset.studioAction='details';status.append(q('.evo-office-state'));dock.prepend(status);q('.evo-main').append(dock);
  const gateButton=document.createElement('button');gateButton.type='button';gateButton.className='evo-gate-button';gateButton.dataset.studioAction='proposals';gateButton.hidden=true;status.insertAdjacentElement('afterend',gateButton);
  const floorButton=document.createElement('button');floorButton.type='button';floorButton.className='evo-floor-button';floorButton.dataset.studioAction='floor';floorButton.hidden=true;gateButton.insertAdjacentElement('afterend',floorButton);
  const concierge=document.createElement('button');concierge.type='button';concierge.className='evo-concierge';concierge.dataset.studioAction='conversation';
  concierge.innerHTML=`<span class="evo-concierge-avatar" aria-hidden="true">T</span><span><strong>${text('talkCoordinator')}</strong><small></small></span><span aria-hidden="true">↗</span>`;q('.evo-main').append(concierge);
  const coordinatorModel=document.createElement('button');coordinatorModel.type='button';coordinatorModel.className='evo-button';coordinatorModel.dataset.studioAction='coordinator-model';coordinatorModel.textContent=copy('coordinatorModel');dock.append(coordinatorModel);
  const back=document.createElement('a');back.className='evo-button evo-back';back.href='#/chat';back.dataset.workspaceLink='chat';back.setAttribute('aria-label',text('backToChat'));back.textContent='← Tofu';
  const menuButton=document.createElement('wa-button');menuButton.dataset.studioAction='menu';menuButton.setAttribute('appearance','filled');menuButton.setAttribute('aria-label',text('navigation'));menuButton.textContent=copy('studioMenu');
  dock.append(back,menuButton);
  const bottomChrome=document.createElement('div');bottomChrome.className='evo-bottom-chrome';bottomChrome.append(concierge,dock);q('.evo-main').append(bottomChrome);
  const conversation=mountGoalConversation(conversationHost,{api:ports.api,copy,refresh:refreshAfterWrite,navigate:ports.navigate});
  q('.evo-page-head').hidden=true;q('.evo-studio-aside').hidden=true;q('.evo-office-caption').hidden=true;
  function readDelivery(){openDrawer(detailDrawer,copy('deliverable'));}
  const roleModels=mountRoleModels(roleHost,{copy,api:ports.api,models:()=>models,
    ready:()=>briefingOpen||!selectedId()||!!active,current:()=>briefingOpen?undefined:active,draft:()=>draftRoleModels,setDraft:value=>{draftRoleModels=value;},
    refresh:refreshAfterWrite,openTask:ports.openTask,askHuman:()=>openDrawer(detailDrawer,copy('askHuman'))});
  function configureRole(role:EvolutionRole){roleModels.open(role);openDrawer(roleDrawer,copy('roleModelTitle'));}
  const scene = mountOfficeScene(q('.evo-office-live'),q('.evo-office-controls'),{copy,configureRole:index=>configureRole((['executor','reviewer','coordinator'] as EvolutionRole[])[index]),openTask:ports.openTask,askHuman:()=>openDrawer(detailDrawer,copy('askHuman'))});
  // The floor grows rooms with the swarm: one office per rostered sub-agent.
  const rooms = mountOfficeRooms(q('.evo-office'),dock,{copy,openTask:ports.openTask,onSelect:()=>{
    // The mission comes first on mobile. Selecting a room is explicit navigation
    // to its scene, not permission to animate/load rooms that remain offscreen.
    revealSelectedRoom=roomsDrawer.hasAttribute('open');
    roomsDrawer.removeAttribute('open');
  }});
  const ledgerHost=document.createElement('section');ledgerHost.className='evo-live-ledger';ledgerHost.hidden=true;
  q('.evo-main').append(ledgerHost);
  const ledger=mountOfficeLedger(ledgerHost,copy);
  const rail=document.createElement('aside');rail.className='evo-room-rail';
  const brand=document.createElement('a');brand.className='evo-studio-brand';brand.href='#/evolution';
  brand.innerHTML=`<span class="evo-brand-mark" aria-hidden="true">T<span>↗</span></span><span class="evo-brand-copy"><b>Tofu</b><span>${text('studio')}</span></span>`;
  const railFooter=document.createElement('footer');railFooter.className='evo-rail-footer';
  const newGoal=createButton({label:`＋ ${copy('newGoal')}`,variant:'primary',
    onClick:()=>{if(path.split('/')[1]==='start')showBriefing();else ports.navigate('evolution/start');}},root.ownerDocument);
  railFooter.append(concierge,newGoal);
  const destinations=document.createElement('nav');destinations.className='evo-destinations ui-stack';
  destinations.setAttribute('aria-label',copy('navigation'));
  for(const [label,route,mark] of [['programNotebook','evolution/programs','clipboard'],['research','evolution/research','search'],['evidence','evolution/capabilities','chart']]){
    const destination=createButton({label:copy(label!),variant:'ghost',wrap:true,onClick:()=>ports.navigate(route!)},root.ownerDocument);
    const icon=document.createElement('span');icon.className='evo-destination-icon';icon.setAttribute('aria-hidden','true');icon.innerHTML=iconHtml(mark!);
    destination.prepend(icon);destinations.append(destination);
  }
  rail.append(brand,destinations,rooms.bar,rooms.inspector,railFooter);q('.evo-main').prepend(rail);
  const actions=document.createElement('div');actions.className='evo-studio-actions';
  const roomsButton=document.createElement('button');roomsButton.type='button';roomsButton.className='evo-rooms-open';
  roomsButton.textContent=copy('rooms');roomsButton.setAttribute('aria-controls','evo-rooms-drawer');
  roomsButton.setAttribute('aria-haspopup','dialog');roomsButton.addEventListener('click',()=>openDrawer(roomsDrawer));
  actions.append(roomsButton,menuButton);ledgerHost.append(actions);
  const sceneToolbar=document.createElement('header');sceneToolbar.className='evo-scene-toolbar ui-row ui-row--between';
  sceneToolbar.append(rooms.heading);q('.evo-studio-grid').prepend(sceneToolbar);
  // Secondary management actions remain available in the existing menu.
  const management=document.createElement('div');management.className='evo-menu-actions';
  management.append(status,gateButton,floorButton,coordinatorModel,back);menuDrawer.prepend(management);
  const facts=q('.evo-ledger-facts');bottomChrome.append(facts);
  const panHint=document.createElement('span');panHint.className='evo-pan-hint';panHint.textContent=copy('roomPan');q('.evo-studio-grid').append(panHint);
  const narrow=window.matchMedia('(max-width: 850px)');
  function placeRoomRail(){
    roomsDrawer.removeAttribute('open');
    if(narrow.matches){roomsDrawer.append(rail);bottomChrome.prepend(rooms.inspector);}
    else{q('.evo-main').prepend(rail);rail.insertBefore(rooms.inspector,railFooter);}
  }
  narrow.addEventListener('change',placeRoomRail);placeRoomRail();
  // At narrow widths the stage keeps playable scale and pans horizontally.
  // Center the first view once; native touch/trackpad scrolling owns the rest.
  if(window.matchMedia?.('(max-width: 650px)').matches){
    window.requestAnimationFrame(()=>{
      const office=q<HTMLElement>('.evo-office');
      office.scrollLeft=Math.max(0,(office.scrollWidth-office.clientWidth)/2);
    });
  }

  const room = mountGoalRoom(q('.evo-goal-panel'),q('.evo-office-objects'),q('.evo-place-shortcuts'),{api:ports.api,copy,refresh:refreshAfterWrite,openTask:id=>{drawers.forEach(item=>item.removeAttribute('open'));ports.openTask(id);},readDelivery,create:showBriefing,openPanel:()=>openDrawer(roomDrawer,copy('exploreGoal'))});
  const goalSelect=q<HTMLSelectElement>('#evo-goal');
  const error = q('.evo-error');
  const objective = q<HTMLTextAreaElement>('#evo-objective');
  const model = q<HTMLSelectElement>('#evo-model');

  const missionHost=document.createElement('aside');q('.evo-main').append(missionHost);
  const command=mountGoalCommandPanel(missionHost,{copy,openTask:ports.openTask,
    stop:()=>{void write('cancel');},retry:prepareRetry,details:()=>openDrawer(detailDrawer),
    conversation:()=>openDrawer(conversationDrawer),create:showBriefing});
  const center=mountCommandCenter(missionHost,q<HTMLFormElement>('.evo-objective-form'),{
    copy,details:()=>openDrawer(conversationDrawer),configure:()=>configureRole('executor'),briefing:showBriefing,
    inspect:place=>room.open(place),dismiss:()=>{briefingOpen=false;render();}});
  const executionEnvironment=mountExecutionEnvironment(center.settings,{copy,signal:abort.signal,
    load:signal=>ports.api.listExecutorPlugins(signal)});
  const dispatch=center.submit;
  // Progress belongs with the task evidence, not above the illustrated room.
  missionHost.insertBefore(center.hud,q('.evo-mission-body'));
  const focus=createButton({label:copy('sceneFocus'),variant:'ghost',size:'sm',onClick:()=>{
    const focused=root.dataset.sceneFocus!=='true';root.dataset.sceneFocus=String(focused);
    focus.setAttribute('aria-pressed',String(focused));focus.textContent=copy(focused?'sceneRestore':'sceneFocus');
    command.visibilityChanged(!focused&&!document.hidden&&!briefingOpen);
  }});
  focus.dataset.sceneFocusToggle='';focus.setAttribute('aria-pressed','false');sceneToolbar.append(focus);
  const headerNewGoal=createButton({label:copy('newGoal'),variant:'primary',size:'sm',onClick:showBriefing},root.ownerDocument);
  const notebook=createButton({label:copy('programNotebook'),variant:'ghost',size:'sm',onClick:()=>ports.navigate('evolution/programs')},root.ownerDocument);
  notebook.dataset.studioAction='notebook';
  headerNewGoal.dataset.briefingAction='new';actions.append(notebook,headerNewGoal);
  const sceneNote=document.createElement('p');sceneNote.className='evo-scene-guide';sceneNote.textContent=copy('sceneGuide');
  q('.evo-studio-grid').append(sceneNote);
  management.append(createButton({label:copy('programNotebook'),variant:'ghost',onClick:()=>ports.navigate('evolution/programs')}));
  function showBriefing(){
    briefingFocusPending=drawers.some(item=>item.open||item.hasAttribute('open'));
    objective.disabled=briefingFocusPending;dispatch.disabled=busy||briefingFocusPending;
    briefingOpen=true;drawers.forEach(item=>item.removeAttribute('open'));
    root.dataset.sceneFocus='false';focus.setAttribute('aria-pressed','false');focus.textContent=copy('sceneFocus');
    render();center.focus();
  }
  const detailHost=q('.evo-run-detail'),detailToolbar=document.createElement('div'),detailBody=document.createElement('div');
  detailToolbar.className='evo-mission-actions';detailHost.append(detailToolbar,detailBody);
  const detailStop=createButton({label:copy('cancel'),variant:'danger',onClick:()=>{void write('cancel');}});
  const detailRefresh=createButton({label:copy('reconcile'),onClick:()=>{void write('reconcile');}});
  const detailDownload=createButton({label:copy('download'),onClick:download});
  detailToolbar.append(detailStop,detailRefresh,detailDownload);
  runInspector=mountGoalRunInspector(detailBody,{copy,openTask:ports.openTask,detailed:true,deliverableId:'evo-deliverable',
    readOutcome:ports.api.getStageOutcome?(id,taskId,offset,signal,sourceKind)=>ports.api.getStageOutcome(id,taskId,offset,signal,sourceKind):undefined,
    visible:()=>detailDrawer.hasAttribute('open')&&!document.hidden});
  // Retry prepares a new authorization in the existing form; it never silently
  // replays side effects or reuses the failed task. The user confirms Start.
  function prepareRetry(){
    if(!active||!terminal(active)||busy)return;
    objective.value=active.objective;draftRoleModels={...active.role_models};
    const index=models.findIndex(entry=>JSON.stringify(entry.model)===JSON.stringify(active?.model));
    model.value=index>=0?String(index+1):'';
    showBriefing();
  }
  function showError(value: unknown) { if(disposed)return; error.textContent=value instanceof Error?value.message:String(value||''); error.hidden=!value;if(value)(drawers.find(item=>item.hasAttribute('open'))||q('.evo-main')).prepend(error); }
  function selectedId() { try { return path.split('/')[1]==='runs'?decodeURIComponent(path.split('/')[2] || ''):''; } catch { return ''; } }
  function setBusy(value: boolean) { busy=value; executionEnvironment.setBusy(value); dispatch.disabled=value||briefingFocusPending||!!focusReturnTimer; objective.readOnly=value; model.disabled=value; dispatch.textContent=copy(value?'starting':'briefingDispatch'); command.update({run:active,workspace,coordination,questions:questions.length,loading:!!selectedId()&&!active},value);detailStop.disabled=value;detailRefresh.disabled=value||!active; }
  function render() {
    if(disposed)return;
    const run = active;
    const working = run && !terminal(run);
    const rows=run&&!runs.some(r=>r.run_id===run.run_id)?[run,...runs]:runs;
    const goalOptions=`<option value="">${text('newGoal')}</option>`+rows.map(r=>`<option value="${esc(r.run_id)}">${esc(r.objective.slice(0,90))} · ${text(r.status)}</option>`).join('');
    if(goalSelect.dataset.options!==goalOptions){goalSelect.innerHTML=goalOptions;goalSelect.dataset.options=goalOptions;}
    goalSelect.value=run?.run_id||selectedId();
    goalSelect.title=run?.objective||'';
    const hasSelection=!!run||!!selectedId();
    q('.evo-delivery').hidden=hasSelection;
    roleModels.sync();
    room.update(run,workspace);
    rooms.update(swarmAgents,run?.status==='executing'?run.task_id:'',swarmKnown);
    conversation.update(run,coordination,coordinationError,questions.length);
    concierge.hidden=!run;
    concierge.querySelector('small')!.textContent=copy(questions.length?'askHuman':coordination?.coordinator.status==='running'?'coordinatorThinking':run?.result?'resultAvailable':'conversationHint');
    concierge.dataset.attention=String(!!questions.length||coordination?.coordinator.status==='needs_attention'||proposals.items.length>0);
    gateButton.hidden=!proposals.items.length&&!gateReceipts.size;
    gateButton.textContent=`${proposals.items.length||gateReceipts.size} · ${copy('gatePending')}`;
    patch(proposalsHost,proposalsPanelHtml(proposals,gateReceipts));
    q('.evo-office-state').textContent=questions.length?copy('askHuman'):proposals.items.length?copy('gatePending'):run?runStatusLabel(run.status,copy):copy('idle');
    const coordinatorStatus=coordination?.coordinator.status||'';
    scene.update(['executing','reviewing'].map((stage,index) => {
      const isActive = !!working && run!.status===stage;
      const taskId = isActive ? run!.task_id : [...(run?.receipts||[])].reverse().find(r=>r.stage===stage)?.task_id;
      const waiting = isActive && questions.length>0;
      return {name:copy(index?'reviewer':'executor'),state:copy(waiting?'askHuman':isActive?'working':taskId?'recorded':'idle'),taskId:taskId||'',working:isActive,waiting};
    }).concat({
      name:copy('coordinator'),
      state:copy(coordinatorStatus==='running'?'coordinatorThinking':coordinatorStatus==='needs_attention'?'coordinatorBlocked':'idle'),
      taskId:coordination?.coordinator.task_id||'',
      working:coordinatorStatus==='running',waiting:coordinatorStatus==='needs_attention',
    }),{goalId:run?.run_id||'',materials:workspace?.materials||[],
      assignmentSeq:Math.max(0,...(coordination?.updates||[]).filter(item=>item.kind==='assignment'&&item.target==='executing').map(item=>item.seq)),
      agents:swarmAgents,agentsTaskId:run?.status==='executing'?run.task_id:''});
    ledger.update({run,workspace,coordination,coordinationError,agents:swarmAgents,agentsKnown:swarmKnown,questions:questions.length});
    syncActivityStream(run);
    const observation={run,workspace,coordination,questions:questions.length,loading:!!selectedId()&&!run};
    command.update(observation,busy);runInspector?.update(observation);
    const briefing=briefingOpen||!hasSelection;
    missionHost.dataset.briefing=String(briefing);center.update(observation,briefing);
    command.visibilityChanged(!briefing&&!document.hidden&&root.dataset.sceneFocus!=='true');
    detailStop.hidden=!working;detailStop.disabled=busy;detailRefresh.disabled=busy||!run;detailDownload.hidden=!run?.result;
    patch(q('.evo-human'), questions.map(question=>{
      const fields=question.questions.length?question.questions:[question];
      return `<form data-key="${esc(question.guidance_id)}" data-evo-island="true" data-question="${esc(question.guidance_id)}"><h2>${text('askHuman')}</h2><p>${text('askNote')}</p>${fields.map((f,i)=>`<label>${esc(f.question)}<textarea name="answer-${i}" required maxlength="6000" placeholder="${text('yourAnswer')}"></textarea></label>${(f.options||[]).map(option=>`<button type="button" class="evo-button" data-studio-action="option" data-index="${i}" data-value="${esc(option)}">${esc(option)}</button>`).join('')}`).join('')}<button class="evo-button evo-primary" type="submit">${text('answerContinue')}</button><p role="status"></p></form>`;
    }).join(''));
    const kindCopy:Record<EvolutionFloor['items'][number]['kind'],string>={evolution:copy('floorKindEvolution'),flow:copy('floorKindFlow'),goal:copy('floorKindGoal')};
    const floorHtml=floor.items.length||floor.degraded
      ?`<div class="evo-floor" aria-label="${text('floorLive')}"><h3>${text('floorLive')}</h3>${floor.items.map(item=>{
        const external=item.kind!=='evolution';
        const href=external?'#/chat':`#/evolution/runs/${encodeURIComponent(item.run_id)}`;
        const conv=item.conversation_id?` data-studio-action="open-conv" data-conv="${esc(item.conversation_id)}"`:'';
        return `<a class="evo-floor-row" data-kind="${esc(item.kind)}" href="${href}"${conv}${external?` title="${text('floorOpenChat')}"`:''}><small>${esc(kindCopy[item.kind])}</small><span>${esc(item.title)}</span><em>${text(item.status)}</em></a>`;
      }).join('')}${floor.degraded?`<p class="evo-muted">${text('floorDegraded')}</p>`:''}</div>`
      :'';
    floorButton.hidden=!floor.items.length&&!floor.degraded;
    floorButton.textContent=`${floor.items.length} · ${copy('floorLive')}`;
    patch(floorHost,floorHtml);
    patch(q('.evo-run-history>div'), runs.length ? runs.map(r=>`<a href="#/evolution/runs/${encodeURIComponent(r.run_id)}" class="evo-history-row" ${r.run_id===run?.run_id?'aria-current="true"':''}><span>${esc(r.objective)}</span><small>${text(r.status)}　↗</small></a>`).join(''):`<p class="evo-muted">${text('historyEmpty')}</p>`);
  }
  async function refresh() {
    if(disposed || document.hidden)return;
    if(flight)return flight;
    const requestedPath=path;
    flight=(async()=>{
      try {
        const response=await ports.api.listRuns(abort.signal);
        if(disposed||path!==requestedPath)return;
        runs=response.runs.slice(0,50).sort((a,b)=>b.created_at-a.created_at);
        floor=response.floor||{items:[],degraded:false};
        proposals=response.proposals||{items:[],degraded:false};
        const id=path.split('/')[1]==='start'?undefined:selectedId() || active?.run_id || runs.find(r=>!terminal(r))?.run_id || runs[0]?.run_id;
        if(id){
          const [detail,dialogue]=await Promise.all([ports.api.getRun(id,abort.signal),ports.api.getCoordination(id,abort.signal).then(value=>({value,error:''}),reason=>({value:undefined,error:reason instanceof Error?reason.message:String(reason)}))]);
          if(disposed||path!==requestedPath)return;
          // A navigation during the request must not replace the new selection.
          if(selectedId() && id!==selectedId())return;
          active=detail.run; workspace=detail.workspace;questions=detail.pending_questions||[];
          coordination=dialogue.value;coordinationError=dialogue.error;
          // Live sub-agent roster for the office door: only the executing task
          // can own a swarm; fail-soft to an empty floor.
          const swarmTaskId=detail.run.status==='executing'?detail.run.task_id:'';
          const swarm=swarmTaskId?await ports.api.getSwarmStatus(swarmTaskId,abort.signal).then(value=>({value}),()=>({value:undefined})):{value:undefined};
          if(disposed||path!==requestedPath)return;
          swarmAgents=(swarm.value?.agents||[]).slice(0,8);
          swarmKnown=!swarmTaskId||Boolean(swarm.value&&swarm.value.known!==false);
        } else {active=undefined;workspace=undefined;questions=[];coordination=undefined;coordinationError='';swarmAgents=[];swarmKnown=true;}
        showError(''); render();
      } catch(e){if(!abort.signal.aborted)showError(e);}
    })().finally(()=>{flight=undefined;schedule();});
    return flight;
  }
  async function refreshAfterWrite(){if(flight)await flight;await refresh();}
  function schedule(){
    clearTimeout(timer);timer=0;
    if(!disposed&&!document.hidden)timer=window.setTimeout(()=>{void refresh();},(active&&!terminal(active))||coordination?.coordinator.status==='running'?3000:15000);
  }
  function visibility(){runInspector?.visibilityChanged(!document.hidden&&detailDrawer.hasAttribute('open'));command.visibilityChanged(!document.hidden&&!briefingOpen&&root.dataset.sceneFocus!=='true');if(document.hidden){clearTimeout(timer);timer=0;}else void refresh();}
  // Live tool bubbles are pure decoration over the polling authority: one SSE
  // subscription per active stage task, swapped whenever the run/stage flips.
  function closeActivityStream(){
    activityStream?.close();activityStream=undefined;activityTaskId='';
    activeToolCalls.clear();
    scene.setToolBubble(0,'');scene.setToolBubble(1,'');
  }
  function syncActivityStream(run: EvolutionRun|undefined){
    const liveTaskId=run&&!terminal(run)&&run.task_id&&(run.status==='executing'||run.status==='reviewing')?run.task_id:'';
    if(liveTaskId===activityTaskId)return;
    closeActivityStream();
    if(!liveTaskId||!run)return;
    activityTaskId=liveTaskId;
    const actorIndex=run.status==='executing'?0:1;
    function projectActivity(){
      const current=[...activeToolCalls.values()].at(-1);
      scene.setToolBubble(actorIndex,current?t('evolution.studio.toolBubble',{tool:current.tool}):'',current?.tool);
    }
    activityStream=followTaskActivity(liveTaskId,{
      toolStart:activity=>{
        activeToolCalls.set(activity.toolCallId||activity.tool,activity);
        projectActivity();
      },
      toolEnd:id=>{
        if(id)activeToolCalls.delete(id);else activeToolCalls.clear();
        projectActivity();
      },
      done:()=>closeActivityStream(),
    });
  }
  async function onSubmit(event: Event){
    const form=event.target as HTMLFormElement;
    if(!root.contains(form))return;event.preventDefault();
    if(busy)return;
    if(form.classList.contains('evo-objective-form')){
      let executionOptions: Pick<CreateRunBody, 'project_path' | 'executor_plugins'>;
      try {executionOptions=executionEnvironment.read();}catch(reason){showError(reason instanceof Error?reason.message:String(reason));return;}
      const body={...executionOptions,objective:objective.value.trim(),...(Object.keys(draftRoleModels).length?{role_models:{...draftRoleModels}}:{}),...(model.value?{model:models[Number(model.value)-1].model}:{})};
      if(!body.objective)return;
      if(!pendingSubmit||JSON.stringify(pendingSubmit.body)!==JSON.stringify(body))pendingSubmit={body,key:crypto.randomUUID()};
      const submittedPath=path;
      setBusy(true);showError('');
      try{
        const response=await ports.api.createRun(pendingSubmit.body,pendingSubmit.key);
        if(disposed)return;
        pendingSubmit=undefined;objective.value='';
        if(path!==submittedPath){await refresh();return;}
        briefingOpen=false;active=response.run;workspace=undefined;questions=[];coordination=undefined;coordinationError='';swarmAgents=[];swarmKnown=false;render();
        ports.navigate(`evolution/runs/${encodeURIComponent(active.run_id)}`);
        await refresh();
      }catch(e){showError(e);}finally{if(!disposed)setBusy(false);}
      return;
    }
    const question=questions.find(question=>question.guidance_id===form.dataset.question);
    if(!question||!active)return;
    const answers=Array.from(form.querySelectorAll('textarea')).map((field,index)=>({index,answer:field.value.trim()}));
    const response=question.questions.length?JSON.stringify(answers):answers[0].answer;
    const button=form.querySelector<HTMLButtonElement>('button[type=submit]')!;
    button.disabled=true;
    try{await ports.api.answerRun(active.run_id,question.guidance_id,response);await refreshAfterWrite();}
    catch(e){showError(e);}finally{if(!disposed)button.disabled=false;}
  }
  async function write(action: 'cancel'|'reconcile'){
    if(!active||busy||disposed)return;
    const runId=active.run_id;
    setBusy(true);showError('');
    try{
      if(action==='cancel')await ports.api.cancelRun(runId);
      else await ports.api.reconcileRun(runId);
      await refreshAfterWrite();
    }catch(e){showError(e);}finally{if(!disposed)setBusy(false);}
  }
  function download(){
    if(!active?.result||disposed)return;
    const url=URL.createObjectURL(new Blob([active.result],{type:'text/markdown;charset=utf-8'}));
    const anchor=document.createElement('a');anchor.href=url;anchor.download=`${active.run_id}.md`;anchor.click();
    window.setTimeout(()=>URL.revokeObjectURL(url),0);
  }
  async function onClick(event: Event){
    const link=(event.target as Element).closest<HTMLAnchorElement>('a[href^="#/"]');
    if(link?.getAttribute('href')==='#/evolution/start'&&path.split('/')[1]==='start'){event.preventDefault();showBriefing();return;}
    if(link)drawers.forEach(item=>item.removeAttribute('open'));
    const target=(event.target as Element).closest<HTMLElement>('[data-studio-action]');
    if(!target||!root.contains(target))return;
    const action=target.dataset.studioAction;
    if(action==='notebook')return; // The shared control owns navigation.
    if(action==='task'){if(target.dataset.task)ports.openTask(target.dataset.task);return;}
    if(action==='open-conv'){const conv=target.dataset.conv||'';const opener=(window as unknown as {openConversation?:(id:string)=>unknown}).openConversation;if(conv&&typeof opener==='function')opener(conv);return;}
    if(action==='option'){
      const field=target.closest('form')?.querySelector<HTMLTextAreaElement>(`[name="answer-${Number(target.dataset.index)}"]`);
      if(field){field.value=target.dataset.value||'';field.focus();}return;
    }
    if(action==='coordinator-model'){configureRole('coordinator');return;}
    if(action==='conversation'){openDrawer(conversationDrawer);return;}
    if(action==='read'){readDelivery();return;}
    if(action==='menu'){openDrawer(menuDrawer);return;}
    if(action==='details'){openDrawer(detailDrawer,copy(questions.length?'askHuman':'currentTask'));return;}
    if(action==='proposals'){openDrawer(proposalsDrawer,copy('proposalsTitle'));return;}
    if(action==='floor'){openDrawer(floorDrawer,copy('floorLive'));return;}
    if(action==='download'){download();return;}
    if(action==='cancel'||action==='reconcile'){await write(action);return;}
    if(busy)return;
    target.setAttribute('disabled','');
    try{
      if(action==='proposal-enable'||action==='proposal-waiver'||action==='proposal-reject'||action==='proposal-dismiss'){
        const id=target.dataset.id||'';
        if(id){
          if(action==='proposal-dismiss'){gateReceipts.delete(id);render();return;}
          const pendingItem=proposals.items.find(item=>item.mutation_id===id);
          const title=gateReceipts.get(id)?.title||pendingItem?.title||id;
          if(action==='proposal-reject'){
            await ports.api.mutationAction(id,'reject');
            if(!disposed)gateReceipts.set(id,{result:{mutation:{},final_status:'rejected'},title});
          }else{
            const result=await ports.api.mutationAction(id,action==='proposal-waiver'?'enable_waiver':'enable');
            if(!disposed)gateReceipts.set(id,{result:result as GateReceipt['result'],title});
          }
          if(!disposed)render();
        }
      }
      await refreshAfterWrite();
    }catch(e){showError(e);}finally{if(!disposed)target.removeAttribute('disabled');}
  }
  function changeGoal(){ports.navigate(goalSelect.value?`evolution/runs/${encodeURIComponent(goalSelect.value)}`:'evolution/start');}
  goalSelect.addEventListener('change',changeGoal);
  root.addEventListener('submit',onSubmit);root.addEventListener('click',onClick);
  document.addEventListener('visibilitychange',visibility);
  render();if(briefingOpen)center.focus();void refresh();
  void ports.api.listRunModels(abort.signal).then(response=>{
    if(disposed)return;models=response.models.slice(0,128);roleModels.refreshOptions();
    models.forEach((entry,i)=>model.add(new Option(entry.label,String(i+1))));
    if(!models.length)q('.evo-model-message').textContent=copy('noModels');
  }).catch(()=>{if(!disposed)q('.evo-model-message').textContent=copy('modelsUnavailable');});
  return {
    updateRoute(next: string){briefingFocusPending=next.split('/')[1]==='start'&&drawers.some(item=>item.open||item.hasAttribute('open'));objective.disabled=briefingFocusPending;dispatch.disabled=busy||briefingFocusPending;drawers.forEach(item=>item.removeAttribute('open'));path=next;briefingOpen=path.split('/')[1]==='start';active=undefined;workspace=undefined;questions=[];coordination=undefined;coordinationError='';swarmKnown=false;render();if(briefingOpen)center.focus();void (flight||Promise.resolve()).then(()=>refresh());},
    dispose(){disposed=true;clearTimeout(focusReturnTimer);command.dispose();runInspector?.dispose();narrow.removeEventListener('change',placeRoomRail);closeActivityStream();root.removeEventListener('wa-after-hide',restoreDrawerFocus);drawers.forEach(item=>item.removeAttribute('open'));conversation.dispose();room.dispose();scene.dispose();rooms.dispose();roleModels.dispose();goalSelect.removeEventListener('change',changeGoal);abort.abort();clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);root.removeEventListener('submit',onSubmit);root.removeEventListener('click',onClick);delete root.dataset.sceneFocus;root.classList.remove('evo-studio');},
  };
}
