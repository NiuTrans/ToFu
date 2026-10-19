"""tests/browser/test_browser_custom_controls.py — collector surfaces hidden-input controls.

Incident conv mu8e3bdwdqovh6 (2026-09-22): on the MLP CodeLab create form the
model could not read/click/fill ANY custom control — the MTD radios and
selects all resolved to nothing, so the autoresearch test container could not
be created. Root cause: component libraries (YourProvider MTD, Element-UI, Ant
Design) hide the real ``<input>`` (``display:none`` / 0×0) and render a styled
wrapper. The collector's ``_isVisible`` excludes the hidden input, and the
pointer sweep skips the (non-``cursor:pointer``) wrapper, so the element was
never indexed — and nothing downstream (read_page elements, text= click
resolution, fill_form) could target it.

Extension 5.6.0 adds a bounded custom-control sweep to
``_getInteractiveElements``: it surfaces the *visible wrapper* of hidden
radio/checkbox inputs (role=radio/checkbox, with label text + checked state +
the hidden input as the action target), combobox/select triggers, and any
currently-open dropdown options — so text= resolution and the CDP trusted
click can reach them. These pins guard that sweep against regression.
"""

from __future__ import annotations

import pytest

from tests._browser_extension_probe import run_extension_probe


pytestmark = pytest.mark.unit


def test_collector_surfaces_custom_radios_combobox_and_options():
    probe = run_extension_probe('customControls')

    roles = probe['byRole']
    # Both MTD radio wrappers surface with role + label + checked state, and
    # each points at its hidden <input> as the action target.
    assert roles.get('radio', 0) == 2
    radios = {r['text']: r for r in probe['radioTexts']}
    assert set(radios) == {'aipnlp', 'your-username'}
    assert radios['your-username']['checked'] is True
    assert radios['aipnlp']['checked'] is False
    assert all('input' in (r['target'] or '') for r in radios.values())

    # The select trigger surfaces as a combobox, and the open dropdown
    # options surface as options the model can click.
    assert probe['hasCombobox'] is True
    assert roles.get('combobox', 0) >= 1
    assert set(probe['optionTexts']) == {'autoresearch_agent', 'cpu_job'}

    # The sweep is bounded and reports its own budget usage.
    assert probe['sweep'] and probe['sweep']['added'] == probe['total']


def test_collector_reports_custom_control_sweep_counter():
    probe = run_extension_probe('customControls')
    sweep = probe['sweep']
    assert sweep['scanned'] >= sweep['added'] >= 1


# ── v5.7.0 interaction layer (the 1024-browser-style semantic actions) ──────
# The sweep made custom controls *discoverable*; these pins make them
# *actionable*: a semantic set_checked, a two-step custom-select, and the
# TARGET_* actionability taxonomy that tells the model WHY an action failed.

def test_set_checked_applies_and_verifies_custom_controls():
    probe = run_extension_probe('customControlInteract')

    # Hidden radio set via its visible wrapper; radio group exclusivity holds.
    assert probe['checkRadio']['applied'] is True
    assert probe['checkRadio']['checked'] is True
    assert probe['radioAChecked'] is True

    # Idempotent set on an already-true control is a no-op, not a toggle.
    assert probe['checkAlreadySet']['applied'] is True
    assert probe['checkAlreadySet']['already'] is True
    assert probe['checkAlreadySet']['checked'] is True

    # Checkbox on then off, verifying the real input state each time.
    assert probe['boxCheckedAfterOn'] is True
    assert probe['uncheckBox']['applied'] is True
    assert probe['uncheckBox']['checked'] is False
    assert probe['boxCheckedAfterOff'] is False


def test_set_checked_reports_actionability_codes():
    probe = run_extension_probe('customControlInteract')

    # A non-checkable target is classified, not silently mis-clicked.
    assert probe['notCheckable']['applied'] is False
    assert probe['notCheckable']['targetCode'] == 'TARGET_NOT_CHECKABLE'

    # A missing target is TARGET_NOT_FOUND.
    assert probe['notFound']['applied'] is False
    assert probe['notFound']['targetCode'] == 'TARGET_NOT_FOUND'


def test_actionable_point_forwards_to_hidden_input():
    probe = run_extension_probe('customControlInteract')

    # Clicking the wrapper resolves the real (hidden) input as the control,
    # measures the visible proxy for the trusted coordinate, and forwards.
    pf = probe['pointForward']
    assert pf['ok'] is True
    assert pf['forwarded'] is True
    assert pf['tag'] == 'input'
    assert 'input' in pf['targetSelector']
    assert pf['pointIsProxy'] is True

    # An element covered by an overlay reports TARGET_OBSCURED, naming it.
    assert probe['pointObscured']['targetCode'] == 'TARGET_OBSCURED'
    assert probe['pointObscured']['obscuring']


def test_select_option_two_step_custom_dropdown():
    probe = run_extension_probe('customControlInteract')

    # Custom MTD select: opens on trigger click, then clicks the option.
    assert probe['selectCustom']['selected'] is True
    assert probe['selectCustom']['text'] == 'cpu_job'

    # Native <select> fast path.
    assert probe['selectNative']['selected'] is True
    assert probe['selectNative']['value'] == 'bj'

    # A non-matching option is classified and lists what was available.
    assert probe['selectMissing']['selected'] is False
    assert probe['selectMissing']['targetCode'] == 'TARGET_NOT_SELECTABLE'
    assert 'cpu_job' in probe['selectMissing']['options']


def test_interaction_scripts_run_in_an_independent_page_realm():
    probe = run_extension_probe('isolatedInteraction')
    assert probe['checked']['applied'] is True
    assert probe['checked']['checked'] is True
    assert probe['located']['forwarded'] is True
    assert probe['selected']['selected'] is True
    assert probe['missing']['targetCode'] == 'TARGET_NOT_FOUND'
    assert probe['clicked']['targetCode'] == 'TARGET_NOT_FOUND'


def test_typing_uses_the_native_setter_for_each_control_class():
    probe = run_extension_probe('isolatedInteraction')
    assert probe['textarea']['success'] is True
    assert probe['textarea']['newValue'] == 'New notes'
    assert probe['input']['success'] is True
    assert probe['input']['newValue'] == 'New name'
