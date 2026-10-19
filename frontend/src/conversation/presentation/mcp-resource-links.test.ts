// @vitest-environment jsdom
import { expect, it } from 'vitest';

import original from '../../../../tests/fixtures/llm_canvas_original_fork.json';
import { escapeHtmlText } from '../../html-safety';
import { renderMcpResourceLinks } from './mcp-resource-links';
import { buildToolFallbackCard } from './tool-fallback-presentation';
const canvas='https://model.sankuai.com/user/modelManage/experimentalTaskDetail/381153';

it('links a backend-projected canvas label once without changing the route',()=>{
  const row={_mcpLinks:{'381153':canvas}};
  const card=buildToolFallbackCard(document,{title:'Get experiment 381153',resourceRound:row,sections:[]});
  const link=card.querySelector('a')!;
  expect(link.href).toBe(canvas); expect(link.target).toBe('_blank');
  expect(link.rel).toBe('noopener noreferrer');
  const html=renderMcpResourceLinks(escapeHtmlText('381153 / 381153'),row);
  expect((html.match(/<a /g)||[])).toHaveLength(1);
  expect(renderMcpResourceLinks(html,row)).toBe(html);
});

it('escapes hostile labels and URLs and rejects active/credentialed schemes',()=>{
  const label='<img src=x onerror=alert(1)> & "canvas"';
  const html=renderMcpResourceLinks(escapeHtmlText(label),{_mcpLinks:{[label]:canvas}});
  const node=document.createElement('div'); node.innerHTML=html;
  expect(node.querySelector('img')).toBeNull(); expect(node.querySelector('a')?.textContent).toBe(label);
  for(const href of ['javascript:alert(1)','data:text/html,x','https://user:secret@example.test/a','https://example.test/\nfoo',{}])
    expect(renderMcpResourceLinks('canvas',{_mcpLinks:{canvas:href}})).toBe('canvas');
});

it('does not link inside existing anchors or tag attributes',()=>{
  const text='<span title="381153">381153</span><a href="/381153">381153</a>';
  const html=renderMcpResourceLinks(text,{_mcpLinks:{'381153':canvas}});
  const node=document.createElement('div');node.innerHTML=html;
  expect(node.querySelector('span')?.title).toBe('381153');
  expect(node.querySelectorAll('a')).toHaveLength(2);
  expect(node.querySelector('a a')).toBeNull();
});


it('appends source-verified historical links on failure without guessing an ID from the title', () => {
  const row = { toolName: 'mcp__llm__experiment_eval_jobs', status: 'error',
    toolArgs: { experiment_id_or_name_or_url: '381153' }, toolContent: 'experiment has not run yet' };
  const node = document.createElement('div');
  const html = renderMcpResourceLinks(escapeHtmlText('Read experiment'), row);
  node.innerHTML = html;
  expect(node.querySelectorAll('a')).toHaveLength(1);
  expect(node.querySelector('a')?.href).toBe(canvas);
  expect(renderMcpResourceLinks(html, row)).toBe(html);
  expect(renderMcpResourceLinks('experimentId=381153', { ...row, toolArgs: { experiment_id_or_name_or_url: 'named canvas' } }))
    .toBe('experimentId=381153');
});

it('preserves source/new roles and safe labels when fork output is absent from the old title', () => {
  const label = '<img src=x onerror=alert(1)> & "new"';
  const row = { toolName: 'mcp__llm__experiment_fork', status: 'done',
    toolArgs: { base_id: '381153', name: label },
    toolContent: JSON.stringify({ ok: true, data: { experimentId: '381154' } }) };
  const node = document.createElement('div');
  node.innerHTML = renderMcpResourceLinks(escapeHtmlText('Fork canvas'), row);
  expect(node.querySelector('img')).toBeNull();
  expect(node.querySelectorAll('a')).toHaveLength(2);
  expect(node.querySelector('[data-mcp-canvas-role="source"]')?.getAttribute('href')).toBe(canvas);
  expect(node.querySelector('[data-mcp-canvas-role="new"]')?.getAttribute('href')).toBe(canvas.replace('381153', '381154'));
  expect(node.querySelector('[data-mcp-canvas-role="new"]')?.textContent).toBe('new: ' + label);
  const primary = renderMcpResourceLinks('Fork canvas', { ...row, _mcpLinks: { refused: 'javascript:alert(1)' } });
  expect(primary).toBe('Fork canvas');
});

it('renders source and new anchors for the exact original retained-source row', () => {
  const node = document.createElement('div');
  const row = { ...original, toolContent: JSON.stringify(original.toolContent) };
  const html = renderMcpResourceLinks('llm/experiment_fork — 379851', row);
  node.innerHTML = html;
  expect(node.querySelectorAll('a')).toHaveLength(2);
  expect(node.querySelector('[data-mcp-canvas-role="source"]')?.getAttribute('href')).toBe(canvas.replace('381153', '379851'));
  expect(node.querySelector('[data-mcp-canvas-role="new"]')?.getAttribute('href')).toBe(canvas.replace('381153', '381491'));
  for (const link of node.querySelectorAll('a')) {
    expect(link.target).toBe('_blank'); expect(link.rel).toBe('noopener noreferrer');
  }
  expect(renderMcpResourceLinks(html, row)).toBe(html);
});
