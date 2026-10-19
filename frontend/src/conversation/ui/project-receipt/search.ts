/** Same-query search receipt; no prose-derived success or network reads. */
import { escapeHtmlText as esc } from '../../../html-safety';
import type { ConversationSearch, ProjectReceiptPorts, ProjectReceiptRound, ProjectText } from './contracts';

function requestKeyword(round: ProjectReceiptRound): string {
  try {
    const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
    if (args && typeof args === 'object' && 'keyword' in args) return String(args.keyword || '').trim();
  } catch { /* Historical arguments can be unavailable. */ }
  return (round.query || '').match(/^List conversations matching "([\s\S]*)"$/)?.[1] || '';
}
export function conversationSearchLabel(round: ProjectReceiptRound, text: ProjectText): string {
  const receipt = round.results?.[0]?.conversationSearch;
  const keyword = receipt?.keyword ?? requestKeyword(round);
  const parts = [text(keyword ? 'convSearch.search' : 'convSearch.list', keyword ? 'Search history' : 'List history'), keyword ? `“${keyword.slice(0, 4096)}”` : ''];
  if (receipt) {
    parts.push(text(receipt.scope === 'project' ? 'convSearch.project' : 'convSearch.all', receipt.scope === 'project' ? 'This project' : 'Your other conversations'));
    parts.push(text('convSearch.returned', '{n} returned').replace('{n}', String(receipt.returnedCount)));
  }
  return parts.filter(Boolean).join(' · ');
}
export function renderConversationSearch(receipt: ConversationSearch, ports: ProjectReceiptPorts): string {
  const text = ports.text;
  const incomplete = receipt.incomplete || receipt.coverage !== 'ready';
  const scope = text(receipt.scope === 'project' ? 'convSearch.project' : 'convSearch.all', receipt.scope === 'project' ? 'This project' : 'Your other conversations');
  const lines = [scope + (receipt.projectPath ? ` · ${receipt.projectPath.slice(0, 4096)}` : ''), text('convSearch.fields', 'Searches titles and indexed public text; excludes this conversation. Tool arguments/results are not indexed.')];
  if (receipt.returnedCount === 0) {
    lines.push(text(incomplete ? 'convSearch.noReturned' : 'convSearch.noMatch', incomplete ? 'No matches returned yet; search coverage is incomplete or unknown.' : 'No matching conversations found.'));
    lines.push(text('convSearch.retry', 'Try a shorter or different keyword.'));
    if (receipt.scope === 'project') lines.push(text('convSearch.widen', 'To search your other projects, explicitly use scope="all".'));
  } else if (incomplete) lines.push(text('convSearch.partial', 'Search coverage is incomplete or unknown; these results are not exhaustive.'));
  const caption = lines.map(line => `<div class="ptool-convmeta-why">${esc(line)}</div>`).join('');
  const items = receipt.items.slice(0, 50).map(item => `<li>${esc(item.title || text('convSearch.untitled', '(Untitled)'))} <code>${esc(item.id)}</code></li>`).join('');
  return `${caption}${items ? `<ul>${items}</ul>` : ''}`;
}
