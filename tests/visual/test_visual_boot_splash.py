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
      const phase = {key:'storage_authority_sql', label:'Storage sidecar',
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
    page.locator('.boot-splash__toggle').click()
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


@pytest.mark.parametrize('theme,width', [('light', 1280), ('dark', 1280), ('tofu', 390)])
def test_boot_dock_leaves_shell_interactive_and_restores_send(
    frontend_visual_page, theme, width,
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate('''theme => {
      document.documentElement.dataset.theme = theme;
      document.body.innerHTML = '<main><button id="read-action">Read conversation</button><textarea id="userInput" aria-label="Message"></textarea><button id="sendBtn">Send</button></main>';
      document.querySelector('main').style.cssText='position:fixed;bottom:32px;left:16px;right:16px;display:grid;gap:16px';
      document.querySelector('#read-action').onclick=()=>document.body.dataset.read='yes';
    }''', theme)
    page.add_script_tag(path=native_module_path(
        '.native/composer-boot.js', ROOT / 'frontend/src/conversation/ui/composer-send-controls.ts'))
    page.add_script_tag(path=native_module_path(
        '.native/boot-splash.js', ROOT / 'frontend/src/boot-splash.ts'))
    page.evaluate('''() => {
      let blocked=true;
      const refresh=()=>updateComposerSendControls({document,
        translating:false,startupConnecting:false,turnBusy:false,stopping:false,
        hasAttachmentDraft:false,queueCount:0,sendBlocked:blocked,
        blockedLabel:'恢复中，暂时无法发送',
        labels:{send:'发送',sendTitle:'发送',stop:'停止',stopping:'停止中'},
        onSend:()=>{window.didSend=true},onStop:()=>{},requestRefresh:()=>refresh()});
      const phases=[
        {index:1,key:'frontend_assets',label:'前端已就绪',status:'done',duration_s:0.03},
        {index:2,key:'storage_boundary',label:'存储安全校验',status:'done',duration_s:0.12},
        {index:3,key:'storage_authority_sql',label:'恢复数据服务',status:'running',retry:2,
          error:'StorageError: recovery in progress'},
      ];
      window.bootSnapshot={phases,phaseTotal:9,currentPhase:phases[2],elapsedS:267};
      window.bootVerdict={kind:'starting',snapshot:bootSnapshot};
      window.bootController=createBootSplashController({document,log:console,
        schedule:{setTimeout:cb=>{window.bootPoll=cb;return 1},clearTimeout:()=>{}},
        probeBoot:async()=>bootVerdict,requestRetry:async()=>true,
        onBlockedChange:value=>{blocked=value;refresh()},
        copy:{splashTitle:()=> '服务正在后台启动',
          splashSubtitle:()=> '页面已可浏览，也可以编辑草稿；服务就绪后恢复发送。',
          connecting:()=> '连接中',elapsed:n=>`已等待 ${n}s`,
          failedTitle:()=> '启动失败',failedHint:()=> '可从失败阶段重试',
          retry:()=> '重试',retrying:()=> '自动重试',details:()=> '启动详情 · Debug logs',
          phaseLabel:(_,label)=>label},
      });
    }''')
    page.locator('.boot-splash__card').wait_for()
    page.locator('#read-action').click()
    assert page.locator('body').get_attribute('data-read') == 'yes'
    page.locator('#userInput').fill('重启期间保留的草稿')
    assert page.locator('#sendBtn').is_disabled()
    assert page.locator('.boot-splash__details').is_hidden()
    page.locator('.boot-splash__toggle').click()
    assert 'storage_authority_sql' in page.locator('.boot-splash__logs').inner_text()
    assert 'retry=2' in page.locator('.boot-splash__logs').inner_text()
    assert page.locator('.boot-splash__progress').get_attribute('aria-valuenow') == '22'
    assert page.locator('.boot-splash').bounding_box()['width'] <= width - 16
    page.screenshot(path=f'/tmp/tofu-restart-preview-{theme}-{width}.png')
    page.evaluate("async () => {bootVerdict={kind:'ready'};bootPoll();await Promise.resolve()}")
    assert page.locator('.boot-splash').count() == 0
    assert page.locator('#sendBtn').is_enabled()
    assert page.locator('#userInput').input_value() == '重启期间保留的草稿'
    assert page.evaluate('window.didSend !== true')
    page.evaluate('bootController.destroy()')


def test_shipped_app_keeps_draft_through_boot(browser, live_server):
    """Use the actual app graph, composer and stylesheet on a local server."""
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    phase = {'index': 3, 'key': 'storage_authority_sql', 'label': 'Storage recovery',
             'status': 'running', 'duration_s': None, 'error': None}
    boot = {'status': 'running', 'phases': [phase], 'currentPhase': phase,
            'phaseTotal': 9, 'elapsedS': 120, 'error': None}
    page.route('**/api/v4/boot', lambda route: route.fulfill(json=boot))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.locator('#userInput').wait_for(timeout=30000)
        page.locator('.boot-splash__card').wait_for()
        page.locator('#userInput').fill('Draft while the backend recovers')
        assert page.locator('#sendBtn').is_disabled()
        page.locator('#userInput').press('Enter')
        assert page.locator('#userInput').input_value() == 'Draft while the backend recovers'
        page.screenshot(path='/tmp/tofu-restart-shipped-app.png')
        boot['status'] = 'ready'
        page.locator('.boot-splash').wait_for(state='detached')
        page.wait_for_function("document.getElementById('sendBtn').disabled === false")
        assert page.locator('#userInput').input_value() == 'Draft while the backend recovers'
    finally:
        page.close()
