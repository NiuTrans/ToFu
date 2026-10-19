"""Production cards use shipped owners/CSS; fixture APIs never create provider jobs."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ['light', 'dark', 'tofu'] for width in [320, 1180]])
def test_visible_review_preview_revision_and_lifecycle(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""theme => {
      document.documentElement.dataset.theme=theme; document.documentElement.lang='zh';
      document.body.innerHTML='<main id="works"></main><textarea id="userInput" aria-label="消息"></textarea>';
    }""", theme)
    page.add_style_tag(content='body{position:static!important;display:block!important;height:auto!important;overflow:auto!important;margin:0;padding:var(--space-4)}'
                            '#works,#userInput{display:block;max-width:var(--conversation-reading-measure);width:100%;margin-inline:auto;box-sizing:border-box}')
    page.route('**/api/v4/slides/**/pages/*.png?*', lambda route: route.fulfill(content_type='image/svg+xml', body='''
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540"><rect width="960" height="540" fill="#f4f1eb"/>
      <rect x="65" y="85" width="80" height="8" fill="#235d51"/><text x="65" y="165" font-size="38" fill="#173c34">TOFU / PRODUCT REVIEW</text>
      <text x="65" y="245" font-size="22" fill="#385b52">A preview of the work as it takes shape.</text>
      <rect x="65" y="315" width="240" height="130" rx="12" fill="#dce8df"/><rect x="330" y="315" width="240" height="130" rx="12" fill="#dce8df"/>
      <rect x="595" y="315" width="300" height="130" rx="12" fill="#dce8df"/></svg>'''))
    for owner in ['motion-card/index.ts', 'work-unit/tracker.ts', 'work-unit/composer.ts']:
        page.add_script_tag(path=native_module_path('.native/visual-' + owner.replace('/', '-').replace('.ts', '.js'), ROOT / 'frontend/src/conversation/ui' / owner))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate("""locale => {
      window.locale=locale; window.calls={reads:0,responses:[],opens:[],scenes:0};
      window.unit={kind:'slides-deck',title:'Tofu · 从需求到可以交付的作品',status:'running',
        stages:[{name:'outline',state:'done'},{name:'outline_gate',state:'active'}],stageTotal:9,
        pendingGate:{guidanceId:'human-plan',title:'让人可以随时参与，但不必盯住每个步骤。',pages:[
          {page:1,key_message:'先说清楚希望交付什么'}, {page:2,key_message:'确认关键方向，过程交给 Tofu'}, {page:3,key_message:'预览作品，再针对具体部分提出修改'}]},
        pages:{total:4,rendered:[1,2,3],rev:{'1':1,'2':1,'3':1},href:'/api/v4/slides/deck/pages/{n}.png'},
        links:{abort:'/api/v4/tasks/deck/abort'},artifacts:[]};
      const text=(key,fallback)=>locale[key]||fallback;
      const markup=renderMotionCard({toolName:'produce_slides',roundNum:1,toolContent:JSON.stringify({ok:true,topic:unit.title}),
        results:[{workUnitRef:{taskId:'deck',kind:'slides-deck'}}]},'', '',{text});
      document.querySelector('#works').innerHTML=markup;
      window.tracker=createWorkUnitTracker({document,text,ownerId:()=>7,
        getSnapshot:async()=>{calls.reads++;return {workUnit:structuredClone(unit)};},
        humanResponse:async(...args)=>{calls.responses.push(args);return {ok:true};},abort:async()=>({status:'aborting'}),
        scenes:async()=>{calls.scenes++;return {scenes:[]};},regenerate:async()=>({task_id:'revision'}),resolveUrl:p=>p,
        sceneFileUrl:(id,s)=>'/api/v4/motion/videos/'+id+'/scenes/'+s+'/file',
        openSlides:(...args)=>calls.opens.push(args),prefill:message=>prefillWorkUnitComposer(document,message,()=>{})});
      tracker.demand();
    }""", locale)
    gate = page.locator('.ptool-wu-gate')
    expect(gate).to_be_visible()
    assert gate.evaluate('(el)=>el.closest("details")===null')
    expect(page.locator('.ptool-work-receipt')).not_to_have_attribute('open', '')
    draft = gate.locator('textarea')
    draft.fill('第二页请强调人可以随时介入。')
    draft.evaluate('(el)=>el.setSelectionRange(2,6)')
    page.evaluate("unit.pendingGate.title='方向已梳理，等待你确认'; unit.pages.rev['1']=2")
    page.wait_for_function('calls.reads>=2')
    expect(draft).to_have_value('第二页请强调人可以随时介入。')
    expect(draft).to_be_focused()
    assert draft.evaluate('(el)=>[el.selectionStart,el.selectionEnd]') == [2, 6]
    expect(page.locator('[data-wu-page="1"] img')).to_have_attribute('src', '/api/v4/slides/deck/pages/1.png?v=2')
    page.screenshot(path=str(tmp_path / f'work-review-{theme}-{width}.png'), full_page=True)
    gate.get_by_role('button', name=locale['wu.gateSubmit']).click()
    page.wait_for_function('calls.responses.length===1')
    assert page.evaluate('calls.responses[0]') == ['human-plan', '第二页请强调人可以随时介入。']
    expect(draft).to_be_disabled()
    page.evaluate("unit.status='done';delete unit.pendingGate;unit.pages.rendered=[1,2,3,4];unit.stages.forEach(s=>s.state='done');unit.artifacts=[{label:'pptx',href:'/api/v4/slides/deck/file'}]")
    expect(gate).not_to_be_visible(timeout=8000)
    expect(page.locator('.ptool-wu-download')).to_have_attribute('href', '/api/v4/slides/deck/file')
    page.locator('[data-wu-page="2"]').click()
    assert page.evaluate('calls.opens[0]') == ['deck', 2, 4]
    page.locator('#userInput').fill('我还有一个想法。')
    page.get_by_role('button', name=locale['workUnit.revise'], exact=True).click()
    assert page.locator('#userInput').input_value().startswith('我还有一个想法。\n\n')
    expect(page.locator('#userInput')).to_be_focused()
    page.locator('.ptool-wu-more-pages summary').click()
    expect(page.locator('[data-wu-page="4"]')).to_be_visible()
    page.locator('.ptool-wu-process summary').click()
    expect(page.locator('.ptool-wu-stages')).to_be_visible()
    assert page.evaluate('tracker.snapshot().polling') is False
    geometry = page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
      cards:[...document.querySelectorAll('.ptool-work-card,.ptool-wu-gate')].map(n=>({width:n.clientWidth,scroll:n.scrollWidth}))})""")
    assert geometry['scroll'] <= geometry['width'] + 1, geometry
    assert all(card['scroll'] <= card['width'] + 1 for card in geometry['cards']), geometry
    page.screenshot(path=str(tmp_path / f'work-ready-{theme}-{width}.png'), full_page=True)
    page.evaluate("document.querySelector('#works').replaceChildren()")
    page.wait_for_function('tracker.snapshot().tasks===0')
    assert page.evaluate('tracker.snapshot()') == {'tasks': 0, 'views': 0, 'requests': 0, 'polling': False, 'observing': False}
    page.evaluate('tracker.dispose()')
