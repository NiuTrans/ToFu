/** Read-only experiment visuals. Uses the shared validated chart presenter;
 * no requests, timers, execution commands or inferred experimental outcomes.
 */
import { parseRichResponse, splitRichMarkdown } from '../../rich-response/validation';
import { renderRichResponse } from '../../rich-response/renderer';
import { projectRichToolResult } from '../../rich-response/tool-result';
import { t, type I18nKey, type I18nParamsFor } from '../../i18n';

type PlainKey = {[K in I18nKey]: I18nParamsFor<K> extends never ? K : never}[I18nKey];
export const chartCopy = (key: string) => t(`evolution.studio.${key}` as PlainKey);
export const richChartText = (key: string, _fallback: string) => t(key as PlainKey);

/** Enrich complete, admitted data-chart blocks only. The exact original remains
 * available beside the projection, including invalid/unsupported/truncated data.
 * Chart schemas cap series and points; never execute supplied markup or code.
 */
export function evidenceCharts(doc: Document, source: string): HTMLElement | null {
  if (source.length > 200_000) return null;
  const host=doc.createElement('section');host.className='evo-evidence-charts';
  const responses=source.includes('tofu-ui') ? splitRichMarkdown(source).flatMap(part=>part.kind==='rich' ? [parseRichResponse(part.source)] : []) : [projectRichToolResult([source])];
  let count=0;
  const supported=new Set(['data_chart','table','timeline','comparison','chart','metric_grid','workflow','notice','resource_panel']);
  for(const response of responses){
    if(!response)continue;
    const blocks=response.blocks.filter(block=>supported.has(block.type)).slice(0,8-count);
    if(!blocks.length)continue;
    const figure=doc.createElement('section');
    const value={...response,blocks};if(blocks.length!==response.blocks.length)delete value.layout;
    renderRichResponse(figure,value,{text:richChartText});
    host.append(figure);count+=blocks.length;if(count>=8)break;
  }
  if(!count)return null;
  const note=doc.createElement('p');note.className='evo-chart-note';note.textContent=chartCopy('resultChartsNote');host.prepend(note);
  return host;
}
