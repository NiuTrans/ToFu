/** Inspector routing and escaped anchor HTML; bounded content-free diagnostics, no requests or DOM. */
import { escapeHtml } from '../../../html-safety';
import { inspectorReference } from '../../../features/inspector-evidence';
import type { ToolGroupRound, ToolGroupPorts } from './contracts';
const _RI_TOOL_ANCHOR_SVG =
  '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="m8 6-6 6 6 6"/><path d="m16 6 6 6-6 6"/></svg>';
const INSPECTOR_DIAGNOSTIC_KEY_LIMIT = 500;
export function createToolInspectorRenderer(ports: ToolGroupPorts) {
  const _riDebugEntryLogged = new Set<string>();
  return function renderDebugEntry(r: ToolGroupRound): string {
    if (!ports.debugEnabled()) return '';
    if (!r || r._inboxInject || r._peerInject || r._userSteerInject || r._bgCommandInject || r._stallNudge || r._programSynthetic) return '';
    const baseTaskId = r._taskId || ports.taskIdForRound(r);
    const ref = inspectorReference(r, baseTaskId);
    if (!ref) return '';
    const { taskId, roundNum: round, turn } = ref;
    const lr = r.llmRound;
    const agentId = r.agentId || '';
    const riKey = taskId + ':' + round;
    if (!_riDebugEntryLogged.has(riKey)) {
      if (_riDebugEntryLogged.size > INSPECTOR_DIAGNOSTIC_KEY_LIMIT) _riDebugEntryLogged.clear();
      _riDebugEntryLogged.add(riKey);
      ports.debugLog('[ri] badge ' + riKey + ' (base=' + baseTaskId + ', agent=' +
          (agentId || '-') + ', llmRound=' + lr + ')');
    }
    const tip = ports.text('ri.toolAnchorTip', { round });
    const esc = escapeHtml(String(taskId));
    return `<button type="button" class="ri-tool-anchor" ` +
      `data-ri-state="${esc}:${round}" ` +
      `data-ri-turn="${escapeHtml(turn)}" ` +
      `title="${escapeHtml(tip)}" ` +
      `data-tofu-action="openToolDebugPanel('${esc}',${round},this)">` +
      `${_RI_TOOL_ANCHOR_SVG}<span class="ri-tool-anchor-label">R${round}</span></button>`;
  };
}
