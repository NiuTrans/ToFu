"""Live browser binds protocol drafting to the opened historical task."""
import json
import pytest
from tests.e2e.test_e2e_smoke import _wait_app_ready
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from tests.research.test_research_draft_protocol import _ARTIFACTS, _MODEL_REPLY, _fake_dispatch
pytestmark = pytest.mark.visual


def test_historical_idea_uses_original_record_despite_latest_direction(page, monkeypatch, assert_no_js_errors):
    from lib.research import records, persistence, draft, capabilities
    from lib.research.workspace import empty_workspace, save_workspace
    _wait_app_ready(page)
    direction = 'Historical draft evidence binding'
    original = {**_ARTIFACTS, 'survey_md':'Original source evidence only'}
    records.persist_record({'task_id':'historical-draft-source','user_id':1,'direction':direction,
        'lang':'en','status':'done','result':original})
    persistence.persist_survey(direction,'en','Newer conflicting survey',_ARTIFACTS['open_gaps'],user_id=1)
    persistence.persist_ideate(direction,'en',{'accepted':[{**_ARTIFACTS['accepted'][0],
        'title':'Newer conflicting idea'}],'rejected':[]},user_id=1)
    workspace = empty_workspace(direction,'en');workspace['selected_idea_id'] = 'idea_1'
    save_workspace(direction,'en',workspace,user_id=1,expected_revision=0)
    monkeypatch.setattr(capabilities,'build_capability_catalog',lambda **_: {
        'contract_version':'tofu.research-capabilities/v1','capabilities':[],'tools':[],'fingerprint':''})
    calls = []
    def dispatch(messages,**kwargs):
        calls.append((messages,kwargs))
        return _fake_dispatch(_MODEL_REPLY)(messages,**kwargs)
    monkeypatch.setattr(draft,'dispatch_stream',dispatch)
    page.evaluate("window.location.hash='/evolution/research'")
    page.locator('.research-record-item').filter(has_text=direction).click()
    page.locator('[data-rs-tab="workspace"]').click()
    button = page.locator('[data-tofu-action="_draftResearchProtocol(this)"]')
    button.wait_for()
    with page.expect_request(lambda request: '/research/workspace/draft-protocol' in request.url) as request:
        button.click()
    assert request.value.post_data_json['source_task_id'] == 'historical-draft-source'
    page.locator('[data-rsw-field="hypothesis"]').wait_for()
    page.wait_for_function("document.querySelector('[data-rsw-field=\"hypothesis\"]').value.includes('Per-layer learned')")
    assert len(calls) == 1 and calls[0][1]['user_id'] == 1
    prompt = json.dumps(calls[0][0])
    assert 'Original source evidence only' in prompt
    assert 'Newer conflicting survey' not in prompt and 'Newer conflicting idea' not in prompt

    with page.expect_response(lambda response: '/research/workspace' in response.url
                              and response.request.method == 'PUT') as response:
        page.locator('[data-tofu-action="_saveResearchWorkspace(this)"]').click()
    assert response.value.status == 200
    from lib.research.workspace import load_workspace
    saved = load_workspace(direction,'en',user_id=1)
    assert saved['selected_idea_source_task_id'] == 'historical-draft-source'

    # A source-specific intake draft must leave the user's unrelated creator draft intact.
    page.evaluate("""() => { localStorage.setItem('tofu.exploration.drafts.v1:' + encodeURIComponent(location.pathname) + ':1',
      JSON.stringify({version:1,drafts:{'["create"]':{fields:['Existing unrelated mission','','','','auto']}}})); }""")
    page.set_viewport_size({'width':390,'height':844})
    page.locator('[data-research-programme]').scroll_into_view_if_needed()
    assert page.locator('.rsw-draft-bar').evaluate('(el)=>el.scrollWidth <= el.clientWidth + 1')
    page.locator('[data-research-programme]').click()
    objective = page.locator('.exp-create-form [name="objective"]');objective.wait_for()
    assert objective.input_value() == direction
    assert page.locator('[data-research-source-note]').is_visible()
    objective.fill('Complete decisive validation from this original research')
    page.reload();_wait_app_ready(page)
    page.locator('.exp-create-form [name="objective"]').wait_for()
    assert page.locator('.exp-create-form [name="objective"]').input_value() == 'Complete decisive validation from this original research'
    saved_drafts = page.evaluate("""() => JSON.parse(localStorage.getItem('tofu.exploration.drafts.v1:' + encodeURIComponent(location.pathname) + ':1')).drafts""")
    assert saved_drafts['["create"]']['fields'][0] == 'Existing unrelated mission'
    scoped = saved_drafts['["create","research","historical-draft-source","idea_1"]']
    assert scoped['researchSource'] == {'task_id':'historical-draft-source','idea_id':'idea_1'}
