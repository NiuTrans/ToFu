/** Canonical projection for conversation-level agent interaction modes.
 *
 * The product presents Standard, Delegate, Plan, and Autopilot as one radio
 * choice. The transport keeps one boolean per non-default mode so
 * conversations and headless clients share one representation. This module
 * is the single frontend boundary that resolves them when stale snapshots
 * contain conflicting current modes.
 */

export const AGENT_MODES = [
  'standard', 'delegate', 'plan', 'autopilot',
] as const;

export type AgentMode = typeof AGENT_MODES[number];

export interface AgentModeFlags {
  planMode: boolean;
  autopilotEnabled: boolean;
  delegateMode: boolean;
}

export interface ConversationInteractionSnapshot {
  planMode?: unknown;
  autopilotEnabled?: unknown;
  delegateMode?: unknown;
  activeFlow?: unknown;
}

export interface NormalizedConversationInteraction extends AgentModeFlags {
  agentMode: AgentMode;
  activeFlow: string;
}

export function normalizeAgentMode(value: unknown): AgentMode {
  return AGENT_MODES.includes(value as AgentMode)
    ? value as AgentMode : 'standard';
}

export function agentModeFlags(value: unknown): AgentModeFlags {
  const mode = normalizeAgentMode(value);
  return {
    planMode: mode === 'plan',
    autopilotEnabled: mode === 'autopilot',
    delegateMode: mode === 'delegate',
  };
}

export function resolveAgentMode(
  planMode: unknown,
  autopilotEnabled: unknown,
  delegateMode: unknown = false,
): AgentMode {
  if (planMode === true) return 'plan';
  if (autopilotEnabled === true) return 'autopilot';
  if (delegateMode === true) return 'delegate';
  return 'standard';
}

/** Normalize all loop owners using the backend's established precedence.
 *
 * Plan is the fail-closed authority. Otherwise an explicit orchestration flow
 * wins, then Autopilot, then Delegate (a policy of the standard loop). Valid
 * UI transitions never create a clash; this ordering only heals cross-tab or
 * partially-written snapshots.
 */
export function normalizeConversationInteractionModes(
  snapshot: ConversationInteractionSnapshot,
): NormalizedConversationInteraction {
  const activeFlow = typeof snapshot.activeFlow === 'string'
    ? snapshot.activeFlow.trim() : '';
  let agentMode: AgentMode;
  let normalizedFlow = '';
  if (snapshot.planMode === true) {
    agentMode = 'plan';
  } else if (activeFlow) {
    agentMode = 'standard';
    normalizedFlow = activeFlow;
  } else {
    agentMode = resolveAgentMode(
      false, snapshot.autopilotEnabled, snapshot.delegateMode);
  }
  return {
    agentMode,
    ...agentModeFlags(agentMode),
    activeFlow: normalizedFlow,
  };
}
