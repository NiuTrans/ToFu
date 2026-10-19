"""Real-server studio contract: objective/model intake, waiting, delivery and teardown."""
import json
from pathlib import Path
import pytest
pytestmark = pytest.mark.visual


def switch_goal(page, value):
    """Goal switching lives in the menu drawer since the studio has no top bar."""
    page.locator('wa-button[data-studio-action=menu]').click()
    page.locator('#evo-goal').select_option(value)
    page.keyboard.press('Escape')


@pytest.fixture
def page(browser,live_server):
    """Real shell, isolated unrelated catalog; goal API is controlled per journey."""
    import re
    from tests.conftest import _attach_js_error_capture, _dismiss_onboarding_modals
    context=browser.new_context()
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page=context.new_page();_attach_js_error_capture(page)
    # This suite tests goals, not the independently evolving chat catalog envelope.
    page.route(re.compile(r'.*/api/v[14]/conversations(?:\?.*)?$'),
               lambda route:route.fulfill(status=200,content_type='application/json',
               body=json.dumps({'items':[],'page':{'hasMore':False,'totalCount':0}})))
    page.goto(live_server,wait_until='domcontentloaded')
    page.wait_for_function('window.TofuModules?.version === 3')
    _dismiss_onboarding_modals(page)
    try:yield page
    finally:context.close()


@pytest.mark.parametrize('width', [390, 1536])
def test_studio_goal_model_human_delivery_and_chat_draft(page, live_server, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':1024})
    page.evaluate("localStorage.setItem('tofu_onboarding_v1_done','1')")
    _dismiss_onboarding_modals(page)
    run={'run_id':'studio-test','objective':'Create a verified report','status':'executing',
         'task_id':'studio-task','attempt':1,'created_at':1,'updated_at':1,
         'actuals':{'tokens':20,'tool_calls':1,'wall_clock_seconds':2},
         'receipts':[],'result':'','review':None,'error':''}
    posted=[]; answers=[]; reads=[]
    pending=[{'guidance_id':'human-studio','question':'Which format?','response_type':'choice',
              'options':['Markdown','Plain text'],'questions':[]}]
    def route(r):
        tail=r.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        reads.append(tail)
        if tail=='programs':body={'programs':[]}
        elif tail=='models':body={'models':[{'model':{'creator_id':'test','model_id':'model-a'},'label':'Test Model A'}]}
        elif tail=='runs' and r.request.method=='POST':
            posted.append((r.request.post_data_json,r.request.headers.get('idempotency-key')))
            body={'run':run}
        elif tail.endswith('/coordination'):body={'updates':[],'delivery':{},'revision':0,'before_seq':0,'has_earlier':False,'coordinator':{'status':'idle'}}
        elif tail=='runs':body={'runs':[run] if posted else []}
        elif tail.endswith('/answers'):
            answers.append(r.request.post_data_json);pending.clear()
            run.update(status='completed',result='Verified report\n\nThe test evidence is attached.',
                       review={'outcome':'success','criterion_results':[{'criterion_id':'delivery','outcome':'pass','evidence_ref':'Observed test result'}]})
            body={'run':run}
        else:body={'run':run,'pending_questions':pending}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/evolution/**',route)
    page.locator('#userInput').fill('Preserve my chat draft')
    page.locator('#evolutionBtn').click()
    page.locator('wa-button[data-studio-action=menu]').click()
    page.locator('.evo-goal-switcher a').click()
    expect(page.locator('#evo-objective')).to_be_visible(timeout=20000)
    expect(page.locator('#evo-model option')).to_have_count(2)
    _dismiss_onboarding_modals(page)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('#evo-objective').fill('Create a verified report')
    page.locator('.evo-briefing-settings summary').click()
    page.locator('#evo-model').select_option('1')
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(page.locator('#evo-objective')).to_have_value('Create a verified report')
    page.locator('.evo-objective-form button[type=submit]').click()
    expect(page.locator('.evo-human form')).to_have_count(1)
    page.locator('.evo-ledger-head[data-studio-action=details]').click()
    expect(page.locator('.evo-human form')).to_be_visible()
    assert posted[0][0]=={'objective':'Create a verified report','model':{'creator_id':'test','model_id':'model-a'}}
    assert posted[0][1]
    page.locator('[data-studio-action=option]').first.click()
    expect(page.locator('.evo-human textarea')).to_have_value('Markdown')
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(page.locator('.evo-human textarea')).to_have_value('Markdown')
    _dismiss_onboarding_modals(page)
    page.locator('.evo-human button[type=submit]').click()
    expect(page.locator('#evo-deliverable')).to_contain_text('Verified report')
    page.keyboard.press('Escape')
    page.locator('.evo-place-shortcut[data-place=delivery]').click()
    page.locator('[data-room-action=delivery]').click()
    assert page.url.endswith('#/evolution/runs/studio-test')
    assert answers==[{'guidance_id':'human-studio','response':'Markdown'}]
    expect(page.locator('.evo-human form')).to_have_count(0)
    output=Path('output/evolution-design');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'studio-delivery-{width}.png'),full_page=True)
    page.goto(live_server+'/#/chat')
    expect(page.locator('#userInput')).to_be_visible(timeout=20000)
    # Use same-document navigation to preserve the in-memory draft in the next check.
    page.locator('#userInput').fill('Preserve my chat draft')
    page.locator('#evolutionBtn').click()
    expect(page.locator('#evo-goal')).to_be_attached()
    page.evaluate("location.hash='/chat'")
    expect(page.locator('#userInput')).to_have_value('Preserve my chat draft')
    before=len(reads);page.wait_for_timeout(3300)
    assert len(reads)==before, 'disposed studio must stop polling'


@pytest.mark.parametrize('width',[390,1536])
def test_goal_rooms_keep_materials_separate_and_objects_navigate(page,live_server,width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':1024})
    page.evaluate("localStorage.setItem('tofu_onboarding_v1_done','1')")
    _dismiss_onboarding_modals(page)
    runs={rid:{'run_id':rid,'objective':title,'status':'completed','task_id':'','attempt':1,'created_at':ts,'updated_at':ts,'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},'receipts':[],'result':'','review':None,'error':''} for rid,title,ts in [('research','Research goal',2),('code','Code goal',1)]}
    shelves={'research':[],'code':[]};writes=[]
    def route(r):
        tail=r.request.url.split('/api/v4/evolution/')[1].split('?')[0];parts=tail.split('/')
        if tail=='programs':body={'programs':[]}
        elif tail=='models':body={'models':[]}
        elif tail.endswith('/coordination'):body={'updates':[],'delivery':{},'revision':0,'before_seq':0,'has_earlier':False,'coordinator':{'status':'idle'}}
        elif tail=='runs':body={'runs':list(runs.values())}
        elif len(parts)>=3 and parts[2]=='materials':
            rid=parts[1]
            if r.request.method=='POST':
                item={**r.request.post_data_json,'material_id':'material-1','run_id':rid,'origin':'user','task_id':'','created_at':1,'updated_at':1};shelves[rid]=[item];writes.append(rid);body={'material':item}
            else:shelves[rid]=[];body={'removed':parts[-1]}
        else:
            rid=parts[1];body={'run':runs[rid],'pending_questions':[],'workspace':{'materials':shelves[rid],'stages':[{'kind':'Inspect','detail':'Verify the goal-specific evidence'}],'criteria':[{'criterion_id':'delivery','requirement':'Deliver a tested result'}]}}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/evolution/**',route)
    page.locator('#evolutionBtn').click()
    expect(page.locator('#evo-goal')).to_have_value('research')
    expect(page.locator('.evo-objective-form')).not_to_be_visible()
    page.locator('.evo-place-shortcut[data-place=plan]').click();expect(page.locator('.evo-room-content')).to_contain_text('Verify the goal-specific evidence')
    page.keyboard.press('Escape')
    page.locator('.evo-place-shortcut[data-place=library]').click()
    page.locator('.evo-material-add summary').click()
    page.locator('.evo-material-add [name=title]').fill('Source only for research')
    page.locator('.evo-material-add [name=url]').fill('https://example.org/paper')
    page.locator('.evo-material-add [name=note]').fill('Relevant source')
    page.locator('.evo-material-add button[type=submit]').click()
    link=page.locator('.evo-material-list a');expect(link).to_have_count(1)
    expect(link).to_have_attribute('href','https://example.org/paper')
    expect(link).to_have_attribute('rel','noopener noreferrer')
    assert writes==['research']
    expect(page.locator('.evo-object-library b')).to_have_text('1')
    page.locator('[data-room-action=filter][data-filter=repository]').click();expect(link).to_have_count(0)
    page.locator('[data-room-action=filter][data-filter=all]').click();expect(link).to_have_count(1)
    page.keyboard.press('Escape')
    switch_goal(page,'code');expect(page.locator('.evo-room-goal-name')).to_have_text('Code goal');expect(link).to_have_count(0)
    page.reload();expect(page.locator('#evo-goal')).to_have_value('code',timeout=20000);expect(link).to_have_count(0)
    _dismiss_onboarding_modals(page)
    switch_goal(page,'research');expect(link).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    output=Path('output/evolution-design/v3');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'goal-room-{width}.png'),full_page=True)
    page.locator('.evo-place-shortcut[data-place=library]').click()
    page.locator('[data-room-action=remove]').click();expect(link).to_have_count(0)
    page.keyboard.press('Escape')
    page.locator('wa-button[data-studio-action=menu]').click()
    page.locator('.evo-goal-switcher a').click()
    expect(page.locator('#evo-objective')).to_be_visible()
    expect(page.locator('.evo-goal-panel')).not_to_be_visible()


@pytest.mark.parametrize('width',[390,1536])
def test_swarm_agents_get_own_rooms_and_switcher(page,live_server,width,assert_no_js_errors):
    """Every rostered sub-agent owns a visitable room scoped to its own brief."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':1024})
    page.evaluate("localStorage.setItem('tofu_onboarding_v1_done','1')")
    _dismiss_onboarding_modals(page)
    run={'run_id':'studio-swarm','objective':'Ship a verified swarm report','status':'executing','task_id':'swarm-task','attempt':1,'created_at':1,'updated_at':1,'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},'receipts':[],'result':'','review':None,'error':''}
    agents=[{'id':'a1','role':'researcher','status':'running','objective':'Compare evaluation methods'},{'id':'a2','role':'coder','status':'pending','objective':'Build the prototype'}]
    def route(r):
        tail=r.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        if tail=='programs':body={'programs':[]}
        elif tail=='models':body={'models':[]}
        elif tail.endswith('/coordination'):body={'updates':[],'delivery':{},'revision':0,'before_seq':0,'has_earlier':False,'coordinator':{'status':'idle'}}
        elif tail=='runs':body={'runs':[run]}
        else:body={'run':run,'pending_questions':[],'workspace':{'materials':[],'stages':[],'criteria':[]}}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/evolution/**',route)
    # The studio decorates actors over the task SSE stream; end it terminally.
    page.route('**/api/v4/tasks/**',lambda r:r.fulfill(status=200,content_type='text/event-stream',body='data: {"type":"done","status":"completed"}\n\n'))
    def swarm(r):
        body={'active':bool(agents),'known':True,'agents':agents}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/swarm/status/**',swarm)
    page.locator('#evolutionBtn').click()
    tabs=page.locator('.evo-room-tab')
    expect(tabs).to_have_count(6,timeout=20000)
    # Workstation chairs stay tucked at their desks, never loose on the floor.
    assert page.evaluate("document.querySelector('.evo-office-live').dataset.chairs")=='in,in'
    # The first sub-agent room: main scene swaps out, brief stays scoped.
    if width<=850:page.locator(".evo-rooms-open").click()
    tabs.nth(1).click()
    assert page.evaluate("document.querySelector('.evo-office').dataset.room")=='pod'
    expect(page.locator('.evo-office-live')).to_be_hidden()
    expect(page.locator('.evo-office-pod')).to_be_visible()
    expect(page.locator('.evo-pod-brief strong')).to_have_text('researcher')
    expect(page.locator('.evo-pod-objective')).to_have_text('Compare evaluation methods')
    assert page.evaluate("document.querySelector('.evo-office-pod').dataset.status")=='running'
    assert page.evaluate("document.querySelector('.evo-pod-person').getAttribute('aria-label')||''").startswith('researcher · ')
    # The second room renders its own roster state.
    if width<=850:page.locator(".evo-rooms-open").click()
    tabs.nth(2).click()
    expect(page.locator('.evo-pod-brief strong')).to_have_text('coder')
    assert page.evaluate("document.querySelector('.evo-office-pod').dataset.status")=='pending'
    # Back to the supervising office restores the main scene.
    if width<=850:page.locator(".evo-rooms-open").click()
    tabs.nth(0).click()
    assert page.evaluate("document.querySelector('.evo-office').dataset.room")=='main'
    expect(page.locator('.evo-office-live')).to_be_visible()
    output=Path('output/evolution-design');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'agent-rooms-{width}.png'),full_page=True)
    # Rooms remain visitable after the roster empties; no phantom member remains.
    agents.clear()
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(tabs).to_have_count(6)
    if width<=850:page.locator('.evo-rooms-open').click()
    expect(tabs.nth(1)).to_contain_text('暂无成员',timeout=20000)
    tabs.nth(1).click();expect(page.locator('.evo-pod-person')).to_be_hidden()



@pytest.mark.parametrize('width',[390,1536])
def test_spokesperson_message_receipts_goal_switch_and_followup(page,live_server,width,assert_no_js_errors):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':1024})
    page.evaluate("localStorage.setItem('tofu_onboarding_v1_done','1')")
    _dismiss_onboarding_modals(page)
    runs={rid:{'run_id':rid,'objective':title,'status':'completed','task_id':'','attempt':1,'created_at':ts,'updated_at':ts,'actuals':{'tokens':12,'tool_calls':1,'wall_clock_seconds':2},'receipts':[],'result':'A usable result with evidence.','review':{'outcome':'success','criterion_results':[]},'error':''} for rid,title,ts in [('a','Compare document parsers',2),('b','Repair CSV export',1)]}
    updates={'a':[],'b':[]};posted=[];followups=[]
    def route(r):
        tail=r.request.url.split('/api/v4/evolution/')[1].split('?')[0];parts=tail.split('/')
        if tail=='programs':body={'programs':[]}
        elif tail=='models':body={'models':[]}
        elif tail=='runs':body={'runs':list(runs.values())}
        else:
            rid=parts[1]
            if tail.endswith('/updates'):
                payload=r.request.post_data_json;posted.append((rid,payload,r.request.headers.get('idempotency-key')))
                event={'seq':len(updates[rid])+1,'event_id':'owner-message','kind':'message','content':payload['content'],'origin':'user','target':'coordinator','task_id':'','created_at':1,'references':[]}
                updates[rid].append(event);updates[rid].append({**event,'seq':event['seq']+1,'event_id':'reply','kind':'reply','origin':'agent','content':'The comparison is complete. I can explain the evidence without rerunning it.'})
                body={'update':event,'coordinator':{'status':'idle'}}
            elif tail.endswith('/coordination'):
                body={'updates':updates[rid],'revision':len(updates[rid]),'through_seq':len(updates[rid]),'has_more':False,'has_earlier':False,'before_seq':1 if updates[rid] else 0,'delivery':{str(e['seq']):{'status':'considered','note':'Answered the question; execution unchanged.'} for e in updates[rid] if e['origin']=='user'},'coordinator':{'enabled':True,'status':'idle','task_id':'','last_response':'','error':''}}
            elif tail.endswith('/followups'):
                followups.append(r.request.post_data_json);runs['child']={**runs[rid],'run_id':'child','objective':'Add scanned PDF support','status':'executing','result':''};updates['child']=[];body={'run':runs['child']}
            else:body={'run':runs[rid],'pending_questions':[],'workspace':{'materials':[],'stages':[],'criteria':[]}}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/evolution/**',route)
    page.locator('#evolutionBtn').click()
    expect(page.locator('#evo-goal')).to_have_value('a')
    if width<=850:page.locator('.evo-rooms-open').click()
    page.locator('[data-studio-action=conversation]').click();expect(page.locator('#evo-message')).to_be_visible()
    assert page.locator('.evo-conversation').evaluate("el=>getComputedStyle(el).getPropertyValue('--space-8').trim()")=='32px'
    assert page.locator('.evo-conversation-heading').evaluate("el=>parseFloat(getComputedStyle(el).gap)")>=16
    page.locator('#evo-message').fill('Why did you choose this parser?')
    page.locator('.evo-conversation-form [type=submit]').click()
    expect(page.locator('.evo-message-receipt')).to_have_attribute('data-status','considered')
    expect(page.locator('.evo-conversation-log')).to_contain_text('without rerunning it')
    assert posted[0][1]=={'kind':'message','content':'Why did you choose this parser?'} and posted[0][2]
    assert followups==[]
    page.locator('#evo-message').fill('Draft for A')
    output=Path('output/evolution-design/joint');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'conversation-{width}.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.keyboard.press('Escape');switch_goal(page,'b');expect(page.locator('#evo-goal')).to_have_value('b')
    if width<=850:page.locator('.evo-rooms-open').click()
    page.locator('[data-studio-action=conversation]').click();expect(page.locator('#evo-message')).to_have_value('');expect(page.locator('.evo-conversation-log')).not_to_contain_text('parser')
    page.keyboard.press('Escape');switch_goal(page,'a');expect(page.locator('#evo-goal')).to_have_value('a')
    if width<=850:page.locator('.evo-rooms-open').click()
    page.locator('[data-studio-action=conversation]').click();expect(page.locator('#evo-message')).to_have_value('Draft for A')
    page.locator('#evo-message').fill('Add scanned PDF support');page.locator('[data-dialogue=followup]').click()
    expect(page.locator('#evo-goal')).to_have_value('child')
    assert followups==[{'kind':'guidance','content':'Add scanned PDF support'}]
    page.screenshot(path=str(output/f'office-{width}.png'),full_page=True)


@pytest.mark.parametrize('width',[390,1536])
def test_actor_model_choice_uses_chat_catalog_and_survives_reload(page,live_server,width,assert_no_js_errors):
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':1024})
    a={'creator_id':'test','model_id':'a'};b={'creator_id':'test','model_id':'b'}
    run={'run_id':'role-goal','objective':'Research a paper','model':a,'role_models':{},
         'status':'executing','task_id':'active-task','attempt':1,'created_at':1,'updated_at':1,
         'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},'receipts':[],'result':'','review':None,'error':''}
    saved=[]
    def route(r):
        tail=r.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        if tail=='programs':body={'programs':[]}
        elif tail=='models':body={'models':[{'model':a,'label':'Chat Model A'},{'model':b,'label':'Chat Model B'}]}
        elif tail=='runs':body={'runs':[run]}
        elif tail.endswith('/coordination'):body={'updates':[],'delivery':{},'revision':0,'before_seq':0,'has_earlier':False,'coordinator':{'status':'idle'}}
        elif tail.endswith('/role-model'):
            payload=r.request.post_data_json;saved.append(payload)
            if payload['model'] is None:run['role_models'].pop(payload['role'],None)
            else:run['role_models'][payload['role']]=payload['model']
            body={'run':run}
        else:body={'run':run,'pending_questions':[]}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
    page.route('**/api/v4/evolution/**',route)
    # The synthetic run's task_id has no live task engine behind it; the
    # studio's activity EventSource gets a clean terminal frame instead of a
    # 404 (the teardown gate fails on any console error).
    page.route('**/api/v4/tasks/active-task/stream**',
               lambda r: r.fulfill(status=200, content_type='text/event-stream',
                                   body='data: {"type":"done","status":"done"}\n\n'))
    page.goto(live_server+'/#/evolution/runs/role-goal')
    actor=page.locator('.evo-person[data-actor="0"]')
    expect(actor).to_be_visible(timeout=20000)
    actor.press('Enter')
    drawer=page.locator('#evo-role-drawer')
    expect(drawer.locator('select')).to_be_visible()
    expect(drawer.locator('select option')).to_have_count(3)
    assert drawer.locator('select').evaluate("el => {const s=getComputedStyle(el);return el.clientHeight-parseFloat(s.paddingTop)-parseFloat(s.paddingBottom)>=parseFloat(s.fontSize)+4}")
    drawer.locator('select').select_option('1')
    drawer.locator('button.ui-btn--primary').click()
    expect(drawer.locator('[role=status]')).not_to_be_empty()
    assert saved==[{'role':'executor','model':b}]
    assert run['task_id']=='active-task'
    page.reload();expect(actor).to_be_visible(timeout=20000);actor.press('Enter')
    expect(drawer.locator('select')).to_be_visible()
    expect(drawer.locator('select')).to_have_value('1')
    output=Path('output/evolution-design/role-models');output.mkdir(parents=True,exist_ok=True)
    page.wait_for_function("document.querySelector('#evo-role-drawer').shadowRoot.querySelector('[part~=dialog]').getBoundingClientRect().right <= innerWidth")
    page.screenshot(path=str(output/f'role-model-{width}.png'),full_page=True)
    drawer.locator('select').select_option('')
    drawer.locator('button.ui-btn--primary').click()
    expect(drawer.locator('[role=status]')).not_to_be_empty()
    assert saved[-1]=={'role':'executor','model':None}
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.parametrize('width', [390, 1536])
def test_mission_panel_exposes_failed_run_without_opening_drawer(page, live_server, width, screenshot_dir):
    """A room animation cannot hide failure, evidence, usage or restart authority."""

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    from playwright.sync_api import expect
    page.set_viewport_size({'width': width, 'height': 1024})
    run = {'run_id': 'mission-failed', 'objective': 'Optimize the harness with measured evidence',
           'status': 'failed', 'task_id': 'mission-task', 'attempt': 1, 'created_at': 1, 'updated_at': 1,
           'budget': {'max_tokens': 0, 'max_tool_calls': 0, 'max_wall_clock_seconds': 0},
           'actuals': {'tokens': 211730, 'tool_calls': 31, 'wall_clock_seconds': 450},
           'receipts': [{'task_id': 'mission-task', 'stage': 'executing', 'status': 'error'}],
           'result': '', 'review': None, 'error': 'execution_token_budget_exhausted'}
    workspace = {'run_id': run['run_id'], 'objective': run['objective'],
                 'criteria': [{'criterion_id': 'measured', 'requirement': 'Record the before and after results'}],
                 'stages': [{'kind': 'Inspect', 'detail': 'Measure the baseline and identify bottlenecks'},
                            {'kind': 'Implement', 'detail': 'Change the measured bottleneck'},
                            {'kind': 'Verify', 'detail': 'Run tests and record comparable results'}],
                 'budget': run['budget'], 'status': 'failed', 'task_id': run['task_id'],
                 'execution_refs': [], 'materials': [], 'records': [], 'proposals': []}
    posted = []

    def handler(route):
        tail = route.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        if route.request.method == 'POST':
            posted.append(tail)
        if tail == 'runs':
            value = {'runs': [run]}
        elif tail == 'runs/mission-failed':
            value = {'run': run, 'workspace': workspace, 'pending_questions': []}
        elif tail.endswith('/coordination'):
            value = {'updates': [{'seq': 1, 'origin': 'executor',
                     'kind': 'report', 'content': 'Baseline captured; regression verification remains outstanding.', 'references': []}],
                     'delivery': {}, 'revision': 1, 'before_seq': 0, 'has_earlier': False,
                     'coordinator': {'status': 'idle'}}
        elif tail.endswith('/questions'):
            value = {'questions': []}
        else:
            value = {'items': []}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.route('**/api/v4/evolution/**', handler)
    page.route('**/api/v4/swarm/status/**', lambda route: route.fulfill(status=200, content_type='application/json', body='{"agents":[],"known":true}'))
    page.goto(live_server + '/#/evolution/runs/mission-failed', wait_until='domcontentloaded')
    panel = page.locator('.evo-mission-panel')
    expect(panel).to_be_visible()

    assert not errors, errors
    expect(page.locator('h1.evo-ledger-heading strong')).to_have_text(run['objective'])
    expect(panel.locator('#evo-mission-title')).not_to_have_text(run['objective'])
    expect(panel.locator('.evo-mission-hud')).to_be_visible()
    expect(page.locator('.evo-studio-grid .evo-mission-hud')).to_have_count(0)
    expect(page.locator('.evo-office-backdrop')).to_be_hidden()
    expect(panel).to_contain_text('Measure the baseline')
    expect(panel).to_contain_text('211,730')
    expect(panel).to_contain_text('Baseline captured')
    assert 'execution_token_budget_exhausted' not in panel.inner_text()
    assert '200,000' not in panel.inner_text()
    expect(panel.locator('[data-mission-action=stop]')).to_be_hidden()
    expect(panel.locator('[data-mission-action=retry]')).to_be_visible()
    expect(panel.locator('[data-mission-action=execution]')).to_be_visible()
    assert page.locator('wa-drawer[open]').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
    # Visibility alone cannot detect text clipped by its row or covered by the room.
    layout = page.evaluate("""() => {
      const box = s => document.querySelector(s).getBoundingClientRect();
      const head = box('.evo-ledger-head'), title = box('.evo-ledger-head > strong');
      const mission = box('.evo-mission-panel'), body = box('.evo-mission-body');
      const room = box('.evo-studio-grid'), hint = box('.evo-pan-hint');
      return {hintInRoom: hint.top >= room.top && hint.bottom <= room.bottom,
        titleFits: title.bottom <= head.bottom + 1,
        bodyFits: body.bottom <= mission.bottom + 1, missionFirst: room.top >= mission.bottom - 1,
        geometry: ['.evo-main', '.evo-live-ledger', '.evo-mission-panel', '.evo-mission-body', '.evo-studio-grid'].map(s => {
          const el = document.querySelector(s), r = el.getBoundingClientRect(), c = getComputedStyle(el);
          return {s, top:r.top, bottom:r.bottom, height:r.height, rows:c.gridTemplateRows,
            row:c.gridRow, position:c.position, minHeight:c.minHeight, maxHeight:c.maxHeight};
        })};
    }""")
    assert layout['titleFits'], layout
    if width < 850:
        assert layout['bodyFits'] and layout['missionFirst'], json.dumps(layout)

        assert layout['hintInRoom'], json.dumps(layout)
    page.screenshot(path=str(Path(screenshot_dir) / f'evolution-mission-failed-{width}.png'), full_page=True)
    # A retry prepares a visible confirmation; it must not replay effects itself.
    panel.locator('[data-mission-action=retry]').click()
    expect(page.locator('#evo-objective')).to_be_visible()
    assert page.locator('wa-drawer[open]').count() == 0
    expect(page.locator('#evo-objective')).to_have_value(run['objective'])
    assert not posted
    page.goto(live_server + '/#/evolution/runs/mission-failed')
    # A later real snapshot updates this same surface, including explicit stop.
    run.update(status='executing', error='', receipts=[])
    workspace['status'] = 'executing'
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(panel.locator('[data-mission-action=stop]')).to_be_visible(timeout=10000)
    expect(panel.locator('[data-mission-action=retry]')).to_be_hidden()


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('width', [320, 390, 768, 1024, 1536])
def test_studio_briefing_themes_draft_and_focus(page, live_server, screenshot_dir, theme, width):
    """The redesigned intake stays legible, keyboard usable and read-only until dispatch."""
    from playwright.sync_api import expect
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1024})
    page.emulate_media(reduced_motion='reduce')
    language = 'en' if theme == 'dark' else 'zh'
    page.add_init_script(f"localStorage.setItem('claude_ui_theme', {json.dumps(theme)});localStorage.setItem('tofu_ui_lang', {json.dumps(language)})")

    def reads(route):
        if route.request.method != 'GET':
            writes.append(route.request.url)
        route.fulfill(status=200, content_type='application/json', body=json.dumps({
            'runs': [], 'models': [], 'programs': [], 'plugins': [], 'items': [],
        }))

    page.route('**/api/v4/evolution/**', reads)
    # A query forces a new document so language/theme init scripts run; hash-only navigation does not.
    page.goto(live_server + '/?studio-design=' + theme + '#/evolution/start', wait_until='domcontentloaded')
    field = page.locator('#evo-objective')
    expect(field).to_be_visible(timeout=20000)
    expect(page.locator('html')).to_have_attribute('data-theme', theme)
    expect(page.locator('.evo-briefing-examples svg')).to_have_count(3)
    expect(page.locator('.evo-mission-header')).to_be_hidden()
    expect(page.locator('.evo-mission-body')).to_be_hidden()
    page.locator('[data-example=research]').click()
    expect(field).not_to_have_value('')
    expect(field).to_be_focused()
    draft = field.input_value()
    expect(page.locator('[data-example=build]')).to_be_disabled()
    scene_focus = page.locator('[data-scene-focus-toggle]')
    scene_focus.click()
    expect(page.locator('.evo-studio')).to_have_attribute('data-scene-focus', 'true')
    expect(page.locator('.evo-mission-panel')).to_be_hidden()
    scene_focus.click()
    expect(page.locator('.evo-studio')).to_have_attribute('data-scene-focus', 'false')
    expect(field).to_have_value(draft)
    page.locator('[data-studio-action=refresh]').evaluate('(el) => el.click()')
    expect(field).to_have_value(draft)
    assert not writes
    assert not errors
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    geometry = page.evaluate('''() => {
      const box = selector => document.querySelector(selector).getBoundingClientRect();
      const panel = box('.evo-mission-panel'), office = box('.evo-studio-grid');
      const submit = box('.evo-briefing button[type=submit]');
      return {missionFirst: panel.bottom <= office.top, submitFits: submit.left >= panel.left && submit.right <= panel.right};
    }''')
    assert geometry['submitFits']
    if width >= 850:
        assert page.locator('.evo-destinations button').evaluate_all('(buttons) => buttons.every(button => button.scrollWidth <= button.clientWidth + 1)')
    if width < 850:
        assert geometry['missionFirst']
    contrast = page.evaluate('''() => {
      const rgb = value => value.match(/[\\d.]+/g).slice(0,3).map(Number).map(v => {v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});
      const lum = value => {const [r,g,b]=rgb(value);return .2126*r+.7152*g+.0722*b};
      const bg = lum(getComputedStyle(document.querySelector('.evo-mission-panel')).backgroundColor);
      return ['.evo-briefing-intro','.evo-briefing-heading .evo-mission-eyebrow','.evo-briefing h3'].map(selector => {
        const fg = lum(getComputedStyle(document.querySelector(selector)).color);
        return {selector, ratio:(Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};
      });
    }''')
    assert all(item['ratio'] >= 4.5 for item in contrast), contrast
    field.fill('')
    page.locator('.evo-main').evaluate('(el) => el.scrollTop = 0')
    page.screenshot(path=str(Path(screenshot_dir) / f'evolution-briefing-{theme}-{width}.png'), full_page=True)
