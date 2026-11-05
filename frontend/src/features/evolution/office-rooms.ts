/** Room directory and live occupancy. Six fixed rooms exist without a task;
 * members, status and briefs come only from the supplied swarm snapshot. The
 * studio swaps the stage scene on selection; only the selected department
 * room has a pod scene. Tiles are icon-first; names live in tooltips/labels.
 * Entry: mountOfficeRooms.
 */
import { esc } from './components/ui';
import { STUDIO_ROOMS, roomForRole, roleTitleKey } from './room-catalog';
import { glyph } from './studio/glyphs';
import type { PodAgent } from './agent-pod';

export function mountOfficeRooms(nav:HTMLElement,card:HTMLElement,ports:{
  copy:(key:string)=>string;select:(room:string)=>void;openTask:(id:string)=>void;
}) {
  const {copy}=ports;
  nav.classList.add('evo-rooms');nav.setAttribute('aria-label',copy('roomSwitch'));
  card.classList.add('evo-member-card');card.hidden=true;
  card.innerHTML=`<header><span class="evo-member-icon" aria-hidden="true">${glyph('user')}</span><div><strong></strong><span class="evo-chip"></span></div></header>
    <p class="evo-member-brief"></p><div class="evo-member-tabs" role="group"></div>
    <button type="button" class="evo-button evo-member-task">${glyph('arrow')}<span>${esc(copy('roomTask'))}</span></button>`;
  const title=card.querySelector('strong')!,chip=card.querySelector<HTMLElement>('.evo-chip')!;
  const brief=card.querySelector<HTMLElement>('.evo-member-brief')!,tabs=card.querySelector<HTMLElement>('.evo-member-tabs')!;
  const task=card.querySelector<HTMLButtonElement>('.evo-member-task')!;
  tabs.setAttribute('aria-label',copy('roomMembers'));
  let active='',selectedMember='',taskId='',known=false,disposed=false,agents:PodAgent[]=[];
  const tiles=STUDIO_ROOMS.map(room=>{
    const button=document.createElement('button');button.type='button';button.className='evo-room-tile';button.dataset.room=room.id;
    button.innerHTML=`<img src="${room.thumbnail}" width="160" height="120" alt="" decoding="async"><span class="evo-room-count" hidden></span><i class="evo-room-dot" aria-hidden="true"></i>`;
    button.addEventListener('click',()=>select(room.id));nav.append(button);return button;
  });
  const occupants=(id:string)=>agents.filter(agent=>roomForRole(agent.role)===id);
  function statusKey(status:string){return ['running','pending','completed','failed','cancelled','needs_attention'].includes(status)?status:status==='done'?'completed':'idle';}
  function render(){
    tiles.forEach((tile,index)=>{
      const room=STUDIO_ROOMS[index],roster=occupants(room.id),on=active===room.id;
      tile.setAttribute('aria-pressed',String(on));
      const name=copy(room.label),count=room.id&&known?roster.length:0;
      const label=room.id?`${name} · ${known?roster.length:'?'}`:name;
      tile.setAttribute('aria-label',label);tile.dataset.tip=label;
      const badge=tile.querySelector<HTMLElement>('.evo-room-count')!;badge.hidden=!count;badge.textContent=String(count);
      tile.querySelector<HTMLElement>('i')!.dataset.status=roster.some(a=>a.status==='running')?'running':roster.some(a=>a.status==='pending')?'pending':'idle';
    });
    const room=STUDIO_ROOMS.find(item=>item.id===active)!,roster=occupants(active);
    if(!roster.some(agent=>agent.id===selectedMember))selectedMember=roster[0]?.id||'';
    const member=roster.find(agent=>agent.id===selectedMember);
    card.hidden=active==='';
    const titleOf=(agent:PodAgent)=>copy(roleTitleKey(agent.role));
    title.textContent=member?titleOf(member):copy(room.label);
    chip.textContent=copy(member?statusKey(member.status):known?'roomUnoccupied':'roomRosterUnknown');
    chip.dataset.status=member?.status||'idle';
    brief.textContent=member?.objective||'';brief.hidden=!member?.objective;
    const existing=new Map([...tabs.querySelectorAll<HTMLButtonElement>('button')].map(button=>[button.dataset.member!,button]));
    roster.forEach((agent,index)=>{
      let button=existing.get(agent.id);
      if(!button){button=document.createElement('button');button.type='button';button.dataset.member=agent.id;button.onclick=()=>{selectedMember=agent.id;render();ports.select(active);};}
      button.textContent=`${titleOf(agent)} ${index+1}`;button.setAttribute('aria-pressed',String(agent.id===selectedMember));
      if(tabs.children[index]!==button)tabs.insertBefore(button,tabs.children[index]||null);
      existing.delete(agent.id);
    });
    existing.forEach(button=>button.remove());
    tabs.hidden=roster.length<2;task.hidden=!member||!taskId;
  }
  function select(id:string){
    if(disposed||!STUDIO_ROOMS.some(room=>room.id===id))return;
    if(active!==id){active=id;selectedMember='';render();}
    ports.select(id);
  }
  task.addEventListener('click',()=>{if(taskId)ports.openTask(taskId);});
  render();
  return {
    update(next:PodAgent[],nextTaskId:string,rosterKnown=true){
      if(disposed)return;
      if(taskId!==nextTaskId)selectedMember='';
      taskId=nextTaskId;known=rosterKnown;agents=[...new Map(next.map(agent=>[agent.id,agent])).values()];render();
    },
    select,
    active(){return active;},
    member():PodAgent|null{return occupants(active).find(agent=>agent.id===selectedMember)||null;},
    accent(){return Math.max(0,STUDIO_ROOMS.findIndex(room=>room.id===active)-1);},
    taskId(){return taskId;},
    dispose(){disposed=true;nav.replaceChildren();card.replaceChildren();},
  };
}
