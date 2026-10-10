/** Timer receipt display metadata; recovery and scheduler authority remain external. Entry points below; dependencies are explicit imports and ports. */

export interface TimerTrace { name?: string; isError?: unknown; elapsed?: unknown; argsBrief?: string }
export interface TimerPoll {
  decision?: string; pollNum?: string | number; reason?: unknown; ts?: string | number;
  tokensUsed?: unknown; parseError?: unknown; rawContent?: unknown; model?: string;
  pollId?: string; toolTrace?: TimerTrace[]; cmdOutput?: string;
}
export interface TimerRecovery { polls: TimerPoll[]; triggered: boolean }
export interface TimerRound {
  results?: { timerReceipt?: TimerCreationReceipt }[];
  _timerBackground?: boolean;
  roundNum?: string | number; status?: string; _timerTimerId?: string; _timerConditionKind?: string;
  _timerCheckInstruction?: string; _timerConditionCommand?: string; _timerCheckCommand?: string;
  _timerPollInterval?: number | string; _timerMaxPolls?: number | string; _timerNextPollTs?: unknown;
  _timerOrphaned?: unknown; _timerSkipCount?: number; _timerLastSkipTs?: string | number;
  _timerLastSkipPollNum?: string | number;
}
export interface TimerCreationReceipt {
  timerId: string; background: boolean; checkInstruction: string;
  conditionCommand: string; checkCommand: string; conditionKind: string;
  pollInterval: number; maxPolls: number;
}
export type TimerText = (key: string, fallback: string) => string;
export interface TimerClockPorts { text: TimerText; now(): number }
export interface TimerCardPorts extends TimerClockPorts {
  icon(name: string, size?: number): string;
  markdown?(content: string): string;
  toolIcon?(name: string): string | undefined;
  formatTime(timestamp: number | string): string;
  demandCountdown?(): void;
}
export const TIMER_CARD_POLICY = Object.freeze({ visiblePolls: 5, reasonPreviewChars: 120,
  commandPreviewChars: 300, longInstructionChars: 160 });
