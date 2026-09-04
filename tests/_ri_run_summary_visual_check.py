"""Real-browser acceptance check for the Run details drawer.

This is intentionally an opt-in instrument rather than a pytest case because
it launches Chromium and writes screenshots to ``/tmp``.

Current design contract (2026-08-31 redesign):
  * NO summary card ("Completed N operations" was retired — it duplicated
    the round list and answered a question nobody asked).
  * The Technical details section is OPEN BY DEFAULT — the round list IS
    the drawer's payload.
  * Each round row reads like the chat timeline's turn blocks: round
    number + the NAMES of the tools that round invoked (`.ri-tool-chip`),
    plus per-attempt chips. No "N messages · ~M tokens" meta jargon.
  * State mirrors never appear as a separate "tool-result records" block.

Run: ``python tests/_ri_run_summary_visual_check.py``
(requires ``npm run build:frontend`` for the stylesheet)
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_PAGE = r"""
<!doctype html><html data-theme="__THEME__"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="file://__MAINCSS__">
</head><body>
<div class="ri-drawer" id="riDrawer" style="display:none">
  <div class="ri-side">
    <div class="ri-side-header"><span class="ri-title">运行详情</span><button class="debug-clear">×</button></div>
    <div class="ri-task-list" id="riTaskList"></div>
    <div class="ri-round-list" id="riRoundList"></div>
  </div>
  <div class="ri-main"><div class="debug-panel" id="debugPanel">
    <div class="debug-header"><span id="debugTitle">单轮内容</span></div>
    <div id="debugContent"></div>
  </div></div>
</div>
<script>
window.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
window.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}" height="${size||14}"></svg>`;
const labels = {
  'ri.empty':'该会话暂无任务记录','ri.loading':'加载中…',
  'ri.expired':'详细记录已清理，无法展开运行过程',
  'ri.expiredHint':'详细记录约保留 30 天后清理；回复与工具卡片仍在上方的对话里',
  'ri.selectTask':'选择左侧的一次运行，查看它做了什么','ri.detailTitle':'单轮内容',
  'ri.selectRound':'选择一轮，查看该轮的完整请求与工具状态。',
  'ri.viewProcess':'查看完成过程','ri.statusRunning':'任务正在处理',
  'ri.statusDone':'任务已完成','ri.statusFailed':'任务未完成','ri.statusStopped':'任务已停止',
  'ri.statusUnknown':'状态未知',
  'ri.taskLabel':'任务 {id}','ri.technicalDetails':'技术详情','ri.roundTotal':'{n} 轮请求',
  'ri.roundNumber':'第 {n} 轮',
  'ri.availableTools':'可用工具 {n} 种','ri.coveragePartial':'部分技术记录不可用',
  'ri.traceEntry':'耗时分析','ri.traceEntryHint':'每个阶段花了多久',
};
window.t = (key, vars) => {
  let value = labels[key] || key;
  for (const [name, replacement] of Object.entries(vars || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
window.activeConvId = 'conv-visual'; window.debugVisible = false;
window.showMessagesInDebug = (messages, label) => {
  document.getElementById('debugTitle').textContent = label || '单轮内容';
  document.getElementById('debugContent').innerHTML =
    '<div style="padding:12px;line-height:1.6">本轮执行搜索并返回了 3 条结果。</div>';
};
window.Api = { tasks: {
  byConv: async () => ({tasks:[{taskId:'task-143-demo',status:'done',live:false,
    hasEvents:true,createdAt:1786467600000}]}),
  getRequests: async () => ({eventsAvailable:true,coverage:'full',requestCount:146,
    requests:[
      {roundNum:1,model:'gpt-5.6-sol',messageCount:12,toolsCount:47,
        toolNames:['web_search','read_files','run_command'],attempts:[
        {tag:'R1',model:'gpt-5.6-sol',tokensIn:35124,tokensOut:104,
          streamElapsedMs:8700,traceId:'tr-1'}]},
      {roundNum:146,model:'gpt-5.6-sol',messageCount:91,toolsCount:47,
        toolNames:['edit_file'],attempts:[
        {tag:'R146',model:'gpt-5.6-sol',tokensIn:50893,tokensOut:350,
          streamElapsedMs:17000,traceId:'tr-146'}]},
    ]}),
  getRequestPayload: async (_taskId, roundNum) => ({
    roundNum, label:'第 ' + roundNum + ' 轮内容', tools:[],
    messages:[{role:'tool',content:'3 条搜索结果'}],
  }),
} };
</script>
<script src="file://__RIJS__"></script>
<script>openRequestInspector(); setTimeout(() => document.querySelector('.ri-task')?.click(), 20);</script>
</body></html>
"""

_PROBE = r"""
() => {
  const parse = (s) => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  const lum = (rgb) => {
    const f = c => { c /= 255; return c <= .03928 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4); };
    return .2126*f(rgb[0]) + .7152*f(rgb[1]) + .0722*f(rgb[2]);
  };
  const contrast = (fg, bg) => (Math.max(lum(fg),lum(bg))+.05)/(Math.min(lum(fg),lum(bg))+.05);
  const paintedGround = el => {
    for (let n=el; n && n !== document.documentElement; n=n.parentElement) {
      const raw = getComputedStyle(n).backgroundColor;
      const p = raw.match(/[\d.]+/g) || [];
      if (p.length === 3 || (p.length > 3 && Number(p[3]) > .9)) return p.slice(0,3).map(Number);
    }
    return parse(getComputedStyle(document.body).backgroundColor);
  };
  const contrastOf = selector => {
    const el = document.querySelector(selector), cs = getComputedStyle(el);
    return Math.round(contrast(parse(cs.color), paintedGround(el))*100)/100;
  };
  const drawer = document.querySelector('.ri-drawer');
  const side = document.querySelector('.ri-side');
  const main = document.querySelector('.ri-main');
  const details = document.querySelector('.ri-technical');
  const taskText = document.querySelector('.ri-task').textContent;
  return {
    viewport:[innerWidth,innerHeight], drawer:[drawer.clientWidth,drawer.clientHeight],
    side:[side.clientWidth,side.clientHeight], main:[main.clientWidth,main.clientHeight],
    overflowX: drawer.scrollWidth > drawer.clientWidth || document.documentElement.scrollWidth > innerWidth,
    summaryCardGone: !document.querySelector('.ri-summary'),
    statesBlockGone: !document.querySelector('.ri-state-row'),
    technicalOpenByDefault: !!details && details.open,
    taskHasRequestCount: /requests|请求\s*\d/.test(taskText),
    taskHasPlainAction: taskText.includes('查看完成过程'),
    technicalLabel: details.querySelector('summary').textContent,
    toolChips: [...document.querySelectorAll('.ri-tool-chip')].map(el => el.textContent),
    metaJargonGone: ![...document.querySelectorAll('.ri-round')].some(el =>
      /messages|条消息|token/i.test(el.textContent)),
    visibleRoundRows: [...document.querySelectorAll('.ri-round')].filter(el =>
      typeof el.checkVisibility === 'function' ? el.checkVisibility() : el.offsetParent !== null).length,
    contrasts: {
      toolChip: contrastOf('.ri-tool-chip'),
      taskStatus: contrastOf('.ri-task-status'), technical: contrastOf('.ri-technical>summary'),
    },
  };
}
"""


def _resolve_assets() -> tuple[str, str]:
    """(main CSS, request_inspector.js) for the file:// page."""
    mains = sorted(glob.glob(os.path.join(ROOT, 'static', 'vite', 'assets',
                                           'main-*.css')))
    if not mains:
        raise SystemExit(
            'no static/vite/assets/main-*.css — run npm run build:frontend')
    sys.path.insert(0, ROOT)
    from tests._runtime_sections import orchestration_legacy_test_root
    legacy = orchestration_legacy_test_root()
    ri_js = os.path.join(legacy, 'static', 'js', 'core', 'request_inspector.js')
    if not os.path.isfile(ri_js):
        raise SystemExit(f'legacy root did not materialize {ri_js}')
    return mains[-1], ri_js


async def main() -> int:
    sys.path.insert(0, ROOT)
    from chromium_env import ensure_chromium_env
    ensure_chromium_env()
    from playwright.async_api import async_playwright

    main_css, ri_js = _resolve_assets()
    html_path = '/tmp/_ri_run_summary.html'
    failures: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox', '--allow-file-access-from-files'])
        for theme, width, height, label in (
            ('dark', 1280, 820, 'desktop-dark'),
            ('tofu', 1280, 820, 'desktop-tofu'),
            ('dark', 390, 844, 'mobile-dark'),
        ):
            with open(html_path, 'w', encoding='utf-8') as handle:
                handle.write(_PAGE.replace('__MAINCSS__', main_css)
                             .replace('__RIJS__', ri_js)
                             .replace('__THEME__', theme))
            page = await browser.new_page(viewport={'width': width, 'height': height})
            await page.goto('file://' + html_path)
            await page.wait_for_selector('.ri-round')
            await page.wait_for_timeout(250)
            result = await page.evaluate(_PROBE)
            print(f'\n=== {label} ===\n{json.dumps(result, ensure_ascii=False, indent=2)}')
            screenshot = f'/tmp/_ri_run_summary_{label}.png'
            await page.locator('.ri-drawer').screenshot(path=screenshot)
            print('screenshot ->', screenshot)
            for key in ('summaryCardGone', 'statesBlockGone',
                        'technicalOpenByDefault', 'taskHasPlainAction',
                        'metaJargonGone'):
                if not result[key]: failures.append(f'{label}: {key} is false')
            if result['taskHasRequestCount']: failures.append(f'{label}: request count leaked into task row')
            if '146 轮请求' not in result['technicalLabel']:
                failures.append(f'{label}: round-total label missing')
            if result['toolChips'] != ['web_search', 'read_files',
                                       'run_command', 'edit_file']:
                failures.append(f"{label}: tool chips wrong: {result['toolChips']}")
            if result['visibleRoundRows'] != 2:
                failures.append(f"{label}: expected 2 visible round rows, got {result['visibleRoundRows']}")
            if result['overflowX']: failures.append(f'{label}: horizontal overflow')
            for name, ratio in result['contrasts'].items():
                if ratio < 4.5: failures.append(f'{label}: {name} contrast {ratio}:1')
            if width <= 640:
                await page.locator('.ri-round').first.click()
                await page.wait_for_selector('.ri-drawer.ri-detail-active')
                detail = await page.evaluate("""() => ({
                  sideHeight: document.querySelector('.ri-side').clientHeight,
                  mainHeight: document.querySelector('.ri-main').clientHeight,
                  detailText: document.getElementById('debugContent').textContent,
                  overflowX: document.querySelector('.ri-drawer').scrollWidth >
                    document.querySelector('.ri-drawer').clientWidth,
                })""")
                print('mobile detail ->', json.dumps(detail, ensure_ascii=False))
                detail_shot = '/tmp/_ri_run_summary_mobile-detail.png'
                await page.locator('.ri-drawer').screenshot(path=detail_shot)
                print('screenshot ->', detail_shot)
                if detail['mainHeight'] < 250 or detail['sideHeight'] < 400:
                    failures.append(f'{label}: selected detail split is unusable')
                if '本轮执行搜索' not in detail['detailText']:
                    failures.append(f'{label}: selected detail did not render')
                if detail['overflowX']:
                    failures.append(f'{label}: selected detail overflows horizontally')
            await page.close()
        await browser.close()

    print('\n' + '=' * 60)
    if failures:
        print('FAIL')
        for failure in failures: print('  •', failure)
        return 1
    print('PASS — open-by-default technical details, turn-block tool chips, '
          'no summary/states noise, responsive layout, and contrast.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
