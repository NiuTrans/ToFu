import { describe, expect, it } from 'vitest';
import { renderProjectReceipt } from './index';
import type { ConversationSearch, ProjectReceiptPorts } from './contracts';
import zh from '../../../i18n/locales/zh.json';
const ports: ProjectReceiptPorts = { text: (key, fallback) => (zh as Record<string, string>)[key] || fallback, markdown: value => value, now: () => 0 };
const receipt: ConversationSearch = { keyword: 'test_opencode_exit_status.py', scope: 'all', returnedCount: 0, limit: 20, coverage: 'ready', items: [] };
function render(search = receipt) {
  return renderProjectReceipt({ toolName: 'list_conversations', roundNum: 1, toolContent: 'No conversations found', results: [{ source: 'Conversations', conversationSearch: search }] }, '', '<span>28 tok</span>', ports);
}
describe('history search receipts', () => {
  it('shows query, scope and returned count without duplicate source or English body', () => {
    const html = render();
    expect(html).toContain('test_opencode_exit_status.py');
    expect(html).toContain('你的其他会话');
    expect(html).toContain('返回 0 条');
    expect(html).toContain('未找到匹配的历史会话');
    expect(html).not.toContain('ptool-convmeta-src');
    expect(html).not.toContain('No conversations found');
    expect(html).not.toContain('scope=');
    expect(html).toContain('28 tok');
  });
  it('does not portray incomplete searches as definitive empty results', () => {
    const html = render({ ...receipt, coverage: 'partial', incomplete: true, scope: 'project', projectPath: '/project' });
    expect(html).toContain('暂未返回匹配结果');
    expect(html).not.toContain('未找到匹配的历史会话');
    expect(html).toContain('scope=&quot;all&quot;');
  });
  it('escapes keywords, project paths, titles and ids', () => {
    const html = render({ ...receipt, keyword: '<img src=x onerror=bad>', scope: 'project', projectPath: '<script>', returnedCount: 1, items: [{ id: '<svg>', title: '<iframe>' }] });
    expect(html).not.toContain('<img'); expect(html).not.toContain('<script>');
    expect(html).not.toContain('<iframe>'); expect(html).not.toContain('<svg>');
  });
  it('keeps historical raw evidence without inventing scope or counts', () => {
    const html = renderProjectReceipt({ toolName: 'list_conversations', toolArgs: { keyword: 'old.py' }, toolContent: 'Error executing list_conversations: offline' }, '', '', ports);
    expect(html).toContain('old.py'); expect(html).toContain('Error executing');
    expect(html).not.toContain('返回 0 条');
  });
});
