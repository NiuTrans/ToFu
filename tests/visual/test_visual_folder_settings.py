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
      window.calls={browse:0,recent:0,saves:[]};
      window.recentPaths=['tofu','llm-mcp','lumix','autoharness','codex'].map(name=>
        '/mnt/workspace/'+'long-parent-directory/'.repeat(8)+name);
      window.openEditor=()=>openFolderSettings({t,
        create:async values=>{calls.saves.push(values);return {id:'created'};},
        update:async ()=>({id:'updated'}),
        recent:async ()=>{calls.recent++;return recentPaths.map(path=>({path}));},
        browse:async path=>{calls.browse++;return {path:'/workspace',parent:'/',
          dirs:[{name:'child',path:'/workspace/child'}]};}
      });
      window.editor=openEditor();
    }''')
    dialog = page.locator('dialog:modal')
    assert page.evaluate('calls.browse===0 && calls.recent===0')
    name = dialog.locator('.folder-settings-identity input')
    name.fill('Tofu')
    assert not dialog.locator('.folder-settings-path-field input').get_attribute('aria-invalid')
    dialog.locator('.folder-settings-recents summary').click()
    cards = dialog.locator('.folder-settings-recent-list button')

    cards.first.wait_for(state='visible')
    assert cards.count() == 5
    assert cards.first.locator('.folder-settings-recent-name').inner_text() == 'tofu'
    assert cards.first.get_attribute('aria-label') == page.evaluate('recentPaths[0]')
    cards.first.click()
    assert cards.first.is_disabled()
    assert dialog.locator('.folder-settings-root-name').inner_text() == 'tofu'
    geometry = dialog.evaluate('''d=>{
      const w=d.querySelector('.folder-settings-workspace').getBoundingClientRect();
      const r=d.querySelector('.folder-settings-recents').getBoundingClientRect();
      const save=d.querySelector('button[type=submit]').getBoundingClientRect();
      return {width:d.getBoundingClientRect().width,overflow:d.scrollWidth>d.clientWidth+1,
        columns:r.left>=w.right,saveTop:save.top,saveBottom:save.bottom,
        border:parseFloat(getComputedStyle(d).borderTopWidth),background:getComputedStyle(d).backgroundColor};
    }''')
    assert geometry['width'] <= width and not geometry['overflow']
    assert geometry['columns'] == (width > 700)
    assert geometry['border'] == 3 and geometry['background'] != 'rgba(0, 0, 0, 0)'
    assert geometry['saveTop'] >= 0 and geometry['saveBottom'] <= 900
    assert cards.first.locator('.folder-settings-recent-path').evaluate('el=>getComputedStyle(el).whiteSpace') == 'nowrap'
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
    dialog.get_by_role('button', name=locale['folder.settings.addRoot'], exact=True).click()
    assert dialog.locator('.folder-settings-path-field input').get_attribute('aria-invalid') == 'true'
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
    dialog.get_by_role('button',name=locale['folder.projectPathBrowse'],exact=True).click()
    dialog.locator('.folder-settings-directories button').first.wait_for(state='visible')
    footer=dialog.locator('button[type=submit]').bounding_box()
    assert footer['y'] >= 0 and footer['y']+footer['height'] <= 740
    use=dialog.get_by_role('button',name=locale['folder.settings.useDirectory'],exact=True)
    assert use.is_enabled()
    use.click()
    assert dialog.locator('.folder-settings-root').get_attribute('data-path') == '/workspace'
    assert not dialog.locator('.folder-settings-browser').is_visible()
