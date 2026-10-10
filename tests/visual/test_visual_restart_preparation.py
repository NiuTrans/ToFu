"""The restart UI keeps preparation separate from accepted process replacement."""
import pytest

pytestmark = pytest.mark.visual


def test_engine_preparation_remains_visible_and_failure_restores_controls(page, screenshot_dir):
    page.wait_for_function('typeof restartServer === "function"')
    page.evaluate('''() => {
      window.enginePreparationTest = {};
      Api.update.getLifecycleApproval = async () => ({record: {preparation: {
        phase: 'compiling', elapsedSeconds: 42, updatedAt: Date.now() / 1000
      }}});
      Api.update.restart = (_payload, options) => {
        enginePreparationTest.options = options;
        return new Promise((_resolve, reject) => {
          enginePreparationTest.reject = reject;
        });
      };
      enginePreparationTest.running = restartServer({approvalId: 'isolated-browser-test'});
    }''')
    page.wait_for_function('!!enginePreparationTest.reject')
    assert page.locator('#updateModal').evaluate('(el) => el.classList.contains("open")')
    assert page.locator('#updRestartCard').is_visible()
    assert page.locator('#updateRestartNowBtn').is_disabled()
    assert page.evaluate('enginePreparationTest.options.timeout') > 2_010_000
    assert page.locator('#updRestartCard').evaluate('(el) => el.classList.contains("is-preparing")')
    description = page.locator('#updRestartSub').inner_text()
    assert '编译' in description or 'compilation' in description.lower()
    page.wait_for_function("document.getElementById('updRestartTitle').textContent.includes('编译') || document.getElementById('updRestartTitle').textContent.includes('Compiling')")
    phase = page.locator('#updRestartPhase').inner_text()
    assert '编译' in phase or 'compiling' in phase.lower()
    assert page.locator('#updRestartPct').inner_text() == '—'
    assert page.locator('#updRestartBar').get_attribute('aria-valuenow') is None
    page.evaluate('''() => { Api.update.getLifecycleApproval = async () => ({record: {
      preparation: {phase: 'frontend', elapsedSeconds: 53, updatedAt: Date.now() / 1000}
    }}); }''')
    page.wait_for_function("document.getElementById('updRestartTitle').textContent.includes('页面资源') || document.getElementById('updRestartTitle').textContent.includes('page resources')")
    page.screenshot(path=f'{screenshot_dir}/restart-engine-preparing.png')
    page.evaluate('''async () => {
      const error = new Error('Matching storage engine could not be prepared.');
      error.status = 409;
      error.body = {restartPreflightFailed: true,
        detail: 'frontend artifact rebuild failed with exit 1; build output tail:\\nsynthetic-vite-failure <script>unsafe</script>',
        requestId: 'restart-preflight-test'};
      enginePreparationTest.reject(error);
      await enginePreparationTest.running;
    }''')
    assert 'Matching storage engine could not be prepared.' in page.locator('#updRestartSub').inner_text()
    # The long detail tail lives in the copyable log block, not the sub line.
    log_text = page.locator('#updRestartCard .upd-log-pre').inner_text()
    assert 'synthetic-vite-failure <script>unsafe</script>' in log_text
    assert 'requestId: restart-preflight-test' in log_text
    assert page.locator('#updRestartCard .upd-log script').count() == 0
    assert page.locator('#updRestartCard .upd-log-copy').is_visible()
    assert page.locator('#updateRestartNowBtn').is_enabled()
    assert page.locator('#updRestartCard button').count() > 0
    page.screenshot(path=f'{screenshot_dir}/restart-engine-preparation-failed.png')
