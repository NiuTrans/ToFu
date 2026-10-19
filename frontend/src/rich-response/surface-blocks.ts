/** Pure data presenters for metrics, explanatory notices and task progress.
 * Native markup owns semantics and SVG; model strings are always inert text.
 */
import { createProgress } from '../ui';
import type { metric_grid, workflow, notice } from './contract.generated';
export type SurfaceText = (key: string, fallback: string) => string;
export const surfaceNode = (doc: Document, tag: string, text = '', className = '') => {
  const element = doc.createElement(tag); element.textContent = text; element.className = className; return element;
};
export function renderSurfaceBlock(doc: Document, block: metric_grid | workflow | notice, text: SurfaceText): HTMLElement {
  const node = (tag: string, value = '', cls = '') => surfaceNode(doc,tag,value,cls);
  const number = (value: number) => new Intl.NumberFormat(doc.documentElement.lang || 'en', {maximumSignificantDigits:6}).format(value);
  if (block.type === 'notice') {
    const host = node('aside', '', 'rich-notice'); host.dataset.tone=block.tone;
    host.append(node('p',block.message,'rich-notice-message'));
    if (block.detail) host.append(node('p',block.detail)); return host;
  }
  if (block.type === 'workflow') {
    const host = node('div'), list = doc.createElement('ol'); list.className='rich-workflow';
    for (const step of block.steps) {
      const row = doc.createElement('li'); row.dataset.state=step.state;
      if (step.state === 'running') row.setAttribute('aria-current','step');
      const status = node('span',text('rich.surface.step.'+step.state,step.state),'rich-step-status');
      row.append(status,node('strong',step.label));
      if (step.detail) row.append(node('p',step.detail)); list.append(row);
    }
    host.append(list); if (block.next) host.append(node('p',block.next,'rich-next-step')); return host;
  }
  const host = node('div','','rich-indicator-grid');
  for (const item of block.items) {
    const card = node('section','','rich-indicator'); card.dataset.tone=item.tone ?? 'neutral';
    card.append(node('h3',item.label),node('strong',item.value === null ? text('rich.surface.unknown','Unknown') : `${number(item.value)} ${item.unit}`,'rich-indicator-value'));
    if (item.description) card.append(node('p',item.description));
    if (item.maximum !== undefined && item.value !== null) {
      const progress = createProgress({value:Math.max(0,Math.min(100,item.value/item.maximum*100)),label:item.label});
      card.append(progress.el,node('p',`${number(item.value)} / ${number(item.maximum)} ${item.unit}`));
    }
    if (item.history?.length) {
      const svg = doc.createElementNS('http://www.w3.org/2000/svg','svg');
      svg.setAttribute('viewBox','0 0 200 40'); svg.classList.add('rich-sparkline'); svg.setAttribute('aria-hidden','true');
      let min=Infinity,max=-Infinity;for(const value of item.history){min=Math.min(min,value);max=Math.max(max,value);}
      const scale=Math.max(Math.abs(min),Math.abs(max))||1, low=min/scale, span=max/scale-low || 1;
      const path=doc.createElementNS(svg.namespaceURI,'polyline');
      path.setAttribute('points',item.history.map((value,index)=>`${item.history!.length===1?100:index/(item.history!.length-1)*200},${36-(value/scale-low)/span*32}`).join(' '));
      svg.append(path);card.append(svg);
      const values=doc.createElement('details');values.append(node('summary',text('rich.surface.values','Source values')),node('p',item.history.map(number).join(', ')));card.append(values);
    }
    host.append(card);
  }
  return host;
}
