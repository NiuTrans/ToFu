import { orchestrationRegistry } from './registry';
type RichCopyWindow = Window & {
  formatOrchestrationRichCopy?: typeof formatOrchestrationRichCopy;
};

const allowedToken = /(<\/?(?:b|i|code)>)/gi;
const escapeCharacter = (character: string): string => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
})[character] ?? character;

/** Format translated copy while allowing only the owned b/i/code tokens. */
export function formatOrchestrationRichCopy(value: unknown): string {
  return String(value == null ? '' : value)
    .split(allowedToken)
    .map((part) => {
      if (/^<i>$/i.test(part)) return '<em>';
      if (/^<\/i>$/i.test(part)) return '</em>';
      if (/^<\/?(?:b|code)>$/i.test(part)) return part.toLowerCase();
      return part.replace(/[&<>"']/g, escapeCharacter);
    })
    .join('');
}

(orchestrationRegistry as unknown as RichCopyWindow).formatOrchestrationRichCopy =
  formatOrchestrationRichCopy;
