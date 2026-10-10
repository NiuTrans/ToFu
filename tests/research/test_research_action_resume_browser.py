"""Shipped page, API facade and action delegation continue an interrupted task."""
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import pytest
from lib.research.workspace import empty_workspace
from tests.e2e.test_e2e_smoke import _wait_app_ready

pytestmark=pytest.mark.visual


def ok(route,data):
    route.fulfill(json={'data':data,'meta':{'requestId':'resume-browser','serverTimeMs':0}})


@pytest.mark.parametrize('discovery',['live','cold'])
@pytest.mark.parametrize('outcome',['done','rejected','late'])
def test_resume_is_single_flight_preserves_draft_and_fences_navigation(page,outcome,discovery):
    _wait_app_ready(page)
    page.add_style_tag(content='#onboardingModal { display: none !important; }')
    workspace=empty_workspace('Interrupted original action','en');workspace['hypothesis']='Saved original hypothesis'
    reads=[];resumes=[];pending_get=[];starts=[]
    def workspace_route(route):
        query=parse_qs(urlparse(route.request.url).query)
        direction=query.get('direction',[workspace['direction']])[0]
        result=dict(workspace,direction=direction)
        if direction=='Second workspace':result['hypothesis']='Second untouched hypothesis'
        ok(route,{'workspace':result})
    def tasks_route(route):
        query=parse_qs(urlparse(route.request.url).query)
        rows=[{'id':'original-action','kind':'research-action','status':'running',
            'meta':{'direction':workspace['direction'],'lang':'en','action':'analyze'}}]
        ok(route,{'tasks':rows if discovery=='live' and query.get('status')==['running'] and query.get('direction')==[workspace['direction']] else []})
    def task_route(route):
        reads.append(route)
        if discovery=='live' and len(reads)==1:ok(route,{'id':'original-action','status':'interrupted','meta':{'action':'analyze'}})
        else:pending_get.append(route)
    page.route('**/api/v4/research/workspace?*',workspace_route)
    page.route('**/api/v4/research/capabilities',lambda route:ok(route,{'catalog':{'tools':[],'capabilities':[]}}))
    record={'task_id':'browser-source','user_id':1,'direction':workspace['direction'],'lang':'en',
        'status':'done','result':{'survey_md':'Synthetic browser fixture','accepted':[],'rejected':[]}}
    page.route('**/api/v4/research/records?*',lambda route:ok(route,{'items':[record],'total':1}))
    page.route('**/api/v4/research/records/browser-source',lambda route:ok(route,{'record':record}))
    def unfinished_route(route):
        query=parse_qs(urlparse(route.request.url).query)
        tasks=[{'id':'original-action','kind':'research-action','status':'interrupted',
            'meta':{'direction':workspace['direction'],'lang':'en','action':'analyze'}}]
        ok(route,{'tasks':tasks if discovery=='cold' and query.get('direction')==[workspace['direction']] else [],'complete':True})
    page.route('**/api/v4/research/actions/unfinished?*',unfinished_route)
    page.route('**/api/v4/tasks?*',tasks_route)
    page.route('**/api/v4/tasks/original-action',task_route)
    page.route('**/api/v4/research/actions/original-action/resume',lambda route:resumes.append(route))
    page.route('**/api/v4/tasks/start',lambda route:starts.append(route))
    page.evaluate("window.location.hash='/evolution/research'")
    page.locator('.research-record-item').filter(has_text=workspace['direction']).click()
    page.locator('[data-rs-tab="workspace"]').click()
    page.wait_for_function('typeof window.TofuModules.resolveAction("_loadResearchWorkspace")==="function"')
    page.evaluate('''direction=>window.TofuModules.resolveAction('_loadResearchWorkspace')(direction,'en')''',workspace['direction'])
    button=page.locator('button[data-tofu-action="_resumeResearchAction(this)"]')
    button.wait_for(state='visible')
    assert 'ui-btn' in (button.get_attribute('class') or '')
    hypothesis=page.locator('[data-rsw-field="hypothesis"]')
    hypothesis.fill('Keep this unsaved edit')
    if outcome=='done':
        page.evaluate("window.TofuModules.resolveAction('_onLanguageChange')('en')")
        page.wait_for_function('document.documentElement.lang==="en"')
        page.evaluate("direction=>window.TofuModules.resolveAction('_loadResearchWorkspace')(direction,'en')",workspace['direction'])
        destination=Path('/tmp/tofu-research-resume-previews');destination.mkdir(exist_ok=True)
        for theme in ('dark','light','tofu'):
            page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
            page.locator('#researchWorkspace .rsw-agent-section').screenshot(path=str(destination/f'continue-task-{theme}.png'))
    button.click()
    page.wait_for_timeout(100)
    assert len(resumes)==1 and resumes[0].request.post_data_json=={}
    assert button.is_disabled()
    page.evaluate('''()=>{void window.TofuModules.resolveAction('_resumeResearchAction')();
      void window.TofuModules.resolveAction('_startResearchAction')('analyze');}''')
    assert len(resumes)==1 and not starts
    if outcome=='late':
        page.evaluate("window.TofuModules.resolveAction('_loadResearchWorkspace')('Second workspace','en')")
        ok(resumes[0],{'task_id':'original-action','resumed':True})
        page.wait_for_timeout(100)
        assert len(reads)==(1 if discovery=='live' else 0)
        assert hypothesis.input_value()=='Second untouched hypothesis'
        assert page.locator('button[data-tofu-action="_resumeResearchAction(this)"]').count()==0
    elif outcome=='rejected':
        resumes[0].fulfill(status=409,json={'data':None,'error':{'code':'research_resume_conflict',
            'message':'Original frontier changed'},'meta':{'requestId':'resume-browser','serverTimeMs':0}})
        page.get_by_text('Original frontier changed',exact=False).wait_for()
        assert not button.is_disabled()
        assert hypothesis.input_value()=='Keep this unsaved edit'
    else:
        ok(resumes[0],{'task_id':'original-action','resumed':True})
        page.wait_for_timeout(100)
        assert len(pending_get)==1
        ok(pending_get[0],{'status':'done','result':{'workspace':dict(workspace,
            revision=workspace['revision']+1,hypothesis='Completed server result')}})
        page.wait_for_function('document.querySelector(\'button[data-tofu-action="_resumeResearchAction(this)"]\')===null')
        assert hypothesis.input_value()=='Keep this unsaved edit'
        assert len(reads)==(2 if discovery=='live' else 1)

    errors=getattr(page,'_tofu_js_errors',[])
    expected='console.error: Failed to load resource: the server responded with a status of 409 (Conflict)'
    # This scenario deliberately drives one HTTP conflict, not a script failure.
    assert not [error for error in errors if outcome!='rejected' or error!=expected]
