/** Tool-group display inputs and ports; Sync state, transport and DOM remain outside this owner. */
export interface ToolGroupRound {
  readonly roundNum?: number | string; readonly toolCallId?: number | string;
  readonly toolName?: string; readonly query?: string; readonly status?: string;
  readonly llmRound?: number | string; readonly agentId?: string; readonly _taskId?: string;
  readonly attemptId?: string; readonly _attemptId?: string; readonly taskId?: string;
  readonly inspectorRef?: { readonly taskId: string; readonly roundNum: number; readonly turn: string } | null;
  readonly parentToolCallId?: string; readonly _programCallId?: string;
  readonly _artifactOrigin?: { readonly sourceToolCallId?: string };
  readonly _swarm?: boolean; readonly _swarmAgents?: readonly unknown[];
  readonly _swarmSnapshot?: { readonly agents?: readonly unknown[] }; readonly results?: readonly unknown[];
  readonly _inboxInject?: boolean; readonly _peerInject?: boolean;
  readonly _userSteerInject?: boolean; readonly _bgCommandInject?: boolean;
  readonly _stallNudge?: boolean; readonly _programSynthetic?: boolean; readonly _swarmActive?: boolean;
}
export interface ToolNarrationSegment {
  readonly type?: string; readonly deliverable?: boolean; readonly text?: string;
  readonly translatedText?: string; readonly llmRound?: number | string;
  readonly attemptId?: string; readonly _attemptId?: string; readonly taskId?: string; readonly _taskId?: string;
  readonly _round?: ToolGroupRound;
}
export type ToolGroupText = (key: string, values?: Readonly<Record<string, number | string>>) => string;
export interface ToolGroupPorts {
  text: ToolGroupText;
  icon(name: string, size: number): string;
  markdown(content: string): string;
  cleanTranslation(content: string): string;
  renderLine(round: ToolGroupRound, searching: boolean): string;
  renderSwarm(round: ToolGroupRound, allRounds: readonly ToolGroupRound[], inspectorAnchorHtml?: string): string | null;
  renderProgram(round: ToolGroupRound): string;
  debugEnabled(): boolean;
  taskIdForRound(round: ToolGroupRound): string;
  debugLog(message: string): void;
}
export interface ToolGroupSession {
  renderSlot(round: ToolGroupRound): string;
  renderGroups(rounds: readonly ToolGroupRound[], narration?: ReadonlyMap<string, readonly ToolNarrationSegment[]>): string;
}
