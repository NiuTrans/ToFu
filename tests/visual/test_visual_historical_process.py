"""Real Chromium/local visual harness for final-first history and bounded process."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def history_browser_bundle(tmp_path_factory):
    folder = tmp_path_factory.mktemp('history-browser-bundle')
    entry = folder / 'history.ts'
    entry.write_text(f"""
export * from '{ROOT}/frontend/src/conversation/index.ts';
export {{setLanguage as locale}} from '{ROOT}/frontend/src/i18n/index.ts';
""")
    output = folder / 'history.js'
    built = subprocess.run([str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
                            '--bundle', '--format=iife', '--platform=browser',
                            '--global-name=History', f'--outfile={output}'], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stderr
    return output


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('language,width', [('en', 1180), ('zh', 390)])
def test_final_first_history_keyboard_replay_scroll_and_resource_budget(
        frontend_visual_page, history_browser_bundle, tmp_path, theme, language, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""theme=>{
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main id="surface"></main>';
    }""", theme)
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);height:auto;overflow:auto}'
                                '#surface{display:block;max-width:100%}')
    page.add_script_tag(path=str(history_browser_bundle))
    dictionary = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r"""async ({language,dictionary})=>{
      await History.locale(language);
      const scope={attemptId:'accepted',taskId:'history-fixture'};
      const segment=(id,n,type,text)=>({type,blockId:id,llmRound:n,...scope,text});
      const segments=[];
      for(let i=0;i<1200;i++){
        if(i%2===0)segments.push(segment('thought-'+i,i,'thinking','Checking authored evidence '+i));
        segments.push({type:'tool_use',blockId:'tool-'+i,id:'tool-'+i,name:'read_files',llmRound:i,...scope,
          input:{path:'synthetic/history-'+i+'.ts'},result:{content:'Synthetic evidence '+i,status:'done'},
          _round:{toolName:'read_files',status:'done',llmRound:i,...scope,results:[{exitCode:0}]}});
      }
      segments.splice(3,0,segment('narration',1,'text','Intermediate narration stays inspectable, not in front of the answer.'));
      segments.push({type:'tool_use',blockId:'error',id:'error',name:'run_command',llmRound:1201,...scope,
        _round:{toolName:'run_command',query:'<img src=x onerror=alert(1)> — validation returned exit 2',status:'done',results:[{exitCode:2}]}});
      segments.push({...segment('answer',1202,'text','Final answer: historical process is available on demand.'),terminal:true,deliverable:true});
      const turn={turnId:'history',conversationId:'fixture-history',laneId:'main',ordinal:1,
        actor:'assistant',kind:'reply',status:'completed',projectionRevision:1,
        projection:{segments,content:'Final answer: historical process is available on demand.'},
        settlement:{outcome:'completed'},createdAt:1,updatedAt:2};
      window.state={conversationId:'fixture-history',conversationRevision:1,transport:'live',turnsById:{history:turn},
        laneOrder:{main:['history']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
      window.intents=[];
      window.surface=History.createConversationSurface(document.getElementById('surface'),{
        scrollViewport:document.documentElement,windowing:{maxBlocksPerTurn:40},onIntent:intent=>intents.push(intent)});
      window.replay=()=>{state=JSON.parse(JSON.stringify(state));surface.render(History.selectConversationViewModel(state));};
      replay();window.originalProcess=document.querySelector('.conversation-history-process');
    }""", {'language': language, 'dictionary': dictionary})
    process = page.locator('.conversation-history-process')
    summary = process.locator(':scope > summary')
    assert page.locator('[data-block-id="answer"]').is_visible()
    assert page.locator('.conversation-block--tool').count() == 0
    assert page.locator('[data-conversation-part="turn-history-notice"]').count() == 0
    assert summary.bounding_box()['y'] + summary.bounding_box()['height'] <= page.locator('[data-block-id="answer"]').bounding_box()['y']
    assert summary.bounding_box()['width'] < page.locator('.conversation-turn-content').bounding_box()['width']
    assert page.locator('img[src="x"]').count() == 0
    assert not process.evaluate('(node)=>node.open')
    assert ('过程' if language == 'zh' else 'Process') in summary.inner_text()
    closed_nodes = page.locator('*').count()
    closed_image = tmp_path / f'history-closed-{theme}-{language}-{width}.png'
    page.screenshot(path=str(closed_image))

    summary.focus()
    summary.press('Enter')
    page.wait_for_selector('[data-history-process-blocks]')
    assert process.locator('[data-block-id]').count() == 32
    assert process.locator('[data-block-id]').first.get_attribute('data-block-id') == 'thought-0'
    inner = process.locator('[data-block-id="tool-0"] details')
    inner.locator('summary').focus()
    inner.locator('summary').press('Enter')
    page.wait_for_function('document.querySelector("[data-block-id=tool-0] details").open')
    page.evaluate('window.originalReader=document.activeElement')
    timings = page.evaluate("""()=>{
      const samples=[];
      for(let i=0;i<25;i++){const start=performance.now();replay();samples.push(performance.now()-start);}
      return samples;
    }""")
    assert page.evaluate('document.activeElement===originalReader')
    assert page.evaluate('document.querySelector(".conversation-history-process")===originalProcess')
    assert inner.evaluate('(node)=>node.open')
    panel = process.locator('.conversation-history-process__body')
    panel.evaluate('(node)=>node.scrollTop=50')
    page.evaluate('replay()')
    assert panel.evaluate('(node)=>node.scrollTop') == 50
    viewport_before = page.evaluate('window.scrollY')
    panel.evaluate('(node)=>node.scrollTop=node.scrollHeight')
    panel.hover()
    page.mouse.wheel(0, 1000)
    page.wait_for_timeout(120)
    assert page.evaluate('window.scrollY') == viewport_before
    assert page.evaluate('intents.length') == 0
    assert page.locator('[data-conversation-part="turn-blocks-earlier"]:visible').count() == 0
    open_nodes = page.locator('*').count()
    open_image = tmp_path / f'history-open-{theme}-{language}-{width}.png'
    panel.evaluate('(node)=>node.scrollTop=0')
    page.screenshot(path=str(open_image))
    process.locator('[data-process-page="next"]').click()
    assert process.locator('[data-block-id]').count() == 32
    assert process.locator('[data-block-id]').first.get_attribute('data-block-id') != 'thought-0'
    process.locator('[data-process-page="previous"]').click()
    assert inner.evaluate('(node)=>node.open')
    summary.focus()
    summary.press('Space')
    page.wait_for_function('!document.querySelector(".conversation-history-process").open')
    assert process.locator('[data-block-id]').count() == 0
    summary.press('Enter')
    page.wait_for_selector('[data-history-process-blocks]')
    assert inner.evaluate('(node)=>node.open')
    geometry = page.evaluate("""()=>({
      viewport:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,
      leaves:document.querySelectorAll('[data-history-process-blocks] [data-block-id]').length,
      processScrollPolicy:getComputedStyle(document.querySelector('.conversation-history-process__body')).overscrollBehavior,
      entries:state.turnsById.history.projection.segments.length,
      outerHistoryIntents:intents.length,
      summaryHeight:document.querySelector('.conversation-history-process > summary').getBoundingClientRect().height})""")
    assert geometry['scrollWidth'] <= geometry['viewport'] + 1
    assert geometry['leaves'] == 32
    assert geometry['processScrollPolicy'] == 'contain'
    assert geometry['summaryHeight'] == page.locator('.conversation-history-process > summary').evaluate(
        'n=>parseFloat(getComputedStyle(n).getPropertyValue("--control-h-lg"))')
    assert closed_nodes < 110
    assert open_nodes < 1000
    report = {**geometry, 'closedNodes': closed_nodes, 'openNodes': open_nodes,
              'repaintMs': timings, 'closedScreenshot': str(closed_image), 'openScreenshot': str(open_image)}
    report_path = tmp_path / f'history-budget-{theme}-{language}-{width}.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'History browser evidence: {report_path}')
    other = 'zh' if language == 'en' else 'en'
    page.evaluate('async language=>{await History.locale(language);replay();}', other)
    assert ('过程' if other == 'zh' else 'Process') in summary.inner_text()
    page.evaluate('surface.dispose()')
