/**
 * Owns localized presentation of conversation-shell titles and timestamps.
 * Durable titles remain language-neutral storage values; this module only
 * maps the historical default sentinel and the fixed titles of server-provisioned
 * role conversations at the browser boundary.
 */

/* The titles the server gives the conversations it provisions for a role:
 * ROUTER_TITLE (lib/router/binding.py), WATCHDOG_TITLE
 * (lib/router/watchdog/binding.py) and PROJECT_ROUTER_TITLE
 * (lib/router/forwarding.py, also a human-created coordinator's title).
 * tests/router/test_role_title_sentinels.py keeps both sides equal. */
const ROLE_TITLE_KEYS: ReadonlyMap<string, string> = new Map([
  ['Global Router', 'globalRouter.entry'],
  ['Watchdog', 'routerWatchdog.entry'],
  ['Project coordinator', 'projectCoordination.coordinator'],
]);

export function stripNoTranslateTags(text: unknown): string {
  if (typeof text !== 'string' || !text) return '';
  return text
    .replace(/<\/?notranslate>/gi, '')
    .replace(/<\/?nt>/gi, '')
    .replace(/[⟦\[\(\{【〔《「『]\s*N\s*T\s*_\s*[0-9０-９]+\s*[⟧\]\)\}】〕》」』]/gi, '');
}

export function conversationDisplayTitle(
  title: unknown,
  localizedDefaultTitle: string,
  translate?: (key: string) => string,
): string {
  const clean = stripNoTranslateTags(title);
  const role = translate ? ROLE_TITLE_KEYS.get(clean) : undefined;
  if (role) {
    const label = translate!(role);
    // Before the catalog loads a lookup returns its key; the stored title is better.
    return label && label !== role ? label : clean;
  }
  if (clean !== 'New Chat') return clean;
  return localizedDefaultTitle && localizedDefaultTitle !== 'chat.newConversation'
    ? localizedDefaultTitle : clean;
}

export interface ConversationTimestampLabels {
  readonly date: string;
  readonly time: string;
}

export function conversationTimestampLabels(
  timestamp: unknown,
  nowTimestamp: number,
  language: string,
  todayLabel: string,
  yesterdayLabel: string,
): ConversationTimestampLabels | null {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return null;
  const date = new Date(value);
  const now = new Date(nowTimestamp);
  const pad = (part: number): string => String(part).padStart(2, '0');
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  let dateLabel: string;
  if (date.toDateString() === now.toDateString()) {
    dateLabel = todayLabel;
  } else {
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (date.toDateString() === yesterday.toDateString()) {
      dateLabel = yesterdayLabel;
    } else {
      dateLabel = new Intl.DateTimeFormat(language || undefined, {
        month: 'short',
        day: 'numeric',
        ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
      }).format(date);
    }
  }
  return { date: dateLabel, time };
}
