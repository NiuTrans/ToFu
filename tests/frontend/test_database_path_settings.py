"""Exercise the retained template and production owner; only the API is faked."""
from pathlib import Path
import json
import re
import pytest
from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'frontend/src/features/settings/database.ts'

@pytest.fixture(scope='module')
def database_bundle():
    with compiled_typescript(MODULE, contents="""
import './database';
import { installActionRegistry } from '../../action-registry';
import { featureRegistry } from '../../feature-registry';
installActionRegistry((name) => featureRegistry[name]);
""", expose_feature_registry_to_window=True) as bundle:
        yield bundle

SETUP = r'''
const fs = require('fs'), path = require('path');
const { setup } = require(process.env.JSDOM_HARNESS);
const root = process.argv[3];
const messages = JSON.parse(fs.readFileSync(path.join(root, 'frontend/src/i18n/locales/en.json')));
const clone = x => JSON.parse(JSON.stringify(x));
const calls = [], probes = [], confirmations = [], toasts = [];
const { window, document, check, report } = setup({ root,
 html: '<!doctype html><body>' + fs.readFileSync(path.join(root, 'frontend/src/settings-panels/database.html')) + '</body>',
 targets: [process.argv[2]], globals: {
  t: (key, values = {}) => (messages[key] || key).replace(/\{(\w+)\}/g, (m, n) => String(values[n] ?? m)),
  showToast: message => toasts.push(message),
  showConfirm: async message => { confirmations.push(message); return false; },
  restartServer: () => { throw Error('Never restart in UI tests'); },
 }});
const byId = id => document.getElementById(id);
const visible = node => !!node && !node.closest('[hidden]');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let i=0; i<8; i++) await tick(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
function fixture(backend = 'tofudb') { return {
 engine: {backend, ready:true, state:'ready', dataRoot:'/original', authorityPath:'/original', filesystem:{class:'local-block'}},
 backends: {current:backend, sqlite:{available:true}, tofudb:{available:true}},
 fastpath: {supported:true, kind:backend === 'tofudb' ? 'native' : 'sqlite', mode:'off', enabled:false,
  directory:'', active:false, activeDirectory:'', restartRequired:false, locked:false, lockReason:'', authorityPath:'/original', shadowPath:''},
 searchProjection: backend === 'tofudb' ? {required:true, directory:'/active/index', configuredDirectory:'/base',
  defaultDirectory:'/default/index', pendingDirectory:'/active/index', restartRequired:false} : null,
 preferences:{backup:{enabled:false,directory:''}}, migration:{},
 }; }
const candidate = (path, extra={}) => ({path,writable:true,storageClass:'local-block',persistence:'persistent',volatile:false,isCurrent:false,...extra});
const candidates = [candidate('/base'),candidate('/other'),candidate('/memory',{storageClass:'memory-filesystem'}),
 candidate('/volatile',{volatile:true}),candidate('/temporary',{persistence:'ephemeral'}),candidate('/network',{storageClass:'network-filesystem'}),
 candidate('/<img src=x onerror=alert(1)>'),candidate('/readonly',{writable:false})];
let state = fixture();
const client = {
 status: async () => clone(state), paths: async refresh => {probes.push(refresh);return {candidates:clone(candidates)};},
 pathsApply: async payload => { calls.push(clone(payload)); if(payload.fastPath) Object.assign(state.fastpath,payload.fastPath);
  if(payload.searchProjection) state.searchProjection.configuredDirectory=payload.searchProjection.directory;
  return {restartRequired:false}; },
 preferences: async payload => { calls.push(clone(payload)); Object.assign(state.preferences.backup,payload.backup); return {preferences:clone(state.preferences)}; },
 backupNow: async () => { calls.push({backupNow:true}); },
};
window.Api = {databasePanel:client};
const populate = async () => {window._populateDatabaseTab();await settle();};

const open = async id => {byId(id).open=true;byId(id).dispatchEvent(new window.Event('toggle'));await settle();};
const change = (id,checked) => { byId(id).checked=checked;byId(id).dispatchEvent(new window.Event('change',{bubbles:true})); };
const pick = (id,path) => [...byId(id).querySelectorAll('button')].find(b => b.querySelector('.db-candidate-path').textContent === path);
'''

def exercise(bundle, body, *, min_pass, label):
    run_harness(target_js=bundle, body_js=SETUP + '\n(async () => { try {\n' + body + r'''
} catch (error) { check('unexpected_' + String(error.stack || error), false); }
finally {window._destroyDatabaseTab();report();} })();
''', min_pass=min_pass, label=label)


def test_native_optional_fastpath_and_independent_required_projection(database_bundle):
    exercise(database_bundle, r'''
await populate();
check('entry_no_discovery',probes.length===0);
await open('dbSearchProjectionDisclosure');await open('dbFastPathDisclosure');
check('discovery_single_cache',probes.length===1);
check('native_default_off_but_available', visible(byId('dbFastPathToggle')) && !byId('dbFastPathToggle').checked && !byId('dbFastPathToggle').disabled);
check('search_independent', visible(byId('dbSearchProjectionCustom')) && visible(byId('dbFastPathField')) && !byId('dbSearchProjectionApply').disabled);
check('active_index_witness', byId('dbSearchProjectionStatus').textContent.includes('/active/index'));
check('projection_rebuildable_memory', !!pick('dbSearchProjectionCandidates','/memory'));
check('safe_candidate_text', !byId('dbSearchProjectionCandidates').querySelector('img'));
change('dbFastPathToggle',true);
check('native_persistent_local_only', !!pick('dbFastPathCandidates','/base') && !pick('dbFastPathCandidates','/memory') && !pick('dbFastPathCandidates','/temporary') && !pick('dbFastPathCandidates','/network') && !pick('dbFastPathCandidates','/volatile'));
byId('dbSearchProjectionCustom').value='/search';byId('dbSearchProjectionApply').click();await settle();
check('search_union_payload', JSON.stringify(calls[0]) === JSON.stringify({searchProjection:{directory:'/search'}}));
check('search_does_not_enable_fastpath', !state.fastpath.enabled);
window.showConfirm = async message => {confirmations.push(message);return true;};
byId('dbFastPathCustom').value='/base';byId('dbFastPathApply').click();await settle();
check('native_union_payload', JSON.stringify(calls[1]) === JSON.stringify({fastPath:{enabled:true,directory:'/base'}}));
check('native_durability_warning', confirmations[0].includes('commit authority') && confirmations[0].includes('unreplicated tail') && confirmations[0].includes('offline export'));
check('saved_not_active', byId('dbFastPathDetail').textContent.includes('/base') && byId('dbFastPathStatus').textContent.includes(messages['settings.dbFastPathOff']));
state.fastpath.active=null;state.fastpath.restartRequired=true;await populate();
check('unknown_not_fabricated', byId('dbFastPathStatus').textContent.includes(messages['settings.dbFastPathUnknown']));
check('pending_restart_honest', byId('dbFastPathStatus').textContent.includes(messages['settings.dbRestartPending']));
''', min_pass=13, label='unified-native-paths')


def test_locked_native_and_confirmation_cancellation(database_bundle):
    exercise(database_bundle, r'''
await populate();change('dbFastPathToggle',true);
byId('dbFastPathCustom').value='/base';byId('dbFastPathApply').click();await settle();
check('cancel_no_write', calls.length===0);
Object.assign(state.fastpath,{enabled:true,active:true,directory:'/saved',activeDirectory:'/local',locked:true,lockReason:'local authority owns commits',authorityPath:'/local',shadowPath:'/original'});
await populate();
check('locked_toggle_disabled', byId('dbFastPathToggle').checked && byId('dbFastPathToggle').disabled);
check('locked_no_relocation', !visible(byId('dbFastPathField')) && byId('dbFastPathApply').disabled);
check('locked_reason_and_export', byId('dbFastPathLockSummary').textContent.includes('local authority owns commits') && byId('dbFastPathLockSummary').textContent.includes('Stop the server'));
check('authority_and_shadow_distinct', byId('dbFastPathDetail').textContent.includes('/local') && byId('dbFastPathDetail').textContent.includes('/original'));
change('dbFastPathToggle',false);window._dbFastPathCustomApply();await settle();
check('handlers_cannot_bypass_lock', calls.length===0 && byId('dbFastPathToggle').checked);
check('index_still_editable', !byId('dbSearchProjectionApply').disabled);
''', min_pass=7, label='native-lock-cancel')


def test_initial_failure_retry_and_refresh_failure_remove_active_claims(database_bundle):
    exercise(database_bundle, r'''
const read=deferred();client.status=()=>read.promise;window._populateDatabaseTab();
check('initial_off_disabled', !byId('dbFastPathToggle').checked && byId('dbFastPathToggle').disabled && byId('dbEngineTofudb').disabled);
check('initial_loading_accessible', visible(byId('dbStatusMessage')) && byId('settingsTab_database').getAttribute('aria-busy')==='true');
read.reject(Error('offline'));await settle();
check('failure_retry_enabled', !byId('dbPathRefresh').disabled && visible(byId('dbPathError')));
check('failure_no_actions', byId('dbFastPathApply').disabled && byId('dbBackupToggle').disabled);
client.status=async()=>clone(state);await window._dbRefreshPaths();
check('retry_ready', !byId('dbFastPathToggle').disabled && !visible(byId('dbPathError')));
Object.assign(state.fastpath,{enabled:true,active:true,activeDirectory:'/actual'});await populate();
client.status=async()=>{throw Error('refresh offline');};await window._dbRefreshPaths();
check('failed_refresh_no_stale_claim', byId('dbFastPathStatus').textContent==='' && byId('dbFastPathToggle').disabled && !byId('dbFastPathToggle').checked);
check('failed_refresh_explained', visible(byId('dbStatusMessage')) && visible(byId('dbPathError')));
client.status=async()=>null;await window._dbRefreshPaths();
check('null_status_is_failure', byId('dbFastPathToggle').disabled && !byId('dbPathRefresh').disabled);
''', min_pass=8, label='database-readiness')


def test_sqlite_save_failure_retry_and_postsave_unknown(database_bundle):
    exercise(database_bundle, r'''
state=fixture('sqlite');await populate();await open('dbFastPathDisclosure');change('dbFastPathToggle',true);
check('sqlite_optional', !visible(byId('dbSearchProjection')) && visible(byId('dbFastPathField')));
const save=deferred();client.pathsApply=async payload=>{calls.push(clone(payload));await save.promise;};
byId('dbFastPathCustom').value='/base';byId('dbFastPathApply').click();window._dbFastPathCustomApply();
check('duplicate_guard',calls.length===1 && byId('dbFastPathToggle').disabled);
save.reject(Error('disk <img src=x> denied'));await settle();
check('save_failure_retains_draft',byId('dbFastPathToggle').checked && !byId('dbFastPathToggle').disabled);
check('error_safe_text',byId('dbPathError').textContent.includes('<img') && !byId('dbPathError').querySelector('img'));
client.pathsApply=async payload=>{calls.push(clone(payload));Object.assign(state.fastpath,payload.fastPath);return {restartRequired:false};};
change('dbFastPathToggle',true);pick('dbFastPathCandidates','/base').click();
check('candidate_draft_no_write',calls.length===1);byId('dbFastPathApply').click();await settle();
check('retry_saved',state.fastpath.enabled && !visible(byId('dbPathError')));
client.status=async()=>{throw Error('lost status');};change('dbFastPathToggle',false);byId('dbFastPathApply').click();await settle();
check('postsave_failure_distinct',byId('dbPathError').textContent.startsWith('Saved, but'));
check('postsave_unknown_not_active',byId('dbFastPathStatus').textContent==='' && byId('dbFastPathToggle').disabled && !byId('dbPathRefresh').disabled);
''', min_pass=7, label='sqlite-save-failure')


def test_destroy_reopen_fences_status_probe_save_and_confirmation(database_bundle):
    exercise(database_bundle, r'''
const old=deferred();client.status=()=>old.promise;window._populateDatabaseTab();
window._destroyDatabaseTab();
check('closed_view_no_stale_claims', !visible(byId('dbEngineCard')) && !visible(byId('dbPathContent')));
state=fixture('sqlite');client.status=async()=>clone(state);await populate();
old.resolve(fixture('tofudb'));await settle();
check('old_status_cannot_change_backend',!visible(byId('dbSearchProjection')));
const probe=deferred();client.paths=()=>probe.promise;const refresh=open('dbFastPathDisclosure');await settle();
window._destroyDatabaseTab();client.paths=async()=>({candidates:clone(candidates)});await populate();byId('dbPathScan').click();await settle();
probe.resolve({candidates:[candidate('/stale')]});await refresh;await settle();change('dbFastPathToggle',true);
check('old_probe_cannot_replace_candidates',!pick('dbFastPathCandidates','/stale') && !!pick('dbFastPathCandidates','/base'));
const save=deferred();client.pathsApply=async payload=>{calls.push(payload);return save.promise;};
byId('dbFastPathCustom').value='/base';byId('dbFastPathApply').click();
window._destroyDatabaseTab();await populate();save.resolve({restartRequired:true});await settle();
check('old_save_no_restart_prompt',confirmations.length===0);
check('old_save_no_new_draft',!byId('dbFastPathToggle').checked && !byId('dbFastPathToggle').disabled);
state=fixture();await populate();change('dbFastPathToggle',true);const confirm=deferred();window.showConfirm=()=>confirm.promise;
byId('dbFastPathCustom').value='/base';byId('dbFastPathApply').click();await settle();
window._destroyDatabaseTab();await populate();confirm.resolve(true);await settle();
check('old_confirmation_no_mutation',calls.length===1);
''', min_pass=6, label='database-lifecycle-fences')


def test_projection_reset_and_backup_remain_independent(database_bundle):
    exercise(database_bundle, r'''
await populate();await open('dbSearchProjectionDisclosure');byId('dbSearchProjectionReset').click();await settle();
check('reset_draft_only',calls.length===0);byId('dbSearchProjectionApply').click();await settle();
check('reset_own_payload',JSON.stringify(calls[0])===JSON.stringify({searchProjection:{directory:''}}));
check('reset_not_fastpath',!state.fastpath.enabled && byId('dbSearchProjectionReset').disabled);
change('dbBackupToggle',true);
check('backup_filters_volatile',!pick('dbBackupCandidates','/memory') && !pick('dbBackupCandidates','/temporary'));
pick('dbBackupCandidates','/base').click();await settle();
check('backup_draft_only',calls.length===1);byId('dbBackupApply').click();await settle();
check('backup_own_payload',calls[1].backup.directory==='/base' && !calls[1].fastPath);
''', min_pass=4, label='database-independent-actions')


def test_copy_bilingual_plain_text_matching_parameters():
    locales={lang:json.loads((ROOT/f'frontend/src/i18n/locales/{lang}.json').read_text()) for lang in ('en','zh')}
    keys=[key for key in locales['en'] if key.startswith(('settings.dbIndex','settings.dbFastPath','settings.dbStatus'))]
    for key in keys:
        en,zh=locales['en'][key],locales['zh'][key]
        assert en and zh and en != zh, key
        assert '<' not in en and '<' not in zh, key
        assert re.findall(r'\{\w+\}',en)==re.findall(r'\{\w+\}',zh), key


def test_cancel_drafts_refresh_preservation_and_unknown_availability(database_bundle):
    exercise(database_bundle, r'''
state.backends.sqlite={available:null,availabilityState:'unknown'};
await populate();await open('dbFastPathDisclosure');await open('dbBackupDisclosure');await open('dbSearchProjectionDisclosure');
check('unknown_not_unavailable',byId('dbSqliteState').textContent.includes(messages['settings.dbAvailabilityUnknown']) && !byId('dbSqliteState').textContent.includes(messages['settings.dbNotInstalled']));
change('dbFastPathToggle',true);pick('dbFastPathCandidates','/other').click();
change('dbBackupToggle',true);pick('dbBackupCandidates','/base').click();
pick('dbSearchProjectionCandidates','/memory').click();
check('all_selections_draft_only',calls.length===0);
await window._dbRefreshPaths();
check('status_refresh_keeps_drafts',byId('dbFastPathToggle').checked && byId('dbFastPathCustom').value==='/other' && byId('dbBackupCustom').value==='/base' && byId('dbSearchProjectionCustom').value==='/memory');
byId('dbFastPathCancel').click();
check('cancel_is_section_scoped',!byId('dbFastPathToggle').checked && byId('dbBackupToggle').checked && byId('dbSearchProjectionCustom').value==='/memory');
byId('dbBackupCancel').click();byId('dbSearchProjectionCancel').click();
check('cancel_restores_server_values',!byId('dbBackupToggle').checked && byId('dbSearchProjectionCustom').value==='/base' && calls.length===0);
const read=deferred();let reads=0;client.status=()=>{reads++;return read.promise;};
const a=window._dbRefreshPaths(),b=window._dbRefreshPaths();
check('status_single_flight',reads===1);read.resolve(clone(state));await a;await b;
check('refresh_does_not_probe',probes.length===1);
window._destroyDatabaseTab();await populate();
check('reopen_discards_drafts',!byId('dbFastPathToggle').checked && !byId('dbBackupToggle').checked && byId('dbSearchProjectionCustom').value==='/base');
''', min_pass=8, label='database-draft-cancel-refresh')


def test_section_cancel_read_dedup_and_copy_feedback(database_bundle):
    exercise(database_bundle, r'''
let reads=0;const read=deferred();client.status=()=>{reads++;return read.promise;};
window._populateDatabaseTab();window._populateDatabaseTab();
check('one_status_flight',reads===1);
read.resolve(clone(state));await settle();
await open('dbFastPathDisclosure');change('dbFastPathToggle',true);
byId('dbFastPathCustom').value='/draft-only';byId('dbFastPathCustom').dispatchEvent(new window.Event('input',{bubbles:true}));
byId('dbFastPathCancel').click();await settle();
check('cancel_discards_without_write',calls.length===0 && !byId('dbFastPathToggle').checked);
await open('dbSearchProjectionDisclosure');
const copied=[];Object.defineProperty(window.navigator,'clipboard',{value:{writeText:async text=>{copied.push(text);}},configurable:true});
const copy=[...byId('dbSearchProjectionStatus').querySelectorAll('button')].find(b=>b.textContent===messages['settings.dbCopyPath']);
copy.click();await settle();
check('copy_actual_full_path',copied[0]==='/active/index');
check('copy_feedback',toasts.includes(messages['settings.dbCopied']));
''', min_pass=4, label='database-cancel-copy-dedup')
