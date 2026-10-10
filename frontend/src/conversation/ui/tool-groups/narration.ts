/** Translated narration selection and keyed HTML; attempt grouping comes from the pure grouping owner. */
import { escapeHtml } from '../../../html-safety';
import { computeExecutionBatches, toolExecutionLlmRound } from '../../presentation/tool-execution-groups';
import type { ToolNarrationSegment, ToolGroupPorts } from './contracts';
export function narrationByRound(segments?: readonly ToolNarrationSegment[]) {
  const result = new Map<string, ToolNarrationSegment[]>();
  if (!Array.isArray(segments)) return result;
  const hasLlm = segments.some(segment => toolExecutionLlmRound(segment) != null);
  for (const group of computeExecutionBatches(segments, hasLlm)) {
    for (const segment of group.items) {
      if (!segment || segment.type !== 'text' || segment.deliverable) continue;
      if (!(segment.text || '').trim() && !(segment.translatedText || '').trim()) continue;
      const rows = result.get(group.key) || [];
      rows.push(segment); result.set(group.key, rows);
    }
  }
  return result;
}
export function renderNarration(segments: readonly ToolNarrationSegment[] | undefined, ports: ToolGroupPorts): string {
  let html = '';
  for (const segment of segments || []) {
    const content = segment.translatedText?.trim() ? segment.translatedText : segment.text;
    if (!content?.trim()) continue;
    const roundKey = segment.llmRound != null ? ` data-seg-round="L${escapeHtml(String(segment.llmRound))}"` : '';
    html += `<div class="md-content seg-narration"${roundKey}>${ports.markdown(ports.cleanTranslation(content))}</div>`;
  }
  return html;
}
