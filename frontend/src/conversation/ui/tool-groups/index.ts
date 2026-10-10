/** Compose narrow tool-group owners; scoped contexts expire after each render, weak immutable title indexes expire with projections. */
import { createToolGroupContext, createStandaloneTitleIndex } from './context';
import { createToolInspectorRenderer } from './inspector';
import { renderToolSlot } from './slot';
import { renderToolGroups } from './batch';
import { renderToolPanel } from './panel';
import type { ToolGroupPorts, ToolGroupRound, ToolNarrationSegment, ToolGroupSession } from './contracts';
export { narrationByRound, renderNarration } from './narration';
export function createToolGroupPresentation(ports: ToolGroupPorts) {
  const inspector = createToolInspectorRenderer(ports);
  const standaloneSuffix = createStandaloneTitleIndex();
  const createSession = (allRounds: readonly ToolGroupRound[]): ToolGroupSession => {
    const context = createToolGroupContext(allRounds);
    const slot = (round: ToolGroupRound) => renderToolSlot(round, allRounds, context.suffix(round), ports, inspector);
    return {
      renderSlot: slot,
      renderGroups: (rounds, narration) => renderToolGroups(rounds, allRounds, context, slot, ports, narration),
    };
  };
  return Object.freeze({
    createSession,
    renderInspector: inspector,
    renderSlot: (round: ToolGroupRound, allRounds: readonly ToolGroupRound[]) =>
      renderToolSlot(round, allRounds, standaloneSuffix(round, allRounds), ports, inspector),
    renderPanel: (rounds: readonly ToolGroupRound[], segments?: readonly ToolNarrationSegment[]) =>
      renderToolPanel(rounds, segments, ports, createSession(rounds)),
  });
}
