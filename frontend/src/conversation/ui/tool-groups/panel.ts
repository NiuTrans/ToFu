/** Settled/live panel count, attention and bounded historical tail; no DOM or transport work. */
import { escapeHtml } from '../../../html-safety';
import { presentToolExecutionPanel } from '../../presentation/tool-execution-groups';
import { narrationByRound } from './narration';
import type { ToolGroupRound, ToolNarrationSegment, ToolGroupPorts, ToolGroupSession } from './contracts';
export function renderToolPanel(allRounds: readonly ToolGroupRound[], segments: readonly ToolNarrationSegment[] | undefined, ports: ToolGroupPorts, session: ToolGroupSession): string {
  const anyActive = allRounds.some((r) => r.status === "searching" || r._swarmActive);
  /* The header counts REAL tool rounds only — never the display-only
   *   inject chips (swarm-inbox / peer / steer / stall-nudge) that ride the
   *   panel for chronological context. They are not calls the model made, and
   *   counting them lied twice over: a hydrated turn reported N+chips tools,
   *   and a TRIMMED turn (toolRounds stripped for transport, only the
   *   `_stallNudges` sidecar survived) rendered its lone chip as "使用了 1 个
   *   工具" (conv msg0cop6qf64ee — 32 real rounds hidden behind the trim).
   *   Parity with the segment-timeline path (renderSegmentTimelineHTML),
   *   which already counts realRounds only. When ONLY chips remain the count
   *   header is omitted entirely — "0 tools used" is not a useful claim; the
   *   trimmed-activity affordance below the panel carries the real count. */
  const realRounds = allRounds.filter((r) => r && !r._inboxInject
    && !r._peerInject && !r._userSteerInject && !r._bgCommandInject && !r._stallNudge
    && !r._programSynthetic);
  const count = allRounds.length;
  const panel = presentToolExecutionPanel(realRounds, allRounds, anyActive, ports.text, escapeHtml, ports.icon('chevronDown', 16));
  const headerHtml = realRounds.length ? panel.html : "";
  /* Per-round narration (translated-in-place) for the SETTLED grouped panel.
   * Empty map when no segments passed (streaming sync / branch / paper-reader
   * / upload callers) → byte-identical to the pre-fix grouped render. */
  const narrByRound = narrationByRound(segments);
  const STATIC_LIMIT = 100;
  let lines, truncHtml = "";
  if (!anyActive && count > STATIC_LIMIT) {
    const tail = allRounds.slice(-50);
    lines = session.renderGroups(tail, narrByRound);
    const hiddenN = count - 50;
    truncHtml = `<div class="ptool-truncated" data-hidden-count="${hiddenN}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${escapeHtml(ports.text("toolPanel.hidden", { n: hiddenN }))}</span></div>`;
  } else {
    lines = session.renderGroups(allRounds, narrByRound);
  }
  return `<div class="ptool-panel${panel.active ? " ptool-panel-active" : ""}${panel.collapsed ? " collapsed" : ""}"${realRounds.length && !panel.active ? ' data-collapsible="true"' : ''} data-attention="${panel.attention}">
       ${headerHtml}
       <div class="ptool-panel-body" data-full-count="${realRounds.length}">${truncHtml}${lines}</div>
     </div>`;
}
