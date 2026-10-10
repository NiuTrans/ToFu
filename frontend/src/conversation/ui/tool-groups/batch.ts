/** Batch chrome, attention and narration adjacency; context selection and body rendering are supplied per pass. */
import { escapeHtml } from '../../../html-safety';
import { computeToolBatches, shouldCollapseToolBatch, summarizeToolAttention,
  toolGroupRoundDisplay, toolGroupRoundNumber, toolGroupRoundTitle } from '../../presentation/tool-execution-groups';
import { isProgramToolRound } from '../../presentation/tool-round-presentation';
import { renderNarration } from './narration';
import type { createToolGroupContext } from './context';
import type { ToolGroupRound, ToolNarrationSegment, ToolGroupPorts } from './contracts';
const _turnForkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg>';

function _turnLabelText(size: number, ports: ToolGroupPorts) {
  return ports.text("toolPanel.parallelCalls", { n: size });
}

/* Standalone round tag for a SOLO turn (one tool call). Solo groups are
 * display:contents (no box), so this renders as a thin label line above
 * the single tool row. */
function _renderSoloRoundTag(group: unknown, ports: ToolGroupPorts) {
  return `<div class="ptool-turn-rno ptool-turn-rno-solo" title="${escapeHtml(toolGroupRoundTitle(group, ports.text))}">${escapeHtml(toolGroupRoundDisplay(group))}</div>`;
}

/* The collapsible header shown atop a multi-call .ptool-turn container.
 * Shared verbatim by the static renderer and the streaming sync path so
 * a finalize swap is seamless. `rno` (optional) prefixes the round number
 * so the header reads e.g. `第3轮 · 2 parallel calls`. */
function _renderTurnHead(size: number, group: unknown, collapsed: boolean, ports: ToolGroupPorts) {
  const rno = toolGroupRoundNumber(group);
  const rnoHtml = rno != null ? `<span class="ptool-turn-rno" title="${escapeHtml(toolGroupRoundTitle(group, ports.text))}">${escapeHtml(toolGroupRoundDisplay(group))}</span>` : "";
  return `<button type="button" class="ptool-turn-head" aria-expanded="${String(!collapsed)}">${rnoHtml}${_turnForkSvg}<span class="ptool-turn-label">${_turnLabelText(size, ports)}</span><span class="ptool-turn-chev">${ports.icon('chevronDown', 16)}</span></button>`;
}

export function renderToolGroups(rounds: readonly ToolGroupRound[], allRounds: readonly ToolGroupRound[],
  context: ReturnType<typeof createToolGroupContext>, slot: (round: ToolGroupRound) => string,
  ports: ToolGroupPorts, narrByRound?: ReadonlyMap<string, readonly ToolNarrationSegment[]>): string {
  const localGroups = rounds === allRounds ? context.groups : computeToolBatches(rounds);
  return localGroups.map(g => {
    const contextGroup = context.groupFor(g);
    const slots = g.rounds.map(slot).join('');
    const programs = g.rounds.filter(isProgramToolRound);
    const size = g.rounds.length - programs.length;
    const collapsed = !programs.length && shouldCollapseToolBatch(g.rounds);
    const rno = toolGroupRoundNumber(contextGroup);
    const head = programs.length ? '' : (size >= 2 ? _renderTurnHead(size, contextGroup, collapsed, ports)
      : (rno != null ? _renderSoloRoundTag(contextGroup, ports) : ''));
    const narr = renderNarration(narrByRound?.get(g.key), ports);
    return narr + `<div class="ptool-turn${programs.length ? " ptool-program-turn" : ""}${collapsed ? " collapsed" : ""}"${!programs.length && size >= 2 ? ' data-collapsible="true"' : ''} data-attention="${summarizeToolAttention(g.rounds).dominant}" data-llm-round="${escapeHtml(String(g.key))}" data-batch-size="${size}" data-round-no="${rno != null ? rno : ""}" data-attempt-ordinal="${contextGroup.attemptOrdinal || ""}">${head}${slots}</div>`;
  }).join('');
}
