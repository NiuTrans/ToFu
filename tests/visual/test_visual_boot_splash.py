"""Native selection/copy must survive unchanged boot polling snapshots."""
import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import (
    ROOT, frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page,  # noqa: F401
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('kind', ['failed', 'starting'])
def test_boot_text_selection_survives_polling(
    frontend_visual_page, frontend_visual_origin, kind,
):
    page = frontend_visual_page
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    page.add_script_tag(path=native_module_path(
        '.native/boot-splash.js', ROOT / 'frontend/src/boot-splash.ts'))
    page.evaluate('''kind => {
      const phase = {key:'storage_sidecar', label:'Storage sidecar',
        status:kind === 'failed' ? 'failed' : 'running',
        error:'Storage connection refused'};
      window.bootSnapshot = {phases:[phase], phaseTotal:1,
        currentPhase:phase, failedPhase:phase, error:phase.error, elapsedS:2};
      window.bootController = createBootSplashController({
        document, log:console,
        schedule:{setTimeout:callback => {window.bootPoll=callback; return 1;},
          clearTimeout:() => {}},
        probeBoot:async () => ({kind, snapshot:window.bootSnapshot}),
        requestRetry:async () => true,
        copy:{splashTitle:()=>'Starting Tofu', splashSubtitle:()=>'Please wait',
          connecting:()=>'Connecting', elapsed:n=>`${n}s`,
          failedTitle:()=>'Startup failed', failedHint:()=>'Inspect the error',
          retry:()=>'Retry', retrying:()=>'Retrying', phaseLabel:(_,label)=>label},
      });
    }''', kind)
    card = page.locator('.boot-splash__card')
    card.wait_for(state='visible')
    for selector in ['.boot-splash__title', '.boot-splash__subtitle',
                     '.boot-splash__phase-label', '.boot-splash__error']:
        node = page.locator(selector)
        node.click(click_count=3)
        selected = page.evaluate('getSelection().toString()')
        assert selected.strip(), f'{selector} cannot be selected'
        # The real poll awaits a fresh transport snapshot. Selection must retain
        # its native text nodes through several identical successful responses.
        for _ in range(3):
            page.evaluate('async () => {bootPoll(); await Promise.resolve();}')
            assert page.evaluate('getSelection().toString()') == selected
        page.keyboard.press('Control+c')
        assert page.evaluate('navigator.clipboard.readText()') == selected
    page.evaluate('''async () => {
      bootSnapshot.currentPhase.error = 'Storage recovery exhausted';
      bootSnapshot.error = 'Storage recovery exhausted';
      bootSnapshot.phases[0].label = 'Storage recovery';
      bootPoll(); await Promise.resolve();
    }''')
    assert 'Storage recovery exhausted' in page.locator('.boot-splash__error').inner_text()
    assert page.locator('.boot-splash__phase-label').inner_text() == 'Storage recovery'
    page.evaluate('bootController.destroy()')
