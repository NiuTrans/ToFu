/** Game HUD over the stage: quest banner, stage track, resource meters, HUD
 * buttons, the hotbar with one context action, and event toasts. Renders the
 * studio projection only; every control reports an action. Owns no transport;
 * its one timer kind expires toasts. Entry: mountHud.
 */
import { esc } from '../components/ui';
import { glyph } from './glyphs';
import { STAGES, statusKey, type StageId, type StudioEvent, type StudioView, type Meter } from './projection';
import { PLACES, type Place } from '../office-layout';
import { failureReason } from '../goal-workbench';

export type HudAction = 'journal'|'menu'|'rooms'|'chat'|'create'|'answer'|'stall'|'read'|'retry'|`place:${Place}`|`stage:${StageId}`;
const STAGE_GLYPH:Record<StageId,string> = {plan:'board',execute:'hammer',review:'lens',deliver:'flag'};
const STAGE_PLACE:Record<StageId,Place> = {plan:'plan',execute:'work',review:'review',deliver:'delivery'};
export const PLACE_GLYPH:Record<Place,string> = {library:'book',plan:'board',work:'monitor',review:'check',delivery:'scroll'};
const PHASE_GLYPH:Record<string,string> = {
  none:'spark',loading:'dots',preparing:'board',executing:'hammer',reviewing:'lens',settling:'scroll',
  completed:'flag',failed:'cross',attention:'alert',cancelled:'stop',unknown:'question',
};
const PRIMARY_GLYPH:Record<string,string> = {answer:'question',stall:'alert',read:'scroll',retry:'retry',create:'plus'};
/** How long a game event toast stays on screen (presentation). */
const TOAST_MS = 4200;

export function formatDuration(seconds:number):string {
  const s=Math.max(0,Math.round(seconds)),h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;
  return h?`${h}:${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`:`${m}:${String(r).padStart(2,'0')}`;
}

export function mountHud(root:HTMLElement,ports:{copy:(key:string)=>string;act:(action:HudAction,origin:HTMLElement)=>void}) {
  const {copy}=ports,text=(key:string)=>esc(copy(key));
  const compact=new Intl.NumberFormat(document.documentElement.lang||undefined,{notation:'compact',maximumFractionDigits:1});
  const exact=new Intl.NumberFormat(document.documentElement.lang||undefined);
  const top=document.createElement('header');top.className='evo-hud-top';
  top.innerHTML=`<button type="button" class="evo-quest" data-hud="journal">
      <span class="evo-emblem" aria-hidden="true"></span>
      <span class="evo-quest-copy"><strong class="evo-quest-title"></strong><span class="evo-quest-status"><span class="evo-quest-state"></span><span class="evo-quest-detail"></span></span></span>
    </button>
    <div class="evo-hud-status"><ol class="evo-track" aria-label="${text('missionRoute')}">${STAGES.map(stage=>`<li data-stage="${stage}"><button type="button" data-hud="stage:${stage}">${glyph(STAGE_GLYPH[stage])}</button></li>`).join('')}</ol>
    <div class="evo-meters">${['tokens','calls','seconds'].map(name=>`<div class="evo-meter" data-meter="${name}" role="img">${glyph(name==='tokens'?'coin':name==='calls'?'wrench':'clock')}<b></b><i class="evo-meter-bar" hidden><span></span></i></div>`).join('')}</div></div>
    <div class="evo-hud-actions">
      <button type="button" class="evo-hud-button" data-hud="rooms" aria-label="${text('rooms')}" data-tip="${text('rooms')}">${glyph('map')}</button>
      <button type="button" class="evo-hud-button" data-hud="journal" aria-label="${text('journal')}" data-tip="${text('journal')} · J">${glyph('notebook')}</button>
      <button type="button" class="evo-hud-button" data-hud="menu" aria-label="${text('navigation')}" data-tip="${text('navigation')} · Esc">${glyph('menu')}<span class="evo-hud-badge" hidden></span></button>
    </div>`;
  const bar=document.createElement('footer');bar.className='evo-hotbar';
  bar.innerHTML=`<div class="evo-slots" role="toolbar" aria-label="${text('exploreGoal')}">
      ${PLACES.map((place,index)=>`<button type="button" class="evo-slot" data-hud="place:${place}" data-place="${place}">${glyph(PLACE_GLYPH[place])}<span class="evo-slot-count" hidden></span><kbd aria-hidden="true">${index+1}</kbd></button>`).join('')}
      <span class="evo-slot-gap" aria-hidden="true"></span>
      <button type="button" class="evo-slot" data-hud="chat" data-slot="chat">${glyph('chat')}<span class="evo-slot-count" hidden></span><kbd aria-hidden="true">T</kbd></button>
      <button type="button" class="evo-slot" data-hud="create" data-slot="create">${glyph('plus')}<kbd aria-hidden="true">N</kbd></button>
    </div>
    <button type="button" class="evo-primary-action" hidden></button>`;
  const toasts=document.createElement('div');toasts.className='evo-toasts';toasts.setAttribute('role','status');toasts.setAttribute('aria-live','polite');
  root.append(top,bar,toasts);
  const q=<T extends HTMLElement>(host:HTMLElement,selector:string)=>host.querySelector<T>(selector)!;
  const quest=q<HTMLButtonElement>(top,'.evo-quest'),emblem=q(top,'.evo-emblem');
  const title=q(top,'.evo-quest-title'),state=q(top,'.evo-quest-state'),detail=q(top,'.evo-quest-detail');
  const primary=q<HTMLButtonElement>(bar,'.evo-primary-action'),menuBadge=q(top,'[data-hud=menu] .evo-hud-badge');
  let emblemGlyph='',disposed=false;
  const timers=new Set<number>();
  function click(event:Event){
    const target=(event.target as Element).closest<HTMLElement>('[data-hud]');
    if(!target||!root.contains(target)||(target as HTMLButtonElement).disabled)return;
    ports.act(target.dataset.hud as HudAction,target);
  }
  top.addEventListener('click',click);bar.addEventListener('click',click);
  function meterText(meter:Meter|undefined,format:(value:number)=>string){
    if(!meter)return '—';
    return `${meter.lowerBound?'≥':''}${format(meter.value)}`;
  }
  function setMeter(name:string,meter:Meter|undefined,format:(value:number)=>string,full:(value:number)=>string,label:string){
    const node=q(top,`[data-meter=${name}]`),value=meterText(meter,format);
    node.querySelector('b')!.textContent=value;
    const fullText=meter?`${label} ${meter.lowerBound?'≥ ':''}${full(meter.value)}${meter.limit?` / ${full(meter.limit)}`:''}`:`${label} —`;
    node.setAttribute('aria-label',fullText);node.dataset.tip=fullText;
    const barNode=node.querySelector<HTMLElement>('.evo-meter-bar')!;barNode.hidden=!meter?.limit;
    if(meter?.limit){const ratio=Math.min(1,meter.value/meter.limit);barNode.style.setProperty('--fill',String(ratio));node.dataset.level=ratio>=1?'full':ratio>=.8?'high':'ok';}
    else delete node.dataset.level;
  }
  return {
    update(view:StudioView,extras:{placeLabels:Record<Place,string>;placeCounts:Record<Place,string>;proposals:number;chatAttention:boolean;busy:boolean}){
      if(disposed)return;
      root.dataset.phase=view.phase;root.dataset.wait=view.wait?.kind||'';
      const tone=view.wait?.kind==='human'||view.coordinator==='attention'?'ask'
        :view.wait?.kind==='compute'&&view.wait.error?'warn'
        :view.wait?'wait':view.phase==='completed'?(view.verdict&&view.verdict!=='pass'?'warn':'good')
        :['failed','attention'].includes(view.phase)?'bad':view.phase==='cancelled'?'muted':view.phase==='none'?'idle':'live';
      quest.dataset.tone=tone;
      const nextGlyph=view.wait?.kind==='human'?'question':view.wait?.kind==='compute'?'server':view.wait?'hourglass':PHASE_GLYPH[view.phase];
      if(nextGlyph!==emblemGlyph){emblemGlyph=nextGlyph;emblem.innerHTML=glyph(nextGlyph);}
      const objective=view.phase==='none'?copy('newGoal'):view.phase==='loading'?copy('missionLoading'):view.objective;
      title.textContent=objective;quest.dataset.action=view.phase==='none'?'create':'journal';
      quest.dataset.hud=view.phase==='none'?'create':'journal';
      const status=view.phase==='none'?'':copy(statusKey(view));
      state.textContent=status;
      const failure=view.failure&&view.failure!=='stopped'?copy('fail_'+view.failure):'';
      const waitDetail=view.wait?.kind==='compute'?view.wait.operation:view.wait?.kind==='human'&&view.wait.count>1?`×${view.wait.count}`:'';
      detail.textContent=waitDetail||failure;detail.hidden=!detail.textContent;
      const tip=[objective,status,view.wait?.kind==='compute'?`${view.wait.operation}${view.wait.error?' · '+view.wait.error:''}`:'',
        view.failure&&view.failure!=='stopped'?failureReason(view.error,copy):''].filter(Boolean).join('\n');
      quest.setAttribute('aria-label',[objective,status,waitDetail||failure].filter(Boolean).join(' · '));quest.dataset.tip=tip;
      for(const stage of STAGES){
        const item=q(top,`[data-stage=${stage}]`),button=item.querySelector('button')!,value=view.stages[stage];
        item.dataset.state=value;
        if(value==='active'||value==='waiting')item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');
        const label=`${copy('stage_'+stage)} · ${copy('stageState_'+value)}`;
        button.setAttribute('aria-label',label);button.dataset.tip=label;button.disabled=view.phase==='none';
      }
      setMeter('tokens',view.meters?.tokens,value=>compact.format(value),value=>exact.format(value),'Tokens');
      setMeter('calls',view.meters?.calls,value=>compact.format(value),value=>exact.format(value),copy('toolCalls'));
      setMeter('seconds',view.meters?.seconds,formatDuration,formatDuration,copy('elapsed'));
      q(top,'.evo-meters').hidden=!view.meters;
      menuBadge.hidden=!extras.proposals;menuBadge.textContent=String(extras.proposals);
      for(const place of PLACES){
        const slot=q<HTMLButtonElement>(bar,`[data-place=${place}]`),count=slot.querySelector<HTMLElement>('.evo-slot-count')!;
        const label=extras.placeLabels[place];
        slot.setAttribute('aria-label',label);slot.dataset.tip=`${label} · ${PLACES.indexOf(place)+1}`;
        count.textContent=extras.placeCounts[place];count.hidden=!extras.placeCounts[place];
        slot.disabled=view.phase==='none'||view.phase==='loading';
        const stage=STAGES.find(id=>STAGE_PLACE[id]===place);
        slot.dataset.live=String(place==='delivery'?view.deliverable:!!stage&&['active','waiting'].includes(view.stages[stage]));
        slot.dataset.verdict=place==='review'?view.verdict||'':'';
      }
      const chat=q<HTMLButtonElement>(bar,'[data-slot=chat]');
      chat.setAttribute('aria-label',copy('talkCoordinator'));chat.dataset.tip=`${copy('talkCoordinator')} · T`;chat.disabled=!view.runId;
      chat.dataset.attention=String(extras.chatAttention);
      chat.querySelector<HTMLElement>('.evo-slot-count')!.hidden=true;
      const create=q<HTMLButtonElement>(bar,'[data-slot=create]');create.setAttribute('aria-label',copy('newGoal'));create.dataset.tip=`${copy('newGoal')} · N`;
      primary.hidden=!view.primary;
      if(view.primary){
        primary.dataset.hud=view.primary;primary.dataset.kind=view.primary;
        primary.innerHTML=`${glyph(PRIMARY_GLYPH[view.primary])}<span>${text('primary_'+view.primary)}</span>`;
        primary.disabled=extras.busy&&view.primary!=='read'&&view.primary!=='stall';
      }
    },
    toast(event:StudioEvent){
      if(disposed)return;
      const spec:{icon:string;key:string;tone:string;value?:string}|undefined=
        event.kind==='phase'?{icon:PHASE_GLYPH[event.phase]||'spark',key:'toast_phase_'+event.phase,tone:['failed','attention'].includes(event.phase)?'bad':event.phase==='completed'?'good':'live'}
        :event.kind==='wait'?{icon:event.wait==='compute'?'server':'hourglass',key:'toast_wait_'+event.wait,tone:'wait'}
        :event.kind==='computeError'?{icon:'alert',key:'toast_computeError',tone:'bad'}
        :event.kind==='materials'?{icon:'book',key:'toast_materials',tone:'good',value:`+${event.added}`}
        :event.kind==='question'?{icon:'question',key:'toast_question',tone:'ask'}
        :event.kind==='verdict'?{icon:event.verdict==='pass'?'check':event.verdict==='fail'?'cross':'lens',key:'verdict_'+event.verdict,tone:event.verdict==='pass'?'good':event.verdict==='fail'?'bad':'warn'}
        :undefined;
      if(!spec)return;
      const node=document.createElement('div');node.className='evo-toast';node.dataset.tone=spec.tone;
      node.innerHTML=`${glyph(spec.icon)}<span>${text(spec.key)}</span>${spec.value?`<b>${esc(spec.value)}</b>`:''}`;
      toasts.append(node);
      const timer=window.setTimeout(()=>{timers.delete(timer);node.remove();},TOAST_MS);timers.add(timer);
    },
    insets(){
      const rootBox=root.getBoundingClientRect(),topBox=top.getBoundingClientRect(),barBox=bar.getBoundingClientRect();
      return {top:Math.max(0,topBox.bottom-rootBox.top),bottom:Math.max(0,rootBox.bottom-barBox.top)};
    },
    dispose(){disposed=true;timers.forEach(timer=>window.clearTimeout(timer));timers.clear();top.removeEventListener('click',click);bar.removeEventListener('click',click);top.remove();bar.remove();toasts.remove();},
  };
}
