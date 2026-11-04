"""Native folder-editor layout against real owner modules and shipped CSS.

Only directory/recent/save ports are synthetic; no application store is opened.
The full API journey remains in tests/e2e/test_folder_settings.py.
"""
import json
import pytest
from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import (
    ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page,  # noqa: F401
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_folder_workbench_layout_and_keyboard(frontend_visual_page, theme, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate('''({theme, locale}) => {
      document.documentElement.dataset.theme = theme;
      document.body.replaceChildren();
      const opener = document.createElement('button'); opener.id='opener'; opener.textContent='Open';
      document.body.append(opener); opener.focus();
      window.t=(key,values)=>(locale[key]||key).replace(/\\{(\\w+)\\}/g,(_,name)=>values?.[name]??'');
    }''', {'theme': theme, 'locale': locale})
    page.add_script_tag(path=native_module_path('.native/folder-workbench.js',
        ROOT / 'frontend/src/conversation/ui/folder-settings.ts'))
    page.evaluate('''() => {
      window.calls={browse:0,recent:0,saves:[],browsed:[]};
      window.recentPaths=['tofu','llm-mcp','lumix','autoharness','codex'].map(name=>
        '/mnt/workspace/'+'long-parent-directory/'.repeat(8)+name);
      window.openEditor=()=>openFolderSettings({t,
        create:async values=>{calls.saves.push(values);return {id:'created'};},
        update:async ()=>({id:'updated'}),
        recent:async ()=>{calls.recent++;return recentPaths.map((path,index)=>({path,count:index+1}));},
        browse:async path=>{calls.browse++;calls.browsed.push(path);return {path:'/workspace',parent:'/',
          dirs:[{name:'child',path:'/workspace/child',hasCode:true,itemCount:12},
                {name:'notes',path:'/workspace/notes',itemCount:3}]};}
      });
      window.editor=openEditor();
    }''')
    dialog = page.locator('dialog:modal')
    dialog.locator('.folder-browser-row').first.wait_for(state='visible')
    # Opening reads the folder listing and recents once each; nothing is written.
    assert page.evaluate('calls.browse===1 && calls.recent===1 && calls.saves.length===0')
    name = dialog.locator('.folder-settings-identity input')
    name.fill('Tofu')
    assert not dialog.locator('.mp-path-input').get_attribute('aria-invalid')
    cards = dialog.locator('.recent-paths-list .recent-path-item')
    cards.first.wait_for(state='visible')
    assert cards.count() == 5
    assert cards.first.locator('.recent-path-name').inner_text() == 'tofu'
    assert cards.first.get_attribute('aria-label') == page.evaluate('recentPaths[0]')
    cards.first.click()
    # A recent pick stages the root and shows it in the browser; the entry stays available.
    page.wait_for_function('calls.browse===2')
    assert page.evaluate('calls.browsed[1]===recentPaths[0] && calls.saves.length===0')
    assert cards.first.is_enabled()
    assert dialog.locator('.mp-row').get_attribute('data-path') == page.evaluate('recentPaths[0]')
    assert dialog.locator('.mp-row .mp-row-name').inner_text() == 'tofu'
    geometry = dialog.evaluate('''d=>{
      const b=d.querySelector('.folder-browser').getBoundingClientRect();
      const r=d.querySelector('.folder-settings-workbench .pm-rail').getBoundingClientRect();
      const save=d.querySelector('button[type=submit]').getBoundingClientRect();
      const list=d.querySelector('.folder-browser-list').getBoundingClientRect();
      return {width:d.getBoundingClientRect().width,overflow:d.scrollWidth>d.clientWidth+1,
        columns:r.left>=b.right,saveTop:save.top,saveBottom:save.bottom,listHeight:list.height,
        border:parseFloat(getComputedStyle(d).borderTopWidth),background:getComputedStyle(d).backgroundColor};
    }''')
    assert geometry['width'] <= width and not geometry['overflow']
    assert geometry['columns'] == (width > 700)
    assert geometry['listHeight'] >= 120, geometry
    assert geometry['border'] == 3 and geometry['background'] != 'rgba(0, 0, 0, 0)'
    assert geometry['saveTop'] >= 0 and geometry['saveBottom'] <= 900
    for selector in ('.recent-path-full', '.mp-row-path', '.folder-browser-current'):
        assert dialog.locator(selector).first.evaluate('el=>getComputedStyle(el).whiteSpace') == 'nowrap'
    # Long paths keep their tail (the folder's own name) visible.
    tail = dialog.locator('.recent-path-full').first.evaluate('''el=>{
      const box=el.getBoundingClientRect(); const range=document.createRange();
      range.selectNodeContents(el.querySelector('bdi')); const text=range.getBoundingClientRect();
      return {clipped:text.width>box.width+1, endVisible:text.right<=box.right+1};
    }''')
    assert tail['clipped'] and tail['endVisible'], tail
    assert dialog.evaluate('''d=>[...d.querySelectorAll('button[hidden]')].every(
      button=>getComputedStyle(button).display==='none' && button.getClientRects().length===0)''')
    out = ROOT / 'output/playwright/folder-settings'
    out.mkdir(parents=True, exist_ok=True)
    dialog.locator('.folder-settings-content').evaluate('el=>el.scrollTop=0')
    page.screenshot(path=str(out / f'{theme}-{width}-{language}.png'))
    # Native modal semantics, not a custom focus trap.
    for _ in range(6):
        page.keyboard.press('Tab')
        assert dialog.evaluate('d=>d.contains(document.activeElement)')
    page.keyboard.press('Escape')
    assert page.locator('dialog:modal').count() == 0
    assert page.locator('#opener').evaluate('el=>el===document.activeElement')
    # Empty-Add validation must never make workspace a required create field.
    page.evaluate('editor=openEditor()')
    dialog=page.locator('dialog:modal')
    dialog.locator('.folder-settings-identity input').fill('Name only')
    dialog.locator('.mp-add-row > .mp-add-btn').click()
    assert dialog.locator('.mp-path-input').get_attribute('aria-invalid') == 'true'
    dialog.locator('button[type=submit]').click()
    assert page.locator('dialog:modal').count() == 0
    assert page.evaluate('calls.saves[0].projectPaths') == []


@pytest.mark.parametrize('width', [1280, 390])
def test_folder_browser_selection_and_footer(frontend_visual_page, width):
    page=frontend_visual_page
    page.set_viewport_size({'width':width,'height':740})
    locale=json.loads((ROOT/'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate('''locale=>{
      document.documentElement.dataset.theme='tofu';
      window.t=(key,values)=>(locale[key]||key).replace(/\\{(\\w+)\\}/g,(_,name)=>values?.[name]??'');
    }''',locale)
    page.add_script_tag(path=native_module_path('.native/folder-workbench.js',
        ROOT/'frontend/src/conversation/ui/folder-settings.ts'))
    page.evaluate('''()=>openFolderSettings({t,create:async()=>({id:'new'}),update:async()=>({}),
      recent:async()=>[],browse:async()=>({path:'/workspace',parent:'/',
        dirs:Array.from({length:20},(_,i)=>({name:'directory-'+i,path:'/workspace/'+i}))})})''')
    dialog=page.locator('dialog:modal')
    rows=dialog.locator('.folder-browser-row')
    rows.first.wait_for(state='visible')
    assert rows.count() == 20
    # The list scrolls inside its pane instead of growing the dialog.
    assert dialog.locator('.folder-browser-list').evaluate('el=>el.scrollHeight>el.clientHeight')
    footer=dialog.locator('button[type=submit]').bounding_box()
    assert footer['y'] >= 0 and footer['y']+footer['height'] <= 740
    shown="d=>[...d.querySelectorAll('.folder-browser-row')].filter(row=>!row.hidden).length"
    search=dialog.locator('.folder-browser-filter')
    search.fill('directory-1')
    assert dialog.evaluate(shown) == 11
    search.press('Escape')
    assert dialog.evaluate(shown) == 20
    assert page.locator('dialog:modal').count() == 1  # Escape cleared the filter only
    add=dialog.get_by_role('button',name=locale['pm.addThisFolder'],exact=True)
    assert add.is_enabled()
    add.click()
    assert dialog.locator('.mp-row').get_attribute('data-path') == '/workspace'
    assert dialog.locator('.folder-browser-footer button').is_disabled()
