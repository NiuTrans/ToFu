"""Real-browser checks of the studio game screen through the source preview.

Covers what the jsdom specifications cannot: Canvas pixels where the camera
projects the world, pointer hit testing on the canvas, keyboard focus and
Escape focus return, the paint budget under reduced motion, pause, offscreen
and hidden pages, department-room scenes and route teardown. The preview is
read-only synthetic data; no production API is reachable from it.
"""
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]
EVOLUTION = ROOT / 'frontend/src/features/evolution'

# Counts stage paints (one clearRect per rendered frame) and studio renders
# (each render() writes the root's data-status once and may wake the stage for
# one frame), and keeps the deduplicated history of target data attributes.
STAGE_PROBE = """(() => {
  const probe = window.__stageProbe = {paints: 0, renders: 0, history: {}};
  const clear = CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.clearRect = function (...args) {
    if (this.canvas.classList && this.canvas.classList.contains('evo-stage-canvas')) probe.paints++;
    return clear.apply(this, args);
  };
  const remember = (key, value) => {
    const list = probe.history[key] || (probe.history[key] = []);
    if (list[list.length - 1] !== value) list.push(value);
  };
  new MutationObserver(records => {
    for (const record of records) {
      const node = record.target;
      if (!node.classList) continue;
      if (record.attributeName === 'data-status' && node.classList.contains('evo-game')) {probe.renders++;continue;}
      if (!node.classList.contains('evo-target')) continue;
      if (record.attributeName === 'style') {
        // Feet with the same frame's library box, so samples convert to world units across camera changes.
        const library = node.dataset.visitor && node.parentElement.querySelector('[data-target="place:library"]');
        if (library) remember(node.dataset.target + ' foot', [parseFloat(node.style.left) + parseFloat(node.style.width) / 2,
          parseFloat(node.style.top) + parseFloat(node.style.height), parseFloat(library.style.left), parseFloat(library.style.top),
          parseFloat(library.style.width)].map(value => value.toFixed(1)).join(','));
      } else remember(node.dataset.target + ' ' + record.attributeName.slice(5), node.getAttribute(record.attributeName));
    }
  }).observe(document, {subtree: true, attributes: true,
    attributeFilter: ['data-status', 'data-chair', 'data-phase', 'data-seated', 'data-beat', 'data-activity', 'style']});
})()"""

QUIET = """async ({quiet, timeout}) => {
  const probe = window.__stageProbe, end = performance.now() + timeout;
  let last = probe.paints, since = performance.now();
  while (performance.now() < end) {
    await new Promise(resolve => setTimeout(resolve, 50));
    if (probe.paints !== last) {last = probe.paints;since = performance.now();}
    else if (performance.now() - since >= quiet) return true;
  }
  return false;
}"""

# World->screen mapping from one furniture target: layout data is the source of
# truth for world units, the target button is the camera's projection of it.
WORLD = """async layoutPath => {
  const layout = await import(layoutPath);
  const box = document.querySelector('.evo-target[data-target="place:library"]').getBoundingClientRect();
  const [x, y, width] = layout.HOTSPOTS.library.rect;
  const scale = box.width / width;
  return {scale, left: box.left - x * scale, top: box.top - y * scale, start: layout.PLAYER_START,
    desks: layout.VISITOR_DESKS, door: layout.DOOR_POINT, sprite: layout.SPRITE_HEIGHT, library: layout.HOTSPOTS.library.rect};
}"""

SNAP = """([name, regions]) => {
  const canvas = document.querySelector('.evo-stage-canvas'), rect = canvas.getBoundingClientRect();
  const k = canvas.width / rect.width, ctx = canvas.getContext('2d');
  const snaps = window.__pixelSnaps || (window.__pixelSnaps = {});
  snaps[name] = regions.map(([x, y, w, h]) => ctx.getImageData(Math.round((x - rect.left) * k), Math.round((y - rect.top) * k),
    Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k))).data);
}"""

# Mean absolute RGB difference (0-255) between two snapshots, region by region.
DIFF = """([a, b]) => window.__pixelSnaps[a].map((data, index) => {
  const other = window.__pixelSnaps[b][index];let sum = 0;
  for (let i = 0; i < data.length; i += 4)
    sum += Math.abs(data[i] - other[i]) + Math.abs(data[i + 1] - other[i + 1]) + Math.abs(data[i + 2] - other[i + 2]);
  return sum / (data.length / 4 * 3);
})"""

HUD_LAYOUT = """includeWindow => {
  const boxes = [];
  const selectors = ['.evo-quest', '.evo-track', '.evo-meters', '.evo-hud-actions', '.evo-rooms', '.evo-member-card',
    '.evo-dialogue', '.evo-slots', '.evo-primary-action', ...(includeWindow ? ['.evo-window'] : [])];
  for (const selector of selectors) for (const node of document.querySelectorAll(selector)) {
    const box = node.getBoundingClientRect();
    if (box.width > 0 && box.height > 0 && getComputedStyle(node).visibility !== 'hidden') boxes.push({selector, box});
  }
  const overlaps = [];
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i].box, b = boxes[j].box;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps.push([boxes[i].selector, boxes[j].selector]);
  }
  const outside = boxes.filter(({box}) => box.left < -1 || box.top < -1 || box.right > innerWidth + 1 || box.bottom > innerHeight + 1).map(item => item.selector);
  return {overlaps, outside, shown: boxes.map(item => item.selector)};
}"""

CONTRAST = """selectors => {
  const parse = value => {
    const numbers = (value.match(/-?[\\d.]+(?:e-?\\d+)?/g) || []).map(Number);
    const scale = value.startsWith('color(') ? 255 : 1;
    return {rgb: numbers.slice(0, 3).map(v => v * scale), alpha: numbers.length > 3 ? numbers[3] : 1};
  };
  const background = node => {
    const layers = [];
    for (let item = node; item; item = item.parentElement) {
      const layer = parse(getComputedStyle(item).backgroundColor);
      if (layer.alpha > 0) layers.push(layer);
      if (layer.alpha >= 1) break;
    }
    return layers.reverse().reduce((base, layer) => base.map((v, i) => v * (1 - layer.alpha) + layer.rgb[i] * layer.alpha), [255, 255, 255]);
  };
  const luminance = rgb => rgb.map(v => {v /= 255;return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;})
    .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
  return selectors.map(selector => {
    const node = document.querySelector(selector), bg = luminance(background(node)), fg = luminance(parse(getComputedStyle(node).color).rgb);
    return {selector, ratio: (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05)};
  });
}"""


def source(relative):
    return '/@fs' + str(EVOLUTION / relative)


def studio_copy(key, lang='zh'):
    return json.loads((ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text())['evolution.studio.' + key]


def stage_counts(page):
    return page.evaluate('({paints: window.__stageProbe.paints, renders: window.__stageProbe.renders})')


def paints_during(page, ms):
    """Stage paints and studio renders within ``ms`` of real time."""
    before = stage_counts(page)
    page.wait_for_timeout(ms)
    after = stage_counts(page)
    return after['paints'] - before['paints'], after['renders'] - before['renders']


def wait_quiet(page, quiet=400, timeout=15000):
    return page.evaluate(QUIET, {'quiet': quiet, 'timeout': timeout})


def world_map(page):
    return page.evaluate(WORLD, source('office-layout.ts'))


def to_screen(world, point):
    return world['left'] + point['x'] * world['scale'], world['top'] + point['y'] * world['scale']


def figure_region(world, point):
    """Screen box covering a standing character whose feet are at ``point``."""
    scale, (x, y) = world['scale'], to_screen(world, point)
    return [x - 18 * scale, y - (world['sprite'] - 6) * scale, 36 * scale, (world['sprite'] - 12) * scale]


def snap(page, name, regions):
    page.evaluate(SNAP, [name, regions])


def pixel_diff(page, a, b):
    return page.evaluate(DIFF, [a, b])


def hud_layout(page, include_window=False):
    return page.evaluate(HUD_LAYOUT, include_window)


def stable_box(page, selector):
    """Bounding box of a target once the camera (insets, follow, zoom) holds still."""
    return page.evaluate("""async selector => {
      const read = () => {const box = document.querySelector(selector).getBoundingClientRect();return {x: box.x, y: box.y, width: box.width, height: box.height};};
      let box = read();
      for (let i = 0; i < 100; i++) {
        await new Promise(resolve => setTimeout(resolve, 80));
        const next = read();
        if (['x', 'y', 'width', 'height'].every(key => Math.abs(next[key] - box[key]) < .5)) return next;
        box = next;
      }
      return box;
    }""", selector)


def uncovered_center(page, selector):
    """Viewport center of a target once the camera holds still, when the canvas itself receives a click there."""
    return page.evaluate("""async selector => {
      const center = () => {const box = document.querySelector(selector).getBoundingClientRect();return [box.left + box.width / 2, box.top + box.height / 2];};
      let [x, y] = center();
      for (let i = 0; i < 40; i++) {
        await new Promise(resolve => setTimeout(resolve, 60));
        const [nx, ny] = center();
        if (Math.abs(nx - x) < .5 && Math.abs(ny - y) < .5) break;
        [x, y] = [nx, ny];
      }
      return x > 0 && y > 0 && x < innerWidth && y < innerHeight
        && document.elementFromPoint(x, y)?.classList.contains('evo-stage-canvas') ? [x, y] : null;
    }""", selector)


# A figure (≈50% of the region) against bare floor differs by ~50/255; an
# unchanged region by ~0 even after a camera pan, so 20 separates them.
FIGURE_DIFF = 20


# The preview server neither watches files nor pushes HMR: a source edit during
# a run must not reload a page in the middle of a journey.
PREVIEW_SERVER = """import {createServer} from 'vite';
const [configFile, port] = process.argv.slice(1);
const server = await createServer({configFile, server: {host: '127.0.0.1', port: Number(port), strictPort: true, hmr: false, watch: null}});
await server.listen();"""


@pytest.fixture(scope='module')
def office_preview():
    from tests._jsdom import require_node_modules
    require_node_modules('vite')
    import socket
    import subprocess
    import time
    import urllib.request
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    process=subprocess.Popen(['node','--input-type=module','-e',PREVIEW_SERVER,
        'frontend/src/features/evolution/testing/vite.config.mjs',str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
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
def test_human_avatar_walks_on_canvas_clicks_and_keys(browser,office_preview,width):
    """Floor taps and held keys move the human's avatar over real Canvas frames; idle worlds sleep."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview+'?scenario=empty')
        root=page.locator('.evo-game')
        expect(page.locator('.evo-target[data-target="place:library"]')).to_be_attached()
        assert wait_quiet(page), 'an idle office stops painting'
        world=world_map(page);start,floor=world['start'],world['desks'][1]
        snap(page,'before',[figure_region(world,start),figure_region(world,floor)])
        target=to_screen(world,floor)
        assert page.evaluate('([x,y])=>document.elementFromPoint(x,y).className',list(target))=='evo-stage-canvas'
        before=stage_counts(page)['paints']
        page.mouse.click(*target)
        page.wait_for_timeout(200)
        assert stage_counts(page)['paints']-before>=5, 'the avatar walks over animation frames'
        assert wait_quiet(page)
        assert not root.get_attribute('data-window'), 'open floor is not a place'
        walked=world_map(page)
        if width<=760:
            # The room overflows a portrait frame: the camera follows the avatar.
            assert walked['left']<world['left']-(floor['x']-start['x'])*world['scale']*.8,(world,walked)
        else:
            assert abs(walked['left']-world['left'])<.5
        snap(page,'after',[figure_region(walked,start),figure_region(walked,floor)])
        left_start,arrived=pixel_diff(page,'before','after')
        assert left_start>FIGURE_DIFF and arrived>FIGURE_DIFF,(left_start,arrived)
        # Held movement keys steer the avatar while the stage has focus.
        stage=page.locator('.evo-stage');stage.focus()
        page.keyboard.down('ArrowLeft');page.wait_for_timeout(350);page.keyboard.up('ArrowLeft')
        assert wait_quiet(page)
        moved=world_map(page)
        snap(page,'keys',[figure_region(moved,start),figure_region(moved,floor)])
        assert pixel_diff(page,'after','keys')[1]>FIGURE_DIFF, 'the avatar left the spot it walked to'
        expect(stage).to_be_focused()
        # Escape with no window opens the menu; Escape again returns focus to the stage.
        page.keyboard.press('Escape');expect(root).to_have_attribute('data-window','menu')
        page.keyboard.press('Escape');expect(page.locator('.evo-window')).to_be_hidden();expect(stage).to_be_focused()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        layout=hud_layout(page);assert not layout['overlaps'] and not layout['outside'],layout
        output=Path('output/evolution-design/v2');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/f'walking-{width}.png'))
        assert not errors
    finally:context.close()


def test_reduced_motion_and_offscreen_canvas_budget(browser,office_preview):
    """Keyframes only while working; pause, reduced motion, offscreen and teardown own no frames."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        root,stage=page.locator('.evo-game'),page.locator('.evo-stage')
        executor=page.locator('.evo-target[data-target="actor:executor"]')
        expect(executor).to_have_attribute('data-phase','seated',timeout=15000)
        # Measured before any window moves the camera; the canvas itself returns there afterwards.
        world=world_map(page);start,floor=world['start'],world['desks'][1]
        canvas=page.locator('.evo-stage-canvas')
        backing=canvas.evaluate('(c)=>({width:c.width,height:c.height,expected:[Math.round(c.clientWidth*devicePixelRatio),Math.round(c.clientHeight*devicePixelRatio)]})')
        assert [backing['width'],backing['height']]==backing['expected'],backing
        # A seated working executor animates on its emote/screen keyframes, not a 60 Hz ticker.
        paints,renders=paints_during(page,1000)
        assert 1<=paints<=5+2*renders,(paints,renders)
        # Pausing motion (menu) settles the scene and owns no frames.
        page.locator('[data-hud=menu]').click()
        motion=page.locator('[data-studio-action=motion]');motion.click()
        expect(motion).to_have_attribute('aria-pressed','true');expect(stage).to_have_attribute('data-motion','paused')
        page.keyboard.press('Escape');expect(page.locator('.evo-window')).to_be_hidden()
        page.wait_for_timeout(150)
        paints,renders=paints_during(page,1000);assert paints<=2*renders,(paints,renders)
        page.locator('[data-hud=menu]').click();motion.click();expect(stage).to_have_attribute('data-motion','live')
        page.keyboard.press('Escape')
        # Reduced motion is honoured live through the media query.
        page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(150)
        paints,renders=paints_during(page,1000);assert paints<=2*renders,(paints,renders)
        # Keyboard movement is retained without animation frames after release.
        snap(page,'rest',[figure_region(world,start)])
        stage.focus();before=stage_counts(page)['paints']
        page.keyboard.down('ArrowLeft');page.wait_for_timeout(300)
        assert stage_counts(page)['paints']-before>=3, 'held keys render while held'
        page.keyboard.up('ArrowLeft');page.wait_for_timeout(100)
        paints,renders=paints_during(page,600);assert paints<=2*renders,(paints,renders)
        snap(page,'stepped',[figure_region(world,start)])
        assert pixel_diff(page,'rest','stepped')[0]>FIGURE_DIFF
        # A floor tap settles the avatar at once and the world sleeps again.
        snap(page,'empty-floor',[figure_region(world,floor)])
        page.mouse.click(*to_screen(world,floor));page.wait_for_timeout(120)
        snap(page,'teleported',[figure_region(world,floor)])
        assert pixel_diff(page,'empty-floor','teleported')[0]>FIGURE_DIFF
        assert wait_quiet(page,timeout=3000)
        # IntersectionObserver stops the loop even while a movement key is held.
        stage.focus();page.keyboard.down('ArrowRight');page.wait_for_timeout(100)
        page.evaluate("document.querySelector('.evo-game').style.transform='translateY(3000px)'")
        page.wait_for_timeout(150);before=stage_counts(page)['paints']
        page.wait_for_timeout(400);assert stage_counts(page)['paints']==before
        page.keyboard.up('ArrowRight')
        page.evaluate("document.querySelector('.evo-game').style.transform=''")
        page.wait_for_function('n=>window.__stageProbe.paints>n',arg=before,timeout=5000)
        # Route teardown releases the old canvas backing store.
        page.evaluate("window.oldStageCanvas=document.querySelector('.evo-stage-canvas');location.hash='/evolution/capabilities'")
        expect(root).to_have_count(0)
        assert page.evaluate('[window.oldStageCanvas.width,window.oldStageCanvas.height,window.oldStageCanvas.isConnected]')==[0,0,False]
        assert not errors
    finally:context.close()


def test_pausing_motion_also_stills_the_hud(browser,office_preview):
    """The menu's motion pause covers the HUD's CSS pulses as well as the canvas world."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1536,'height':1024})
    page=context.new_page()
    try:
        page.goto(office_preview)
        step=page.locator('.evo-track li[data-state=active] button')
        expect(step).to_be_visible()
        assert step.evaluate('el=>el.getAnimations().length')>0, 'the active stage pulses while motion is live'
        page.locator('[data-hud=menu]').click()
        page.locator('[data-studio-action=motion]').click()
        expect(page.locator('.evo-stage')).to_have_attribute('data-motion','paused')
        page.keyboard.press('Escape')
        assert step.evaluate('el=>el.getAnimations().length')==0, 'paused motion leaves no running HUD animation'
    finally:context.close()


def test_swarm_visitors_enter_through_door(browser,office_preview):
    """Live sub-agents enter through the front door, keep their role models and are hit-testable."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        visitors=page.locator('.evo-target[data-visitor]');expect(visitors).to_have_count(2)
        scout=page.locator('.evo-target[data-target="visitor:demo-scout"]')
        expect(scout).to_have_attribute('data-model','researcher')
        expect(page.locator('.evo-target[data-target="visitor:demo-coder"]')).to_have_attribute('data-model','engineer')
        assert (scout.get_attribute('aria-label') or '').startswith(studio_copy('memberTitle_research')+' · ')
        # Settled: neither visitor's foot position changed for 600 ms.
        page.wait_for_function("""() => {
          const seen = window.__visitorFeet || (window.__visitorFeet = {});
          return ['visitor:demo-scout','visitor:demo-coder'].map(id => {
            const length = (window.__stageProbe.history[id + ' foot'] || []).length;
            if (seen[id]?.length !== length) {seen[id] = {length, at: performance.now()};return false;}
            return length > 5 && performance.now() - seen[id].at > 600;
          }).every(Boolean);
        }""",timeout=20000,polling=100)
        world=world_map(page);door=world['door'];lx,ly,lw,_=world['library']
        def in_world(sample):
            x,y,left,top,width=map(float,sample.split(','));scale=width/lw
            return (x-left)/scale+lx,(y-top)/scale+ly
        for visitor in ['visitor:demo-scout','visitor:demo-coder']:
            feet=[in_world(sample) for sample in page.evaluate('id=>window.__stageProbe.history[id+" foot"]',visitor)]
            (first_x,first_y),(_,last_y)=feet[0],feet[-1]
            assert abs(first_x-door['x'])<12 and first_y>=door['y']-2,(visitor,feet[0],door)
            assert first_y-last_y>80,(visitor,feet[0],feet[-1])
        # A real pointer click on the scout's body enters its department room.
        center=uncovered_center(page,'.evo-target[data-target="visitor:demo-scout"]');assert center
        page.mouse.click(*center)
        expect(page.locator('.evo-game')).to_have_attribute('data-room','research')
        expect(page.locator('.evo-member-card strong')).to_have_text(studio_copy('memberTitle_research'))
        assert not errors
    finally:context.close()


def test_workstation_chair_and_role_silhouettes_render_in_browser(browser,office_preview):
    """Rolling chairs follow the sit beat and every live role keeps its own authored model."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1536,'height':1024})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        page.wait_for_function("""() => ['executor','reviewer','coordinator','researcher','engineer','chair'].every(role =>
          performance.getEntriesByType('resource').some(entry => entry.name.includes('game-'+role+'.webp') && !entry.name.includes('?import')))""")
        executor=page.locator('.evo-target[data-target="actor:executor"]')
        expect(executor).to_have_attribute('data-seated','true',timeout=15000)
        expect(page.locator('.evo-target[data-target="place:work"]')).to_have_attribute('data-chair','in',timeout=5000)
        resources=page.evaluate("performance.getEntriesByType('resource').map(entry=>entry.name)")
        decoded={name.split('/')[-1].split('?')[0] for name in resources if '.webp' in name and '?import' not in name}
        assert not [name for name in resources if 'people.png' in name and '?import' not in name], 'the atlas is a failure-only fallback'
        assert not {'game-analyst.webp','game-general.webp'}&decoded, decoded
        assert [page.locator(f'.evo-target[data-actor={role}]').get_attribute('data-model') for role in ['executor','reviewer','coordinator']]==['executor','reviewer','coordinator']
        history=page.evaluate('window.__stageProbe.history')
        work,review=history['place:work chair'],history['place:review chair']
        assert 'out' in work and work[-1]=='in', work  # pulled out to sit, rolled back in seated
        assert 'out' not in review and review[-1]=='in', review  # the idle reviewer's chair stays tucked
        assert history['actor:executor seated'][-1]=='true'
        page.mouse.move(8,8)
        output=Path('output/evolution-design/live-office');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/'workstations.png'))
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_hud_facts_and_role_rooms_are_visible_without_a_window(browser,office_preview,width):
    """The HUD exposes backend facts; each rostered role has its own rendered room."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        root=page.locator('.evo-game')
        expect(page.locator('.evo-quest-title')).to_contain_text('Agent 长期记忆')
        tokens=page.locator('.evo-meter[data-meter=tokens]')
        expect(tokens.locator('b')).to_contain_text('≥')  # a running stage's usage is a lower bound
        expect(tokens).to_have_attribute('aria-label',re.compile('18,420'))
        assert page.locator('.evo-meter-bar:not([hidden])').count()==0, 'no limit is invented'
        expect(page.locator('.evo-slot[data-place=library] .evo-slot-count')).to_have_text('3')
        expect(page.locator('.evo-target[data-target="place:library"]')).to_have_attribute('data-material-ids','paper,repo,web')
        expect(page.locator('.evo-target[data-visitor]')).to_have_count(2)
        for room in ['research','engineering']:
            expect(page.locator(f'.evo-room-tile[data-room={room}] .evo-room-count')).to_have_text('1')
        expect(page.locator('.evo-window')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        layout=hud_layout(page);assert not layout['overlaps'] and not layout['outside'],layout
        output=Path('output/evolution-design/live-office');output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/f'board-{width}.png'))
        if width<=760:
            page.locator('[data-hud=rooms]').click();expect(root).to_have_attribute('data-rooms-open','true')
        member=page.locator('.evo-target[data-target=member]');card=page.locator('.evo-member-card')
        for room,agent,brief in [('research','demo-scout','比较长期记忆方案的评测方法'),('engineering','demo-coder','实现可复现原型')]:
            tile=page.locator(f'.evo-room-tile[data-room={room}]');tile.click()
            expect(root).to_have_attribute('data-room',room);expect(tile).to_have_attribute('aria-pressed','true')
            expect(member).to_have_attribute('data-member',agent);expect(member).to_have_attribute('data-status','running')
            expect(member).to_have_attribute('data-seated','true')  # first sight settles immediately
            expect(card.locator('strong')).to_have_text(studio_copy('memberTitle_'+room))
            expect(card.locator('.evo-member-brief')).to_have_text(brief)
            page.wait_for_function("room=>performance.getEntriesByType('resource').some(entry=>entry.name.includes('pod-'+room+'-clear.webp'))",arg=room)
            page.wait_for_timeout(400)
            snap(page,room,[[x*width/8,y*1024/8,width/8,1024/8] for y in range(8) for x in range(8)])
            page.screenshot(path=str(output/f'{room}-{width}.png'))
            if width==1536:
                # A seated running member paints only on its emote keyframes.
                paints,renders=paints_during(page,1200);assert paints<=6+2*renders,(paints,renders)
        cells=pixel_diff(page,'research','engineering')
        assert max(cells)>FIGURE_DIFF and sum(cell>10 for cell in cells)>=2, ('each department renders its own scenery',cells)
        page.locator('.evo-room-tile[data-room=""]').click()
        expect(root).to_have_attribute('data-room','main');expect(card).to_be_hidden()
        expect(member).to_have_count(0);expect(page.locator('.evo-target[data-target="place:library"]')).to_be_attached()
        assert not errors
    finally:context.close()


def test_pod_status_change_walks_to_chair_and_settles(browser,office_preview):
    """A real roster transition plays one arrive/leave beat in the member's room, then stops painting."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    roster=lambda status:page.evaluate("status=>window.__setEvolutionPreviewRoster([{id:'scout',role:'researcher',status,objective:'Survey'}])",status)
    try:
        page.goto(office_preview)
        expect(page.locator('.evo-target[data-target="actor:executor"]')).to_be_attached()
        roster('pending')
        tile=page.locator('.evo-room-tile[data-room=research]')
        expect(tile.locator('.evo-room-count')).to_have_text('1');tile.click()
        member=page.locator('.evo-target[data-target=member]')
        expect(member).to_have_attribute('data-status','pending');expect(member).to_have_attribute('data-beat','')
        expect(member).to_have_attribute('data-seated','false')
        door=member.bounding_box()
        roster('running')
        expect(member).to_have_attribute('data-beat','arrive')
        expect(member).to_have_attribute('data-seated','true',timeout=15000);expect(member).to_have_attribute('data-beat','',timeout=5000)
        seat=member.bounding_box();assert seat['x']<door['x']-100,(door,seat)
        expect(page.locator('.evo-member-card .evo-chip')).to_have_text(studio_copy('running'))
        roster('completed')
        expect(member).to_have_attribute('data-beat','leave')
        expect(member).to_have_attribute('data-beat','',timeout=15000);expect(member).to_have_attribute('data-seated','false')
        done=member.bounding_box();assert done['x']>seat['x']+50 and done['y']>seat['y'],(seat,done)
        history=page.evaluate('window.__stageProbe.history')
        assert 'arrive' in history['member beat'] and 'leave' in history['member beat']
        page.wait_for_timeout(150)
        paints,renders=paints_during(page,400);assert paints<=2*renders,(paints,renders)
        assert not errors
    finally:context.close()


def test_task_stream_research_route_tracks_overlapping_calls(browser,office_preview):
    """Only real task tool frames move the executor; a completed sibling call keeps the current tool visible."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.add_init_script('''(() => {
      window.EventSource=class {
        constructor(url){this.url=url;window.officeStream=this;}
        close(){}
      };
    })()''')
    try:
        page.goto(office_preview)
        page.wait_for_function('!!(window.officeStream && window.officeStream.onmessage)')
        assert '/api/v4/tasks/demo-execution/stream' in page.evaluate('window.officeStream.url')
        executor=page.locator('.evo-target[data-target="actor:executor"]')
        expect(executor).to_have_attribute('data-activity','working',timeout=15000)
        library=page.locator('.evo-target[data-target="place:library"]');books=library.get_attribute('data-books')
        def emit(kind,tool,id,seq):
            page.evaluate('''({kind,tool,id,seq}) => window.officeStream.onmessage({
              data:JSON.stringify({type:kind,toolName:tool,toolCallId:id}),lastEventId:String(seq)
            })''',{'kind':kind,'tool':tool,'id':id,'seq':seq})
        emit('tool_start','web_search','search-1',1)
        expect(executor).to_have_attribute('data-tool','web_search')
        expect(executor).to_have_attribute('data-activity','research',timeout=10000)  # walked to the shelf
        emit('tool_start','fetch_url','fetch-2',2)
        expect(executor).to_have_attribute('data-tool','fetch_url')
        emit('tool_complete','web_search','search-1',3)
        expect(executor).to_have_attribute('data-tool','fetch_url')
        emit('tool_complete','fetch_url','fetch-2',4)
        expect(executor).to_have_attribute('data-tool','')
        expect(executor).to_have_attribute('data-activity','working',timeout=10000)
        expect(executor).to_have_attribute('data-seated','true')
        assert library.get_attribute('data-books')==books
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_immersive_components_focus_and_goal_drafts(browser,office_preview,width):
    """The world fills the page between HUD bars; windows return focus and keep goal drafts."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();external=[];errors=[]
    page.on('request',lambda req:external.append(req.url) if not req.url.startswith('http://127.0.0.1:') else None)
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        root,stage=page.locator('.evo-game'),page.locator('.evo-stage')
        expect(page.locator('.evo-target[data-target="actor:executor"]')).to_have_attribute('data-phase','seated',timeout=15000)
        expect(page.locator('.evo-window')).to_be_hidden()
        library=page.locator('.evo-target[data-target="place:library"]')
        base=stable_box(page,'.evo-target[data-target="place:library"]')['width']
        frame=page.evaluate('''() => {
          const box=s=>document.querySelector(s).getBoundingClientRect();
          const root=box('.evo-game'),canvas=box('.evo-stage-canvas'),top=box('.evo-hud-top'),bar=box('.evo-hotbar'),rail=box('.evo-rooms');
          const places=[...document.querySelectorAll('.evo-target[data-place]')].map(t=>t.getBoundingClientRect());
          return {fills:Math.abs(canvas.width-root.width)<1&&Math.abs(canvas.height-root.height)<1,
            between:places.every(p=>p.top>=top.bottom-1&&p.bottom<=bar.top+1),
            beside:places.every(p=>p.left>=rail.right-1&&p.right<=root.right+1)};
        }''')
        assert frame['fills'] and frame['between'],frame
        layout=hud_layout(page);assert not layout['overlaps'] and not layout['outside'],layout
        if width>760:
            assert frame['beside'],frame  # desktop frames the whole room beside the room rail
        else:
            # Phones crop the room horizontally; a drag pans it without scrolling the document.
            before=library.bounding_box()['x']
            page.mouse.move(width/2,560);page.mouse.down();page.mouse.move(width/2+80,560,steps=8);page.mouse.up()
            assert abs(library.bounding_box()['x']-before)>20
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth&&scrollX===0')
            expect(root).not_to_have_attribute('data-window',re.compile('.+'))
        # Wheel zooms around the pointer; 0 on the focused stage resets the view.
        page.mouse.move(width/2,560);page.mouse.wheel(0,-100)
        page.wait_for_function('w=>document.querySelector(\'.evo-target[data-target="place:library"]\').getBoundingClientRect().width>w*1.1',arg=base)
        stage.focus();page.keyboard.press('0')
        page.wait_for_function('w=>Math.abs(document.querySelector(\'.evo-target[data-target="place:library"]\').getBoundingClientRect().width-w)<1',arg=base)
        # A hotbar slot opens the journal tab; Escape returns focus to the slot.
        slot=page.locator('.evo-slot[data-place=plan]');slot.click()
        expect(root).to_have_attribute('data-window','journal')
        expect(page.locator('[role=tab][data-place=plan]')).to_have_attribute('aria-selected','true')
        expect(page.locator('#evo-window-title')).to_have_text(studio_copy('place_plan'))
        expect(page.locator('#evo-window-title')).to_be_focused()
        expect(page.locator('.evo-objective-text')).to_be_visible()
        page.keyboard.press('Escape');expect(page.locator('.evo-window')).to_be_hidden();expect(slot).to_be_focused()
        # Game hotkeys: 1-5 journal tabs, T chat, J journal, Escape with nothing open opens the menu.
        page.keyboard.press('Digit1');expect(page.locator('[role=tab][data-place=library]')).to_have_attribute('aria-selected','true')
        page.keyboard.press('Escape');expect(slot).to_be_focused()
        page.keyboard.press('KeyT');expect(root).to_have_attribute('data-window','chat');expect(page.locator('#evo-message')).to_be_visible()
        page.keyboard.press('Escape');page.keyboard.press('KeyJ');expect(root).to_have_attribute('data-window','journal')
        page.keyboard.press('Escape');page.keyboard.press('Escape');expect(root).to_have_attribute('data-window','menu')
        expect(page.locator('.evo-goal-row')).to_have_count(2)
        expect(page.locator('[data-studio-route="evolution/capabilities"]')).to_be_visible()
        # A new-goal draft survives switching to another goal and back to the briefing.
        page.locator('.evo-menu [data-studio-action=create]').click()
        field=page.locator('#evo-objective');expect(root).to_have_attribute('data-window','briefing');expect(field).to_be_focused()
        field.fill('Keep this draft while inspecting another mission')
        page.keyboard.press('Escape');page.keyboard.press('Escape')
        expect(root).to_have_attribute('data-window','menu')
        page.locator('.evo-goal-row[data-key=csv-export]').click()
        expect(page.locator('.evo-window')).to_be_hidden()
        expect(page.locator('.evo-quest-title')).to_have_text('修复 CSV 导出的中文编码问题')
        expect(page.locator('.evo-primary-action')).to_have_attribute('data-kind','read')
        page.keyboard.press('KeyN');expect(field).to_have_value('Keep this draft while inspecting another mission')
        page.keyboard.press('Escape')
        delivery=page.locator('.evo-slot[data-place=delivery]');delivery.click()
        expect(page.locator('#evo-deliverable')).to_be_visible()
        page.keyboard.press('Escape');expect(delivery).to_be_focused()
        assert not external, 'All studio assets, including icons, stay local'
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_canvas_pointer_hits_open_places_and_people(browser,office_preview,width):
    """Real pointer clicks hit-test furniture and residents on the canvas; E interacts after walking there."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        root,stage=page.locator('.evo-game'),page.locator('.evo-stage')
        executor=page.locator('.evo-target[data-target="actor:executor"]')
        expect(executor).to_have_attribute('data-phase','seated',timeout=15000)
        center=uncovered_center(page,'.evo-target[data-target="actor:executor"]');assert center
        page.mouse.click(*center)
        expect(root).to_have_attribute('data-window','actor')
        expect(page.locator('#evo-window-title')).to_have_text(studio_copy('executor'))
        expect(page.locator('.evo-role-models select')).to_be_visible()
        expect(executor).to_have_attribute('aria-pressed','true')
        page.keyboard.press('Escape');expect(executor).to_have_attribute('aria-pressed','false')
        # Each pointer phase starts from a fresh camera (see the target-layer test below).
        page.reload();expect(page.locator('.evo-target[data-target="actor:coordinator"]')).to_be_attached()
        center=uncovered_center(page,'.evo-target[data-target="actor:coordinator"]')
        if center:
            page.mouse.click(*center);expect(root).to_have_attribute('data-window','chat')
            page.reload();expect(page.locator('.evo-target[data-target="actor:coordinator"]')).to_be_attached()
        place,center=next((place,center) for place in ['work','plan','library','review','delivery']
                          if (center:=uncovered_center(page,f'.evo-target[data-place={place}]')))
        page.mouse.click(*center)
        expect(root).to_have_attribute('data-window','journal')
        expect(page.locator(f'[role=tab][data-place={place}]')).to_have_attribute('aria-selected','true')
        page.keyboard.press('Escape');expect(page.locator('.evo-window')).to_be_hidden();expect(stage).to_be_focused()
        # The click also walked the avatar to that furniture; E then interacts with it.
        page.locator('[role=tab][data-place=library]').evaluate('(tab)=>tab.click()')
        tab=page.locator(f'[role=tab][data-place={place}]')
        for _ in range(40):
            page.keyboard.press('KeyE')
            if root.get_attribute('data-window')=='journal':
                if tab.get_attribute('aria-selected')=='true':break
                page.keyboard.press('Escape')  # passed another furniture on the way
            page.wait_for_timeout(150)
        expect(tab).to_have_attribute('aria-selected','true')
        page.keyboard.press('Escape');expect(stage).to_be_focused()
        # Targets stay keyboard reachable and return focus after Escape.
        review=page.locator('.evo-target[data-target="place:review"]');review.focus();review.press('Enter')
        expect(page.locator('[role=tab][data-place=review]')).to_have_attribute('aria-selected','true')
        page.keyboard.press('Escape');expect(review).to_be_focused()
        assert not errors
    finally:context.close()


def test_target_layer_moves_with_the_world_when_a_window_docks(browser,office_preview):
    """Focus rings and pointer positions come from the target layer, so it must track every camera frame change."""
    from playwright.sync_api import expect
    # Wide enough that the room still fits beside a docked window: only the frame center moves.
    context=browser.new_context(viewport={'width':2560,'height':1024})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(office_preview)
        root=page.locator('.evo-game')
        executor=page.locator('.evo-target[data-target="actor:executor"]')
        expect(executor).to_have_attribute('data-phase','seated',timeout=15000)
        world=world_map(page)
        before=uncovered_center(page,'.evo-target[data-target="actor:executor"]');assert before
        snap(page,'undocked',[[before[0]-15,before[1]-20,30,40]])
        page.locator('.evo-slot[data-place=plan]').click();expect(root).to_have_attribute('data-window','journal')
        # The frame eases over a few hundred milliseconds: wait for the world to move
        # (never a fixed sleep, which races a loaded host), then for it to settle.
        page.wait_for_function("""([x, shift]) => {
          const box = document.querySelector('.evo-target[data-target="actor:executor"]').getBoundingClientRect();
          return box.left + box.width / 2 < x - shift;
        }""", arg=[before[0], world['scale']*50], timeout=15000)
        during=uncovered_center(page,'.evo-target[data-target="actor:executor"]')
        snap(page,'docked',[[before[0]-15,before[1]-20,30,40]])
        assert pixel_diff(page,'undocked','docked')[0]>FIGURE_DIFF, 'the drawn room recenters beside the docked window'
        assert during and during[0]<before[0]-world['scale']*50, (
            'the executor target must move with the drawn executor when the window docks', before, during)
        page.mouse.click(*during)
        expect(root).to_have_attribute('data-window','actor')
        assert not errors
    finally:context.close()


@pytest.mark.parametrize('width',[320,390,768,1024,1536])
def test_hud_bars_never_overlap_across_widths(browser,office_preview,width):
    """Quest banner, stage track, meters, HUD buttons, rail, dialogue and hotbar never cover each other."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':1024},reduced_motion='reduce')
    page=context.new_page()
    try:
        page.goto(office_preview)
        expect(page.locator('.evo-meter[data-meter=tokens] b')).to_contain_text('≥')
        expect(page.locator('.evo-dialogue')).to_be_visible()
        layout=hud_layout(page)
        assert not layout['overlaps'] and not layout['outside'],layout
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    finally:context.close()


@pytest.mark.parametrize('width',[390,1536])
def test_room_directory_survives_empty_roster_and_switches_with_keyboard(browser,office_preview,width):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':width,'height':844})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=empty')
        root=page.locator('.evo-game')
        expect(page.locator('.evo-target[data-target="place:library"]')).to_be_attached()
        if width<=760:
            page.locator('[data-hud=rooms]').click();expect(root).to_have_attribute('data-rooms-open','true')
        expect(page.locator('.evo-room-tile')).to_have_count(6)
        card=page.locator('.evo-member-card')
        for room,label in [('research','roomResearch'),('engineering','roomEngineering'),('review','roomReview'),
                           ('writing','roomWriting'),('general','roomGeneral')]:
            tile=page.locator(f'.evo-room-tile[data-room="{room}"]')
            tile.focus();tile.press('Enter')
            expect(root).to_have_attribute('data-room',room);expect(tile).to_have_attribute('aria-pressed','true')
            expect(card).to_be_visible();expect(card.locator('strong')).to_have_text(studio_copy(label))
            expect(card.locator('.evo-chip')).to_have_text(studio_copy('roomUnoccupied'))
            expect(page.locator('.evo-target[data-target=member]')).to_have_count(0)
            expect(tile).to_be_focused()
        # An empty room owns no animation callbacks. Its art may still be decoding
        # on a loaded host (that paint is real); it must then fall quiet.
        assert wait_quiet(page),'an empty room stops painting once its art has loaded'
        paints,renders=paints_during(page,700);assert paints<=2*renders,(paints,renders)
        page.locator('.evo-room-tile[data-room=""]').click()
        expect(root).to_have_attribute('data-room','main');expect(card).to_be_hidden()
        expect(page.locator('.evo-target[data-target="place:library"]')).to_be_attached()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors
    finally:context.close()


def test_room_occupancy_reconciles_without_losing_room_or_focused_member(browser,office_preview):
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':900})
    context.add_init_script(STAGE_PROBE)
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(f'{office_preview}?scenario=swarm')
        root=page.locator('.evo-game')
        page.locator('.evo-room-tile[data-room=research]').click()
        agents=[{'id':'scout-a','role':'researcher','status':'running','objective':'Research A'},
                {'id':'scout-b','role':'researcher','status':'pending','objective':'Research B'}]
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        tab=page.locator('.evo-member-tabs [data-member=scout-b]');expect(tab).to_be_visible();tab.click()
        brief=page.locator('.evo-member-brief');expect(brief).to_have_text('Research B')
        member=page.locator('.evo-target[data-target=member]')
        expect(member).to_have_attribute('data-member','scout-b');expect(member).to_have_attribute('data-status','pending')
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        expect(tab).to_be_focused()
        agents[1]['status']='running'
        page.evaluate('agents=>window.__setEvolutionPreviewRoster(agents)',agents)
        expect(member).to_have_attribute('data-status','running')
        expect(tab).to_be_focused();expect(brief).to_have_text('Research B')
        page.evaluate('window.__setEvolutionPreviewRoster([])')
        expect(root).to_have_attribute('data-room','research');expect(member).to_have_count(0)
        expect(page.locator('.evo-member-card .evo-chip')).to_have_text(studio_copy('roomUnoccupied'))
        page.evaluate('window.__setEvolutionPreviewRoster([],false)')
        expect(page.locator('.evo-member-card .evo-chip')).to_have_text(studio_copy('roomRosterUnknown'))
        # Empty rooms never run an animation timer; the hidden main office paints nothing either.
        page.wait_for_timeout(300)
        paints,renders=paints_during(page,700);assert paints<=2*renders,(paints,renders)
        assert not errors
    finally:context.close()


def test_office_runtime_deterministic_render_cost(browser,office_preview):
    """CPU submission cost, not GPU/display FPS: the real stage and office scene on an exact 60 Hz virtual clock."""
    from playwright.sync_api import expect
    context=browser.new_context(viewport={'width':1280,'height':1000},reduced_motion='no-preference')
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(office_preview.replace('/preview.html','/office-runtime.html'))
        page.evaluate('''async ({stagePath,scenePath,projectionPath}) => {
          const [{mountStage},{createOfficeScene},{projectStudio}]=await Promise.all([import(stagePath),import(scenePath),import(projectionPath)]);
          // Track every image the scene decodes, so measurement starts with loaded art.
          const images=[],NativeImage=window.Image;
          window.Image=class extends NativeImage{constructor(...args){super(...args);images.push(this);}};
          // Virtual frames from mount on: the loop's callbacks run only when the test drains them.
          const clock={now:0,seq:0,frames:new Map(),timers:new Map(),peakFrames:0};
          window.requestAnimationFrame=fn=>{clock.frames.set(++clock.seq,fn);clock.peakFrames=Math.max(clock.peakFrames,clock.frames.size);return clock.seq;};
          window.cancelAnimationFrame=id=>{clock.frames.delete(id);};
          const host=document.createElement('div');host.id='runtime-sample';
          host.style.cssText='position:fixed;left:0;top:0;width:960px;height:720px;z-index:10000';
          document.body.append(host);
          const stage=mountStage(host,{label:'runtime sample'});
          const office=createOfficeScene({copy:key=>key,wake:()=>stage.wake(),activate:()=>{}});
          stage.setScene(office.scene);
          const names={executor:'Executor',reviewer:'Reviewer',coordinator:'Coordinator'};
          const placeLabels={library:'Library',plan:'Plan',work:'Work',review:'Review',delivery:'Delivery'};
          const run=over=>({run_id:'sample',objective:'Sample',status:'executing',task_id:'sample-task',attempt:1,created_at:1,updated_at:1,
            actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},receipts:[],result:'',review:null,error:'',...over});
          const agents=Array.from({length:8},(_,i)=>({id:'sample-'+i,role:'researcher',status:'running'}));
          const state=(snapshot,extra={})=>({goalId:'sample',view:projectStudio(snapshot),materials:[],assignmentSeq:0,agents,
            names,actorLabels:names,placeLabels,attention:[],...extra});
          office.update(state({run:run()}));stage.wake();
          // Warm-up: draw until every character, including the visitors stepping in from
          // outside the door, has requested its sheet; then wait for decoding.
          for(let i=0;i<10&&images.filter(image=>image.getAttribute('src')).length<8;i++){
            clock.now+=1000/60;const batch=[...clock.frames.values()];clock.frames.clear();batch.forEach(fn=>fn(clock.now));
          }
          window.Image=NativeImage;
          window.runtimeSample={stage,office,state,run,clock,images,host};
        }''',{'stagePath':source('engine/stage.ts'),'scenePath':source('office-scene.ts'),'projectionPath':source('studio/projection.ts')})
        expect(page.locator('#runtime-sample canvas')).to_be_visible()
        # The character painter's fallback atlas has no source unless a sheet fails.
        loaded=page.evaluate('''async () => {
          const images=()=>window.runtimeSample.images.filter(image=>image.getAttribute('src'));
          for(let i=0;i<300&&!(images().length>=8&&images().every(image=>image.complete&&image.naturalWidth>0));i++)
            await new Promise(resolve=>setTimeout(resolve,100));
          return images().map(image=>[image.getAttribute('src').split('/').pop(),image.complete,image.naturalWidth]);
        }''')
        assert len(loaded)>=8 and all(complete and width for _,complete,width in loaded),loaded
        result=page.evaluate('''async workPath => {
          const {emoteFrameMs}=await import(workPath);
          const {stage,office,state,run,clock,host}=window.runtimeSample;
          const realTimeout=window.setTimeout,realClear=window.clearTimeout,realRaf=window.requestAnimationFrame;
          window.setTimeout=(fn,ms)=>{clock.timers.set(++clock.seq,{fn,at:clock.now+Math.max(0,ms||0)});return clock.seq;};
          window.clearTimeout=id=>{clock.timers.delete(id);};
          const ctx=host.querySelector('canvas').getContext('2d'),drawImage=ctx.drawImage;let drawCalls=0;
          ctx.drawImage=function(...args){drawCalls++;return drawImage.apply(this,args);};
          const costs=[],heights=new Set();let callbacks=0;
          const executor=()=>office.scene.targets().find(target=>target.id==='actor:executor');
          const frame=()=>{
            clock.now+=1000/60;const batch=[...clock.frames.values()];clock.frames.clear();
            for(const fn of batch){const start=performance.now();fn(clock.now);costs.push(performance.now()-start);callbacks++;}
            heights.add(executor().height.toFixed(3));
          };
          const drain=()=>{for(let i=0;clock.frames.size;i++){if(i>=5000)throw new Error('runtime sample exceeded the 5000-frame bound');frame();}};
          const advance=ms=>{
            const end=clock.now+ms;
            for(let i=0;i<5000;i++){
              if(clock.frames.size){frame();continue;}
              const next=[...clock.timers.entries()].sort((a,b)=>a[1].at-b[1].at)[0];
              if(!next||next[1].at>end)break;
              clock.timers.delete(next[0]);clock.now=Math.max(clock.now,next[1].at);next[1].fn();
            }
            clock.now=Math.max(clock.now,end);
          };
          const pick=metrics=>({frames:metrics.frames,steps:metrics.steps,paints:metrics.paints,sleeps:metrics.sleeps,timers:metrics.timers,
            pending:metrics.pending,backingPixels:metrics.backingPixels});
          const delta=(a,b)=>Object.fromEntries(['frames','steps','paints','sleeps','timers'].map(key=>[key,b[key]-a[key]]));
          try{
            const start=pick(stage.metrics());
            drain();
            const entered=pick(stage.metrics()),settledAt=clock.now;
            const people=office.scene.targets().filter(target=>/^(actor|visitor):/.test(target.id)).length;
            const seated=executor().data.seated,chairs=office.diagnostics().chairs;
            advance(10000);
            const keyframes=pick(stage.metrics());
            const idleStart=performance.now();
            for(let i=0;i<100;i++){office.update(state({run:run()}));stage.wake();}
            const idleUpdateMs=performance.now()-idleStart;
            drain();
            const unchanged=pick(stage.metrics());
            stage.setMotionPaused(true);drain();
            const pausedAt=pick(stage.metrics());
            office.update(state({run:run(),questions:1}));stage.wake();drain();
            const waiting=executor().data;
            advance(10000);
            const paused=pick(stage.metrics());
            stage.setMotionPaused(false);drain();
            Object.defineProperty(document,'hidden',{configurable:true,value:true});
            document.dispatchEvent(new Event('visibilitychange'));
            office.update(state({run:run()},{agents:[]}));stage.wake();
            const hiddenPending={frames:clock.frames.size,timers:clock.timers.size};
            const backingPixels=stage.metrics().backingPixels;
            stage.dispose();office.dispose();
            const disposed={...pick(stage.metrics()),callbacks:clock.frames.size+clock.timers.size};
            costs.sort((a,b)=>a-b);const sum=costs.reduce((a,b)=>a+b,0);
            return {workload:'3 residents + 8 entering visitors; walk, pull chair, sit, roll in; 10 s keyframes; 100 unchanged snapshots; paused question wait; hidden; dispose',
              callbacks,simulatedMs:clock.now,settledMs:settledAt,peakPending:clock.peakFrames,seatPoseSamples:heights.size,seated,chairs,people,drawCalls,
              keyframePeriodMs:emoteFrameMs('work'),
              phases:{enter:delta(start,entered),keyframes:delta(entered,keyframes),unchanged:delta(keyframes,unchanged),
                paused:delta(pausedAt,paused),pausedPending:paused.pending},
              entered,waiting:{phase:waiting.phase,activity:waiting.activity,seated:waiting.seated},
              callbackCpuMs:{total:sum,mean:sum/costs.length,p95:costs[Math.floor(costs.length*.95)],max:costs.at(-1)},
              idleUpdateMs,hiddenPending,backingPixels,disposed};
          }finally{
            delete document.hidden;stage.dispose();office.dispose();
            window.setTimeout=realTimeout;window.clearTimeout=realClear;window.requestAnimationFrame=realRaf;ctx.drawImage=drawImage;
          }
        }''',source('engine/paint.ts'))
        output=Path('output/evolution-design/runtime');output.mkdir(parents=True,exist_ok=True)
        (output/'deterministic-render-cost.json').write_text(json.dumps(result,indent=2))
        phases=result['phases']
        assert 0<result['callbacks']<5000
        assert result['peakPending']==1, 'one loop requests at most one frame at a time'
        assert result['people']==11 and result['seated']=='true' and result['chairs']=='in,in'
        assert result['seatPoseSamples']>5, 'sitting is an eased pose, not a jump'
        assert phases['enter']['steps']>0 and phases['enter']['paints']==phases['enter']['frames']
        assert result['entered']['pending']=={'frame':False,'timer':True}, 'a settled working world sleeps until its next keyframe'
        # Keyframes only: one frame per timer, no simulated steps, keyframe cadence rather than 60 Hz.
        keyframes=phases['keyframes']
        assert keyframes['steps']==0 and keyframes['frames']==keyframes['timers']
        assert 10000/result['keyframePeriodMs']-2<=keyframes['frames']<=10000/result['keyframePeriodMs']+1,keyframes
        assert phases['unchanged']['frames']<=1 and phases['unchanged']['steps']==0
        # Paused: the question wait settles at once and the world owns no callbacks.
        assert result['waiting']=={'phase':'idle','activity':'waiting','seated':'false'}
        assert phases['paused']['frames']==1 and phases['paused']['timers']==0
        assert phases['pausedPending']=={'frame':False,'timer':False}
        assert result['hiddenPending']=={'frames':0,'timers':0}
        assert result['backingPixels']==960*720*1 and result['disposed']['backingPixels']==0
        assert result['disposed']['pending']=={'frame':False,'timer':False} and result['disposed']['callbacks']==0
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
            const inspector=mountGoalRunInspector(host,{copy,openTask(){},sections:['status','records']});
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


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('width', [320, 390, 768, 1024, 1536])
def test_studio_hierarchy_and_intake_in_source_preview(browser, office_preview, theme, width):
    """The real source graph stays legible, theme-invariant and draft-safe without a storage server."""
    from playwright.sync_api import expect
    context = browser.new_context(viewport={'width': width, 'height': 1024}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('tofu_ui_lang', 'en')")
    try:
        page.goto(office_preview)
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        expect(page.locator('.evo-quest-title')).to_have_text('梳理 Agent 长期记忆方案，形成可复现的研究报告')
        expect(page.locator('.evo-quest-state')).to_have_text(studio_copy('state_executing', 'en'))
        expect(page.locator('.evo-track li[data-stage=execute]')).to_have_attribute('data-state', 'active')
        # The studio palette is theme-invariant: windows and HUD chrome ignore the chat theme.
        palettes = page.evaluate('''theme => {
          const read = () => ['.evo-window', '.evo-quest', '.evo-slot'].map(selector => {
            const style = getComputedStyle(document.querySelector(selector));return style.backgroundColor + ' ' + style.color;}).join('|');
          const values = ['tofu', 'light', 'dark'].map(name => {document.documentElement.dataset.theme = name;return read();});
          document.documentElement.dataset.theme = theme;return values;
        }''', theme)
        assert len(set(palettes)) == 1, palettes
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto(office_preview + '?scenario=empty#/evolution/start')
        field = page.locator('#evo-objective')
        expect(field).to_be_visible()
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        expect(page.locator('.evo-game')).to_have_attribute('data-window', 'briefing')
        expect(page.locator('.evo-meters')).to_be_hidden()
        expect(page.locator('.evo-seed svg')).to_have_count(3)
        contrast = page.evaluate(CONTRAST, ['#evo-window-title', '#evo-objective', '.evo-seed', '.evo-briefing-settings summary', '.evo-dispatch'])
        assert all(item['ratio'] >= 4.5 for item in contrast), contrast
        page.locator('[data-example=research]').click()
        expect(field).to_be_focused()
        draft = field.input_value()
        assert draft
        expect(page.locator('[data-example=build]')).to_be_disabled()
        # Closing the intake to look at the world keeps the unsent draft.
        page.keyboard.press('Escape')
        expect(page.locator('.evo-window')).to_be_hidden()
        page.keyboard.press('KeyN')
        expect(field).to_have_value(draft)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        geometry = page.evaluate('''() => {
          const panel = document.querySelector('.evo-window').getBoundingClientRect();
          const submit = document.querySelector('.evo-dispatch').getBoundingClientRect();
          return submit.left >= panel.left && submit.right <= panel.right && panel.right <= innerWidth + 1;
        }''')
        assert geometry
        assert not errors, errors
    finally:
        context.close()
