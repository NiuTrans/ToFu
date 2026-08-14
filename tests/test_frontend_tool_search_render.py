"""Tool Search renders the exact matched native tools as readable cards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _node_deps_available() -> bool:
    return bool(shutil.which('node')) and os.path.isdir(
        os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[1];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!doctype html><body></body>', {url:'http://localhost/'});
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (key, params) => {
  const en = {
    'toolSearch.found': '{total} candidate matches · showing {shown}',
    'toolSearch.none': 'No matching tools',
    'toolSearch.more': 'more candidates available',
    'toolSearch.failOpen': 'full catalog restored',
  };
  let value = en[key] || key;
  if (params && typeof params === 'object') {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replaceAll('{' + name + '}', String(replacement));
    }
  }
  return value;
};
global.renderMarkdown = (s) => String(s);
global.Icon = () => '';
global.projectState = {extraRoots: []};
global._featureFlags = {debug_mode: false};

eval(fs.readFileSync(process.argv[2], 'utf8'));

const round = {
  status: 'done', toolName: 'search_tools', query: '编辑学城文档',
  toolSearchTotal: 73, toolSearchNextCursor: 'next',
  results: [
    {type:'tool_catalog_match', toolName:'mcp__xuecheng__prepare_doc_edit',
     namespace:'xuecheng', snippet:'Prepare a document edit.',
     arguments:[{name:'doc_id', type:'string', required:true}]},
    {type:'tool_catalog_match', toolName:'mcp__xuecheng__update_doc',
     namespace:'xuecheng', snippet:'Update <the> document.',
     arguments:[{name:'content', type:'string', required:true},
                {name:'confirm', type:'boolean', required:false}]},
  ],
};
const html = _renderUnifiedToolLine(round, false);
const empty = _renderUnifiedToolLine({
  status:'done', toolName:'search_tools', query:'nothing',
  toolSearchTotal:0, results:[]
}, false);
const gatewayOnly = renderToolRoundsHTML([{
  roundNum:1, llmRound:0, status:'done', toolName:'execute_tools',
  query:'execute_tools', toolContent:'{"status":"ok"}', results:[]
}], false);
const childOnly = renderToolRoundsHTML([
  {roundNum:1, llmRound:0, status:'done', toolName:'execute_tools',
   query:'execute_tools', toolContent:'{"status":"ok"}', results:[]},
  {roundNum:8700000, llmRound:0, status:'error', toolName:'read_files',
   query:'read_files', toolContent:'failed',
   results:[{toolName:'read_files', title:'read_files', badge:'error'}]},
], false);
const checks = {
  card: html.includes('ptool-tool-search-block'),
  query: html.includes('编辑学城文档'),
  count: html.includes('73 candidate matches · showing 2') &&
         !html.includes('73 tools found'),
  prepare: html.includes('mcp__xuecheng__prepare_doc_edit'),
  update: html.includes('mcp__xuecheng__update_doc'),
  namespace: html.includes('xuecheng'),
  args: html.includes('doc_id') && html.includes('content') && html.includes('confirm'),
  required: html.includes('ptool-tool-arg-required'),
  escaped: html.includes('Update &lt;the&gt; document.') && !html.includes('Update <the>'),
  more: html.includes('more candidates available'),
  no_wrapper: !html.includes('execute_tools'),
  empty: empty.includes('No matching tools') &&
         empty.includes('0 candidate matches · showing 0'),
  gateway_hidden: gatewayOnly === '',
  child_visible: childOnly.includes('read_files') &&
                 !childOnly.includes('execute_tools') &&
                 childOnly.includes('data-full-count="1"'),
};
for (const [name, ok] of Object.entries(checks)) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
}
dom.window.close();
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_tool_search_result_cards():
    proc = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, SOURCE],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    failures = [line for line in proc.stdout.splitlines()
                if line.startswith('FAIL ')]
    assert not failures, proc.stdout
    assert proc.stdout.count('PASS ') == 14


def test_gateway_adapter_is_filtered_from_live_and_history_projections():
    core = open(os.path.join(ROOT, 'static', 'js', 'core.js'),
                encoding='utf-8').read()
    streaming = open(os.path.join(ROOT, 'static', 'js', 'ui',
                                  'streaming_ui.js'),
                     encoding='utf-8').read()
    assert 'r.toolName === "execute_tools"' in core
    assert 'r.toolName === "execute_tools"' in streaming
