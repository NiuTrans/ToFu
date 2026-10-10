"""Shipped workspace stops polling interrupted tasks without losing observations."""
from pathlib import Path
import subprocess

import pytest
from tests._runtime_sections import _require_frontend_bundler
from tests.e2e.test_e2e_smoke import _wait_app_ready
from lib.research.workspace import empty_workspace

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


def test_interrupted_action_preserves_workspace_and_releases_polling(page, tmp_path, assert_no_js_errors):
    _wait_app_ready(page)
    _require_frontend_bundler()
    entry = tmp_path / 'entry.ts'
    entry.write_text(
        f'export * from "{ROOT}/frontend/src/features/evolution/research/controllers/workspace";\n'
        f'export {{ featureRegistry }} from "{ROOT}/frontend/src/feature-registry";\n')
    bundle = tmp_path / 'workspace.js'
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
        '--bundle', '--format=iife', '--global-name=InterruptedWorkspace',
        f'--outfile={bundle}'], cwd=ROOT, check=True, capture_output=True, text=True)
    workspace = empty_workspace('Interrupted action fixture', 'en')
    workspace['hypothesis'] = 'Retain the original hypothesis after executor loss'
    page.add_script_tag(path=str(bundle))
    page.on('dialog', lambda dialog: dialog.accept())
    page.evaluate('''async workspace => {
      const r = InterruptedWorkspace, registry = r.featureRegistry;
      registry._researchStream = {direction:workspace.direction,lang:'en',accepted:[],rejected:[]};
      registry.Api = {research: {
        workspace:async()=>({workspace}), capabilities:async()=>({catalog:{tools:[],capabilities:[]}}),
        saveWorkspace:async payload=>({workspace:payload.workspace}),
      },tasks: {
        start:async()=>({taskId:'research_action_interrupted_fixture'}),
        get:async()=>{ window.interruptedReads++; return {status:'interrupted',result:null,
          meta:{interruption_reason:'executor_process_lost',settlement_recorded:false}}; },
      }};
      window.interruptedReads = 0;
      document.querySelectorAll('#researchWorkspace').forEach(node=>node.remove());
      const host=document.createElement('main');host.id='researchWorkspace';document.body.append(host);
      await r.loadResearchWorkspace(workspace.direction,'en');
      await r.startResearchAction('analyze');
    }''', workspace)
    alert = page.locator('#researchWorkspace .rs-alert.is-error')
    alert.wait_for()
    message = alert.inner_text()
    assert ('interrupted' in message.lower() and 'not resumed automatically' in message) or \
        ('已中断' in message and '尚未自动恢复' in message)
    assert page.locator('#researchWorkspace textarea').first.is_visible()
    assert 'Retain the original hypothesis' in page.locator('#researchWorkspace').inner_text() or \
        page.locator('#researchWorkspace textarea').evaluate_all(
            "nodes=>nodes.some(node=>node.value.includes('Retain the original hypothesis'))")
    page.wait_for_timeout(1700)
    assert page.evaluate('window.interruptedReads') == 1
    page.evaluate('InterruptedWorkspace.destroyResearchWorkspace()')
