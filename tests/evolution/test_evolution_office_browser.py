"""Real browser rendering/movement/resource checks for the native office scene."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def office_preview():
    import socket
    import subprocess
    import time
    import urllib.request
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    process=subprocess.Popen(['node','node_modules/vite/bin/vite.js','--config',
        'frontend/src/features/evolution/testing/vite.config.mjs','--host','127.0.0.1',
        '--port',str(port),'--strictPort'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base=f'http://127.0.0.1:{port}/preview.html'
    try:
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError('Preview exited')
            try:
                urllib.request.urlopen(base,timeout=.2).close();break
            except OSError:time.sleep(.1)
        yield base
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


@pytest.mark.parametrize('width',[390,1536])
def test_human_sprites_walk_pause_and_keyboard(browser,office_preview,width):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        scene=page.locator('.evo-office-live');expect(scene).to_have_attribute('data-sprites','ready')
        scene.scroll_into_view_if_needed()
        people=page.locator('.evo-person');expect(people).to_have_count(3)
        person=people.first
        before=person.get_attribute('style')
        rect=scene.bounding_box()
        # Open floor, clear of furniture and of every actor's feet-anchored
        # button (actors fill relatively more of a viewport-fitted stage).
        scene.click(position={'x':rect['width']*.56,'y':rect['height']*.68})
        page.wait_for_function("old=>document.querySelector('.evo-person').getAttribute('style')!==old",arg=before)
        first_frame=scene.locator('canvas').evaluate('(c)=>c.toDataURL()')
        page.wait_for_timeout(220)
        assert scene.locator('canvas').evaluate('(c)=>c.toDataURL()')!=first_frame
        page.locator('.evo-scene-pause').click()
        expect(scene).to_have_attribute('data-paused','true')
        stopped=person.get_attribute('style');page.wait_for_timeout(250)
        assert person.get_attribute('style')==stopped
        # Paused/reduced motion retains keyboard movement, without animated frames.
        person.focus();person.press('ArrowDown')
        assert person.get_attribute('style')!=stopped
        # Furniture is not a valid movement target.
        before=person.get_attribute('style')
        # Furniture now has a sibling navigation hit target: a real pointer
        # click opens it, while leaving the paused actor in place.
        rect=scene.bounding_box()
        page.mouse.click(rect['x']+rect['width']*.22,rect['y']+rect['height']*.26)
        assert person.get_attribute('style')==before
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        output=Path('output/evolution-design/v2');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/f'walking-{width}.png'),full_page=True)
        page.keyboard.press('Escape')
        page.evaluate("document.documentElement.dataset.theme='dark'")
        assert page.locator('.evo-studio').evaluate("el=>getComputedStyle(el).colorScheme")=='light'
        expect(page.locator('#preview-theme, #preview-state, .evo-preview-tools')).to_have_count(0)
        expect(people.first).to_be_visible()
        assert not errors
    finally:context.close()


def test_reduced_motion_and_offscreen_canvas_budget(browser,office_preview):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900},reduced_motion='reduce')
    page=context.new_page()
    try:
        page.goto(office_preview);scene=page.locator('.evo-office-live')
        expect(scene).to_have_attribute('data-sprites','ready')
        expect(scene).to_have_attribute('data-paused','true')
        canvas=scene.locator('canvas')
        assert canvas.evaluate('(c)=>c.width*c.height')<=1440*1080
        page.evaluate("""() => {
          window.officePaints=0;
          const original=CanvasRenderingContext2D.prototype.clearRect;
          CanvasRenderingContext2D.prototype.clearRect=function(...args){
            if(this.canvas.closest('.evo-office-live'))window.officePaints++;
            return original.apply(this,args);
          };
        }""")
        page.wait_for_timeout(250);assert page.evaluate('window.officePaints')==0
        page.locator('.evo-scene-pause').click()
        scene.scroll_into_view_if_needed();rect=scene.bounding_box()
        scene.click(position={'x':rect['width']*.78,'y':rect['height']*.55})
        page.evaluate('window.officePaints=0');page.wait_for_timeout(1000)
        paints=page.evaluate('window.officePaints');assert 5<=paints<=32
        # IntersectionObserver must stop the renderer, even with a path pending.
        page.evaluate("document.querySelector('.evo-office').style.transform='translateY(3000px)'")
        page.wait_for_timeout(100);before=page.evaluate('window.officePaints')
        page.wait_for_timeout(250);assert page.evaluate('window.officePaints')==before
        # Route teardown releases the backing store of the old canvas.
        page.evaluate("window.oldOfficeCanvas=document.querySelector('.evo-office-live canvas');location.hash='/evolution/capabilities'")
        expect(scene).to_have_count(0)
        assert page.evaluate('window.oldOfficeCanvas.width')==0
    finally:context.close()


def test_swarm_visitors_enter_through_door(browser,office_preview):
    """The executing goal's live sub-agents enter the office through the door."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        expect(page.locator('.evo-office-live')).to_have_attribute('data-sprites','ready')
        expect(page.locator('.evo-visitor')).to_have_count(2)
        assert page.locator('.evo-door').count()==1
        expect(page.locator('.evo-visitor .evo-person-label').first).to_have_text('researcher')
        assert not errors
    finally:context.close()


def test_workstation_chair_and_role_silhouettes_render_in_browser(browser,office_preview):
    """The chair is a visible, keyboard-activatable prop and roles keep their shapes."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1536,'height':1024})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        scene=page.locator('.evo-office-live')
        expect(scene).to_have_attribute('data-sprites','ready')
        page.wait_for_function("""() => ['executor','reviewer','coordinator','researcher','engineer','chair'].every(role =>
          performance.getEntriesByType('resource').some(entry => entry.name.includes('game-'+role+'.webp') && !entry.name.includes('?import')))""")
        resources=page.evaluate("performance.getEntriesByType('resource').map(entry=>({name:entry.name,initiator:entry.initiatorType}))")
        decoded=[entry['name'].split('/')[-1].split('?')[0] for entry in resources if '.webp' in entry['name'] and '?import' not in entry['name']]
        assert not [entry for entry in resources if 'people.png' in entry['name'] and '?import' not in entry['name']], 'The old atlas must remain a failure-only fallback'
        assert 'game-writer.webp' not in decoded and 'game-analyst.webp' not in decoded
        assert page.locator('[data-actor="0"]').get_attribute('data-model')=='executor'
        assert page.locator('[data-actor="1"]').get_attribute('data-model')=='reviewer'
        assert page.locator('[data-actor="2"]').get_attribute('data-model')=='coordinator'
        assert [item.get_attribute('data-model') for item in page.locator('.evo-visitor').all()]==['researcher','engineer']
        chair=scene.locator('[data-chair="0"]')
        assert 'Sit or stand' in chair.get_attribute('aria-label')
        page.wait_for_function("document.querySelector('[data-actor=\"0\"]').dataset.seated==='true'",timeout=15000)
        chair.focus();chair.press('Enter')
        page.wait_for_function("document.querySelector('.evo-office-live').dataset.chairActions.startsWith('roll-out')",timeout=5000)
        page.wait_for_function("document.querySelector('[data-actor=\"0\"]').dataset.seated==='false' && document.querySelector('.evo-office-live').dataset.chairs==='in,in'",timeout=10000)
        chair.click()
        page.wait_for_function("document.querySelector('.evo-office-live').dataset.chairActions.startsWith('pull')",timeout=10000)
        page.wait_for_function("document.querySelector('[data-actor=\"0\"]').dataset.seated==='true' && document.querySelector('.evo-office-live').dataset.chairs==='in,in'",timeout=10000)
        chair.blur()
        page.mouse.move(8,8)
        output=Path('output/evolution-design/live-office');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/'chair-interaction.png'),full_page=True)
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_live_board_and_role_rooms_are_visible_without_a_drawer(browser,office_preview,width):
    """The scene itself exposes backend facts and the roster gives distinct rooms."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        board=page.locator('.evo-live-ledger')
        expect(board).to_be_visible()
        expect(board.locator('.evo-ledger-head strong')).to_contain_text('Agent 长期记忆')
        expect(page.locator('[data-fact=materials]')).to_contain_text('3')
        expect(page.locator('[data-fact=agents]')).to_contain_text('2')
        expect(page.locator('[data-fact=usage]')).to_contain_text('18,420 tokens')
        expect(page.locator('[data-fact=usage]')).to_contain_text('不设上限')
        assert page.locator('wa-drawer[open]').count()==0
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        output=Path('output/evolution-design/live-office');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/f'board-{width}.png'),full_page=True)
        if width==1536:
            # Observe entry/decode, not only the already-settled initial pose.
            page.evaluate("""() => {
              window.podPaints=0;
              const original=CanvasRenderingContext2D.prototype.clearRect;
              CanvasRenderingContext2D.prototype.clearRect=function(...args){
                if(this.canvas.closest('.evo-office-pod'))window.podPaints++;
                return original.apply(this,args);
              };
            }""")
        tabs=page.locator('.evo-room-tab')
        if width<=850:page.locator('.evo-rooms-open').click()
        tabs.nth(1).click()
        pod=page.locator('.evo-office-pod')
        expect(pod).to_have_attribute('data-theme','research')
        expect(pod).to_have_attribute('data-art','ready')
        research=pod.locator('canvas').evaluate('(canvas)=>canvas.toDataURL()')
        page.screenshot(path=str(output/f'research-{width}.png'),full_page=True)
        if width==1536:
            assert page.evaluate('window.podPaints')>0
            page.evaluate('window.podPaints=0')
            # Identical seated snapshots need no paint. Late asset decodes may
            # still invalidate the surface, but must not start a perpetual ticker.
            page.wait_for_timeout(1200)
            assert 0<=page.evaluate('window.podPaints')<=4
        if width<=850:page.locator('.evo-rooms-open').click()
        tabs.nth(2).click()
        expect(pod).to_have_attribute('data-theme','engineering')

        if width<=850:
            page.wait_for_function("""() => {
                const r=document.querySelector('.evo-office-pod').getBoundingClientRect();
                return r.top < innerHeight && r.bottom > 0;
            }""")
        expect(pod).to_have_attribute('data-art','ready')
        assert pod.locator('canvas').evaluate('(canvas)=>canvas.toDataURL()')!=research
        page.screenshot(path=str(output/f'engineering-{width}.png'),full_page=True)
        if width==1536:
            tabs.first.click()
            page.evaluate('window.podPaints=0')
            page.wait_for_timeout(700)
            assert page.evaluate('window.podPaints')==0
        assert not errors
    finally:context.close()


def test_pod_status_change_walks_to_chair_and_settles(browser,office_preview):
    """A real roster transition moves the actor and chair, then stops painting."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        page.wait_for_function('typeof window.__mountEvolutionPreviewPod==="function"')
        page.evaluate("""() => {
          const host=document.createElement('div');
          host.id='evo-pod-test';host.className='evo-office-pod';
          Object.assign(host.style,{position:'fixed',inset:'0 auto auto 0',width:'960px',height:'720px',zIndex:'100'});
          document.body.append(host);
          window.testPod=window.__mountEvolutionPreviewPod(host,{copy:key=>key,openTask:()=>{}});
          window.testPod.update({id:'scout',role:'researcher',status:'pending'},{sprite:3,accent:0});
        }""")
        pod=page.locator('#evo-pod-test');expect(pod).to_have_attribute('data-art','ready')
        person=pod.locator('.evo-pod-person')
        initial=person.evaluate('(el)=>parseFloat(el.style.left)')
        assert initial>80
        page.evaluate("window.testPod.update({id:'scout',role:'researcher',status:'running'},{sprite:3,accent:0})")
        page.wait_for_function("parseFloat(document.querySelector('#evo-pod-test .evo-pod-person').style.left)<75")
        page.wait_for_function("Math.abs(parseFloat(document.querySelector('#evo-pod-test .evo-pod-person').style.left)-50)<1",timeout=5000)
        page.evaluate("window.testPod.update({id:'scout',role:'researcher',status:'completed'},{sprite:3,accent:0})")
        page.wait_for_function("parseFloat(document.querySelector('#evo-pod-test .evo-pod-person').style.left)>65",timeout=5000)
        expect(pod).to_have_attribute('data-chair-beat','',timeout=5000)
        page.evaluate("""() => {
          window.podTestPaints=0;
          const old=CanvasRenderingContext2D.prototype.clearRect;
          CanvasRenderingContext2D.prototype.clearRect=function(...args){
            if(this.canvas.closest('#evo-pod-test'))window.podTestPaints++;
            return old.apply(this,args);
          };
        }""")
        page.wait_for_timeout(350)
        assert page.evaluate('window.podTestPaints')==0
        assert not errors
    finally:
        page.evaluate("window.testPod?.dispose();document.querySelector('#evo-pod-test')?.remove()")
        context.close()


def test_task_stream_research_route_tracks_overlapping_calls(browser,office_preview):
    """Only real task tool frames move the actor; a completed sibling call stays visible."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    page=context.new_page()
    page.add_init_script('''(() => {
      window.EventSource=class {
        constructor(url){this.url=url;window.officeStream=this;}
        close(){}
      };
    })()''')
    try:
        page.goto(office_preview)
        scene=page.locator('.evo-office-live')
        expect(scene).to_have_attribute('data-sprites','ready')
        page.wait_for_function('!!(window.officeStream && window.officeStream.onmessage)')
        books=scene.get_attribute('data-books')
        def emit(kind,tool,id,seq):
            page.evaluate('''({kind,tool,id,seq}) => window.officeStream.onmessage({
              data:JSON.stringify({type:kind,toolName:tool,toolCallId:id}),lastEventId:String(seq)
            })''',{'kind':kind,'tool':tool,'id':id,'seq':seq})
        emit('tool_start','web_search','search-1',1)
        bubble=page.locator('.evo-person[data-actor="0"] .evo-person-bubble')
        expect(bubble).to_have_attribute('data-kind','research')
        emit('tool_start','fetch_url','fetch-2',2)
        emit('tool_complete','web_search','search-1',3)
        expect(bubble).to_be_visible()
        emit('tool_complete','fetch_url','fetch-2',4)
        expect(bubble).to_be_hidden()
        assert scene.get_attribute('data-books')==books
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_immersive_components_focus_and_goal_drafts(browser,office_preview,width):
    """The game fills the page; secondary controls preserve focus and input islands."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    page=context.new_page();external=[];errors=[]
    page.on('request',lambda req:external.append(req.url) if not req.url.startswith('http://127.0.0.1:') else None)
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview);expect(page.locator('.evo-office-live')).to_have_attribute('data-sprites','ready')
        assert page.locator('wa-drawer[open]').count()==0
        # Navigation and telemetry have reserved space; neither covers the scene.
        assert page.evaluate('''() => {
          const office=document.querySelector('.evo-office').getBoundingClientRect();
          const hud=document.querySelector('.evo-live-ledger').getBoundingClientRect();
          const shelf=document.querySelector('.evo-bottom-chrome').getBoundingClientRect();
          return office.width>300&&office.height>300&&office.top>=hud.bottom&&office.bottom<=shelf.top;
        }''')
        # Desktop contains the panorama. Phones keep actors legible and pan the
        # stage horizontally inside the office without scrolling the document.
        geometry = page.evaluate('''() => {
          const office=document.querySelector('.evo-office');
          const box=office.getBoundingClientRect();
          const stage=document.querySelector('.evo-office-stage').getBoundingClientRect();
          const noScroll=office.scrollWidth<=office.clientWidth&&office.scrollHeight<=office.clientHeight;
          const inside=stage.left>=box.left&&stage.right<=box.right&&stage.top>=box.top&&stage.bottom<=box.bottom;
          return {noScroll,inside,office:{width:box.width,height:box.height,scrollWidth:office.scrollWidth,scrollHeight:office.scrollHeight,clientWidth:office.clientWidth,clientHeight:office.clientHeight},stage:{left:stage.left,right:stage.right,top:stage.top-box.top,bottom:stage.bottom-box.top}};
        }''')
        if width<=650:
            assert geometry['office']['scrollWidth']>geometry['office']['clientWidth'],geometry
            assert 0<=geometry['stage']['top']<geometry['stage']['bottom']<=geometry['office']['height'],geometry
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            office=page.locator('.evo-office')
            initial=office.evaluate('(el)=>el.scrollLeft')
            assert initial>0
            office.evaluate('(el)=>el.scrollLeft=0')
            assert office.evaluate('(el)=>el.scrollLeft')==0
            office.evaluate('(el)=>el.scrollLeft=el.scrollWidth-el.clientWidth')
            assert office.evaluate('(el)=>el.scrollLeft')>initial
            office.evaluate('(el,value)=>el.scrollLeft=value',initial)
        else:
            assert geometry['noScroll'] and geometry['inside'], geometry
        trigger=page.locator('.evo-place-shortcut[data-place=plan]');trigger.click()
        drawer=page.locator('#evo-room-drawer');expect(drawer).to_have_attribute('open','')
        expect(page.locator('.evo-room-objective')).to_be_visible()
        page.keyboard.press('Escape');expect(drawer).not_to_have_attribute('open','')
        expect(trigger).to_be_focused()
        # Goal switching lives in the menu drawer since the studio has no top bar.
        menu=page.locator('wa-button[data-studio-action=menu]')
        menu.click();page.locator('.evo-goal-switcher a').click();field=page.locator('#evo-objective')
        field.fill('Keep this draft across closing the drawer')
        page.keyboard.press('Escape');expect(field).not_to_be_visible()
        menu.click();page.locator('.evo-goal-switcher a').click();expect(field).to_have_value('Keep this draft across closing the drawer')
        page.keyboard.press('Escape')
        menu.click();page.locator('#evo-goal').select_option('csv-export');page.keyboard.press('Escape')
        delivery=page.locator('.evo-place-shortcut[data-place=delivery]');delivery.click()
        page.locator('[data-room-action=delivery]').click()
        expect(page.locator('#evo-deliverable')).to_be_visible()
        page.keyboard.press('Escape');expect(delivery).to_be_focused()
        page.locator('[data-studio-action=menu]').click()
        expect(page.locator('.evo-nav a[href="#/evolution/capabilities"]')).to_be_visible()
        page.keyboard.press('Escape')
        assert not external, 'All component assets, including system icons, must stay local'
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_room_directory_survives_empty_roster_and_switches_with_keyboard(browser,office_preview,width):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':844})
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=empty')
        expect(page.locator('.evo-office-live')).to_have_attribute('data-sprites','ready')
        def open_rooms():
            if width<=850:page.locator('.evo-rooms-open').click()
        open_rooms()
        tabs=page.locator('.evo-room-tab');expect(tabs).to_have_count(6)
        for room in ['research','engineering','review','writing','general']:
            target=page.locator(f'.evo-room-tab[data-room="{room}"]')
            target.focus();target.press('Enter')
            expect(page.locator('.evo-office')).to_have_attribute('data-room-id',room)
            expect(page.locator('.evo-office-pod')).to_have_attribute('data-art','ready')
            expect(page.locator('.evo-pod-person')).to_be_hidden()
            expect(page.locator('.evo-pod-status')).to_have_text('暂无成员')
            expect(page.locator('.evo-pod-objective')).to_be_visible()
            if width<=850:
                expect(page.locator('#evo-rooms-drawer')).not_to_have_attribute('open','')
                expect(page.locator('.evo-rooms-open')).to_be_focused()
            open_rooms()
        tabs.first.click()
        expect(page.locator('.evo-office-live')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors
    finally:context.close()


def test_room_occupancy_reconciles_without_losing_room_or_focused_member(browser,office_preview):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    page=context.new_page()
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        page.locator('.evo-room-tab[data-room=research]').click()
        agents=[{'id':'scout-a','role':'researcher','status':'running','objective':'Research A'},
                {'id':'scout-b','role':'researcher','status':'pending','objective':'Research B'}]
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        member=page.locator('.evo-room-members [data-member=scout-b]');expect(member).to_be_visible();member.click()
        expect(page.locator('.evo-pod-objective')).to_have_text('Research B')
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        expect(member).to_be_focused()
        agents[1]['status']='running'
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        expect(page.locator('.evo-office-pod')).to_have_attribute('data-status','running')
        expect(member).to_be_focused()
        expect(page.locator('.evo-pod-objective')).to_have_text('Research B')
        page.evaluate('window.__setEvolutionPreviewRoster([])')
        expect(page.locator('.evo-office')).to_have_attribute('data-room-id','research')
        expect(page.locator('.evo-pod-person')).to_be_hidden()
        expect(page.locator('.evo-pod-status')).to_have_text('暂无成员')
        page.evaluate('window.__setEvolutionPreviewRoster([],false)')
        expect(page.locator('.evo-pod-status')).to_have_text('成员状态未知')
        # Empty rooms never run an animation timer; hidden main-room frames stop too.
        page.wait_for_timeout(300)
        page.evaluate('''() => {
          window.emptyPaints=0;const old=CanvasRenderingContext2D.prototype.clearRect;
          CanvasRenderingContext2D.prototype.clearRect=function(...args){window.emptyPaints++;return old.apply(this,args);};
        }''')
        page.wait_for_timeout(700);assert page.evaluate('window.emptyPaints')==0
    finally:context.close()


def test_office_runtime_deterministic_render_cost(browser,office_preview):
    """CPU submission cost, not GPU/display FPS; exact 60 Hz virtual-time workload."""
    import json
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':1000},reduced_motion='reduce')
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    source=Path(__file__).resolve().parents[2]/'frontend/src/features/evolution/office-scene.ts'
    try:
        page.goto(office_preview.replace('/preview.html','/office-runtime.html'))
        page.evaluate('''async moduleUrl => {
          const {mountOfficeScene}=await import(moduleUrl);
          const root=document.createElement('section');root.id='runtime-sample';
          root.style.cssText='position:fixed;inset:0;z-index:10000;background:white;overflow:auto';
          const host=document.createElement('div'),controls=document.createElement('div');
          host.style.cssText='position:relative;width:960px;height:720px';
          root.append(host,controls);document.body.append(root);
          const scene=mountOfficeScene(host,controls,{copy:key=>key,openTask:()=>{},askHuman:()=>{}});
          const agents=Array.from({length:8},(_,i)=>({id:'sample-'+i,role:'researcher',status:'running'}));
          scene.update([],{agents});
          window.runtimeSample={root,host,controls,scene,agents};
        }''',f'/@fs/{source}')
        expect(page.locator('#runtime-sample canvas')).to_be_visible()
        page.wait_for_function("window.runtimeSample.host.dataset.sprites==='ready'")
        result=page.evaluate('''() => {
          const {host,controls,scene,agents}=window.runtimeSample;
          scene.update([],{agents:[]});
          const oldRaf=window.requestAnimationFrame,oldCancel=window.cancelAnimationFrame;
          const frames=new Map(),costs=[],poses=new Set();let seq=0,now=0,callbacks=0,peakPending=0;
          const canvas=host.querySelector('canvas'),ctx=canvas.getContext('2d'),drawImage=ctx.drawImage;
          let spriteDraws=0;
          window.requestAnimationFrame=fn=>{frames.set(++seq,fn);peakPending=Math.max(peakPending,frames.size);return seq;};
          window.cancelAnimationFrame=id=>frames.delete(id);
          ctx.drawImage=function(...args){spriteDraws++;return drawImage.apply(this,args);};
          const drain=()=>{
            for(let i=0;frames.size&&i<5000;i++){
              now+=1000/60;const batch=[...frames.values()];frames.clear();
              for(const fn of batch){const start=performance.now();fn(now);costs.push(performance.now()-start);callbacks++;}
              poses.add(host.querySelector('[data-actor="0"]').dataset.seatAmount);
            }
            if(frames.size)throw new Error('runtime sample exceeded 5000-frame bound');
          };
          try{
            controls.querySelector('.evo-scene-pause').click();
            const start=scene.metrics();
            const roles=['Execute','Review','Lead'].map(name=>({name,state:'working',working:true,waiting:false,taskId:'sample'}));
            scene.update(roles,{agents});drain();
            const working=scene.metrics();
            const seated=host.querySelector('[data-actor="0"]').dataset.seated;
            scene.update(roles.map(role=>({...role,working:false,waiting:true})),{agents});drain();
            const active=scene.metrics(),idleStart=performance.now();
            for(let i=0;i<100;i++)scene.update(roles.map(role=>({...role,working:false,waiting:true})),{agents});
            const idleUpdateMs=performance.now()-idleStart,idle=scene.metrics();
            // A hidden update must not schedule any frames, even when visitors depart.
            Object.defineProperty(document,'hidden',{configurable:true,value:true});
            document.dispatchEvent(new Event('visibilitychange'));scene.update(roles,{agents:[]});
            const hiddenPending=frames.size;
            scene.dispose();const disposed=scene.metrics();
            costs.sort((a,b)=>a-b);
            const sum=costs.reduce((a,b)=>a+b,0);
            return {workload:'3 roles + 8 visitors; work/sit then stand/wait; 100 unchanged snapshots',
              callbacks,simulatedMs:now,peakPending,seatPoseSamples:poses.size,seated,
              callbackCpuMs:{total:sum,mean:sum/costs.length,p95:costs[Math.floor(costs.length*.95)],max:costs.at(-1)},
              start,working,active,idle,idleUpdateMs,spriteDraws,hiddenPending,disposed,disposedPending:frames.size};
          }finally{
            delete document.hidden;scene.dispose();window.requestAnimationFrame=oldRaf;window.cancelAnimationFrame=oldCancel;ctx.drawImage=drawImage;
          }
        }''')
        output=Path('output/evolution-design/runtime');output.mkdir(parents=True,exist_ok=True)
        (output/'deterministic-render-cost.json').write_text(json.dumps(result,indent=2))
        assert 0<result['callbacks']<5000
        assert result['peakPending']==1
        assert result['seatPoseSamples']>5 and result['seated']=='true'
        assert result['working']['actors']==11
        assert result['active']['paints']-result['start']['paints']<=result['simulatedMs']/1000*30+4
        assert result['idle']['paints']==result['active']['paints']
        assert result['idle']['skippedPaints']-result['active']['skippedPaints']==100
        assert result['idle']['pendingFrames']==result['hiddenPending']==result['disposedPending']==0
        assert result['disposed']['backingPixels']==0
        # Loose regression guard; reported raw timings are evidence, not a display-FPS claim.
        assert result['callbackCpuMs']['p95']<50
        assert not errors
        print('runtime cost sample: '+json.dumps(result,separators=(',',':')))
    finally:context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_current_experiment_rejects_foreign_run_snapshot_in_browser(browser, office_preview, width):
    context = browser.new_context(viewport={'width':width,'height':900})
    page = context.new_page()
    try:
        page.goto(office_preview)
        module_path = '/@fs/' + str(Path('frontend/src/features/evolution/programs/current-experiment.ts').resolve())
        result = page.evaluate('''async path => {
            const {mountCurrentExperiment} = await import(path);
            const host = document.createElement('section'); document.body.append(host);
            const opened = [];
            const view = mountCurrentExperiment(host,{copy:key=>key,hypothesis:()=>null,
                replan:()=>document.createElement('details'),openTask:id=>opened.push(id)});
            const program = {program_id:'owned-program',status:'active',current:{sequence:2,
                run_id:'new-run',step:{kind:'experiment',title:'Current experiment',checks:['Inspect current evidence']}}};
            view.update(program,{run_id:'old-run',status:'completed',task_id:'old-task',actuals_complete:false});
            const foreign = {text:host.textContent,buttonHidden:host.querySelector('button').hidden};
            view.update(program,{run_id:'new-run',status:'reviewing',task_id:'current-task'});
            host.querySelector('button').click();
            const current = {text:host.textContent,opened};
            view.update(program,{run_id:'old-run',status:'completed',task_id:'old-task'},'Read unavailable');
            const unavailable = {text:host.textContent,buttonHidden:host.querySelector('button').hidden};
            view.dispose();host.remove();return {foreign,current,unavailable};
        }''', module_path)
        assert 'preparing' in result['foreign']['text']
        assert 'run.completed' not in result['foreign']['text']
        assert 'usageIncomplete' not in result['foreign']['text']
        assert result['foreign']['buttonHidden']
        assert 'run.reviewing' in result['current']['text']
        assert result['current']['opened'] == ['current-task']
        assert 'runUnavailable' in result['unavailable']['text']
        assert 'runRetained' not in result['unavailable']['text']
        assert result['unavailable']['buttonHidden']
    finally:
        context.close()


def test_program_detail_request_starts_before_catalog_finishes_in_browser(browser, office_preview):
    context = browser.new_context();page = context.new_page()
    try:
        page.goto(office_preview)
        module_path = '/@fs/' + str(Path('frontend/src/features/evolution/programs/controller.ts').resolve())
        observed = page.evaluate('''async path => {
            const {createProgramController} = await import(path);
            const requests = [], states = [];let release;
            const program = id => ({program_id:id,status:'active',current:null,settled_through:0});
            const api = {listPrograms:()=>new Promise(resolve=>release=resolve),
                listRunModels:async()=>({models:[]}),getProgram:async(id,signal)=>{
                    requests.push({id,signal});return {program:program(id)};},
                getProgramExperiments:async()=>({experiments:[],settled_through:0}),
                getProgramMemories:async()=>({memory:{entries:[],retained_limit:32}})};
            const controller = createProgramController(api,{update:s=>states.push(s),
                visible:()=>true,schedule:()=>1,cancel:()=>{}});
            try {
                controller.select('a');const flight=controller.refresh();
                await new Promise(resolve=>setTimeout(resolve,0));
                const before = requests.map(r=>r.id);
                controller.select('b');const aborted=requests[0].signal.aborted;
                release({programs:[program('stale')]});await flight;
                await new Promise(resolve=>setTimeout(resolve,0));
                const current=controller.refresh();release({programs:[program('b')]});await current;
                return {before,aborted,requests:requests.map(r=>r.id),
                    selected:states.at(-1).program.program_id,
                    stale:states.some(s=>s.program?.program_id==='a')};
            } finally {controller.dispose();}
        }''', module_path)
        assert observed == {'before':['a'],'aborted':True,'requests':['a','b'],'selected':'b','stale':False}
    finally:
        context.close()


@pytest.mark.parametrize('width',[390,1280])
def test_required_original_review_retry_notice_is_visible_and_distinct(browser, office_preview, width):
    from playwright.sync_api import expect
    root = Path(__file__).resolve().parents[2]
    context = browser.new_context(viewport={'width':width,'height':900})
    page = context.new_page()
    errors = []
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(office_preview)
        page.evaluate("""async ({modulePath,localePath}) => {
            const {mountGoalRunInspector}=await import(modulePath);
            const locale=await (await fetch(localePath)).json();
            const copy=key=>locale['evolution.studio.'+key]||key;
            const host=document.createElement('div');host.id='required-original-inspector';
            document.body.prepend(host);
            const inspector=mountGoalRunInspector(host,{copy,openTask(){},detailed:true});
            const receipt={task_id:'uninspected-review',stage:'reviewing',terminal_verdict:'completed',outcome_available:true,
                actuals:{tokens:30,tool_calls:0,wall_clock_seconds:1},
                review_format_retry:{error:'originals_uninspected',admitted:true,intent_id:'same-executor',refusal:''}};
            const run={run_id:'proof',objective:'Audit original evidence',status:'reviewing',task_id:'replacement-review',attempt:1,
                actuals:{tokens:30,tool_calls:0,wall_clock_seconds:1},receipts:[receipt],result:'',error:'',review:null};
            inspector.update({run,loading:false});
            window.finishOriginalRetry=()=>{receipt.review_format_retry.admitted=false;receipt.review_format_retry.refusal='retry_budget';
                inspector.update({run,loading:false});};
            window.disposeOriginalInspector=()=>inspector.dispose();
        }""",{'modulePath':'/@fs/'+str(root/'frontend/src/features/evolution/studio/goal-run-inspector.ts'),
               'localePath':'/@fs/'+str(root/'frontend/src/i18n/locales/en.json')})
        notice=page.locator('#required-original-inspector [data-review-format-retry="uninspected-review"]')
        expect(notice).to_be_visible()
        expect(notice).to_contain_text('Required originals were not fully checked.')
        expect(notice).to_contain_text('A new review was scheduled')
        page.evaluate('window.finishOriginalRetry()')
        expect(notice).to_contain_text('No further review was scheduled')
        assert 'could not be read' not in notice.inner_text()
        page.evaluate('window.disposeOriginalInspector()')
        assert not errors
    finally:
        context.close()
