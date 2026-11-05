/** Dialogue box over the stage. A pending human question is an RPG choice
 * panel with the asking role's portrait; a compute wait whose job status
 * cannot be read or verified is the executor explaining where it is stuck
 * (error, raising frame, since when), that the job is not resubmitted, and
 * whether the Router Watchdog holds the fault, with the record and Watchdog
 * as its actions; otherwise the coordinator's newest unseen line appears once
 * and can be dismissed or answered in chat. A dismissed stall returns when its
 * error changes or through the HUD's primary action.
 * Question forms are keyed islands so drafts survive polling. Answers and the
 * Watchdog go through injected ports; this module owns no transport or timers.
 * Entry: mountDialogue.
 */
import executorArt from '../assets/game-executor.webp';
import reviewerArt from '../assets/game-reviewer.webp';
import coordinatorArt from '../assets/game-coordinator.webp';
import { GAME_FRAMES } from '../office-game-frames';
import { esc, patch } from '../components/ui';
import { glyph } from './glyphs';
import type { EvolutionRun, GoalCoordination, HumanQuestion } from '../../../api/evolution-runs';
import type { ActorRole } from './projection';
import { computeStallFacts, stalledComputeWait } from '../goal-workbench';

const SHEETS:Record<ActorRole,{url:string;width:number}> = {
  executor:{url:executorArt,width:1190},reviewer:{url:reviewerArt,width:1189},coordinator:{url:coordinatorArt,width:1189},
};
const PORTRAIT_PX = 64;
/** Inline style that frames a role's front-facing sprite as a square portrait. */
export function portraitStyle(role:ActorRole):string {
  const [x,y,w]=GAME_FRAMES[role][0][1],scale=PORTRAIT_PX*1.15/w;
  const left=x*scale-(PORTRAIT_PX-w*scale)/2;
  return `background-image:url("${SHEETS[role].url}");background-size:${SHEETS[role].width*scale}px auto;background-position:${-left}px ${-(y*scale)}px`;
}

export function mountDialogue(host:HTMLElement,ports:{
  copy:(key:string)=>string;answer:(questionId:string,response:string)=>Promise<void>;openChat:()=>void;
  /** Opens the executor's record of the stalled job. */
  openRecord:()=>void;
  /** Ensures and opens the owner's Router Watchdog; absent without an application shell. */
  openWatchdog?:()=>Promise<void>;
}) {
  const {copy}=ports,text=(key:string)=>esc(copy(key));
  host.className='evo-dialogue';host.hidden=true;
  host.innerHTML=`<div class="evo-portrait" aria-hidden="true"></div><div class="evo-dialogue-body">
    <header><strong class="evo-speaker"></strong><span class="evo-dialogue-count"></span>
    <button type="button" class="evo-dialogue-close" data-dialogue-action="dismiss" aria-label="${text('dismiss')}">${glyph('close')}</button></header>
    <div class="evo-dialogue-content"></div></div>`;
  const portrait=host.querySelector<HTMLElement>('.evo-portrait')!,speaker=host.querySelector<HTMLElement>('.evo-speaker')!;
  const count=host.querySelector<HTMLElement>('.evo-dialogue-count')!,content=host.querySelector<HTMLElement>('.evo-dialogue-content')!;
  const close=host.querySelector<HTMLButtonElement>('.evo-dialogue-close')!;
  const seen=new Map<string,number>();
  let run:EvolutionRun|undefined,questions:HumanQuestion[]=[],line:{seq:number;content:string}|undefined,portraitRole:ActorRole|''='';
  let disposed=false,collapsed=false,stallDismissed='',watchdogPending=false,watchdogError='';
  const stallKey=(current:EvolutionRun,wait:{last_error?:string})=>`${current.run_id}\n${wait.last_error}`;
  function setPortrait(role:ActorRole){if(portraitRole!==role){portraitRole=role;portrait.setAttribute('style',portraitStyle(role));portrait.dataset.role=role;}}
  function render(){
    if(disposed)return;
    if(questions.length&&run){
      const asking:ActorRole=run.status==='reviewing'?'reviewer':'executor';
      setPortrait(asking);host.dataset.mode='question';host.hidden=collapsed;
      speaker.textContent=copy(asking);
      count.textContent=questions.length>1?`1 / ${questions.length}`:'';count.hidden=questions.length<2;
      close.setAttribute('aria-label',copy('dismiss'));
      const question=questions[0],fields=question.questions.length?question.questions:[question];
      patch(content,`<form class="evo-answer" data-key="${esc(question.guidance_id)}" data-evo-island="true" data-question="${esc(question.guidance_id)}">${fields.map((field,index)=>`
        <fieldset><legend>${esc(field.question)}</legend>
        ${(field.options||[]).length?`<div class="evo-choices">${(field.options||[]).map(option=>`<button type="button" class="evo-choice" data-dialogue-action="option" data-index="${index}" data-value="${esc(option)}">${esc(option)}</button>`).join('')}</div>`:''}
        <textarea name="answer-${index}" required maxlength="6000" rows="2" aria-label="${esc(field.question)}"></textarea></fieldset>`).join('')}
        <div class="evo-dialogue-actions"><p role="status"></p><button type="submit" class="evo-button evo-primary">${glyph('check')}<span>${text('answerContinue')}</span></button></div></form>`);
      return;
    }
    const stall=stalledComputeWait(run);
    if(stall&&run&&stallDismissed!==stallKey(run,stall)){
      setPortrait('executor');host.dataset.mode='stall';host.hidden=false;
      speaker.textContent=copy('executor');count.hidden=true;
      const routed=!!stall.watchdog_signal_id;
      const exit=stall.last_error!.startsWith('successful_exit_unverified');
      patch(content,`<p class="evo-line" data-key="stall-line">${text(exit?'stallLineExit':'stallLine')}</p>
        <dl class="evo-facts evo-stall-facts" data-key="stall-facts">${computeStallFacts(run,copy).map(fact=>`<div data-fact="${fact.fact}"${fact.bad?' data-tone="bad"':''}><dt>${esc(fact.label)}</dt><dd>${fact.value?`<span>${esc(fact.value)}</span> `:''}${fact.code?`<code>${esc(fact.code)}</code>`:''}</dd></div>`).join('')}</dl>
        <p class="evo-stall-note" data-key="stall-note">${text('stallSafe')}</p>
        <p class="evo-stall-route" data-key="stall-route" data-routed="${routed}">${text(routed?'stallRouted':'stallUnrouted')}</p>
        <div class="evo-dialogue-actions" data-key="stall-actions"><p role="status">${esc(watchdogError)}</p>
          <button type="button" class="evo-button" data-dialogue-action="record">${glyph('scroll')}<span>${text('stallOpenRecord')}</span></button>
          ${ports.openWatchdog?`<button type="button" class="evo-button evo-primary" data-dialogue-action="watchdog"${watchdogPending?' disabled aria-busy="true"':''}>${glyph('arrow')}<span>${text(routed?'stallOpenWatchdog':'stallHandToWatchdog')}</span></button>`:''}</div>`);
      return;
    }
    if(line&&run&&(seen.get(run.run_id)||0)<line.seq){
      setPortrait('coordinator');host.dataset.mode='line';host.hidden=false;
      speaker.textContent=copy('coordinator');count.hidden=true;
      patch(content,`<p class="evo-line" data-key="line-${line.seq}">${esc(line.content)}</p>
        <div class="evo-dialogue-actions"><button type="button" class="evo-button" data-dialogue-action="chat">${glyph('chat')}<span>${text('reply')}</span></button></div>`);
      return;
    }
    host.hidden=true;host.dataset.mode='';content.replaceChildren();
  }
  async function submit(event:Event){
    const form=event.target as HTMLFormElement;
    if(!host.contains(form))return;
    event.preventDefault();event.stopPropagation();
    const question=questions.find(item=>item.guidance_id===form.dataset.question);
    if(!question)return;
    const answers=Array.from(form.querySelectorAll('textarea')).map((field,index)=>({index,answer:field.value.trim()}));
    const response=question.questions.length?JSON.stringify(answers):answers[0].answer;
    const button=form.querySelector<HTMLButtonElement>('button[type=submit]')!,status=form.querySelector<HTMLElement>('[role=status]')!;
    button.disabled=true;status.textContent='';
    try{await ports.answer(question.guidance_id,response);}
    catch(error){if(!disposed&&form.isConnected)status.textContent=error instanceof Error?error.message:String(error);}
    finally{if(!disposed&&form.isConnected)button.disabled=false;}
  }
  async function openWatchdog(){
    if(!ports.openWatchdog||watchdogPending)return;
    watchdogPending=true;watchdogError='';render();
    try{await ports.openWatchdog();}
    catch(error){watchdogError=error instanceof Error?error.message:String(error);}
    finally{watchdogPending=false;render();}
  }
  function click(event:Event){
    const target=(event.target as Element).closest<HTMLElement>('[data-dialogue-action]');
    if(!target||!host.contains(target))return;
    const action=target.dataset.dialogueAction;
    if(action==='option'){
      const field=target.closest('form')?.querySelector<HTMLTextAreaElement>(`[name="answer-${Number(target.dataset.index)}"]`);
      if(field){field.value=target.dataset.value||'';field.focus();}
      return;
    }
    if(action==='chat'){if(run&&line)seen.set(run.run_id,line.seq);render();ports.openChat();return;}
    if(action==='record'){ports.openRecord();return;}
    if(action==='watchdog'){void openWatchdog();return;}
    if(action==='dismiss'){
      if(questions.length){collapsed=true;render();return;}
      const stall=stalledComputeWait(run);
      if(stall&&run&&stallDismissed!==stallKey(run,stall)){stallDismissed=stallKey(run,stall);render();return;}
      if(run&&line)seen.set(run.run_id,line.seq);render();
    }
  }
  host.addEventListener('submit',submit);host.addEventListener('click',click);
  return {
    update(next:EvolutionRun|undefined,pending:HumanQuestion[],coordination:GoalCoordination|undefined){
      if(disposed)return;
      const before=questions.map(item=>item.guidance_id).join(',');
      if(next?.run_id!==run?.run_id){collapsed=false;watchdogError='';}
      run=next;questions=pending;
      if(questions.map(item=>item.guidance_id).join(',')!==before&&questions.length)collapsed=false;
      const latest=[...(coordination?.updates||[])].filter(item=>item.origin!=='user'&&item.kind==='reply'&&item.content.trim()).sort((a,b)=>b.seq-a.seq)[0];
      line=latest?{seq:latest.seq,content:latest.content}:undefined;
      render();
    },
    /** Reopen a collapsed question or dismissed stall (the HUD's primary action). */
    reveal(){
      collapsed=false;stallDismissed='';render();
      (host.querySelector<HTMLElement>('textarea')||host.querySelector<HTMLElement>('[data-dialogue-action=watchdog]')
        ||host.querySelector<HTMLElement>('[data-dialogue-action=record]'))?.focus({preventScroll:true});
    },
    unseenLine(){return !!(line&&run&&(seen.get(run.run_id)||0)<line.seq);},
    /** The chat window shows the whole log, so its latest line is read. */
    markSeen(){if(!disposed&&run&&line&&(seen.get(run.run_id)||0)<line.seq){seen.set(run.run_id,line.seq);render();}},
    dispose(){disposed=true;host.removeEventListener('submit',submit);host.removeEventListener('click',click);seen.clear();host.replaceChildren();},
  };
}
