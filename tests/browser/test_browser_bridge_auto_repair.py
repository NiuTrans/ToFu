"""Zero-input pairing through the shipped worker and an independent page realm.

Recovery preserves the user's browser session, exact server identity, silent
background repair, explicit foreground gestures and restart cleanup. Chrome
API boundaries are faked; the worker, transport and serialized mint are real.
"""
import json
from pathlib import Path

import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _src(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def auth_probe():
    return run_extension_probe('authBackoff')


def test_poll_fetch_carries_browser_cookies(auth_probe):
    assert all(row['credentials'] == 'include' for row in auth_probe['requests'])


def test_401_is_classified_by_body(auth_probe):
    assert 'Bridge auth failed' in auth_probe['responses'][0]['status']['lastError']
    assert 'proxy/SSO edge' in auth_probe['proxy']['status']['lastError']


def test_every_401_kicks_the_auto_repair_ladder(auth_probe):
    failures = [r for r in auth_probe['requests'] if r['responseKind'] != 'success']
    assert len(failures) == 13
    assert auth_probe['repairs'] == len(failures)


def test_repairing_copy_is_honest_about_automation(auth_probe):
    for row in auth_probe['responses']:
        assert 're-pairing automatically' in row['status']['lastError']
        assert 'set the Bridge Secret' not in row['status']['lastError']


def test_poll_reports_the_manifest_version(auth_probe):
    assert all(row['extVersion'] == 'test' for row in auth_probe['requests'])


def test_existing_page_mints_in_main_world_through_its_own_api():
    probe = run_extension_probe('repairFlow:existing')
    assert probe['outcome'] is True
    assert probe['mintCalls'] == ['browser-ext-autorepair']
    assert probe['scriptCalls'] == [{'target': {'tabId': 17}, 'world': 'MAIN',
                                     'args': ['https://tofu.test/proxy/15000']}]
    assert probe['secret'] == 'fresh-owned-key'
    assert probe['createdTabOptions'] == probe['removedTabIds'] == []


def test_existing_page_refusal_never_opens_another_copy():
    probe = run_extension_probe('repairFlow:existing-refused')
    assert probe['outcome'] is False
    assert len(probe['mintCalls']) == 1
    assert probe['secret'] == 'initial'
    assert probe['createdTabOptions'] == probe['removedTabIds'] == []


@pytest.mark.parametrize('scenario,success', [('hidden-success', True), ('hidden-refused', False)])
def test_hidden_repair_is_ledgered_before_wait_and_always_closed(scenario, success):
    probe = run_extension_probe('repairFlow:' + scenario)
    assert probe['outcome'] is success
    assert probe['createdTabOptions'] == [{'url': 'https://tofu.test/proxy/15000', 'active': False}]
    assert list(probe['ledgerAtWait'][0].values()) == [7]
    assert probe['removedTabIds'] == [7]
    assert probe['ledgerAfter'] == {}
    assert probe['status']['repairBusy'] is False


def test_popup_gesture_leaves_failed_foreground_tab_for_sign_in():
    probe = run_extension_probe('repairFlow:foreground-refused')
    assert probe['outcome'] == {'ok': False}
    assert probe['createdTabOptions'] == [{'url': 'https://tofu.test/proxy/15000', 'active': True}]
    assert probe['ledgerAtWait'] == [{}]
    assert probe['removedTabIds'] == []


def test_failed_hidden_repair_observes_cooldown():
    probe = run_extension_probe('repairFlow:cooldown')
    assert probe['outcome'] is probe['second'] is False
    assert len(probe['createdTabOptions']) == len(probe['mintCalls']) == 1


def test_repair_is_single_flight_and_reports_busy():
    probe = run_extension_probe('repairFlow:singleflight')
    assert probe['busy'] is True
    assert probe['second'] is False
    assert probe['outcome'] is True
    assert len(probe['mintCalls']) == 1
    assert probe['status']['repairBusy'] is False


def test_worker_restart_reaps_only_its_unfinished_repair_tabs():
    probe = run_extension_probe('repairReaper')
    assert probe['removed'] == [2, 5]
    assert probe['ledgerAfter'] == {}


def test_popup_demotes_the_manual_key_field():
    html = _src('browser_extension/popup.html')
    i_secret = html.index("id=\"bridgeSecret\"")
    head = html[:i_secret]
    assert '<details' in head and (
        '</details>' not in head
        or head.rindex('<details') > head.rindex('</details>')), (
        'the manual bridge-key input must live INSIDE a collapsed '
        '<details> — a visible secret field invites the exact manual step '
        'the 2026-08-04 decree forbids')
    assert 'id="repairRow"' in html and 'id="repairBtn"' in html, (
        'the popup’s ONE visible remedy is the automatic re-pair row')


def test_popup_js_wires_the_repair_row():
    js = _src('browser_extension/popup.js')
    assert "repairRow.style.display" in js and 'needsRepair' in js, (
        'the repair row must appear exactly when the background declares '
        'the credential dead')
    assert "{ type: 'repairNow' }" in js, (
        'the repair button must trigger the ladder’s user-gesture variant')


def test_manifests_moved_to_470_together():
    dev = json.loads(_src('browser_extension/manifest.json'))
    store = json.loads(_src('docs/chrome-web-store/manifest.store.json'))
    assert dev['version'] == store['version'], 'manifest skew'
    major, minor, _ = (int(x) for x in dev['version'].split('.'))
    assert (major, minor) >= (4, 7), (
        'auto re-pair landed without a version bump — installed bridges '
        'cannot be told apart from the manual-secret generation')
