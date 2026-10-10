"""Composer keyboard outcomes in Chromium with retained production handlers."""
import pytest

from tests._runtime_sections import runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('mode', ['enter', 'ctrl_enter'])
def test_alt_enter_newline_selection_input_and_send(frontend_visual_page, mode):
    page = frontend_visual_page
    page.set_content('<textarea id="composer"></textarea>')
    page.add_script_tag(content=runtime_section('main/main_input_handling.js'))
    helpers = runtime_section('main/main_folders_mobile.js')
    start = helpers.index('function _insertNewlineAtCursor(')
    end = helpers.index('function _doSendOrGenerate(', start)
    page.add_script_tag(content=helpers[start:end])
    page.evaluate('''mode => {
      window.config = {inputSendMode: mode}; window.sends = 0; window.inputs = 0;
      window._doSendOrGenerate = () => { window.sends++; };
      const input = document.getElementById('composer');
      input.addEventListener('keydown', handleKeyDown);
      input.addEventListener('input', () => { window.inputs++; });
    }''', mode)
    field = page.locator('#composer')
    for shortcut in ['Alt+Enter', 'Alt+Shift+Enter', 'Control+Alt+Enter']:
        field.fill('beforeSELECTafter')
        field.evaluate('(input) => input.setSelectionRange(6, 12)')
        inputs_before = page.evaluate('inputs')
        field.press(shortcut)
        assert field.input_value() == 'before\nafter'
        assert field.evaluate('(input) => input.selectionStart === 7 && input.selectionEnd === 7')
        assert page.evaluate('inputs') == inputs_before + 1
        assert page.evaluate('sends') == 0
    field.fill('draft')
    field.press('End')
    field.press('Alt+Enter')
    assert field.input_value() == 'draft\n'
    field.press('Shift+Enter')
    assert field.input_value() == 'draft\n\n'
    # IME commit must remain untouched even with Alt held.
    assert field.evaluate('''input => input.dispatchEvent(new KeyboardEvent('keydown',
      {key: 'Enter', altKey: true, isComposing: true, bubbles: true, cancelable: true}))''')
    assert field.input_value() == 'draft\n\n'
    field.fill('send this')
    field.press('Control+Enter' if mode == 'ctrl_enter' else 'Enter')
    assert page.evaluate('sends') == 1
    assert field.input_value() == 'send this'
