/** Persistent room directory and live occupancy. Rooms exist without a task;
 * members, status and briefs come only from the supplied swarm snapshot.
 * One selected pod owns the canvas; switching never mounts hidden renderers. */
import { esc } from './components/ui';
import { mountAgentPod, type PodAgent } from './agent-pod';
import { STUDIO_ROOMS, roomForRole, type PodTheme } from './room-catalog';
export function mountOfficeRooms(office:HTMLElement, dock:HTMLElement, ports:{
  copy:(key:string)=>string;openTask:(id:string)=>void;onSelect?:()=>void;
}) {
  const {copy}=ports;
  const stage=office.querySelector<HTMLElement>('.evo-office-stage')!;
  const sceneImg=stage.querySelector<HTMLImageElement>(':scope>img');
  const backdrop=office.querySelector<HTMLImageElement>('.evo-office-backdrop');
  const live=stage.querySelector<HTMLElement>('.evo-office-live')!;
  const objects=stage.querySelector<HTMLElement>('.evo-office-objects')!;
  const shortcuts=dock.querySelector<HTMLElement>('.evo-place-shortcuts')!;
  const bar=document.createElement('nav');bar.className='evo-room-switch';
  bar.setAttribute('aria-label',copy('roomSwitch'));
  bar.innerHTML=`<h2>${esc(copy('rooms'))}<span>${STUDIO_ROOMS.length.toString().padStart(2,'0')}</span></h2><div class="evo-room-list"></div>`;
  const heading=document.createElement('div');heading.className='evo-current-room';
  heading.innerHTML='<strong></strong><span></span>';
  const inspector=document.createElement('div');inspector.className='evo-room-inspector';
  const members=document.createElement('div');members.className='evo-room-members';
  members.setAttribute('role','group');members.setAttribute('aria-label',copy('roomMembers'));
  inspector.append(members);
  const taskButton=document.createElement('button');taskButton.type='button';taskButton.className='evo-room-task';
  taskButton.textContent=copy('roomTask');taskButton.hidden=true;taskButton.onclick=()=>{if(taskId)ports.openTask(taskId);};
  inspector.append(taskButton);
  const podHost=document.createElement('div');podHost.className='evo-office-pod';podHost.hidden=true;stage.append(podHost);
  let active='',selectedMember='',taskId='',known=false,disposed=false;
  let agents:PodAgent[]=[];
  let pod:ReturnType<typeof mountAgentPod>|undefined;
  const buttons=STUDIO_ROOMS.map(room=>{
    const button=document.createElement('button');button.type='button';button.className='evo-room-tab';button.dataset.room=room.id;
    button.innerHTML=`<img src="${room.thumbnail}" width="160" height="120" alt="" decoding="async"><span class="evo-room-tab-copy"><strong>${esc(copy(room.label))}</strong><small></small></span><i class="evo-room-dot" aria-hidden="true"></i>`;
    button.addEventListener('click',()=>select(room.id));bar.querySelector('.evo-room-list')!.append(button);return button;
  });
  function occupants(id:string){return agents.filter(agent=>roomForRole(agent.role)===id);}
  function render(){
    buttons.forEach((button,index)=>{
      const room=STUDIO_ROOMS[index],roster=occupants(room.id),on=active===room.id;
      button.setAttribute('aria-pressed',String(on));button.dataset.active=String(on);
      button.querySelector('small')!.textContent=!room.id?copy('roomResidents'):!known?copy('roomRosterUnknown'):roster.length?`${roster.length} · ${copy('roomMembers')}`:copy('roomUnoccupied');
      button.querySelector<HTMLElement>('i')!.dataset.status=roster.some(a=>a.status==='running')?'running':roster.some(a=>a.status==='pending')?'pending':'idle';
    });
    const room=STUDIO_ROOMS.find(item=>item.id===active)!;
    const roster=occupants(active),main=active==='';
    if(!roster.some(agent=>agent.id===selectedMember))selectedMember=roster[0]?.id||'';
    heading.querySelector('strong')!.textContent=copy(room.label);
    heading.querySelector('span')!.textContent=main?copy('roomResidents'):!known?copy('roomRosterUnknown'):roster.length?`${roster.length} · ${copy('roomMembers')}`:copy('roomUnoccupied');
    // Reconcile by ID so polling/status changes preserve keyboard focus.
    const existing=new Map([...members.querySelectorAll<HTMLButtonElement>('button')].map(button=>[button.dataset.member!,button]));
    roster.forEach((agent,index)=>{
      let button=existing.get(agent.id);
      if(!button){
        button=document.createElement('button');button.type='button';button.dataset.member=agent.id;
        button.onclick=()=>{selectedMember=agent.id;render();};
      }
      button.textContent=agent.role;button.title=agent.objective||agent.role;
      if(members.children[index]!==button)members.insertBefore(button,members.children[index]||null);
      existing.delete(agent.id);
    });
    existing.forEach(button=>button.remove());
    members.hidden=roster.length<2;
    members.querySelectorAll<HTMLButtonElement>('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.member===selectedMember)));
    inspector.hidden=main;taskButton.hidden=main||!selectedMember||!taskId;
    if(sceneImg)sceneImg.hidden=!main;
    live.hidden=!main;objects.hidden=!main;shortcuts.hidden=!main;podHost.hidden=main;
    office.dataset.room=main?'main':'pod';office.dataset.roomId=active||'main';
    if(backdrop&&backdrop.getAttribute('src')!==room.image)backdrop.src=room.image;
    if(main){pod?.dispose();pod=undefined;}
    else {
      pod=pod||mountAgentPod(podHost,{copy,briefHost:inspector,openTask:()=>{if(taskId)ports.openTask(taskId);},motionPaused:()=>live.dataset.paused==='true'});
      pod.update(roster.find(agent=>agent.id===selectedMember)||null,{sprite:3,accent:STUDIO_ROOMS.indexOf(room)-1,theme:active as PodTheme,title:copy(room.label),known});
    }
  }
  function select(id:string){
    if(disposed||!STUDIO_ROOMS.some(room=>room.id===id))return;
    if(active!==id){active=id;selectedMember='';render();}
    ports.onSelect?.();
  }
  const motionObserver=new window.MutationObserver(()=>pod?.refreshMotion());
  motionObserver.observe(live,{attributes:true,attributeFilter:['data-paused']});
  render();
  return {bar,heading,inspector,
    update(next:PodAgent[],nextTaskId:string,rosterKnown=true){
      if(disposed)return;
      if(taskId!==nextTaskId)selectedMember='';
      taskId=nextTaskId;known=rosterKnown;agents=[...new Map(next.map(a=>[a.id,a])).values()];render();
    },
    activeRoom(){return active;},
    dispose(){disposed=true;motionObserver.disconnect();pod?.dispose();bar.remove();heading.remove();inspector.remove();podHost.remove();office.dataset.room='main';},
  };
}
