"""The common browser setup must dismiss onboarding before a delayed trigger."""
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


def test_dismissal_survives_onboarding_trigger_after_helper_returns(frontend_visual_page):
    from tests.conftest import _dismiss_onboarding_modals

    page = frontend_visual_page
    # Use the actual first-run owner with only its locale/action-registry ports.
    # No timers race the test: the delayed startup trigger runs after dismissal.
    page.add_script_tag(content="""
      window.runtimeScope = {};
      window.t = key => key;
      window.Icon = () => '';
      window.escapeHtml = value => {
        const node = document.createElement('span'); node.textContent = value;
        return node.innerHTML;
      };
      window.TofuModules = {resolveAction: name => runtimeScope[name]};
    """)
    page.add_script_tag(path=str(ROOT / 'frontend/src/runtime/sections/onboarding.js'))
    _dismiss_onboarding_modals(page)
    opened = page.evaluate('() => runtimeScope.maybeShowOnboarding()')
    assert opened is False
    assert page.locator('.modal-overlay.open').count() == 0
    assert page.evaluate("localStorage.getItem('tofu_onboarding_v1_done')") == '1'
