/** Goal spokesperson view. Journal is server-owned; only bounded unsent drafts live here.
 * No timers. Studio supplies snapshots. Late reads/writes are fenced by goal identity;
 * ambiguous writes keep their original idempotency key and payload until acknowledged.
 */
import type {EvolutionApi} from './controller';
import type {EvolutionRun,GoalCoordination,GoalUpdate} from '../../api/evolution-runs';
import {esc,patch} from './components/ui';
import {safeExternalUrl} from './goal-room';
export function mountGoalConversation(host:HTMLElement,ports:{
  api:EvolutionApi;copy:(key:string)=>string;refresh:()=>Promise<unknown>;navigate:(path:string)=>void;
}){
  const {copy}=ports,text=(key:string)=>esc(copy(key));
  let run:EvolutionRun|undefined,state:GoalCoordination|undefined,disposed=false,busy=false,loading=false;
  let entries:GoalUpdate[]=[],earlier=false,error='',notice='';
  const drafts=new Map<string,string>();
  const attempts=new Map<string,{content:string;key:string;kind:'message'|'followup'}>();
  const abort=new AbortController();
  host.innerHTML=`<header class="evo-conversation-heading"><span class="evo-conversation-avatar" aria-hidden="true">T</span><div><p>${text('coordinator')}</p><h2>${text('conversationTitle')}</h2></div></header><p class="evo-conversation-objective"></p><p class="evo-conversation-state" role="status"></p><button type="button" class="evo-button evo-conversation-question" data-studio-action="details" hidden>${text('askHuman')} ↗</button><button type="button" class="evo-button evo-conversation-older" data-dialogue="older">${text('earlierMessages')}</button><div class="evo-conversation-log" aria-label="${text('conversationTitle')}"></div><p class="evo-conversation-error" role="alert" hidden></p><p class="evo-conversation-notice" role="status"></p><div class="evo-conversation-recovery"></div><form class="evo-conversation-form"><label for="evo-message">${text('messageLabel')}</label><textarea id="evo-message" name="message" rows="3" maxlength="4000" required placeholder="${text('messagePlaceholder')}"></textarea><div class="evo-conversation-actions"><button class="evo-button evo-primary" type="submit">${text('sendMessage')} ↗</button><button type="button" class="evo-button" data-dialogue="followup">${text('executeFollowup')}</button></div><p class="evo-conversation-hint">${text('messageHint')}</p></form>`;
  const q=<T extends HTMLElement>(selector:string)=>host.querySelector<T>(selector)!;
  const input=q<HTMLTextAreaElement>('textarea');
  function remember(){if(run){drafts.delete(run.run_id);drafts.set(run.run_id,input.value);while(drafts.size>8)drafts.delete(drafts.keys().next().value!);}}
  function merge(page:GoalCoordination,older=false){
    const updates=new Map(entries.map(e=>[e.seq,e]));page.updates.forEach(e=>{const prior=updates.get(e.seq);updates.set(e.seq,prior&&!prior.detail_required&&e.detail_required?prior:e);});
    entries=[...updates.values()].sort((a,b)=>a.seq-b.seq).slice(-128);
    if(!older&&(!state||page.revision>=state.revision))state=page;
    if(older||entries[0]?.seq===page.before_seq)earlier=page.has_earlier;
  }
  function render(){
    if(disposed)return;
    q('.evo-conversation-objective').textContent=run?.objective||copy('roomNoGoal');
    q('.evo-conversation-state').textContent=copy(!state?'conversationLoading':state.coordinator.status==='running'?'coordinatorThinking':state.coordinator.status==='needs_attention'?'coordinatorBlocked':'conversationReady');
    const timeline=entries.map(event=>{
      const receipt=state?.delivery[String(event.seq)];
      const role=event.origin==='user'?'messageYou':event.kind==='reply'?'coordinator':'activityRecord';
      const links=event.references.map(url=>{const safe=safeExternalUrl(url);return safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(new URL(safe).hostname)} ↗</a>`:'';}).join('');
      const body=`<p class="evo-message-body">${esc(event.content)}</p>${links?`<div class="evo-message-links">${links}</div>`:''}${event.detail_required?`<button type="button" class="evo-button" data-dialogue="detail" data-seq="${event.seq}">${text('fullMessage')}</button>`:''}`;
      return `<article class="evo-message" data-key="event-${event.seq}" data-origin="${event.origin}"><header><strong>${text(role)}</strong><small>#${event.seq}</small></header>${['message','reply'].includes(event.kind)?body:`<details><summary>${text('event_'+event.kind)}</summary>${body}</details>`}${receipt?`<footer class="evo-message-receipt" data-status="${receipt.status}"><span>${text('receipt_'+receipt.status)}</span>${receipt.note?`<p>${esc(receipt.note)}</p>`:''}</footer>`:''}</article>`;
    }).join('');
    const log=q('.evo-conversation-log'),followTail=!loading&&log.scrollHeight-log.clientHeight-log.scrollTop<80;
    patch(log,timeline||`<div class="evo-conversation-empty"><h3>${text('conversationEmptyTitle')}</h3><p>${text('conversationEmpty')}</p></div>`);
    if(followTail)log.scrollTop=log.scrollHeight;
    q('.evo-conversation-older').hidden=!earlier;q<HTMLButtonElement>('.evo-conversation-older').disabled=loading;
    q('.evo-conversation-error').textContent=error;q('.evo-conversation-error').hidden=!error;
    q('.evo-conversation-notice').textContent=notice;
    const followup=state?.followup;
    patch(q('.evo-conversation-recovery'),`${run&&attempts.has(run.run_id)&&!busy?`<button type="button" class="evo-button" data-dialogue="retry">${text('retryMessage')}</button>`:''}${state?.coordinator.status==='needs_attention'||notice===copy('wakePending')?`<button type="button" class="evo-button" data-dialogue="recover">${text('checkCoordinator')}</button>`:''}${followup?.status==='submitted'?`<button type="button" class="evo-button" data-dialogue="openFollowup">${text('openFollowup')} ↗</button>`:''}`);
    const terminal=!!run&&['completed','failed','needs_attention','cancelled'].includes(run.status);
    q('[data-dialogue=followup]').hidden=!terminal||!!followup;
    host.querySelectorAll<HTMLButtonElement>('.evo-conversation-actions button').forEach(button=>button.disabled=!run||busy);
    input.readOnly=busy||!!(run&&attempts.has(run.run_id));input.disabled=!run;
  }
  async function send(kind:'message'|'followup'){
    if(busy||!run||!input.value.trim())return;
    const id=run.run_id,content=input.value.trim();remember();
    const existing=attempts.get(id);
    if(existing&&(existing.content!==content||existing.kind!==kind)){
      error=copy('ambiguousMessage');render();return;
    }
    if(!existing&&attempts.size>=8){error=copy('pendingMessageLimit');render();return;}
    const attempt=existing||{content,key:crypto.randomUUID(),kind};attempts.set(id,attempt);
    busy=true;error='';notice='';render();
    try{
      if(kind==='followup'){
        const response=await ports.api.followupRun(id,content,attempt.key);
        attempts.delete(id);drafts.delete(id);
        if(disposed||run?.run_id!==id)return;
        input.value='';ports.navigate(`evolution/runs/${encodeURIComponent(response.run.run_id)}`);
      }else{
        const response=await ports.api.sendGoalMessage(id,content,attempt.key);
        attempts.delete(id);drafts.delete(id);
        if(disposed||run?.run_id!==id)return;
        input.value='';entries=[...entries.filter(e=>e.seq!==response.update.seq),response.update].sort((a,b)=>a.seq-b.seq).slice(-128);
        notice=copy(response.coordinator.status==='pending'?'wakePending':'messageSaved');render();
        await ports.refresh();
      }
    }catch(reason){
      const status=Number((reason as {status?:number})?.status);
      if([400,401,403,404,409,422].includes(status))attempts.delete(id);
      if(!disposed&&run?.run_id===id)error=reason instanceof Error?reason.message:String(reason);
    }
    finally{busy=false;render();}
  }
  function submit(event:Event){event.preventDefault();event.stopPropagation();void send('message');}
  async function click(event:Event){
    const button=(event.target as Element).closest<HTMLButtonElement>('[data-dialogue]');if(!button||!run)return;
    const id=run.run_id,action=button.dataset.dialogue;
    if(action==='retry'){const attempt=attempts.get(id);if(attempt){input.value=attempt.content;void send(attempt.kind);}return;}
    if(action==='followup'){void send('followup');return;}
    if(action==='openFollowup'){if(state?.followup?.run_id)ports.navigate(`evolution/runs/${encodeURIComponent(state.followup.run_id)}`);return;}
    if(loading)return;loading=true;button.disabled=true;error='';
    try{
      if(action==='older'){
        const page=await ports.api.getCoordination(id,abort.signal,entries[0]?.seq);
        if(!disposed&&run?.run_id===id)merge(page,true);
      }else if(action==='detail'){
        const response=await ports.api.getGoalUpdate(id,Number(button.dataset.seq),abort.signal);
        if(!disposed&&run?.run_id===id)entries=entries.map(e=>e.seq===response.update.seq?response.update:e);
      }else if(action==='recover'){
        await ports.api.reconcileCoordinator(id);if(!disposed&&run?.run_id===id){notice='';await ports.refresh();}
      }
    }catch(reason){if(!disposed&&run?.run_id===id)error=reason instanceof Error?reason.message:String(reason);}
    finally{loading=false;button.disabled=false;render();}
  }
  input.addEventListener('input',remember);host.addEventListener('submit',submit);host.addEventListener('click',click);
  return {
    update(next:EvolutionRun|undefined,page?:GoalCoordination,readError='',questionCount=0){
      if(disposed)return;
      if(run?.run_id!==next?.run_id){remember();entries=[];state=undefined;earlier=false;error='';notice='';input.value=next?drafts.get(next.run_id)||'':'';}
      q('.evo-conversation-question').hidden=questionCount===0;
      run=next;if(page)merge(page);if(readError)error=readError;render();
    },
    dispose(){disposed=true;abort.abort();drafts.clear();attempts.clear();input.removeEventListener('input',remember);host.removeEventListener('submit',submit);host.removeEventListener('click',click);},
  };
}
