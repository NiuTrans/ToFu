"""Real Chromium exercises shipped research owners with isolated transport ports."""
from pathlib import Path
import subprocess

import pytest
from tests._runtime_sections import _require_frontend_bundler

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def research_runtime_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('research-runtime-browser')
    entry = directory / 'entry.ts'
    entry.write_text(
        f'export * from "{ROOT}/frontend/src/features/evolution/research/controllers/runtime";\n'

        f'export * from "{ROOT}/frontend/src/features/evolution/research/components/view";\n'
        f'export {{ featureRegistry }} from "{ROOT}/frontend/src/feature-registry";\n',
        encoding='utf-8')
    output = directory / 'runtime.js'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
                    '--bundle', '--format=iife', '--global-name=ResearchRuntime',
                    f'--outfile={output}'], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    return output


def test_disconnect_cancel_failure_and_late_snapshot(browser, research_runtime_bundle):
    context = browser.new_context(reduced_motion='reduce')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('<main id="researchViewer"></main>')
        page.add_script_tag(path=str(research_runtime_bundle))
        result = page.evaluate('''async () => {
          const r = ResearchRuntime;
          const registry = r.featureRegistry;
          let disconnected = false;
          let cancelFailure = true;
          let reads = 0;
          registry._paintResearch = () => {};
          registry._evolutionRunsApi = {
            createRun: async () => ({run:{run_id:'synthetic-evolution-run',task_id:'synthetic-run',status:'executing'}}),
            cancelRun: async () => cancelFailure ? {ok:false,error:'Stop request failed'} : {run:{run_id:'synthetic-evolution-run'}},
          };
          registry.Api = {tasks: {
            get: async () => {
              reads++;
              if (disconnected) throw new Error('offline');
              return {ok:true,status:'running',createdAt:Date.now()-180000,
                updatedAt:Date.now()-180000,result:{}};
            },
            abort: async () => cancelFailure ? {ok:false,error:'Stop request failed'} : {ok:true},
          }};
          await r.startResearchJob('三分钟零成果的合成问题');
          const stream = registry._researchStream;
          stream.surveyMd = 'Previously available evidence';
          disconnected = true;
          try { await r.pollResearchOnce(stream); } catch (_) {}
          const offline = {status:stream.status,evidence:stream.surveyMd};
          disconnected = false;
          await r.abortResearchJob();
          const failed = {status:stream.status,error:stream.commandError};
          cancelFailure = false;
          await r.abortResearchJob();
          const requested = {status:stream.status,cancelling:stream.cancelling};
          r.researchIngestEvent(stream,{seq:20,type:'done',status:'aborted'});
          r.researchApplySnapshot(stream,{status:'running',cursor:{next:10},result:{}});
          const terminal = stream.status;
          r.destroyResearchRuntime();
          return {offline,failed,requested,terminal,reads,released:registry._researchStream === null};
        }''')
        assert result['offline'] == {'status': 'running', 'evidence': 'Previously available evidence'}
        assert result['failed']['status'] == 'running'
        assert result['failed']['error'] == 'Stop request failed'
        assert result['requested'] == {'status': 'running', 'cancelling': True}
        assert result['terminal'] == 'aborted'
        assert result['released'] is True
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1280])
def test_real_workbench_view_keeps_evidence_readable(browser, research_runtime_bundle, width):
    context = browser.new_context(viewport={'width': width, 'height': 850}, reduced_motion='reduce')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('<div id="researchViewer"></div>')
        page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/03-paper-reader.css'))
        # Research (rs-*) styles split out of 03-paper-reader.css (2026-09).
        page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/27-evolution-research.css'))
        page.add_script_tag(path=str(research_runtime_bundle))
        result = page.evaluate('''() => {
          const r = ResearchRuntime;
          const registry = r.featureRegistry;
          registry.t = key => key;
          registry.renderMarkdown = text => text;
          const stream = r.newResearchStream('长中文研究标题'.repeat(20));
          stream.status = 'running'; stream.startedAt = Date.now()-180000;
          stream.surveyMd = '已有证据仍可阅读。'.repeat(200);
          stream.corpusIds = Array.from({length:200},(_,i)=>'2601.'+String(i).padStart(5,'0'));
          stream.corpusSize = 200;
          stream.papers = Array.from({length:12},(_,i)=>({arxivId:'2601.'+String(i).padStart(5,'0'),title:'真实文献卡片长标题'.repeat(12),status:'parsed',textLength:500}));
          registry._researchStream = stream;
          r.paintResearch(); r.filterResearchThread('evidence');
          const panel = document.querySelector('[data-rs-kind="evidence"]');
          const text = panel.textContent;
          const chip = document.querySelector('[data-rs-filter="evidence"]');
          chip.focus();
          stream.reconnecting = true;
          r.paintResearch();
          const style = getComputedStyle.bind(window);
          return {visible:style(panel).display!=='none',
            filtered:style(document.querySelector('[data-rs-kind="papers"]')).display==='none'
              && style(document.querySelector('[data-rs-kind="ledger"]')).display==='none'
              && style(document.querySelector('[data-rs-kind="live"]')).display==='none',
            order:[...document.querySelectorAll('.rs-thread-entry')].map(n=>n.getAttribute('data-rs-kind')).join(','),
            shellFilter:document.querySelector('[data-research-shell]').getAttribute('data-research-filter'),
            stable:panel===document.querySelector('[data-rs-kind="evidence"]'),
            evidence:panel.textContent===text && text.includes('已有证据仍可阅读'),
            focus:document.activeElement===chip,
            title:document.querySelector('.rs-run-title h1').textContent,
            noPercent:!document.querySelector('[role="progressbar"][aria-valuenow]'),
            overflow:document.documentElement.scrollWidth>window.innerWidth+2,
            overflowing:[...document.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right>window.innerWidth+2).slice(0,12).map(n=>({tag:n.tagName,cls:n.className,width:n.getBoundingClientRect().width}))};
        }''')
        assert result['visible'] and result['stable'] and result['evidence'] and result['focus'], result
        assert result['filtered'], result
        assert result['order'] == 'papers,evidence,ideas,ledger,live', result
        assert result['shellFilter'] == 'evidence', result
        assert result['noPercent'], result
        assert not result['overflow'], str(result['overflowing'])
        page.keyboard.press('Tab')
        assert page.evaluate('document.activeElement.tagName') == 'BUTTON'
    finally:
        context.close()
