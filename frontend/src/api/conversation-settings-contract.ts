/**
 * Browser view of contracts/conversation_settings_v1.json.
 *
 * Responsibility: expose the declared conversation-settings key sets so browser
 * owners derive them instead of copying literals. Entry points:
 * `CONVERSATION_CACHE_SETTING_KEYS` and `conversationSettingReadAliases`.
 * Dependencies: the JSON authority only.
 */
import contract from '../../../contracts/conversation_settings_v1.json';

interface ConversationSettingDeclaration {
  readonly author: string;
  readonly adopt?: boolean;
  readonly cache?: boolean;
  readonly catalog?: boolean;
  readonly clearWhenAbsent?: boolean;
  readonly readAliases?: readonly string[];
  readonly owners?: readonly string[];
}

const declarations = contract.keys as Readonly<Record<string, ConversationSettingDeclaration>>;

/** Keys the browser metadata cache persists, in contract order. */
export const CONVERSATION_CACHE_SETTING_KEYS: readonly string[] = Object.freeze(
  Object.keys(declarations).filter(key => declarations[key].cache === true));

/** Ordered fallback keys read when `key` itself is empty. */
export function conversationSettingReadAliases(key: string): readonly string[] {
  return declarations[key]?.readAliases ?? [];
}
