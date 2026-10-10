/**
 * Explicit Markdown, localization, rich-markup and lazy-result ports. Types
 * only; composition injects every effect.
 * Entry points: ClassicConversationMarkdownOptions,
 * ClassicConversationRendererPorts.
 * Dependencies: conversation-view-model, lazy-tool-result.
 */
import {
  type ConversationBlockViewModel,
  type ConversationTurnAction,
  type ConversationTurnActionViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import type { LazyToolResultLoader } from '../lazy-tool-result';

export interface ClassicConversationMarkdownOptions {
  /** Human-authored input is plain text: neutralize raw '<tag>' HTML so it
   * renders literally instead of being swallowed as an unknown element. */
  escapeRawHtml?: boolean;
  /** Streaming hot path: the renderer may defer expensive flourishes
   * (syntax highlighting, KaTeX) until the terminal re-render. */
  streaming?: boolean;
}

export interface ClassicConversationRendererPorts {
  loadToolResult?: LazyToolResultLoader;
  renderSafeMarkdownHtml(
    markdown: string,
    options?: ClassicConversationMarkdownOptions,
  ): string;
  /** Transitional trusted mascot markup from the retained branding module. */
  renderTurnAvatarHtml?(turn: ConversationTurnViewModel): string;
  /**
   * Full-fidelity context fragments. The fold and rail intentionally have
   * different DOM homes; both strings must already be sanitized.
   */
  renderTurnContextParts?(
    block: Extract<ConversationBlockViewModel, { kind: 'context' }>,
  ): { fold: string; rail: string } | null;
  /** Transitional rich tool/program-timeline port; output is sanitized. */
  renderToolBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'tool' | 'program' }>,
    turn: ConversationTurnViewModel,
  ): string;
  /** Transitional injection-row port; output must already be sanitized. */
  renderInjectionBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'injections' }>,
    turn: ConversationTurnViewModel,
  ): string;
  /** Transitional full-fidelity finish bar; output must already be sanitized. */
  renderTurnFooterHtml?(turn: ConversationTurnViewModel): string;
  /** Transitional provenance strip; output must already be sanitized. */
  renderProvenanceBlockHtml?(
    block: Extract<ConversationBlockViewModel, { kind: 'provenance' }>,
    turn: ConversationTurnViewModel,
  ): string;
  /** Escaped acquisition metadata placed INSIDE a context note disclosure. */
  renderContextProvenanceHtml?(value: unknown): string;
  roleLabel?(turn: ConversationTurnViewModel): string;
  actionLabel?(
    action: ConversationTurnAction,
    turn: ConversationTurnViewModel,
    actionView?: ConversationTurnActionViewModel,
  ): string;
  formatTimestamp?(timestamp: string | number): string;
  resolveMediaUrl?(url: string): string;
  localizedText?(
    key: string,
    fallback: string,
    values?: Readonly<Record<string, string | number>>,
  ): string;
  /** Lazy full stop report for one concluded goal run (the fold record only
   * carries the bounded excerpt; the full text rides the durable GoalRun
   * transition). Resolve null when the report is unavailable. */
  fetchAutopilotRunReport?(
    runId: string,
  ): Promise<{ report?: string } | null>;
}
