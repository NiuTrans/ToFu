"""Real Chromium checks cached search presentation/reload, not storage behavior.

Uses the production owner graph and shipped stylesheet on a local HTTP origin;
actual backend cache execution is covered by test_tool_search_cache_hit.py.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT

pytestmark = pytest.mark.visual
pytest_plugins = ('tests.support.frontend_visual',)


@pytest.mark.parametrize('width', [1280, 390])
def test_cached_tool_search_cards_survive_reload(frontend_visual_page, tmp_path, width):
    from playwright.sync_api import expect
    from lib.tasks_pkg.tool_search_projection import cached_tool_search_projection

    payload = {
        'status': 'ok', 'query': 'send_agent_message', 'total': 1,
        'next_cursor': None, 'items': [{
            'name': 'send_agent_message', 'namespace': 'swarm',
            'summary': 'Send <a> message safely.',
            'arguments_schema': {
                'type': 'object', 'properties': {'content': {'type': 'string'}},
                'required': ['content'],
            },
        }],
    }
    content = json.dumps(payload)
    matches, metadata = cached_tool_search_projection(content)
    legacy = {
        'roundNum': 1, 'toolCallId': 'legacy-cached-search',
        'toolName': 'search_tools', 'status': 'done',
        'query': 'send_agent_message', 'toolContent': content,
        'results': [{'toolName': 'search_tools', 'badge': 'cached',
                     'source': 'Cache', 'title': 'search_tools (cached)',
                     'snippet': 'Duplicate call — returning cached result'}],
    }
    native = {**legacy, 'roundNum': 2, 'toolCallId': 'native-cached-search',
              'results': matches, **metadata, 'cacheSource': 'cache'}
    empty = {**native, 'roundNum': 3, 'toolCallId': 'empty-cached-search',
             'query': 'missing_capability', 'results': [], 'toolSearchTotal': 0,
             'toolContent': json.dumps({**payload, 'items': [], 'total': 0})}
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('rows=>localStorage.setItem("cached-search-fixture",JSON.stringify(rows))',
                  [legacy, native, empty])
    owner = native_module_path('.native/cached-search-visual.js', ROOT /
                               'frontend/src/conversation/presentation/tool-search-presentation.ts')
    messages = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    for iteration in range(2):
        page.add_style_tag(content='body{display:block;overflow:auto;height:auto}'
                                   '#fixture{max-width:960px;margin:24px auto;padding:0 16px}')
        page.add_script_tag(path=owner)
        page.evaluate(r'''messages=>{
          const translate=(key,params)=>String(messages[key]||key).replace(
            /\{(\w+)\}/g,(_,name)=>String(params?.[name]??''));
          const presentation=createToolSearchPresentation({translate,iconHtml:()=>''});
          const rows=JSON.parse(localStorage.getItem('cached-search-fixture'));
          document.body.innerHTML='<main id="fixture"></main>';
          document.getElementById('fixture').innerHTML=rows.map(row=>
            presentation.renderSearchHtml(row,row.results,{
              iconHtml:'',queryHtml:'search_tools',rightControlsHtml:''})).join('');
        }''', messages)
        blocks = page.locator('.ptool-tool-search-block')
        expect(blocks).to_have_count(3)
        expect(blocks.filter(has_text='1 个候选匹配 · 当前显示 1 个')).to_have_count(2)
        cards = blocks.locator('.ptool-tool-search-card')
        expect(cards).to_have_count(2)
        for card in cards.all():
            expect(card).to_contain_text('send_agent_message')
            expect(card).to_contain_text('swarm')
            expect(card).to_contain_text('content')
            expect(card.locator('.ptool-tool-arg-required')).to_have_count(1)
            expect(card.locator('a')).to_have_count(0)
        expect(blocks.filter(has_text='0 个候选匹配 · 当前显示 0 个')).to_have_count(1)
        expect(blocks.filter(has_text='没有找到匹配的工具')).to_have_count(1)
        screenshot = tmp_path / f'tool-search-cache-{width}-{iteration}.png'
        page.screenshot(path=str(screenshot), full_page=True)
        print(f'Browser evidence: {screenshot}')
        if iteration == 0:
            page.reload(wait_until='load')
