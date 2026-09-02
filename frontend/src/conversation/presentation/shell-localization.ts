/**
 * Owns localized presentation of conversation-shell titles and timestamps.
 * Durable titles remain language-neutral storage values; this module only
 * maps the historical default sentinel at the browser boundary.
 */

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
): string {
  const clean = stripNoTranslateTags(title);
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
