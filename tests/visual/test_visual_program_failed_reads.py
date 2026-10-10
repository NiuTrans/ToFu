"""Actual failed WASM reads retain truthful counters in the production card."""
import json
import uuid

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT

pytestmark = pytest.mark.visual
pytest_plugins = ('tests.tasks_pkg.test_javascript_dependency_chains', 'tests.support.frontend_visual')


@pytest.mark.parametrize('width,language,dark', [(1280, 'zh', False), (390, 'en', True)])
def test_failed_retained_read_card_reports_actual_work(
        local_gateway, frontend_visual_page, width, language, dark, tmp_path, request):
    from lib.tasks_pkg.handlers.tool_gateway import handle_execute_program
    from lib.tools.program_results import retain_program_result, describe_program_input
    from lib.tools.result_value import capture_result
    task, dispatched, _ = local_gateway
    from lib.agent_core.admission import fire_terminal_callbacks
    task['id'] = 'failed-read-' + uuid.uuid4().hex
    request.addfinalizer(lambda: fire_terminal_callbacks(task['id']))
    task['_executable_tool_catalog'] = [{'type':'function', 'function':{
        'name':'read_rows', 'parameters':{'type':'object', 'properties':{}}}}]
    entry = retain_program_result(task, 'read_rows', {}, 'source',
        capture_result('[{"amount":3},{"amount":8}]'), user_id=1)
    metadata = describe_program_input(task, entry, user_id=1, catalog_names={'read_rows'})
    code = 'const rows=' + metadata.read_expression + '; return rows.items.length;'
    handle_execute_program(task, {}, 'execute_program', 'failed-read', {'code':code},
        1, {'calls':[], 'llmRound':1}, {}, None, False)
    run = task['programRuns'][0]
    assert run['status'] == 'error' and run['stats']['outcome'] == 'error'
    assert run['stats']['result_reads'] == run['stats']['result_read_attempts'] == 1
    assert run['stats']['tool_calls'] == run['stats']['successful_tool_calls'] == 0
    assert not dispatched
    row = next(row for row in task['toolRounds'] if row.get('_programSynthetic'))
    page = frontend_visual_page
    page.set_viewport_size({'width':width, 'height':880})
    page.evaluate('''dark=>{
      document.documentElement.dataset.theme=dark?'dark':'light';
      document.documentElement.classList.toggle('dark',dark);
      document.body.innerHTML='<main id="program-fixture"></main>';
    }''', dark)
    page.add_style_tag(content='body{display:block;overflow:auto;height:auto}#program-fixture{max-width:960px;margin:24px auto;padding:0 16px}')
    messages = json.loads((ROOT/'frontend/src/i18n/locales'/f'{language}.json').read_text())
    page.evaluate('messages=>window.messages=messages', messages)
    page.add_script_tag(content=r'''
      window._featureFlags={debug_mode:false};
      window.Icon=()=>'<svg aria-hidden="true" viewBox="0 0 16 16"></svg>';
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window.t=(key,params)=>String(messages[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
    ''')
    for name, source in [('groups','ui/tool-groups/index.ts'), ('card','ui/program-execution-card.ts')]:
        page.add_script_tag(path=native_module_path(f'.native/program-failed-{name}.js',
            ROOT/'frontend/src/conversation'/source))
    page.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    page.evaluate('row=>{window.programRow=row;document.getElementById("program-fixture").innerHTML=_renderToolSlot(row,[row])}', row)
    card = page.locator('.ptc-card')
    assert card.is_visible()
    assert card.locator('.ptc-observation').count() == 0
    assert card.locator('.ptc-meta').inner_text() == messages['ptc.calls.successful'].replace('{done}','0').replace('{attempts}','0')
    assert messages['ptc.measurement.reused'].replace('{n}','1') in card.inner_text()
    assert 'TypeError' in card.inner_text()
    assert card.locator('.ptc-result').evaluate('node=>node.open') is True
    assert card.locator('.ptc-code').evaluate('node=>node.open') is False
    assert page.evaluate('''()=>{
      const card=document.querySelector('.ptc-card');
      return card.getBoundingClientRect().right<=innerWidth+1 && card.scrollWidth<=card.clientWidth+1;
    }''')
    card.screenshot(path=str(tmp_path/f'failed-read-{width}-{language}.png'))
    page.evaluate('()=>document.getElementById("program-fixture").innerHTML=_renderToolSlot(programRow,[programRow])')
    assert messages['ptc.measurement.reused'].replace('{n}','1') in card.inner_text()
    assert 'TypeError' in card.inner_text()
