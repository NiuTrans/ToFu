"""Command working directory shares the command tray in the real browser."""

import pytest

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['tofu', 'light'])
def test_command_directory_is_inside_code_tray(browser, tmp_path, width, theme):
    # This visual unit runs Chromium against the shipped CSS and real typed
    # presenter without requiring the application's storage engine to boot.
    context = browser.new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    try:
        page.set_content(f'''<!doctype html><html data-theme="{theme}"><body>
          <main style="max-width:100%;padding:12px"><div id="cards"></div></main>
          </body></html>''')
        page.add_style_tag(path=str(ROOT / 'static/styles.css'))
        page.add_script_tag(path=native_module_path(
            '.native/visual-command-cwd.js',
            ROOT / 'frontend/src/conversation/presentation/tool-command-execution-presentation.ts'))
        page.evaluate("""() => {
      const presenter = createToolCommandExecutionPresentation({translate: key => key});
      const header = {iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''};
      const closed = {bodyExpanded:false,outputExpanded:false};
      const directory = '/work/' + 'project/'.repeat(20);
      const round = {toolName:'run_command',query:'npm test',toolCallId:'cwd-fixture',
        toolArgs:{working_dir:directory}};
      const longRound = {...round, toolCallId:'cwd-fold',
        query:'npm test -- ' + 'spec '.repeat(30),
        toolArgs:{working_dir:directory,description:'Check all tests'}};
      document.getElementById('cards').innerHTML =
        presenter.renderRunningCommandHtml(round,header,closed)
        + presenter.renderSettledCommandHtml(round,{exitCode:0},header,closed)
        + presenter.renderSettledCommandHtml(longRound,{exitCode:0},header,closed);
    }""")
        blocks = page.locator('.ptool-cmd-block')
        assert blocks.count() == 3
        for index in (0, 1):
            block = blocks.nth(index)
            assert block.locator('.ptool-cmd-code .ptool-cmd-cwd').is_visible()
            assert block.locator('.ptool-cmd-code').inner_text().endswith('$ npm test')
            assert block.locator('.ptool-cmd-reason').count() == 0
            dimensions = block.evaluate('''el => ({width:el.clientWidth, scroll:el.scrollWidth,
              children:[...el.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right>el.getBoundingClientRect().right+1)
                .map(n=>({class:n.className,width:n.getBoundingClientRect().width,text:n.textContent.slice(0,90)}))})''')
            assert dimensions['scroll'] <= dimensions['width'] + 1, dimensions
        assert page.locator('.ptool-cmd-cwd').first.evaluate(
            '(el) => getComputedStyle(el).color !== getComputedStyle(el.parentElement).color')
        folded = blocks.nth(2)
        assert folded.locator('.ptool-cmd-cwd').count() == 1
        assert not folded.locator('.ptool-cmd-cwd').is_visible()
        # The retained interaction owner toggles this class (covered by the
        # wiring contract); real CSS reveals both lines in the same pane.
        folded.evaluate('(el) => el.classList.add("cmd-open")')
        assert folded.locator('.ptool-cmd-cwd').is_visible()
        assert folded.locator('.ptool-cmd-code').inner_text().splitlines()[1].startswith('$ npm test --')
        if theme == 'tofu':
            screenshot = tmp_path / f'command-cwd-{width}.png'
            page.locator('#cards').screenshot(path=str(screenshot))
            print(f'Command-card {width}px screenshot: {screenshot}')
    finally:
        context.close()
