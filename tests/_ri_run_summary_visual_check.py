"""Real-browser acceptance check for the Run details drawer.

This is intentionally an opt-in instrument rather than a pytest case because
it launches Chromium and writes screenshots to ``/tmp``.

Current design contract (2026-09-22 flat wire feed, owner directive):
  * ONE flat message scroll for the whole conversation — every message
    exactly once. Turn, agent and rebuild sections render as divider rows
    between the shared debug message blocks.
  * The turn nav (‹ 回复 #N › stepper + jump list) is a SCROLL model:
    jumping scrolls to the divider; it never loads or switches a view.
  * NO task list, NO round list, NO single-round detail pane — the
    drill-down (and the 264px `.ri-side` before it) is gone.
  * Each turn divider carries status, preview, time and the per-turn
    耗时分析 entry; a per-turn Turn Trace renders in place with a
    back-to-feed bar.

Run: ``python tests/_ri_run_summary_visual_check.py``
(requires ``npm run build:frontend`` for the stylesheet)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_PAGE = r"""
<!doctype html><html data-theme="__THEME__"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="file://__MAINCSS__">
<link rel="stylesheet" href="file://__TRACECSS__">
</head><body>
<div class="ri-drawer" id="riDrawer" style="display:none">
  <div class="ri-head">
    <span class="ri-title">调试消息</span>
    <div class="ri-turn-nav" id="riTurnNav" hidden>
      <button class="ri-turn-step icon-box" id="riTurnPrev" type="button">‹</button>
      <button class="ri-turn-pos" id="riTurnPos" type="button"><span class="ri-turn-label">轮次</span> <span class="ri-turn-readout" id="riTurnReadout"></span></button>
      <button class="ri-turn-step icon-box" id="riTurnNext" type="button">›</button>
    </div>
    <span class="ri-head-actions"><button class="debug-clear icon-box">×</button></span>
  </div>
  <div class="ri-main"><div class="debug-panel" id="debugPanel">
    <div class="debug-header"><span id="debugTitle">调试消息</span></div>
    <div id="debugContent"></div>
  </div></div>
  <div class="ri-turn-jump" id="riTurnJump" hidden></div>
</div>
<script>
window.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
window.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}" height="${size||14}"></svg>`;
const labels = {
  'ri.empty':'该会话暂无任务记录','ri.loading':'加载中…',
  'ri.expired':'详细记录已清理，无法展开运行过程',
  'ri.expiredHint':'详细记录约保留 30 天后清理；回复与工具卡片仍在上方的对话里',
  'ri.loadFailed':'加载失败，请检查网络后重试','ri.retry':'重试',
  'ri.statusRunning':'任务正在处理','ri.statusDone':'任务已完成',
  'ri.statusFailed':'任务未完成','ri.statusStopped':'任务已停止','ri.statusUnknown':'状态未知',
  'ri.feedTurns':'{n} 轮回复','ri.feedTruncated':'内容过长，最早的 {n} 轮回复未展开',
  'ri.rebuildNote':'上下文在此被压缩或改写：仅前 {k} 条与之前一致，其后 {d} 条为重建后的内容',
  'ri.agentSection':'子代理执行过程','ri.backToFeed':'返回消息流',
  'ri.traceEntry':'耗时分析','ri.traceEntryHint':'每个阶段花了多久',
  'ri.turnChip':'回复 #{n}','ri.turnChipTip':'定位到这次运行对应的回复',
  'ri.earlierShort':'更早','ri.jumpToTurn':'跳转到某一轮','ri.turns':'轮次',
  'ri.prevTurn':'上一轮','ri.nextTurn':'下一轮',
  'ri.timeJustNow':'刚刚','ri.timeMinAgo':'{n} 分钟前',
  'ri.timeHoursAgo':'{n} 小时前','ri.timeDaysAgo':'{n} 天前',
  'stream.roundMessages':'第 {round} 轮 · {n}条',
  'debug.structContent':'内容','debug.structReasoning':'思考过程',
  'debug.structToolCalls':'工具调用','debug.structToolResult':'工具结果',
  'debug.structFields':'其他字段','debug.structRawJson':'原始 JSON','debug.structImage':'图片',
};
window.t = (key, vars) => {
  let value = labels[key] || key;
  for (const [name, replacement] of Object.entries(vars || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
window.activeConvId = 'conv-visual'; window.debugVisible = false;
window.DebugShellState = {
  visible: false, activeConversationId: 'conv-visual',
  requests: {}, cache: {}, conversations: [{ id: 'conv-visual' }],
  recordSnapshot() {}, reportError() {},
};
window.ConversationTurnRead = {
  state: () => ({ turnsById: {
      t1: { turnId: 't1', actor: 'assistant' },
      t2: { turnId: 't2', actor: 'assistant' } },
    attemptsById: {} }),
  ordered: () => [
    { turnId: 't1', actor: 'assistant' },
    { turnId: 't2', actor: 'assistant' }],
};
const NOW = 1786467600000;
const U = (text) => ({ role: 'user', content: text });
const A = (text) => ({ role: 'assistant', content: text });
const TC = (name, args) => ({ role: 'assistant', content: '',
  tool_calls: [{ id: 'call-1', type: 'function',
    function: { name, arguments: args } }] });
const TOOL = (text) => ({ role: 'tool', tool_call_id: 'call-1', content: text });
window.Api = { tasks: {
  convWire: async () => ({
    convId: 'conv-visual',
    messages: [
      { role: 'system', content: '你是 Tofu，一个部署在用户机器上的助手。' },
      U('整理上周的笔记，把要点汇总成一份清单'),
      A('好的，我先看一下笔记目录里有什么。'),
      TC('list_dir', '{"path":"notes/2026-W37"}'),
      TOOL('[{"name":"week-summary.md"},{"name":"reading.md"}]'),
      A('找到了两份笔记，我读取并汇总要点：\n\n1. 存储层完成了重试预算改造\n2. 前端完成了消息流平铺重构'),
      U('调研一下最新的模型发布，重点关注长上下文'),
      A('我先搜一下本周的发布动态。'),
      TC('web_search', '{"query":"new model releases long context September 2026"}'),
      TOOL('[{"title":"Provider X releases 2M context model"}]'),
      A('本周要点：Provider X 发布了 200 万上下文窗口的模型……'),
    ],
    sections: [
      { type: 'turn', msgIndex: 0, turnId: 't1', taskId: 'task-090-demo',
        createdAt: NOW - 3600000, live: false, status: 'done',
        preview: '整理上周的笔记' },
      { type: 'turn', msgIndex: 6, turnId: 't2', taskId: 'task-143-demo',
        createdAt: NOW - 600000, live: true, status: 'running',
        preview: '调研一下最新的模型发布' },
      { type: 'rebuild', msgIndex: 8, kept: 8, dropped: 2 },
      { type: 'agent', msgIndex: 9, agentId: 'researcher',
        taskId: 'task-143-demo#agent:researcher', keptPrefix: 8 },
    ],
    turns: [
      { turnId: 't1', taskId: 'task-090-demo', msgIndex: 0,
        createdAt: NOW - 3600000, live: false, status: 'done',
        preview: '整理上周的笔记' },
      { turnId: 't2', taskId: 'task-143-demo', msgIndex: 6,
        createdAt: NOW - 600000, live: true, status: 'running',
        preview: '调研一下最新的模型发布' },
    ],
    coverage: 'full',
  }),
  getTrace: async () => ({ eventsAvailable: true, tStart: NOW - 5000,
    totalMs: 42000, summary: { llmMs: 30000, toolMs: 8000, ttftMs: 1200 },
    spans: [], gaps: [], statusHistory: [], clientObservations: [],
    running: true }),
} };
</script>
<script src="file://__DBGJS__"></script>
<script src="file://__RIJS__"></script>
<script>openRequestInspector();</script>
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
  const main = document.querySelector('.ri-main');
  const turnNav = document.getElementById('riTurnNav');
  const content = document.getElementById('debugContent');
  return {
    viewport:[innerWidth,innerHeight], drawer:[drawer.clientWidth,drawer.clientHeight],
    main:[main.clientWidth,main.clientHeight],
    turnNavVisible: turnNav && !turnNav.hidden,
    turnReadout: document.getElementById('riTurnReadout').textContent,
    overflowX: drawer.scrollWidth > drawer.clientWidth || document.documentElement.scrollWidth > innerWidth,
    drillDownGone: !document.querySelector('.ri-task') && !document.querySelector('.ri-round') &&
      !document.querySelector('.ri-technical') && !document.querySelector('.ri-nav') &&
      !document.querySelector('.ri-side'),
    firstChildIsTurnDivider: content.firstElementChild &&
      content.firstElementChild.classList.contains('ri-feed-turn'),
    turnDividers: document.querySelectorAll('.ri-feed-turn').length,
    rebuildDividers: document.querySelectorAll('.ri-feed-rebuild').length,
    agentDividers: document.querySelectorAll('.ri-feed-agent').length,
    messageBlocks: document.querySelectorAll('#debugContent .debug-msg-block').length,
    turnChips: [...document.querySelectorAll('.ri-feed-turn .ri-turn-chip')].map(el => el.textContent),
    traceButtons: document.querySelectorAll('.ri-feed-turn .ri-feed-trace').length,
    titleText: document.getElementById('debugTitle').textContent,
    contrasts: {
      turnPreview: contrastOf('.ri-feed-turn-preview'),
      taskStatus: contrastOf('.ri-task-status'),
      rebuild: contrastOf('.ri-feed-rebuild'),
    },
  };
}
"""


def _resolve_assets() -> tuple[str, str, str, str]:
    """(composed application CSS, request_inspector.js) for the file:// page.

    The drawer's rules live in the composed application sheet
    ``static/styles.css`` (the same sheet index.html links); the Vite
    ``main-*.css`` chunk does not carry them."""
    main_css = os.path.join(ROOT, 'static', 'styles.css')
    if not os.path.isfile(main_css):
        raise SystemExit(
            'no static/styles.css — run npm run build:frontend')
    sys.path.insert(0, ROOT)
    from tests._runtime_sections import orchestration_legacy_test_root
    legacy = orchestration_legacy_test_root()
    ri_js = os.path.join(legacy, 'static', 'js', 'core', 'request_inspector.js')
    dbg_js = os.path.join(legacy, 'static', 'js', 'core', 'debug_panel.js')
    if not os.path.isfile(ri_js) or not os.path.isfile(dbg_js):
        raise SystemExit('legacy root did not materialize the core bundles')
    trace_css = os.path.join(ROOT, 'static', 'request-inspector-trace.css')
    return main_css, trace_css, dbg_js, ri_js


async def main() -> int:
    sys.path.insert(0, ROOT)
    from chromium_env import ensure_chromium_env
    ensure_chromium_env()
    from playwright.async_api import async_playwright

    main_css, trace_css, dbg_js, ri_js = _resolve_assets()
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
                             .replace('__TRACECSS__', trace_css)
                             .replace('__DBGJS__', dbg_js)
                             .replace('__RIJS__', ri_js)
                             .replace('__THEME__', theme))
            page = await browser.new_page(viewport={'width': width, 'height': height})
            await page.goto('file://' + html_path)
            await page.wait_for_selector('.ri-feed-turn')
            await page.wait_for_timeout(250)
            result = await page.evaluate(_PROBE)
            print(f'\n=== {label} ===\n{json.dumps(result, ensure_ascii=False, indent=2)}')
            screenshot = f'/tmp/_ri_run_summary_{label}.png'
            await page.locator('.ri-drawer').screenshot(path=screenshot)
            print('screenshot ->', screenshot)
            for key in ('drillDownGone', 'firstChildIsTurnDivider',
                        'turnNavVisible'):
                if not result[key]: failures.append(f'{label}: {key} is false')
            if result['turnReadout'] != '回复 #2':
                failures.append(f"{label}: turn readout wrong: {result['turnReadout']!r}")
            if result['turnDividers'] != 2 or result['rebuildDividers'] != 1 \
                    or result['agentDividers'] != 1:
                failures.append(f"{label}: dividers wrong: "
                                f"{result['turnDividers']}/{result['rebuildDividers']}/{result['agentDividers']}")
            if result['messageBlocks'] != 11:
                failures.append(f"{label}: expected 11 message blocks, got {result['messageBlocks']}")
            if result['turnChips'] != ['回复 #1', '回复 #2']:
                failures.append(f"{label}: turn chips wrong: {result['turnChips']}")
            if result['traceButtons'] != 2:
                failures.append(f"{label}: expected 2 trace entries, got {result['traceButtons']}")
            if 'Messages (11)' not in result['titleText']:
                failures.append(f"{label}: title summary wrong: {result['titleText']!r}")
            if result['overflowX']: failures.append(f'{label}: horizontal overflow')
            for name, ratio in result['contrasts'].items():
                if ratio < 4.5: failures.append(f'{label}: {name} contrast {ratio}:1')
            if width <= 640:
                await page.locator('.ri-feed-turn .ri-feed-trace').first.click()
                await page.wait_for_selector('#debugContent .tr-wrap')
                detail = await page.evaluate("""() => ({
                  traceVisible: !!document.querySelector('#debugContent .tr-wrap'),
                  backBar: !!document.querySelector('#debugContent .ri-feed-back'),
                  overflowX: document.querySelector('.ri-drawer').scrollWidth >
                    document.querySelector('.ri-drawer').clientWidth,
                })""")
                print('mobile trace ->', json.dumps(detail, ensure_ascii=False))
                detail_shot = '/tmp/_ri_run_summary_mobile-trace.png'
                await page.locator('.ri-drawer').screenshot(path=detail_shot)
                print('screenshot ->', detail_shot)
                if not detail['traceVisible'] or not detail['backBar']:
                    failures.append(f'{label}: trace view or back bar missing')
                if detail['overflowX']:
                    failures.append(f'{label}: trace view overflows horizontally')
                await page.locator('.ri-feed-back button').click()
                await page.wait_for_selector('.ri-feed-turn')
                restored = await page.evaluate(
                    "document.querySelectorAll('#debugContent .debug-msg-block').length")
                if restored != 11:
                    failures.append(f'{label}: back-to-feed restored {restored} blocks, want 11')
            await page.close()
        await browser.close()

    print('\n' + '=' * 60)
    if failures:
        print('FAIL')
        for failure in failures: print('  •', failure)
        return 1
    print('PASS — flat wire feed with turn/agent/rebuild dividers, scroll-model '
          'turn nav, in-place trace, responsive layout, and contrast.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
