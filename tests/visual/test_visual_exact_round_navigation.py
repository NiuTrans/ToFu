"""Real Chromium exact rank controls on the local, storage-free visual origin."""
import subprocess
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def navigation_browser_bundle(tmp_path_factory):
    folder = tmp_path_factory.mktemp('exact-navigation-browser')
    entry = folder / 'navigation.ts'
    entry.write_text(f"export {{createTurnNavigation}} from '{ROOT}/frontend/src/features/conversation-bar/turn-navigation.ts';")
    output = folder / 'navigation.js'
    result = subprocess.run([str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
                             '--bundle', '--format=iife', '--platform=browser',
                             '--global-name=Navigation', f'--outfile={output}'],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return output


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390])
def test_exact_round_keyboard_numeric_first_last_and_bounded_page(
        frontend_visual_page, navigation_browser_bundle, tmp_path, theme, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""theme=>{
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main><div id="turnNav"></div><button id="outside">Outside</button></main>';
    }""", theme)
    page.add_style_tag(content='body{display:block;height:auto;padding:var(--space-4)}main{padding-top:120px}')
    page.add_script_tag(path=str(navigation_browser_bundle))
    page.evaluate("""()=>{
      window.requests=[];window.hydrates=[];window.scrolls=[];
      window.nav=Navigation.createTurnNavigation({host:document.getElementById('turnNav'),
        translate:(key,params)=>params?`${params.current} / ${params.total}`:key,
        directoryKey:()=> 'owner-11:head-9',
        directory:async(id,offset,limit,anchor)=>{
          requests.push({id,offset,limit,anchor});
          return {ownerId:11,conversationId:id,laneId:'main',syncSeq:9,stale:false,
            offset,totalRounds:1203,activeRound:1203,
            entries:Array.from({length:Math.min(limit,1203-offset)},(_,i)=>({
              turnId:`rank-${offset+i+1}`,ordinal:(offset+i)*3,
              actor:['human','critic','virtual_user'][(offset+i)%3],roundNumber:offset+i+1}))};
        },hydrateRound:async(id,entry)=>hydrates.push(entry),
        scrollToTurn:id=>scrolls.push(id)});
      nav.update('fixture',{laneOrder:{main:['loaded']},turnsById:{loaded:{actor:'human',projection:{content:'loaded'}}},
        historyByLane:{main:{hasMore:true,nextBeforeOrdinal:3600,totalTurns:3609}}});
    }""")
    page.wait_for_function("document.querySelector('.turn-nav-readout').textContent==='1203 / 1203'")
    assert page.locator('#turnNav button').count() == 3
    position = page.locator('.turn-nav-position')
    position.focus()
    position.press('Enter')
    page.wait_for_function("document.querySelectorAll('.turn-jump-entry').length===3 && document.getElementById('turnNav').getAttribute('aria-busy')==='false'")
    panel = page.locator('.turn-jump-panel')
    box = panel.bounding_box()
    assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
    panel.locator('input').fill('270')
    panel.locator('input').press('Enter')
    page.wait_for_function("scrolls.at(-1)==='rank-270'")
    assert page.evaluate('requests.at(-1).offset') == 269
    assert page.evaluate('requests.at(-1).limit') == 1
    assert page.evaluate('hydrates.map(x=>x.turnId)') == ['rank-270']
    assert panel.count() == 0
    assert position.evaluate('(node)=>document.activeElement===node')
    for selector, target in [('.turn-jump-first', 'rank-1'), ('.turn-jump-last', 'rank-1203')]:
        position.click()
        page.wait_for_function("document.getElementById('turnNav').getAttribute('aria-busy')==='false'")
        page.locator(selector).click()
        page.wait_for_function('(target)=>scrolls.at(-1)===target', arg=target)
    position.click()
    page.wait_for_function("document.getElementById('turnNav').getAttribute('aria-busy')==='false'")
    page.locator('[data-direction=older]').click()
    page.wait_for_function("document.querySelectorAll('.turn-jump-entry').length===50 && document.getElementById('turnNav').getAttribute('aria-busy')==='false'")
    assert page.locator('.turn-jump-entry').count() == 50
    page.screenshot(path=str(tmp_path / f'exact-navigation-{theme}-{width}.png'))
    page.keyboard.press('Escape')
    assert page.locator('.turn-jump-panel').count() == 0
    assert position.evaluate('(node)=>document.activeElement===node')
    page.evaluate('nav.destroy()')
    assert page.locator('#turnNav button').count() == 0
