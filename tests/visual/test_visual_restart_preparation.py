"""The restart UI keeps preparation separate from accepted process replacement."""
import pytest


from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page
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
    assert not page.locator('#updateModal').evaluate('(el) => el.classList.contains("open")')
    assert page.locator('#restart-progress-dock #updRestartCard').is_visible()
    assert page.locator('#updRestartCard').is_visible()
    assert page.locator('#updateRestartNowBtn').is_disabled()
    assert page.evaluate('enginePreparationTest.options.timeout') > 2_010_000
    assert page.locator('#updRestartCard').evaluate('(el) => el.classList.contains("is-preparing")')
    description = page.locator('#updRestartSub').inner_text()
    assert '编译' in description or 'compilation' in description.lower()
    page.wait_for_function("document.getElementById('updRestartTitle').textContent.includes('编译') || document.getElementById('updRestartTitle').textContent.includes('Compiling')")
    phase = page.locator('#updRestartPhase').inner_text()
    assert '编译' in phase or 'compiling' in phase.lower()
    page.locator('[aria-controls="restart-debug-logs"]').click()
    assert 'preparation=compiling' in page.locator('#restart-debug-logs').inner_text()
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


@pytest.mark.parametrize('locale,theme,width', [
    ('en', 'light', 1440), ('en', 'dark', 390),
    ('zh', 'light', 1440), ('zh', 'dark', 390),
])
def test_source_change_retry_stays_preparing(frontend_visual_page, screenshot_dir, locale, theme, width):
    """Exercise the actual observer/renderer without starting application storage."""
    import json
    from pathlib import Path
    from tests._runtime_sections import native_module_path, runtime_section_path

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    root = Path(__file__).resolve().parents[2]
    messages = json.loads((root / f'frontend/src/i18n/locales/{locale}.json').read_text())
    page.evaluate('''({messages, theme}) => {
      document.documentElement.dataset.theme = theme;
      document.body.innerHTML = '<div id="updateModal" class="modal-overlay open"><div id="updateModalBody"></div></div>' +
        '<button id="updateRestartNowBtn"><span>Restart</span></button>' +
        '<button id="updateRestartBtn">Restart after update</button><button id="updateShutdownBtn">Shutdown</button>';
      window.t = key => messages[key] || key;
      window.escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.runtimeScope = window;
      window._onReady = () => {}; // This harness does not boot unrelated update checks.
      window.enginePreparationTest = {
        now: Date.now(), phase: 'compiling', elapsed: 0, posts: [], decisions: 0,
        healthReads: 0, inFlight: 0, maxInFlight: 0, approvalReads: [],
      };
      Date.now = () => enginePreparationTest.now;
      window.Api = {health: {
        info: async () => { enginePreparationTest.healthReads++; return {bootId: 'unchanged-server'}; },
      }, update: {
        getLifecycleApproval: async id => {
          const test = enginePreparationTest;
          test.approvalReads.push(id);
          test.inFlight++;
          test.maxInFlight = Math.max(test.maxInFlight, test.inFlight);
          try {
            await new Promise(resolve => setTimeout(resolve, 30));
            return {record: {id, status: 'approved', preparation: {
              phase: test.phase, elapsedSeconds: test.elapsed, updatedAt: test.now / 1000,
              recovering: test.recovering === true,
            }}};
          } finally { test.inFlight--; }
        },
        decideLifecycleApproval: async () => { enginePreparationTest.decisions++; },
        restart: payload => {
          enginePreparationTest.posts.push(payload);
          return new Promise((_, reject) => { enginePreparationTest.reject = reject; });
        },
      }};
    }''', {'messages': messages, 'theme': theme})
    for name, source in [
        ('restart-source-retry-observer', 'frontend/src/features/settings/restart-preparation.ts'),
        ('restart-source-retry-dock', 'frontend/src/features/settings/restart-progress-dock.ts'),
    ]:
        page.add_script_tag(path=native_module_path(name, source))
    page.add_script_tag(path=runtime_section_path('update.js'))
    page.evaluate('''() => {
      void restartServer({approvalId: 'existing-human-approval'});
    }''')
    page.wait_for_function("document.getElementById('updRestartTitle')?.textContent === t('update.preparation.compiling')")
    page.evaluate("() => { enginePreparationTest.card = document.getElementById('updRestartCard'); }")

    # Miss the retrying window entirely on the first recovery observation.
    # The same compiling phase must now explain the automatic recovery.
    for phase, elapsed in [('compiling', 10), ('retrying', 20), ('checking', 30)]:
        page.evaluate('''({phase, elapsed}) => {
          const test = enginePreparationTest;
          test.now += (elapsed - test.elapsed) * 1000;
          test.elapsed = elapsed;
          test.phase = phase;
          test.recovering = true;
        }''', {'phase': phase, 'elapsed': elapsed})
        page.wait_for_function("phase => document.getElementById('updRestartTitle').textContent === t('update.preparation.' + phase)", arg=phase)
        page.wait_for_function("elapsed => document.getElementById('updRestartPhase').textContent.endsWith(t('update.restartElapsed').replace('%s', String(elapsed)))", arg=elapsed)
        page.wait_for_function("document.getElementById('updRestartSub').textContent === t('update.preparation.retryingHint')")
        assert page.locator('#updRestartSub').inner_text() == messages['update.preparation.retryingHint']
        assert page.locator('#updRestartBar').get_attribute('aria-valuetext') == messages[f'update.preparation.{phase}']
        assert page.locator('#updRestartBar').get_attribute('aria-valuenow') is None
        assert page.locator('#updRestartPct').inner_text() == '—'
        assert page.locator('#restart-progress-dock #updRestartCard.is-preparing').is_visible()
        assert page.evaluate("document.getElementById('updRestartCard') === enginePreparationTest.card")
        for control in ['#updateRestartNowBtn', '#updateRestartBtn', '#updateShutdownBtn']:
            assert page.locator(control).is_disabled()
        assert page.evaluate('''() => {
          const card = document.getElementById('updRestartCard');
          const title = document.getElementById('updRestartTitle');
          const rect = card.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight &&
            card.scrollWidth <= card.clientWidth + 1 && title.scrollWidth <= title.clientWidth + 1;
        }'''), 'retry copy must remain readable without horizontal overflow'
        if phase == 'retrying':
            page.locator('#restart-progress-dock').screenshot(
                path=f'{screenshot_dir}/restart-source-retrying-{locale}-{theme}-{width}.png')

    evidence = page.evaluate('''() => ({
      posts: enginePreparationTest.posts, decisions: enginePreparationTest.decisions,
      healthReads: enginePreparationTest.healthReads, maxInFlight: enginePreparationTest.maxInFlight,
      approvalReads: enginePreparationTest.approvalReads,
    })''')
    assert len(evidence['posts']) == 1, 'automatic recovery must not resubmit restart'
    assert evidence['posts'][0]['approvalId'] == 'existing-human-approval'
    assert not evidence['posts'][0].get('force')
    assert evidence['decisions'] == 0
    assert evidence['healthReads'] == 1, 'preparation must not start process-replacement probes'
    assert evidence['maxInFlight'] == 1
    assert set(evidence['approvalReads']) == {'existing-human-approval'}
    page.evaluate('''async () => {
      const error = new Error('Restart preparation could not finish.');
      error.status = 409;
      error.body = {restartPreflightFailed: true,
        detail: 'Application files kept changing; automatic retries exhausted.',
        requestId: 'source-recovery-exhausted'};
      enginePreparationTest.reject(error);
    }''')
    page.wait_for_function("document.getElementById('updRestartTitle')?.textContent === t('update.restartNotStartedTitle')")
    assert page.locator('#updRestartPhase').inner_text() == messages['update.restartNotStartedHint']
    assert page.locator('#updRestartCard .upd-log-copy').is_visible()
    assert 'source-recovery-exhausted' in page.locator('#updRestartCard .upd-log-pre').inner_text()
    for control in ['#updateRestartNowBtn', '#updateRestartBtn', '#updateShutdownBtn']:
        assert page.locator(control).is_enabled()
    assert page.evaluate('''() => {
      const card = document.getElementById('updRestartCard');
      const hint = document.getElementById('updRestartPhase');
      const rect = hint.getBoundingClientRect();
      return rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight &&
        card.scrollWidth <= card.clientWidth + 1 && hint.scrollWidth <= hint.clientWidth + 1;
    }'''), 'failure guidance must be readable on narrow screens'
    assert page.evaluate('enginePreparationTest.posts.length') == 1
    assert page.evaluate('enginePreparationTest.healthReads') == 1
    page.locator('#restart-progress-dock').screenshot(
        path=f'{screenshot_dir}/restart-source-exhausted-{locale}-{theme}-{width}.png')
