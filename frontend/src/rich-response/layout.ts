/** Layout composition over existing keyed block nodes. Layout never owns data. */
import { createTabs, type TabsHandle } from '../ui';
import type { layout as Layout } from './contract.generated';

export function createResponseLayout(doc: Document) {
  const node=doc.createElement('div');node.className='rich-layout';
  let identity='',tabs:TabsHandle|undefined;
  const groups=new Map<string,HTMLElement>();
  function update(layout:Layout|undefined,blocks:Array<{id:string;node:HTMLElement}>) {
    const focused=node.contains(doc.activeElement)?doc.activeElement as HTMLElement:null;
    const next=JSON.stringify(layout ?? null);
    if(identity!==next){
      const active=tabs?.activeId();identity=next;tabs=undefined;groups.clear();
      node.replaceChildren();node.dataset.mode=layout?.mode ?? 'stack';node.dataset.density=layout?.density ?? 'comfortable';
      if(layout){
        const panels=layout.groups.map(group=>{
          const panel=doc.createElement('section');panel.className='rich-layout-group';
          if(layout.mode!=='tabs'){const heading=doc.createElement('h3');heading.textContent=group.title;panel.append(heading);}
          const content=doc.createElement('div');content.className='rich-layout-content';panel.append(content);groups.set(group.id,content);
          return {id:group.id,label:group.title,panel};
        });
        if(layout.mode==='tabs'){
          tabs=createTabs({tabs:panels,activeId:panels.some(p=>p.id===active)?active:undefined},doc);node.append(tabs.el);
        }else panels.forEach(panel=>node.append(panel.panel));
      }
    }
    const destinations=layout?.groups ?? [{id:'',title:'',blocks:blocks.map(block=>block.id)}];
    const byId=new Map(blocks.map(block=>[block.id,block.node]));
    for(const group of destinations){
      const host=layout?groups.get(group.id)!:node;
      group.blocks.forEach((id,index)=>{const child=byId.get(id)!;if(host.children[index]!==child)host.insertBefore(child,host.children[index]??null);});
    }
    if(focused?.isConnected && node.contains(focused) && doc.activeElement!==focused)focused.focus({preventScroll:true});
  }
  return {node,update};
}
