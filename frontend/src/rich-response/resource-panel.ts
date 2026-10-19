/** Interactive capacity explorer over one owned provider snapshot.
 * Search, sort and page choices survive refresh. Unknown is distinct from
 * zero; capacity is not a scheduling promise. Writes remain existing commands.
 */
import { createButton, createChoice, createField, createProgress } from '../ui';
import type { resource_panel, resourceRow, resourceSnapshot } from './contract.generated';
import { resourceHub, type ResourceHub, type ResourceState, type SurfaceContext } from './resource-client';
import { surfaceNode, type SurfaceText } from './surface-blocks';

export function renderResourcePanel(doc: Document, block: resource_panel, ports: {
  context?: SurfaceContext; hub?: ResourceHub; text: SurfaceText;
}) {
  const text = ports.text, node = (tag: string,value='',cls='') => surfaceNode(doc,tag,value,cls);
  const host = node('section','','rich-resource-panel');
  const toolbar=node('div','','ui-toolbar'), status=node('p','','rich-resource-status');
  status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
  const refresh=createButton({label:text('rich.surface.refresh','Refresh'),size:'sm'},doc);
  refresh.dataset.richControl='resource-refresh';
  const live=createChoice({type:'checkbox',label:text('rich.surface.live','Update while visible')},doc);
  live.input.dataset.richControl='resource-live';
  const provenance=node('p','','rich-resource-provenance');
  const source=doc.createElement('details'); source.className='rich-resource-source';
  const sourceBody=node('pre');source.append(node('summary',text('rich.surface.source','Source and freshness')),sourceBody);
  const filters=node('div','','rich-resource-filters');
  const search=createField({label:text('rich.surface.search','Find queue or resource')},doc);
  const searchInput=search.control as HTMLInputElement;searchInput.dataset.richControl='resource-search';
  const kind=createField({label:text('rich.surface.kind','Resource type'),choices:[
    {value:'all',label:text('rich.surface.all','All')},...['gpu','cpu','memory','storage','other'].map(value=>({value,label:text('rich.surface.kind.'+value,value.toUpperCase())}))]},doc);
  const kindInput=kind.control as HTMLSelectElement;kindInput.dataset.richControl='resource-kind';
  const sort=createField({label:text('rich.surface.sort','Sort by'),choices:[
    {value:'queue',label:text('rich.surface.queue','Queue')},
    {value:'available',label:text('rich.surface.available','Available')}]},doc);
  const sortInput=sort.control as HTMLSelectElement;sortInput.dataset.richControl='resource-sort';
  filters.append(search.el,kind.el,sort.el);
  const overview=node('div','','rich-resource-overview');
  const tableWrap=node('div','','rich-table-scroll');tableWrap.tabIndex=0;tableWrap.setAttribute('role','region');tableWrap.setAttribute('aria-label',block.title);
  const table=doc.createElement('table'), head=doc.createElement('thead'), headings=doc.createElement('tr'), body=doc.createElement('tbody');
  const scrollHint=node('p',text('rich.surface.scrollHint','Scroll horizontally to see all capacity columns.'),'rich-resource-scroll-hint');
  const labels=['queue','resource','total','allocated','available','pending'];
  for (const key of labels) {const cell=doc.createElement('th');cell.scope='col';cell.textContent=text('rich.surface.'+key,key);headings.append(cell);}
  head.append(headings);table.append(head,body);tableWrap.append(table);
  const empty=node('p','','rich-resource-empty');
  const pager=node('nav','','ui-toolbar');pager.setAttribute('aria-label',text('rich.surface.pages','Resource pages'));
  const previous=createButton({label:text('rich.surface.previous','Previous'),size:'sm'},doc);
  const next=createButton({label:text('rich.surface.next','Next'),size:'sm'},doc);
  const count=node('span');pager.append(previous,count,next);
  previous.dataset.richControl='resource-previous';next.dataset.richControl='resource-next';
  toolbar.append(refresh,live.el);
  host.append(toolbar,status,provenance,filters,overview,scrollHint,tableWrap,empty,pager,
    node('p',text('rich.surface.capacityNote','Availability is an observation, not a reservation. Scheduling also depends on quota, topology and provider policy.'),'rich-resource-note'),source);
  let snapshot: resourceSnapshot | undefined=block.snapshot, state: ResourceState={phase:'idle'};
  let page=0, disposed=false, serverData=false;
  const pageSize=20; // Presentation window only: every row remains reachable.
  const unit=(row:resourceRow)=>row.unit==='count'&&row.kind==='gpu'?text('rich.surface.gpuUnit','GPUs'):row.unit;
  const number=(value: number|null|undefined,unit='') => value == null ? text('rich.surface.unknown','Unknown')
    : new Intl.NumberFormat(doc.documentElement.lang || 'en',{maximumSignificantDigits:6}).format(value)+(unit ? ' '+unit : '');
  function draw() {
    if (disposed) return;
    const loading=state.phase==='loading';
    refresh.disabled=loading || !ports.context;live.input.disabled=!ports.context;
    host.setAttribute('aria-busy',String(loading));
    host.dataset.state=state.phase;
    status.textContent = loading ? text('rich.surface.loading','Reading provider resources…')
      : state.phase==='error' ? text('rich.surface.readError','Refresh failed. Any values below are the last snapshot, not current capacity. Retry when the provider is available.')
      : !ports.context ? text('rich.surface.noContext','Historical snapshot. Open this view in its task to read current resources.')
      : serverData ? text(live.input.checked?'rich.surface.liveReady':'rich.surface.snapshotReady',live.input.checked?'Updates every 30 seconds while visible.':'Snapshot loaded. Refresh to check current capacity.')
      : text('rich.surface.reported','Reported snapshot; live data has not been read.');
    provenance.textContent=snapshot ? `${block.provider} · ${text('rich.surface.asOf','Fetched at')} ${new Date(snapshot.fetchedAt).toLocaleString(doc.documentElement.lang || 'en')}` : block.provider;
    sourceBody.textContent=JSON.stringify({provider:block.provider,origin:serverData?'provider read':'authored snapshot',...snapshot},null,2);
    const query=searchInput.value.trim().toLocaleLowerCase();
    const rows=(snapshot?.rows ?? []).filter(row=>(kindInput.value==='all'||row.kind===kindInput.value)
      && (!query||`${row.queue} ${row.name}`.toLocaleLowerCase().includes(query))).slice();
    rows.sort((a,b)=>sortInput.value==='available' ? (a.available===null ? (b.available===null?0:1) : b.available===null ? -1 : b.available-a.available)||a.id.localeCompare(b.id)
      : a.queue.localeCompare(b.queue)||a.name.localeCompare(b.name));
    page=Math.min(page,Math.max(0,Math.ceil(rows.length/pageSize)-1));
    const visible=rows.slice(page*pageSize,(page+1)*pageSize);
    body.replaceChildren();
    for (const row of visible) {
      const line=doc.createElement('tr');line.dataset.resourceId=row.id;
      const identity=doc.createElement('th');identity.scope='row';identity.textContent=row.queue;line.append(identity);
      line.append(node('td',`${row.name} · ${row.kind.toUpperCase()}`));
      for (const key of ['total','allocated','available','pending'] as const) line.append(node('td',number(row[key],unit(row))));
      body.append(line);
    }
    tableWrap.hidden=!rows.length;scrollHint.hidden=!rows.length;pager.hidden=rows.length<=pageSize;empty.hidden=Boolean(rows.length);
    empty.textContent=snapshot?.rows.length ? text('rich.surface.noMatch','No resources match these filters.') : text('rich.surface.noData','No resource data is available.');
    previous.disabled=page===0;next.disabled=(page+1)*pageSize>=rows.length;
    count.textContent=`${rows.length ? page*pageSize+1 : 0}–${Math.min((page+1)*pageSize,rows.length)} / ${rows.length}`;
    overview.replaceChildren();
    // An overview follows the selected resource type; different devices or units
    // are never added together into an invented global GPU count.
    for (const row of visible.filter(row=>row.kind==='gpu').slice(0,3)) {
      const card=node('section','','rich-resource-capacity');
      card.append(node('h4',row.name),node('strong',number(row.available,unit(row))),node('p',row.queue));
      if (row.total!==null && row.total>0 && row.allocated!==null) {
        card.append(createProgress({value:Math.min(100,row.allocated/row.total*100),label:`${row.queue} ${row.name}`}).el);
      }
      card.append(node('p',`${text('rich.surface.allocated','Allocated')}: ${number(row.allocated)} / ${number(row.total)}`));
      overview.append(card);
    }
  }
  const subscription=ports.context ? (ports.hub ?? resourceHub(doc)).subscribe(ports.context,block.provider,value=>{
    state=value;if(value.snapshot){snapshot=value.snapshot;serverData=true;}draw();
  },block.connection,block.queue) : undefined;
  const filter=()=>{page=0;draw();};
  const refreshClick=()=>subscription?.refresh();
  const liveChange=()=>{subscription?.live(live.input.checked);draw();};
  const prevClick=()=>{page--;draw();},nextClick=()=>{page++;draw();};
  searchInput.addEventListener('input',filter);kindInput.addEventListener('change',filter);sortInput.addEventListener('change',filter);
  refresh.addEventListener('click',refreshClick);live.input.addEventListener('change',liveChange);
  previous.addEventListener('click',prevClick);next.addEventListener('click',nextClick);
  const Observer=doc.defaultView?.IntersectionObserver;
  const observer=Observer && subscription ? new Observer(entries=>subscription?.active(entries.some(entry=>entry.isIntersecting))) : undefined;
  if(observer) observer.observe(host);
  else queueMicrotask(()=>{if(!disposed)subscription?.active(host.isConnected && !host.closest('[hidden]'));});
  draw();
  return {node:host,update(value:resource_panel){
    block=value;if(!serverData)snapshot=value.snapshot;draw();
  },dispose(){
    disposed=true;observer?.disconnect();subscription?.dispose();
    searchInput.removeEventListener('input',filter);kindInput.removeEventListener('change',filter);sortInput.removeEventListener('change',filter);
    refresh.removeEventListener('click',refreshClick);live.input.removeEventListener('change',liveChange);
    previous.removeEventListener('click',prevClick);next.removeEventListener('click',nextClick);
  }};
}
