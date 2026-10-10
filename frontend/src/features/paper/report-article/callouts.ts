/** Classify blockquotes; more specific takeaway keywords precede broad important keywords. */
const CALLOUT_KEYWORDS = [
  { cls: 'takeaway', re: /^(key takeaway|takeaway|key point|key finding|summary|bottom line|关键结论|核心结论|要点|总结|小结)(?:[:：]|\b)/i },
  { cls: 'warning', re: /^(warning|caution|caveat|limitation|警告|注意|局限|风险)(?:[:：]|\b)/i },
  { cls: 'important', re: /^(important|critical|重要|关键)(?:[:：]|\b)/i },
  { cls: 'tip', re: /^(tip|pro tip|提示|建议)(?:[:：]|\b)/i },
  { cls: 'note', re: /^(note|nb|备注|说明)(?:[:：]|\b)/i },
];
export function decorateCallouts(article: HTMLElement): void {
  let quotes = article.querySelectorAll('blockquote');
  for (let i = 0; i < quotes.length; i++) {
    let bq = quotes[i];
    if (bq.closest('.paper-callout')) continue;
    let lead = (bq.textContent || '').trimStart();
    let match = null;
    for (let k = 0; k < CALLOUT_KEYWORDS.length; k++) {
      if (CALLOUT_KEYWORDS[k].re.test(lead)) { match = CALLOUT_KEYWORDS[k]; break; }
    }
    if (!match) continue;
    bq.classList.add('paper-callout', 'paper-callout-' + match.cls);
  }
}
