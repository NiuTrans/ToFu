/** One game window docked over the stage. Panes are persistent DOM, so form
 * drafts, readers and scroll positions survive switching; only the active
 * pane is shown. Opening moves focus to the heading, closing returns it to
 * the opener, or to the fallback when the opener cannot take focus (the page
 * body on a route-opened window, a hidden or detached control). Non-modal: the
 * world stays live beside it.
 * Entry: mountWindows. No transport, timers or task state.
 */
import { esc } from '../components/ui';
import { glyph } from './glyphs';

export type WindowKind = 'journal'|'chat'|'briefing'|'menu'|'actor';
export function mountWindows(host:HTMLElement,ports:{copy:(key:string)=>string;changed:(kind:WindowKind|'')=>void;fallbackFocus:()=>HTMLElement|null}) {
  host.className='evo-window';host.hidden=true;host.setAttribute('role','dialog');host.setAttribute('aria-modal','false');
  host.setAttribute('aria-labelledby','evo-window-title');
  host.innerHTML=`<header class="evo-window-head"><span class="evo-window-icon" aria-hidden="true"></span>
    <h2 id="evo-window-title" tabindex="-1"></h2>
    <button type="button" class="evo-window-close" aria-label="${esc(ports.copy('close'))}" data-tip="${esc(ports.copy('close'))} · Esc">${glyph('close')}</button></header>
    <div class="evo-window-body"></div>`;
  const icon=host.querySelector<HTMLElement>('.evo-window-icon')!,heading=host.querySelector<HTMLElement>('h2')!;
  const body=host.querySelector<HTMLElement>('.evo-window-body')!;
  const panes=new Map<WindowKind,{pane:HTMLElement;icon:string}>();
  let current:WindowKind|''='',opener:HTMLElement|null=null,disposed=false;
  function close(){
    if(!current||disposed)return;
    current='';host.hidden=true;host.dataset.kind='';
    panes.forEach(entry=>{entry.pane.hidden=true;});
    ports.changed('');
    if(opener?.isConnected)opener.focus({preventScroll:true});
    opener=null;
    // Focus never stays inside the hidden window, where it would swallow hotkeys.
    const active=document.activeElement;
    if(!active||active===document.body||host.contains(active))ports.fallbackFocus()?.focus({preventScroll:true});
  }
  function keydown(event:KeyboardEvent){if(event.key==='Escape'&&current){event.stopPropagation();close();}}
  host.querySelector('.evo-window-close')!.addEventListener('click',close);
  host.addEventListener('keydown',keydown);
  return {
    register(kind:WindowKind,pane:HTMLElement,iconName:string){
      pane.classList.add('evo-pane');pane.dataset.pane=kind;pane.hidden=true;body.append(pane);panes.set(kind,{pane,icon:iconName});
    },
    open(kind:WindowKind,title:string,origin?:Element|null){
      const entry=panes.get(kind);if(!entry||disposed)return;
      const focused=(origin as HTMLElement|null)||(document.activeElement as HTMLElement|null);
      if(!current||!host.contains(focused))opener=focused&&!host.contains(focused)?focused:opener;
      panes.forEach((value,key)=>{value.pane.hidden=key!==kind;});
      icon.innerHTML=glyph(entry.icon);heading.textContent=title;
      const changed=current!==kind;current=kind;host.hidden=false;host.dataset.kind=kind;
      if(changed)ports.changed(kind);
      heading.focus({preventScroll:true});
    },
    retitle(title:string){heading.textContent=title;},
    close,
    current(){return current;},
    element:host,
    dispose(){disposed=true;host.removeEventListener('keydown',keydown);panes.clear();host.replaceChildren();},
  };
}
export type Windows = ReturnType<typeof mountWindows>;
