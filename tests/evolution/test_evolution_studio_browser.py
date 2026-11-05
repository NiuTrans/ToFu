"""Real-server studio journeys: intake with model and idempotency, the dialogue,
delivery, per-goal materials, swarm rooms, actor models, failure and retry.

The shell, router, bundle and stage are real; the goal API is controlled per journey.
"""
import json
import re
from pathlib import Path

import pytest

from tests.evolution.test_evolution_office_browser import hud_layout, studio_copy, uncovered_center

pytestmark = pytest.mark.visual


def copy(page, key):
    """Studio copy in the language the shell selected."""
    return studio_copy(key, 'en' if (page.evaluate('document.documentElement.lang') or '').startswith('en') else 'zh')


def switch_goal(page, run_id):
    """Goal switching lives in the menu window."""
    from playwright.sync_api import expect
    page.locator('.evo-hud-button[data-hud=menu]').click()
    page.locator(f'.evo-goal-row[data-key="{run_id}"]').click()
    expect(page.locator('.evo-window')).to_be_hidden()


@pytest.fixture
def page(browser,live_server):
    """Real shell, isolated unrelated catalog; goal API is controlled per journey."""
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
    # The beckoning answer button animates forever; reduced motion keeps it clickable as a still target.
    page.emulate_media(reduced_motion='reduce')
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
    root=page.locator('.evo-game')
    # No goal yet: the quest banner invites one and opens the briefing.
    quest=page.locator('.evo-quest');expect(quest).to_have_attribute('data-hud','create',timeout=20000)
    quest.click()
    field=page.locator('#evo-objective')
    expect(field).to_be_visible();expect(field).to_be_focused()
    expect(page.locator('#evo-model option')).to_have_count(2)
    _dismiss_onboarding_modals(page)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    field.fill('Create a verified report')
    page.locator('.evo-briefing-settings summary').click()
    page.locator('#evo-model').select_option('1')
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(field).to_have_value('Create a verified report')
    page.locator('.evo-objective-form .evo-dispatch').click()
    # The pending question becomes an RPG choice box over the world.
    dialogue=page.locator('.evo-dialogue[data-mode=question]')
    expect(dialogue).to_be_visible(timeout=15000)
    expect(page.locator('.evo-answer')).to_have_attribute('data-question','human-studio')
    expect(root).to_have_attribute('data-status','askHuman')
    expect(page.locator('.evo-primary-action')).to_have_attribute('data-kind','answer')
    assert posted[0][0]=={'objective':'Create a verified report','model':{'creator_id':'test','model_id':'model-a'}}
    assert posted[0][1]
    page.locator('.evo-choice[data-value=Markdown]').click()
    answer=page.locator('.evo-answer textarea');expect(answer).to_have_value('Markdown')
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(answer).to_have_value('Markdown')
    _dismiss_onboarding_modals(page)
    # Dismissing collapses the question; the primary action brings it back with the draft.
    page.locator('.evo-dialogue-close').click();expect(page.locator('.evo-dialogue')).to_be_hidden()
    page.locator('.evo-primary-action[data-kind=answer]').click()
    expect(dialogue).to_be_visible();expect(answer).to_have_value('Markdown')
    page.locator('.evo-answer button[type=submit]').click()
    read=page.locator('.evo-primary-action[data-kind=read]');expect(read).to_be_visible()
    read.click()
    expect(page.locator('#evo-deliverable')).to_contain_text('Verified report')
    expect(page.locator('[role=tab][data-place=delivery]')).to_have_attribute('aria-selected','true')
    assert page.url.endswith('#/evolution/runs/studio-test')
    assert answers==[{'guidance_id':'human-studio','response':'Markdown'}]
    expect(page.locator('.evo-answer')).to_have_count(0)
    output=Path('output/evolution-design');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'studio-delivery-{width}.png'),full_page=True)
    page.goto(live_server+'/#/chat')
    expect(page.locator('#userInput')).to_be_visible(timeout=20000)
    # Use same-document navigation to preserve the in-memory draft in the next check.
    page.locator('#userInput').fill('Preserve my chat draft')
    page.locator('#evolutionBtn').click()
    expect(page.locator('.evo-game .evo-stage-canvas')).to_be_attached()
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
    title=page.locator('.evo-quest-title')
    expect(title).to_have_text('Research goal',timeout=20000)
    expect(page.locator('.evo-window')).to_be_hidden()
    # Furniture on the canvas navigates: a real pointer click opens that place's journal tab.
    place,center=next((place,center) for place in ['plan','library','work','review','delivery']
                      if (center:=uncovered_center(page,f'.evo-target[data-place={place}]')))
    page.mouse.click(*center)
    expect(page.locator(f'[role=tab][data-place={place}]')).to_have_attribute('aria-selected','true')
    page.keyboard.press('Escape')
    page.locator('.evo-slot[data-place=plan]').click()
    expect(page.locator('.evo-tab-static')).to_contain_text('Verify the goal-specific evidence')
    page.keyboard.press('Escape')
    page.locator('.evo-slot[data-place=library]').click()
    page.locator('.evo-material-add summary').click()
    page.locator('.evo-material-add [name=title]').fill('Source only for research')
    page.locator('.evo-material-add [name=url]').fill('https://example.org/paper')
    page.locator('.evo-material-add [name=note]').fill('Relevant source')
    page.locator('.evo-material-add button[type=submit]').click()
    link=page.locator('.evo-materials a');expect(link).to_have_count(1)
    expect(link).to_have_attribute('href','https://example.org/paper')
    expect(link).to_have_attribute('rel','noopener noreferrer')
    assert writes==['research']
    expect(page.locator('.evo-slot[data-place=library] .evo-slot-count')).to_have_text('1')
    expect(page.locator('.evo-target[data-target="place:library"]')).to_have_attribute('data-material-ids','material-1')
    page.locator('[data-room-action=filter][data-filter=repository]').click();expect(link).to_have_count(0)
    page.locator('[data-room-action=filter][data-filter=all]').click();expect(link).to_have_count(1)
    page.keyboard.press('Escape')
    switch_goal(page,'code');expect(title).to_have_text('Code goal');expect(link).to_have_count(0)
    page.reload();expect(title).to_have_text('Code goal',timeout=20000);expect(link).to_have_count(0)
    _dismiss_onboarding_modals(page)
    switch_goal(page,'research');expect(link).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    output=Path('output/evolution-design/v3');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'goal-room-{width}.png'),full_page=True)
    page.locator('.evo-slot[data-place=library]').click()
    page.locator('[data-room-action=remove]').click();expect(link).to_have_count(0)
    page.keyboard.press('Escape')
    page.locator('.evo-hud-button[data-hud=menu]').click()
    page.locator('.evo-menu [data-studio-action=create]').click()
    expect(page.locator('#evo-objective')).to_be_visible()
    expect(page.locator('.evo-pane[data-pane=journal]')).to_be_hidden()


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
    root=page.locator('.evo-game');tiles=page.locator('.evo-room-tile')
    expect(tiles).to_have_count(6,timeout=20000)
    # Live sub-agents visit the main office; workstation chairs end tucked at the desks.
    expect(page.locator('.evo-target[data-visitor]')).to_have_count(2)
    expect(page.locator('.evo-target[data-target="actor:executor"]')).to_have_attribute('data-seated','true',timeout=15000)
    for place in ['work','review']:
        expect(page.locator(f'.evo-target[data-target="place:{place}"]')).to_have_attribute('data-chair','in')
    def open_rooms():
        if width<=760 and root.get_attribute('data-rooms-open')!='true':page.locator('.evo-hud-button[data-hud=rooms]').click()
    card=page.locator('.evo-member-card');member=page.locator('.evo-target[data-target=member]')
    # The first sub-agent room: the stage swaps scenes and the card stays scoped.
    open_rooms();page.locator('.evo-room-tile[data-room=research]').click()
    expect(root).to_have_attribute('data-room','research')
    expect(page.locator('.evo-target[data-target="place:library"]')).to_have_count(0)
    expect(card.locator('strong')).to_have_text(copy(page,'memberTitle_research'))
    expect(card.locator('.evo-member-brief')).to_have_text('Compare evaluation methods')
    expect(member).to_have_attribute('data-member','a1');expect(member).to_have_attribute('data-status','running')
    assert (member.get_attribute('aria-label') or '').startswith(copy(page,'memberTitle_research')+' · ')
    # The second room renders its own roster state.
    open_rooms();page.locator('.evo-room-tile[data-room=engineering]').click()
    expect(card.locator('strong')).to_have_text(copy(page,'memberTitle_engineering'))
    expect(member).to_have_attribute('data-member','a2');expect(member).to_have_attribute('data-status','pending')
    # Back to the supervising office restores the main scene.
    open_rooms();page.locator('.evo-room-tile[data-room=""]').click()
    expect(root).to_have_attribute('data-room','main');expect(card).to_be_hidden()
    expect(page.locator('.evo-target[data-target="place:library"]')).to_be_attached()
    output=Path('output/evolution-design');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'agent-rooms-{width}.png'),full_page=True)
    # Rooms remain visitable after the roster empties; no phantom member remains.
    agents.clear()
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(tiles).to_have_count(6)
    research=page.locator('.evo-room-tile[data-room=research]')
    expect(research).to_have_attribute('aria-label',re.compile(r' · 0$'),timeout=20000)
    expect(page.locator('.evo-target[data-visitor]')).to_have_count(0,timeout=10000)
    open_rooms();research.click()
    expect(card.locator('.evo-chip')).to_have_text(copy(page,'roomUnoccupied'));expect(member).to_have_count(0)



@pytest.mark.parametrize('width',[390,1536])
def test_spokesperson_message_receipts_goal_switch_and_followup(page,live_server,width,assert_no_js_errors):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':1024})
    page.evaluate("localStorage.setItem('tofu_onboarding_v1_done','1')")
    _dismiss_onboarding_modals(page)
    runs={rid:{'run_id':rid,'objective':title,'status':'completed','task_id':'','attempt':1,'created_at':ts,'updated_at':ts,'actuals':{'tokens':12,'tool_calls':1,'wall_clock_seconds':2},'receipts':[],'result':'A usable result with evidence.','review':{'outcome':'success','criterion_results':[]},'error':''} for rid,title,ts in [('a','Compare document parsers',2),('b','Repair CSV export',1)]}
    # Goal B already holds an unseen coordinator reply.
    updates={'a':[],'b':[{'seq':1,'event_id':'b-reply','kind':'reply','origin':'agent','content':'CSV export now writes UTF-8 with a byte order mark.','target':'owner','task_id':'','created_at':1,'references':[]}]}
    posted=[];followups=[]
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
    # The follow-up goal executes a synthetic task: end its activity stream cleanly.
    page.route('**/api/v4/tasks/**',lambda r:r.fulfill(status=200,content_type='text/event-stream',body='data: {"type":"done","status":"completed"}\n\n'))
    page.route('**/api/v4/swarm/status/**',lambda r:r.fulfill(status=200,content_type='application/json',body='{"agents":[],"known":true}'))
    page.locator('#evolutionBtn').click()
    title=page.locator('.evo-quest-title');expect(title).to_have_text('Compare document parsers',timeout=20000)
    chat=page.locator('.evo-slot[data-slot=chat]');message=page.locator('#evo-message')
    chat.click();expect(message).to_be_visible()
    expect(page.locator('.evo-game')).to_have_attribute('data-window','chat')
    message.fill('Why did you choose this parser?')
    page.locator('.evo-conversation-form [type=submit]').click()
    expect(page.locator('.evo-message-receipt')).to_have_attribute('data-status','considered')
    expect(page.locator('.evo-conversation-log')).to_contain_text('without rerunning it')
    assert posted[0][1]=={'kind':'message','content':'Why did you choose this parser?'} and posted[0][2]
    assert followups==[]
    message.fill('Draft for A')
    output=Path('output/evolution-design/joint');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'conversation-{width}.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    # A reply read in the chat window is not repeated by the dialogue box.
    page.keyboard.press('Escape');expect(page.locator('.evo-dialogue')).to_be_hidden()
    switch_goal(page,'b');expect(title).to_have_text('Repair CSV export')
    # B's unseen reply speaks once in the dialogue box and leads into the chat.
    line=page.locator('.evo-dialogue[data-mode=line] .evo-line')
    expect(line).to_have_text('CSV export now writes UTF-8 with a byte order mark.')
    page.locator('.evo-dialogue [data-dialogue-action=chat]').click()
    expect(message).to_have_value('');expect(page.locator('.evo-conversation-log')).not_to_contain_text('parser')
    expect(page.locator('.evo-conversation-log')).to_contain_text('byte order mark')
    page.keyboard.press('Escape');expect(page.locator('.evo-dialogue')).to_be_hidden()
    switch_goal(page,'a');expect(title).to_have_text('Compare document parsers')
    chat.click();expect(message).to_have_value('Draft for A')
    message.fill('Add scanned PDF support');page.locator('[data-dialogue=followup]').click()
    expect(page).to_have_url(re.compile(r'#/evolution/runs/child$'))
    expect(title).to_have_text('Add scanned PDF support')
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
    page.route('**/api/v4/swarm/status/**',lambda r:r.fulfill(status=200,content_type='application/json',body='{"agents":[],"known":true}'))
    page.goto(live_server+'/#/evolution/runs/role-goal')
    root=page.locator('.evo-game')
    actor=page.locator('.evo-target[data-target="actor:executor"]')
    expect(actor).to_be_attached(timeout=20000)
    # The invisible target layer keeps every resident keyboard reachable.
    actor.focus();actor.press('Enter')
    expect(root).to_have_attribute('data-window','actor')
    expect(page.locator('#evo-window-title')).to_have_text(copy(page,'executor'))
    select=page.locator('.evo-role-models select')
    expect(select).to_be_visible()
    expect(select.locator('option')).to_have_count(3)
    assert select.evaluate("el => {const s=getComputedStyle(el);return el.clientHeight-parseFloat(s.paddingTop)-parseFloat(s.paddingBottom)>=parseFloat(s.fontSize)+4}")
    select.select_option('1')
    page.locator('.evo-role-models button.ui-btn--primary').click()
    expect(page.locator('.evo-role-models [role=status]')).not_to_be_empty()
    assert saved==[{'role':'executor','model':b}]
    assert run['task_id']=='active-task'
    page.reload()
    expect(actor).to_have_attribute('data-phase','seated',timeout=20000)
    # A real pointer click on the drawn executor opens the same card.
    center=uncovered_center(page,'.evo-target[data-target="actor:executor"]');assert center
    page.mouse.click(*center)
    expect(select).to_be_visible()
    expect(select).to_have_value('1')
    output=Path('output/evolution-design/role-models');output.mkdir(parents=True,exist_ok=True)
    page.wait_for_function("document.querySelector('.evo-window').getAnimations().every(animation=>animation.playState!=='running')")
    assert page.locator('.evo-window').evaluate('el => {const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth}')
    page.screenshot(path=str(output/f'role-model-{width}.png'),full_page=True)
    select.select_option('')
    page.locator('.evo-role-models button.ui-btn--primary').click()
    expect(page.locator('.evo-role-models [role=status]')).not_to_be_empty()
    assert saved[-1]=={'role':'executor','model':None}
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.parametrize('width', [390, 1536])
def test_hud_exposes_failed_run_without_opening_a_window(page, live_server, width, screenshot_dir):
    """A room animation cannot hide failure, evidence, usage or restart authority."""

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    from playwright.sync_api import expect
    page.set_viewport_size({'width': width, 'height': 1024})
    run = {'run_id': 'mission-failed', 'objective': 'Optimize the harness with measured evidence',
           'status': 'failed', 'task_id': 'mission-task', 'attempt': 1, 'created_at': 1, 'updated_at': 1,
           'budget': {'max_tokens': 0, 'max_tool_calls': 0, 'max_wall_clock_seconds': 0},
           'actuals': {'tokens': 211730, 'tool_calls': 31, 'wall_clock_seconds': 450},
           'receipts': [{'task_id': 'mission-task', 'stage': 'executing', 'terminal_verdict': 'failed',
                         'actuals': {'tokens': 211730, 'tool_calls': 31, 'wall_clock_seconds': 450}}],
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
    page.route('**/api/v4/tasks/**', lambda route: route.fulfill(status=200, content_type='text/event-stream', body='data: {"type":"done","status":"done"}\n\n'))
    page.goto(live_server + '/#/evolution/runs/mission-failed', wait_until='domcontentloaded')
    root, quest = page.locator('.evo-game'), page.locator('.evo-quest')
    expect(page.locator('.evo-quest-title')).to_have_text(run['objective'], timeout=20000)
    expect(page.locator('.evo-quest-state')).to_have_text(copy(page, 'state_failed'))
    expect(page.locator('.evo-quest-detail')).to_have_text(copy(page, 'fail_limit'))
    expect(quest).to_have_attribute('data-tone', 'bad')
    expect(root).to_have_attribute('data-status', 'state_failed')
    expect(page.locator('.evo-track li[data-stage=execute]')).to_have_attribute('data-state', 'failed')
    tokens = page.locator('.evo-meter[data-meter=tokens]')
    expect(tokens).to_have_attribute('aria-label', re.compile('211,730'))
    expect(tokens.locator('b')).not_to_contain_text('≥')  # nothing runs: the recorded usage is exact
    assert page.locator('.evo-meter-bar:not([hidden])').count() == 0, 'an unlimited run shows no invented limit'
    hud_text = page.locator('.evo-hud-top').inner_text() + page.locator('.evo-hotbar').inner_text()
    assert 'execution_token_budget_exhausted' not in hud_text
    assert '200,000' not in page.locator('.evo-game').inner_text()
    primary = page.locator('.evo-primary-action')
    expect(primary).to_have_attribute('data-kind', 'retry')
    expect(page.locator('.evo-window')).to_be_hidden()
    assert not errors, errors
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
    layout = hud_layout(page)
    assert not layout['overlaps'] and not layout['outside'], layout
    page.screenshot(path=str(Path(screenshot_dir) / f'evolution-mission-failed-{width}.png'), full_page=True)
    # Evidence is one tab away; a stopped run offers no stop control.
    page.locator('.evo-slot[data-place=plan]').click()
    expect(page.locator('.evo-tab-static')).to_contain_text('Measure the baseline')
    page.locator('[role=tab][data-place=work]').click()
    work = page.locator('.evo-tab-inspector[data-tab=work]')
    expect(work).to_contain_text('Baseline captured')
    expect(work).to_contain_text('211,730')
    expect(page.locator('[data-room-action=task]')).to_be_visible()
    expect(page.locator('[data-room-action=stop]')).to_have_count(0)
    page.keyboard.press('Escape')
    # A retry prepares a visible confirmation; it must not replay effects itself.
    primary.click()
    expect(root).to_have_attribute('data-window', 'briefing')
    expect(page.locator('#evo-objective')).to_have_value(run['objective'])
    assert not posted
    page.keyboard.press('Escape')
    # A later real snapshot updates this same surface, including explicit stop.
    run.update(status='executing', error='', receipts=[])
    workspace['status'] = 'executing'
    page.locator('[data-studio-action=refresh]').evaluate('(el)=>el.click()')
    expect(primary).to_be_hidden(timeout=10000)
    expect(page.locator('.evo-track li[data-stage=execute]')).to_have_attribute('data-state', 'active')
    page.locator('.evo-slot[data-place=work]').click()
    expect(page.locator('[data-room-action=stop]')).to_be_visible()


@pytest.mark.parametrize('width', [390, 1536])
def test_compute_stall_dialogue_says_where_and_hands_off_to_the_watchdog(page, live_server, width, screenshot_dir):
    """A failing job-status read is the executor saying where it is stuck, inside the frame and never
    as a limit; its hand-off ensures the owner's Router Watchdog once and opens it in chat."""
    from playwright.sync_api import expect
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1024})
    page.emulate_media(reduced_motion='reduce')
    error = "ModuleNotFoundError: No module named 'tofu_environments.capacity'"
    origin = 'tofu_yourprovider_environments/hope_catalog.py:3 in <module>'
    usage = {'tokens': 156059, 'tool_calls': 9, 'wall_clock_seconds': 66}
    run = {'run_id': 'compute-stall', 'objective': 'Finish the case-study training, then collect, analyze and verify the results',
           'status': 'executing', 'task_id': 'stall-task', 'attempt': 1, 'created_at': 1, 'updated_at': 1,
           'budget': {'max_tokens': 0, 'max_tool_calls': 0, 'max_wall_clock_seconds': 0}, 'actuals': usage,
           'compute_wait': {'operation_id': 'job-9ec7ad7f7c3ee2a0f1b7052f1872da02', 'task_id': 'stall-task',
                            'parked': True, 'next_poll_at': 0, 'last_error': error,
                            'last_error_origin': origin, 'failed_since': 1700000000},
           'receipts': [{'task_id': 'stall-task', 'stage': 'executing', 'terminal_verdict': 'completed', 'actuals': usage}],
           'result': 'Compute dependency pending; no completion or scientific acceptance.', 'review': None, 'error': ''}
    workspace = {'run_id': run['run_id'], 'objective': run['objective'], 'criteria': [], 'stages': [],
                 'budget': run['budget'], 'status': 'executing', 'task_id': run['task_id'],
                 'execution_refs': [], 'materials': [], 'records': [], 'proposals': []}
    writes, ensured = [], []

    def evolution(route):
        tail = route.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        if route.request.method != 'GET':
            writes.append(tail)
        if tail == 'runs':
            value = {'runs': [run]}
        elif tail == 'runs/compute-stall':
            value = {'run': run, 'workspace': workspace, 'pending_questions': []}
        elif tail.endswith('/coordination'):
            value = {'updates': [], 'delivery': {}, 'revision': 1, 'before_seq': 0, 'has_earlier': False,
                     'coordinator': {'status': 'idle'}}
        elif tail.endswith('/questions'):
            value = {'questions': []}
        else:
            value = {'items': []}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    def watchdog(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        ensured.append(route.request.headers.get('idempotency-key'))
        route.fulfill(status=200, content_type='application/json', body=json.dumps({
            'projectPath': '/router-watchdog', 'conversationId': 'watchdog-conv', 'revision': 1,
            'availability': 'available', 'capabilities': {'assign': False, 'sendMessage': False, 'steerSubagent': True},
            'review': {'enabled': False, 'schedule': '0 */6 * * *', 'taskId': None}}))

    page.route('**/api/v4/evolution/**', evolution)
    page.route('**/api/v4/router/watchdog', watchdog)
    page.route('**/api/v4/swarm/status/**', lambda route: route.fulfill(status=200, content_type='application/json', body='{"agents":[],"known":true}'))
    page.route('**/api/v4/tasks/**', lambda route: route.fulfill(status=200, content_type='text/event-stream', body='data: {"type":"done","status":"done"}\n\n'))
    page.goto(live_server + '/#/evolution/runs/compute-stall', wait_until='domcontentloaded')
    expect(page.locator('.evo-quest-title')).to_have_text(run['objective'], timeout=20000)
    # The banner names the wait; nothing presents it as a limit, a failure or a delivery.
    expect(page.locator('.evo-quest-state')).to_have_text(copy(page, 'computeStalled'))
    expect(page.locator('.evo-quest')).not_to_have_attribute('data-tone', 'bad')
    assert page.locator('.evo-meter-bar:not([hidden])').count() == 0, 'an unlimited wait shows no invented limit'
    expect(page.locator('.evo-track li[data-stage=deliver]')).not_to_have_attribute('data-state', 'done')
    dialogue = page.locator('.evo-dialogue')
    expect(dialogue).to_be_visible()
    expect(dialogue).to_have_attribute('data-mode', 'stall')
    expect(dialogue.locator('.evo-speaker')).to_have_text(copy(page, 'executor'))
    expect(dialogue.locator('.evo-line')).to_have_text(copy(page, 'stallLine'))
    expect(dialogue.locator('[data-fact=at] dd')).to_contain_text(run['compute_wait']['operation_id'])
    expect(dialogue.locator('[data-fact=error] dd')).to_have_text(error)
    expect(dialogue.locator('[data-fact=origin] dd')).to_have_text(origin)
    expect(page.locator('.evo-primary-action')).to_have_attribute('data-kind', 'stall')
    # The whole box, its actions included, sits inside the frame beside the HUD.
    handoff = dialogue.locator('[data-dialogue-action=watchdog]')
    expect(handoff).to_have_text(copy(page, 'stallHandToWatchdog'))
    for action in (handoff, dialogue.locator('[data-dialogue-action=record]')):
        expect(action).to_be_visible()
        assert action.evaluate("""el => {const r = el.getBoundingClientRect(), box = el.closest('.evo-dialogue').getBoundingClientRect();
          return r.top >= box.top && r.bottom <= box.bottom && r.left >= box.left && r.right <= box.right}""")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
    layout = hud_layout(page)
    assert '.evo-dialogue' in layout['shown'] and not layout['overlaps'] and not layout['outside'], layout
    assert not errors, errors
    page.screenshot(path=str(Path(screenshot_dir) / f'evolution-compute-stall-{width}.png'))
    handoff.click()
    expect(page.locator('body')).to_have_attribute('data-workspace', 'chat', timeout=10000)
    assert len(ensured) == 1 and ensured[0], ensured
    assert not writes, 'the stalled job is never resubmitted or cancelled from the studio'


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('width', [320, 390, 768, 1024, 1536])
def test_studio_briefing_themes_draft_and_focus(page, live_server, screenshot_dir, theme, width):
    """The new-goal briefing stays legible, keyboard usable and read-only until dispatch."""
    from playwright.sync_api import expect
    from tests.evolution.test_evolution_office_browser import CONTRAST
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
    expect(page.locator('.evo-game')).to_have_attribute('data-window', 'briefing')
    expect(page.locator('.evo-seed svg')).to_have_count(3)
    expect(page.locator('.evo-meters')).to_be_hidden()
    contrast = page.evaluate(CONTRAST, ['#evo-window-title', '#evo-objective', '.evo-seed', '.evo-briefing-settings summary', '.evo-dispatch'])
    assert all(item['ratio'] >= 4.5 for item in contrast), contrast
    assert page.locator('.evo-seed').evaluate_all('(seeds) => seeds.every(seed => seed.scrollWidth <= seed.clientWidth + 1)')
    page.locator('[data-example=research]').click()
    expect(field).not_to_have_value('')
    expect(field).to_be_focused()
    draft = field.input_value()
    expect(page.locator('[data-example=build]')).to_be_disabled()
    page.locator('[data-studio-action=refresh]').evaluate('(el) => el.click()')
    expect(field).to_have_value(draft)
    # Closing the briefing to look at the world keeps the unsent draft.
    page.keyboard.press('Escape')
    expect(page.locator('.evo-window')).to_be_hidden()
    page.locator('.evo-slot[data-slot=create]').click()
    expect(field).to_be_visible()
    expect(field).to_have_value(draft)
    assert not writes
    assert not errors
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate('''() => {
      const panel = document.querySelector('.evo-window').getBoundingClientRect();
      const submit = document.querySelector('.evo-dispatch').getBoundingClientRect();
      return submit.left >= panel.left && submit.right <= panel.right && panel.left >= 0 && panel.right <= innerWidth + 1;
    }''')
    field.fill('')
    page.screenshot(path=str(Path(screenshot_dir) / f'evolution-briefing-{theme}-{width}.png'), full_page=True)
