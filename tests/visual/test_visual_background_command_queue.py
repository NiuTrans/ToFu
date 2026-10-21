"""Compact queued results on the real Surface; no processes or durable test data."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin
from tests.support.frontend_visual import frontend_visual_page as frontend_visual_page

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_adjacent_background_completions_share_one_queue_header(
        frontend_visual_page, tmp_path, theme, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 850})
    for name, source in [
        ('state', 'frontend/src/core/turn-state.ts'),
        ('view-model', 'frontend/src/conversation/presentation/conversation-view-model.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('renderers', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/bg-queue-visual-{name}.js', ROOT / source))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r"""({locale,theme,language}) => {
      document.documentElement.dataset.theme=theme;document.documentElement.lang=language;
      const translate=(key,fallback,vars)=>(locale[key]??fallback).replace(/\{(\w+)\}/g,(m,n)=>vars?.[n]??m);
      const host=document.createElement('main');host.id='queue-fixture';
      host.style.cssText='max-width:720px;margin:32px auto;padding:0 16px';document.body.append(host);
      window.intents=[];
      window.surface=createConversationSurface(host,{
        ...createClassicConversationRenderers({localizedText:translate,renderSafeMarkdownHtml:value=>value}),
        onIntent:intent=>intents.push(intent),
      });
      const item=(queueId,position,commandId)=>({queueId,position,kind:'background_receipt',
        priority:80,timestamp:1,text:'RAW_ENVELOPE',backgroundCommand:{commandId,status:'completed',
          command:'printf "completed safely"',exitCode:0,resultTruncated:true,
          resultPreview:'completed safely <script>unsafe</script> '+('selectable output '.repeat(160))}});
      window.items=[item('q-a',1,'bg_3a812dc63e29'),item('q-b',2,'bg_36fa2769fc10')];
      window.render=()=>surface.render(selectConversationViewModel({...createTurnState('visual'),queueItems:items}));
      window.removeFirst=()=>{items=items.slice(1).map((item,index)=>({...item,position:index+1}));render();};
      render();
    }""", {'locale': locale, 'theme': theme, 'language': language})
    assert page.locator('.bgcmd-queue-header').count() == 1
    assert page.locator('.bgcmd-queue-label').inner_text() == locale['bgCommand.queuedMany'].replace('{count}', '2')
    assert page.locator('.bgcmd-details').count() == 2
    assert page.locator('#queue-fixture script, #queue-fixture img').count() == 0
    assert 'RAW_ENVELOPE' not in page.locator('#queue-fixture').inner_text()
    geometry = page.evaluate("""() => {
      const [a,b]=[...document.querySelectorAll('.bgcmd-queue-row')].map(node=>node.getBoundingClientRect());
      return {gap:b.top-a.bottom,left:a.left,right:a.right,secondLeft:b.left,secondRight:b.right,
        overflow:document.documentElement.scrollWidth>innerWidth};
    }""")
    assert abs(geometry['gap']) <= 1, geometry
    assert abs(geometry['left'] - geometry['secondLeft']) <= 1, geometry
    assert abs(geometry['right'] - geometry['secondRight']) <= 1, geometry
    assert not geometry['overflow'], geometry
    collapsed = tmp_path / f'background-queue-{language}-{theme}-{width}-collapsed.png'
    page.locator('#queue-fixture').screenshot(path=str(collapsed))

    second = page.locator('[data-queue-id="q-b"]')
    second.locator('summary').focus()
    page.keyboard.press('Enter')
    assert second.locator('details').evaluate('(node)=>node.open')
    assert not page.locator('[data-queue-id="q-a"] details').evaluate('(node)=>node.open')
    assert '<script>unsafe</script>' in second.locator('.sw-card-raw-pre').inner_text()
    assert len(second.locator('.sw-card-raw-pre').inner_text()) == 2000
    assert second.locator('.bgcmd-note').inner_text() == locale['bgCommand.truncated']
    page.evaluate("""() => {
      const row=document.querySelector('[data-queue-id="q-b"]');
      window.reader={row,details:row.querySelector('details'),summary:row.querySelector('summary'),
        cancel:row.querySelector('[data-conversation-action="remove-queue"]')};
      const range=document.createRange(),text=row.querySelector('.sw-card-raw-pre').firstChild;
      range.setStart(text,0);range.setEnd(text,16);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      reader.selected=selection.toString();items=JSON.parse(JSON.stringify(items));render();removeFirst();
    }""")
    assert page.locator('.bgcmd-queue-header').count() == 1
    assert page.locator('.bgcmd-queue-label').inner_text() == locale['bgCommand.queued']
    assert page.evaluate("""() => reader.row===document.querySelector('[data-queue-id="q-b"]')
      &&reader.details===reader.row.querySelector('details')&&reader.details.open
      &&document.activeElement===reader.summary&&getSelection().toString()===reader.selected
      &&reader.cancel===reader.row.querySelector('[data-conversation-action="remove-queue"]')""")
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
    second.locator('[data-conversation-action="remove-queue"]').click()
    assert page.evaluate('intents') == [{'type': 'remove-queue', 'conversationId': 'visual', 'queueId': 'q-b'}]
    assert second.count() == 1, 'Cancel intent alone is not an authoritative acknowledgement'
    expanded = tmp_path / f'background-queue-{language}-{theme}-{width}-expanded.png'
    page.locator('#queue-fixture').screenshot(path=str(expanded))
    print(f'Background queue screenshots: {collapsed} {expanded}')
    page.evaluate('surface.dispose()')
