"""Real-browser reading continuity for research telemetry; no server or model."""
from pathlib import Path
import subprocess

import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def research_dom_bundle(tmp_path_factory):
    output = tmp_path_factory.mktemp('research-browser') / 'dom.js'
    subprocess.run([
        'node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/features/evolution/research/components/stable-dom.ts'),
        '--bundle', '--format=iife', '--global-name=ResearchDom',
        f'--outfile={output}',
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    return output


@pytest.mark.parametrize('width', [390, 1280])
def test_progress_preserves_reading_context(browser, research_dom_bundle, width):
    context = browser.new_context(viewport={'width': width, 'height': 800},
                                  reduced_motion='reduce')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<main id="host"></main>')
        page.add_script_tag(path=str(research_dom_bundle))
        result = page.evaluate('''() => {
          const host = document.querySelector('#host');
          const markup = n => `<header><h1>研究问题：${'长中文标题'.repeat(16)}</h1>
            <span id="progress" role="status">已读取 ${n} 篇</span></header>
            <input id="filter" aria-label="筛选证据" value="">
            <div id="evidence" style="height:300px;overflow:auto">
            ${Array.from({length:200}, (_,i) => `<details id="paper-${i}">
              <summary>文献 ${i}</summary><p id="quote-${i}">证据片段 ${i} 不代表已经验证的科学结论</p>
            </details>`).join('')}</div>`;
          ResearchDom.patchResearchDom(host, markup(0));
          const input = document.querySelector('#filter');
          const quote = document.querySelector('#quote-0');
          const disclosure = document.querySelector('#paper-0');
          disclosure.open = true;
          input.value = '保留筛选'; input.focus();
          const range = document.createRange(); range.selectNodeContents(quote);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          const selected = selection.toString();
          const scroller = document.querySelector('#evidence'); scroller.scrollTop = 180;
          const scroll = scroller.scrollTop;
          const start = performance.now();
          for (let n=1;n<=30;n++) ResearchDom.patchResearchDom(host, markup(n));
          return {
            sameInput: input === document.querySelector('#filter'),
            sameQuote: quote === document.querySelector('#quote-0'),
            focused: document.activeElement === input,
            filter: input.value, open: disclosure.open,
            selected: selection.toString() === selected && selected.length > 0,
            scroll: scroller.scrollTop === scroll,
            count: host.querySelectorAll('details').length,
            progress: document.querySelector('#progress').textContent,
            updateMs: (performance.now()-start)/30,
          };
        }''')
        assert all(result[key] for key in
                   ('sameInput', 'sameQuote', 'focused', 'open', 'selected', 'scroll')), result
        assert result['filter'] == '保留筛选'
        assert result['count'] == 200
        assert result['progress'] == '已读取 30 篇'
        assert result['updateMs'] < 250, result  # generous host-independent regression ceiling
        assert not errors
    finally:
        context.close()


@pytest.fixture(scope='module')
def literature_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('research-literature-browser')
    entry = directory / 'entry.ts'
    feature = ROOT / 'frontend/src/features/evolution/research'
    entry.write_text(
        f'export {{ paperCards, bindPaperCards }} from "{feature}/components/live-library.ts";\n'
        f'export {{ patchResearchDom }} from "{feature}/components/stable-dom.ts";\n'
    )
    output = directory / 'library.js'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
                    '--bundle', '--format=iife', '--global-name=Literature',
                    f'--outfile={output}'], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    return output


@pytest.mark.parametrize('width', [390, 1280])
def test_compact_literature_sort_and_live_metadata(browser, literature_bundle, width, tmp_path):
    context = browser.new_context(viewport={'width': width, 'height': 800})
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('<main class="rs-workbench rs-console" id="host"></main>')
        page.add_style_tag(content=':root{--bg-primary:#fff;--bg-secondary:#f8f9fb;--text-primary:#232529;--border:#dde2e8;--text-tertiary:#6b7280;--accent:#b7772f}body{margin:12px;font:14px system-ui}')
        page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/03-paper-reader.css'))
        # Research (rs-*) styles split out of 03-paper-reader.css (2026-09).
        page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/27-evolution-research.css'))
        page.add_script_tag(path=str(literature_bundle))

        import json
        page.evaluate('(locale) => { window.researchTestLocale = locale; }',
                      json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text()))
        page.evaluate('''() => {
          const stream = {papers: [
            {paperId:'a', title:'A long research title '.repeat(5),year:2020,citationCount:0,status:'parsed'},
            {paperId:'b', title:'Recent paper',year:2024,citationCount:10,status:'cache_hit'},
            {paperId:'c', title:'Unknown metadata',year:null,citationCount:null,status:'metadata_only'}
          ],citationEdges:[{from:'b',to:'a',relation:'cites'},{from:'c',to:'b',relation:'cites'}]};
          const host = document.querySelector('#host');
          const paint = () => {
            Literature.patchResearchDom(host, Literature.paperCards(stream, key=>window.researchTestLocale[key] || key));
            Literature.bindPaperCards(host,stream,paint);
          };
          paint();
          // Test-only handle: production publishes no extra registry/global.
          window.updateLiterature = () => { stream.papers[0].venue='ICML'; paint(); };
        }''')
        # The Connected-Papers-style graph is the default library surface: a
        # live canvas with one node per paper, then the toggle reveals the table.
        assert page.locator('#researchLiteratureGraph canvas').count() == 1
        assert page.locator('.rs-paper-table').count() == 0
        pixels = page.evaluate('''() => {
          const canvas = document.querySelector('#researchLiteratureGraph canvas');
          const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          let count = 0, sumX = 0, sumY = 0, minY = Infinity, maxY = -1;
          for (let y = 0; y < canvas.height; y += 1) {
            for (let x = 0; x < canvas.width; x += 1) {
              if (data[(y * canvas.width + x) * 4 + 3] > 10) {
                count += 1; sumX += x; sumY += y;
                if (y < minY) minY = y; if (y > maxY) maxY = y;
              }
            }
          }
          return {count, cx: count ? sumX / count : -1, cy: count ? sumY / count : -1,
                  minY, maxY, width: canvas.width, height: canvas.height};
        }''')
        # The fitted camera must center the origin-centered world: painted content
        # keeps vertical margins and a centered centroid instead of clipping into
        # the top-left corner (the camera-less rendering regression).
        assert pixels['count'] > 0, pixels
        assert pixels['minY'] > 0 and pixels['maxY'] < pixels['height'] - 1, pixels
        assert 0.25 * pixels['width'] < pixels['cx'] < 0.75 * pixels['width'], pixels
        assert 0.25 * pixels['height'] < pixels['cy'] < 0.75 * pixels['height'], pixels
        page.locator('[data-literature-view="list"]').click()
        assert page.locator('#researchLiteratureGraph').count() == 0
        page.locator('#rs-citation-lineage > summary').click()

        page.locator('#rs-literature-sort').focus()
        page.select_option('#rs-literature-sort', 'citations-asc')
        page.evaluate('window.updateLiterature()')
        assert page.locator('#rs-literature-sort').input_value() == 'citations-asc'
        assert page.locator('tbody tr').first.get_attribute('id') == 'rs-paper-a'
        assert page.locator('#rs-paper-a td').nth(3).inner_text() == '0'
        assert page.locator('#rs-paper-c td').nth(3).inner_text() == '—'
        assert page.evaluate("document.activeElement.id") == 'rs-literature-sort'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.rs-paper-card').count() == 0

        assert page.locator('#rs-citation-lineage').evaluate('(node)=>node.open')
        assert page.locator('.rs-lineage-edge').count() == 2
        assert page.locator('.rs-lineage-node').count() == 3
        assert page.locator('.rs-lineage-viewport').evaluate('(node)=>node.clientHeight <= 480')
        screenshot = tmp_path / f'literature-{width}.png'
        page.screenshot(path=str(screenshot), full_page=True)
        print(f'Literature screenshot: {screenshot}')
    finally:
        context.close()
