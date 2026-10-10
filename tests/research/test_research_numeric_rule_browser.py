"""Live numerical rule editing keeps drafts and freezes the saved protocol."""
import pytest
from tests.research.test_research_workspace import storage as storage
from tests.research.test_research_history import draft_with_runs
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from lib.research.records import persist_record
from lib.research.workspace import load_workspace, save_workspace

DIRECTION = 'Explicit frozen numerical decision editor'

pytestmark=pytest.mark.visual


def test_explicit_rule_is_required_saved_and_preserved_in_live_workbench(page,storage,monkeypatch,assert_no_js_errors,tmp_path):
    import lib.research.capabilities as capabilities
    _wait_app_ready(page)
    monkeypatch.setattr(capabilities,'build_capability_catalog',lambda **_: {
        'contract_version':'tofu.research-capabilities/v1','capabilities':[],'tools':[],'fingerprint':''})
    draft=draft_with_runs(1);draft['direction']=DIRECTION
    saved=save_workspace(DIRECTION,'en',draft,user_id=1,expected_revision=0)
    persist_record({'task_id':'numeric-editor-fixture','user_id':1,'direction':DIRECTION,'lang':'en','status':'done',
        'result':{'survey_md':'Numerical decision rule.','accepted':[],'rejected':[]}})
    page.evaluate("window.location.hash='/evolution/research'")
    page.locator('.research-record-item').filter(has_text=DIRECTION).click()
    page.locator('[data-rs-tab="workspace"]').click()
    direction=page.locator('#research-rule-direction');direction.wait_for()
    threshold=page.locator('#research-rule-minimum_effect')
    multiplier=page.locator('#research-rule-uncertainty_multiplier')
    assert direction.input_value()=='' and threshold.input_value()=='' and multiplier.input_value()==''
    calls=_SENTINEL['stream_calls'];dialogs=[]
    def dismiss(dialog):dialogs.append(dialog.message);dialog.dismiss()
    page.on('dialog',dismiss)
    experiment=page.locator('[data-tofu-action="_startResearchAction(\'experiment\',this)"]')
    experiment.click()
    page.locator('.rs-alert.is-error').wait_for()
    assert not dialogs
    direction.select_option('lower');threshold.fill('-0.05');multiplier.fill('0')
    page.locator('[data-tofu-action="_saveResearchWorkspace(this)"]').click()
    page.locator('.rs-alert.is-error').wait_for()
    assert load_workspace(DIRECTION,'en',user_id=1)['revision']==saved['revision']
    assert direction.input_value()=='lower' and threshold.input_value()=='-0.05'
    multiplier.fill('2')
    page.locator('[data-tofu-action="_saveResearchWorkspace(this)"]').click()
    page.wait_for_function("!document.querySelector('#researchWorkspace .rs-alert.is-error')")
    frozen=load_workspace(DIRECTION,'en',user_id=1)
    assert frozen['protocol']['decision_rule']=={'direction':'lower','minimum_effect':-.05,'uncertainty_multiplier':2}
    assert frozen['runs'][0]['validity']=='stale'
    experiment.click()
    page.wait_for_function('true')
    assert len(dialogs)==1 and '-0.05' in dialogs[0] and '2' in dialogs[0]
    assert _SENTINEL['stream_calls']==calls
    page.set_viewport_size({'width':390,'height':844})
    page.locator('[data-rsw-decision-rule]').scroll_into_view_if_needed()
    assert page.locator('[data-rsw-decision-rule]').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(tmp_path/'research-numeric-rule-mobile.png'))
