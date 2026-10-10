"""Visual smoke: the self-evolution workbench renders the goal journey."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL  # noqa: F401
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401,E501
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual




def test_evolution_workbench_renders_goal_journey(page, tmp_path):
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    page.locator('#evolutionBtn').click()
    panel = page.locator('.evo-workspace')
    panel.wait_for(state='visible', timeout=10000)
    page.evaluate("location.hash='/evolution/new'")
    page.locator('#evo-statement').fill(
        'visual smoke goal: verify the workbench renders')
    # This smoke tests the real goal/plan/launch journey, not a subprocess
    # checker. Exploration keeps learning rights restricted and bounded.
    page.locator('#evo-verification').select_option('explore')
    page.locator('.evo-form button[type=submit]').click()
    page.locator('.evo-detail-head', has_text='visual smoke goal').wait_for(
        state='visible', timeout=15000)
    confirm = page.locator('[data-evo-action=launch]')
    confirm.wait_for(state='visible', timeout=15000)
    assert not panel.locator('text=priors_source').count()

    import os
    out_dir = os.environ.get('EVO_SHOT_DIR', str(tmp_path))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / 'workbench.png').write_bytes(page.screenshot())

    # Confirming launches the episode; the running section lights up.
    confirm.first.click()
    running = page.locator('.evo-episode [data-status=running]')
    running.first.wait_for(state='visible', timeout=15000)
    assert page.locator('[data-evo-action=assess]').first.is_visible()
