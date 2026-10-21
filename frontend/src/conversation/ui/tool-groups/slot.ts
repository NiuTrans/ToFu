/** Rich slot routing and sibling labels; tool bodies and Swarm lifecycle are injected collaborators. */
import { escapeHtml } from '../../../html-safety';

import { programOwnsGateway } from '../../presentation/program-activity';
import { isProgramToolRound, isSwarmToolRound } from '../../presentation/tool-round-presentation';
import { programChildParentCallId } from '../../presentation/tool-execution-groups';
import type { ToolGroupPorts, ToolGroupRound } from './contracts';
export function renderToolSlot(round: ToolGroupRound, context: readonly ToolGroupRound[], suffix: string,
  ports: ToolGroupPorts, inspector: (round: ToolGroupRound) => string): string {

  if (programOwnsGateway(round, context)) return '';
  if (isProgramToolRound(round)) {
    return `<div data-prn="${round.roundNum}" data-prn-kind="program">${ports.renderProgram(round)}</div>`;
  }
  const displayed = suffix ? { ...round, query: String(round.query || '') + suffix } : round;
  const isSwarm = isSwarmToolRound(displayed);
  /* The debug anchor joins the swarm header-right cluster; the slot no longer
   * renders the solo right-aligned row that used to sit under the panel. */
  const swarmAnchor = isSwarm ? inspector(displayed) : '';
  const inner = (isSwarm ? ports.renderSwarm(displayed, context, swarmAnchor) : null)
    ?? ports.renderLine(displayed, displayed.status === 'searching');
  const swarmAttr = isSwarm ? ' data-prn-kind="swarm"' : '';
  const parentId = programChildParentCallId(displayed);
  const parentAttr = parentId ? ` class="ptool-parent-child" data-parent-tool-call-id="${escapeHtml(parentId)}"` : '';
  /* Swarm debug anchor renders inline in the swarm header-right cluster
   * (renderSwarm consumes it via ports) — never a solo right-aligned row
   * under the panel. */
  return `<div data-prn="${displayed.roundNum}"${swarmAttr}${parentAttr}>${inner}</div>`;
}
