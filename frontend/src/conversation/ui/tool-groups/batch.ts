/** Batch chrome, attention and narration adjacency; context selection and body rendering are supplied per pass. */
import { escapeHtml } from '../../../html-safety';
import { computeToolBatches, programChildParentCallId, shouldCollapseToolBatch, summarizeToolAttention,
  toolGroupRoundDisplay, toolGroupRoundNumber, toolGroupRoundTitle } from '../../presentation/tool-execution-groups';
import { isProgramToolRound } from '../../presentation/tool-round-presentation';
import { programBatchLabelParts, summarizeProgramBatch,
  type ProgramBatchSummary } from '../../presentation/program-batch-presentation';

import { programOwnsGateway } from '../../presentation/program-activity';
import { renderNarration } from './narration';
import type { createToolGroupContext } from './context';
import type { ToolGroupRound, ToolNarrationSegment, ToolGroupPorts } from './contracts';
const _turnForkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg>';

function _turnLabelText(size: number, ports: ToolGroupPorts) {
  return ports.text("toolPanel.parallelCalls", { n: size });
}

/* Same counts and reading order as the Surface program-batch head. */
function _programBatchLabelHtml(summary: ProgramBatchSummary, ports: ToolGroupPorts) {
  return programBatchLabelParts(summary).map(part => {
    const text = escapeHtml(ports.text(part.key, { n: part.count }));
    return part.status ? `<span data-program-status="${part.status}">${text}</span>` : `<span>${text}</span>`;
  }).join(' · ');
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
function _renderTurnHead(labelHtml: string, group: unknown, collapsed: boolean, ports: ToolGroupPorts) {
  const rno = toolGroupRoundNumber(group);
  const rnoHtml = rno != null ? `<span class="ptool-turn-rno" title="${escapeHtml(toolGroupRoundTitle(group, ports.text))}">${escapeHtml(toolGroupRoundDisplay(group))}</span>` : "";
  return `<button type="button" class="ptool-turn-head" aria-expanded="${String(!collapsed)}">${rnoHtml}${_turnForkSvg}<span class="ptool-turn-label">${labelHtml}</span><span class="ptool-turn-chev">${ports.icon('chevronDown', 16)}</span></button>`;
}

export function renderToolGroups(rounds: readonly ToolGroupRound[], allRounds: readonly ToolGroupRound[],
  context: ReturnType<typeof createToolGroupContext>, slot: (round: ToolGroupRound, batched: boolean) => string,
  ports: ToolGroupPorts, narrByRound?: ReadonlyMap<string, readonly ToolNarrationSegment[]>): string {
  const localGroups = rounds === allRounds ? context.groups
    : computeToolBatches(rounds.filter(round => !programOwnsGateway(round, allRounds)));
  return localGroups.map(g => {
    const contextGroup = context.groupFor(g);
    const programs = g.rounds.filter(isProgramToolRound);
    const size = g.rounds.length - programs.length;
    // Sibling programs fold under the shared program-batch policy. A round that
    // also made direct calls keeps its cards: the head would not count those.
    const programBatch = g.rounds.every(r => isProgramToolRound(r) || programChildParentCallId(r))
      ? summarizeProgramBatch(programs, undefined, g.rounds) : null;
    const slots = g.rounds.map(round => slot(round, programBatch !== null)).join('');
    const collapsed = programBatch ? programBatch.defaultCollapsed
      : !programs.length && shouldCollapseToolBatch(g.rounds);
    const collapsible = programBatch !== null || (!programs.length && size >= 2);
    const rno = toolGroupRoundNumber(contextGroup);
    const head = programBatch ? _renderTurnHead(_programBatchLabelHtml(programBatch, ports), contextGroup, collapsed, ports)
      : programs.length ? '' : (size >= 2 ? _renderTurnHead(_turnLabelText(size, ports), contextGroup, collapsed, ports)
        : (rno != null ? _renderSoloRoundTag(contextGroup, ports) : ''));
    const attention = programBatch ? programBatch.attention : summarizeToolAttention(g.rounds).dominant;
    const narr = renderNarration(narrByRound?.get(g.key), ports);
    return narr + `<div class="ptool-turn${programs.length ? " ptool-program-turn" : ""}${collapsed ? " collapsed" : ""}"${collapsible ? ' data-collapsible="true"' : ''}${programBatch ? ` data-program-batch="${escapeHtml(String(g.key))}"` : ''} data-attention="${attention}" data-llm-round="${escapeHtml(String(g.key))}" data-batch-size="${size}" data-round-no="${rno != null ? rno : ""}" data-attempt-ordinal="${contextGroup.attemptOrdinal || ""}">${head}${slots}</div>`;
  }).join('');
}
