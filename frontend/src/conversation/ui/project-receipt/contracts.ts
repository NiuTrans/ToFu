/** Display metadata and injected ports only; backend tools own schemas, authority and write admission. */
export type ProjectText = (key: string, fallback: string) => string;
export interface ProjectReceiptPorts {
  text: ProjectText;
  markdown(content: string): string;
  icon?(name: string, size: number): string | undefined;
  inlineMarkup?(content: string): string | undefined;
  conversationTitle?(conversationId: string): string | undefined;
  now(): number;
}
export interface ProjectReceiptRound {
  toolName?: string; query?: string; roundNum?: number | string;
  toolContent?: unknown; results?: ProjectReceiptMeta[];
}
export interface ProjectReceiptMeta {
  source?: string; snippet?: unknown;
  boardSnapshot?: BoardSnapshot; boardTransition?: BoardTransition;
  peerStatus?: PeerStatus; feedActivity?: FeedActivity;
  peerDelivery?: PeerDelivery; charterProposal?: CharterProposal;
  commitResult?: CommitResult;
}
export interface BoardEpic { owner?: string; dispatched?: unknown; title?: string; id?: string }
export interface BoardSnapshot {
  lanes?: Partial<Record<'open' | 'claimed' | 'done', BoardEpic[]>>;
  open?: number | string; claimed?: number | string; done?: number | string;
}
export interface BoardTransition {
  verb?: string; ok?: unknown; title?: string; taskId?: string; status?: string; error?: string;
}
export interface PeerStatus {
  peers?: { title?: string; convId?: string; agentId?: string; statusLabel?: string;
    round?: number | string; currentFile?: string; claimedEpic?: string }[];
}
export interface CharterProposal { proposal?: string; title?: string }
export interface FeedActivity {
  hiddenSelf?: unknown;
  events?: { kind?: string; title?: string; convId?: string; ts?: unknown; summary?: string }[];
}
export interface PeerDelivery {
  toConv?: string; tool?: string; hardAbort?: unknown; outcome?: string; text?: string;
}
export interface CommitResult {
  mode?: string; ok?: unknown; committed?: string[]; clean?: string[];
  excluded?: { numstat?: string; reason?: string; path?: string }[];
  verified?: unknown; commitSha?: string; error?: string;
}
