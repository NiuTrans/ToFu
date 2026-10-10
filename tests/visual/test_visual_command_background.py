"""Real Chromium check of independent command handoff and interrupt controls.

The disposable fixture uses the retained card renderer and production CSS. It
never opens a user conversation or touches the storage engine.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width,language', [(1100, 'zh'), (390, 'en')])
def test_running_command_has_two_usable_independent_actions(
        browser, tmp_path, width, language):
    context = browser.new_context(viewport={'width': width, 'height': 780})
    page = context.new_page()
    try:
        page.set_content('<!doctype html><html data-theme="tofu"><body>'
                         '<main id="command-card" style="width:100%;box-sizing:border-box">'
                         '</main></body></html>')
        page.add_style_tag(path=str(ROOT / 'static/styles.css'))
        locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
        page.evaluate("""locale => {
          window.t = (key, fallback) => locale[key] || fallback;
          window.escapeHtml = value => String(value ?? '').replaceAll('&','&amp;')
            .replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
          window.fixtureCalls = [];
          window.Api = {chat: {
            backgroundCommand: async (...args) => {
              window.fixtureCalls.push(['background', ...args]);
              return {backgrounded:true};
            },
            interruptCommand: async (...args) => {
              window.fixtureCalls.push(['interrupt', ...args]);
              return {interrupted:true};
            },
          }};
        }""", locale)
        page.add_script_tag(path=native_module_path(
            '.native/visual-command-background-presenter.js',
            ROOT / 'frontend/src/conversation/presentation/tool-command-execution-presentation.ts'))
        # Keep the JS owner's real implementation; the unit-tested fixture's
        # shell stubs supply unrelated singleton state with no project data.
        source = runtime_section('ui/tool_rounds.js', scope_prelude=False)
        controls = source[source.index('function _renderCmdInterruptBtn('):
                          source.index('function _renderSearchingRow(')]
        page.add_script_tag(content="""
          var _riTaskIdForRound = round => round._taskId;
          var showToast = text => {window.fixtureToast = text};
          var renderRunningToolCommandHtml = (...args) =>
            createToolCommandExecutionPresentation({translate: window.t})
              .renderRunningCommandHtml(...args);
        """ + controls + """
          window.renderFixtureCommand = () => {
            const round = {status:'searching',toolName:'run_command',
              roundNum:3,toolCallId:'call-3',_taskId:'task-3',
              query:'sleep 30',tStart:Date.now() - 39000};
            document.getElementById('command-card').innerHTML =
              renderRunningToolCommandHtml(round, {
                iconHtml:'',rootPillHtml:'',timerHtml:'39s',
                interruptHtml:_renderCmdBackgroundBtn(round) + _renderCmdInterruptBtn(round),
                rightControlsHtml:'',
              }, {bodyExpanded:false, outputExpanded:false});
          };
          window.backgroundFixtureClick = event =>
            _cmdBackgroundClick(event.currentTarget, event);
          window.interruptFixtureClick = event =>
            _cmdInterruptClick(event.currentTarget, event);
        """)
        page.evaluate('renderFixtureCommand()')
        background = page.locator('.ptool-cmd-handoff')
        interrupt = page.locator('.ptool-cmd-interrupt')
        assert background.is_visible() and interrupt.is_visible()
        assert background.bounding_box()['x'] < interrupt.bounding_box()['x']
        assert page.locator('#command-card').evaluate(
            '(el) => el.scrollWidth <= el.clientWidth + 1')
        screenshot = tmp_path / f'command-background-{language}-{width}.png'
        page.locator('#command-card').screenshot(path=str(screenshot))
        print(f'Live command controls {width}px ({language}): {screenshot}')
        background.evaluate("el => el.addEventListener('click', window.backgroundFixtureClick)")
        interrupt.evaluate("el => el.addEventListener('click', window.interruptFixtureClick)")
        background.click()
        assert background.is_disabled() and interrupt.is_enabled()
        interrupt.click()
        assert page.evaluate('fixtureCalls') == [
            ['background', 'task-3', 3, 'call-3'], ['interrupt', 'task-3']]
    finally:
        context.close()
