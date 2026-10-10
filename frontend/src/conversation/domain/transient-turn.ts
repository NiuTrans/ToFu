/**
 * Browser-lifecycle state attached only to transient Turn overlays.
 *
 * This state is deliberately outside TurnProjection: it is presentation
 * telemetry, never a durable conversation fact and never part of model
 * context.  The intersection remains assignable to TurnRecord so the normal
 * TurnStore selector and keyed ConversationSurface can render it.
 */
import type {
  TurnModelRoute,
  TurnRecord,
} from '../../api/conversation-sync.generated';

export interface TransientTurnPresentation {
  kind: 'attempt' | 'preparation' | 'autopilot-virtual-user' | 'image-generation';
  phase: string;
  seq?: number;
  label: string;
  detail: string;
  detailKey?: string;
  detailArgs?: Readonly<Record<string, string | number>>;
  tools?: ReadonlyArray<string>;
  toolContext?: string;
  toolContextTools?: ReadonlyArray<string>;
  attempt?: number;
  statusCode?: number;
  model?: string;
  modelRoute?: TurnModelRoute;
  thinkingLength?: number;
}

export type TransientTurnRecord = TurnRecord & {
  transientPresentation?: TransientTurnPresentation;
};

export function transientTurnPresentation(
  turn: TurnRecord,
): TransientTurnPresentation | null {
  return (turn as TransientTurnRecord).transientPresentation ?? null;
}
