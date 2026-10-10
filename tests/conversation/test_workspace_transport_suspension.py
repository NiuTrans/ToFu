"""Hidden workspace streams release browser sockets without cancelling work."""
from pathlib import Path
import json
import subprocess
import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_node(tmp_path, source, harness):
    built = tmp_path / 'owner.cjs'
    result = subprocess.run([
        str(ROOT / 'scripts/vite_test_bundle.mjs'), str(ROOT / source),
        '--bundle', '--format=cjs', '--platform=node', f'--outfile={built}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['node', '-e', harness.replace('BUILT', json.dumps(str(built)))],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout


PRELUDE = r'''
const assert = require('node:assert/strict');
global.window = globalThis;
const streams = [];
class Source extends EventTarget {
  constructor(url) { super(); this.url = url; this.closed = false; streams.push(this); }
  close() { this.closed = true; }
}
const snapshot = id => ({
  ok:true, contract:'tofu.conversation-sync.snapshot/v1', conversationId:id,
  conversationRevision:1, syncSeq:7, cursor:'cursor-7', serverBootId:'boot-test',
  heartbeatIntervalMs:15000, settings:{}, turns:[], attempts:[], queueItems:[],
});
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
'''


def test_coordinator_suspension_fences_late_hydrate_and_wake(tmp_path):
    run_node(tmp_path, 'frontend/src/core/conversation-sync.ts', PRELUDE + r'''
const { ConversationSyncCoordinator } = require(BUILT);
(async () => {
  let reads = 0, release;
  let gate = Promise.resolve();
  const coordinator = new ConversationSyncCoordinator({
    conversationId:'one',
    api:{snapshot:async id => { reads++; await gate; return snapshot(id); },
         eventsUrl:(id,cursor) => `${id}/${cursor}`},
    onSnapshot(){}, onAttemptEvent(){return true;}, onTurnDelta(){return true;},
    eventSourceFactory:url => new Source(url),
  });
  await coordinator.resume();
  assert.equal(streams.length, 1);
  coordinator.setSuspended(true);
  assert.equal(streams[0].closed, true);
  await coordinator.resume(); await coordinator.wake(); coordinator.invalidate();
  await tick();
  assert.equal(streams.length, 1);
  assert.equal(reads, 1);
  coordinator.setSuspended(false);
  assert.equal(streams.length, 2);
  assert.equal(streams[1].url, 'one/cursor-7');
  assert.equal(reads, 1, 'resume must use replay cursor without redundant snapshot');
  gate = new Promise(resolve => {release = resolve;});
  const hydration = coordinator.hydrate(true);
  coordinator.setSuspended(true);
  release(); await hydration;
  assert.equal(streams.length, 2, 'late snapshot cannot reopen hidden transport');
  coordinator.pause(); coordinator.setSuspended(false);
  assert.equal(streams.length, 2, 'pausing demand while hidden must be retained');
  coordinator.setSuspended(true); await coordinator.wake();
  coordinator.setSuspended(false);
  assert.equal(streams.length, 3);
  coordinator.close(); coordinator.setSuspended(true); coordinator.setSuspended(false);
  assert(streams.every(stream => stream.closed));
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


def test_runtime_suspends_all_existing_and_future_conversations(tmp_path):
    run_node(tmp_path, 'frontend/src/core/turn-runtime.ts', PRELUDE + r'''
const { createConversationTurnRuntime } = require(BUILT);
(async () => {
  let reads = 0;
  const runtime = createConversationTurnRuntime({
    api:{snapshot:async id => {reads++; return snapshot(id);},
         eventsUrl:(id,cursor) => `${id}/${cursor}`},
    eventSourceFactory:url => new Source(url), isActive:() => true,
  });
  for(let index=0; index<6; index++) await runtime.hydrateConversation({id:`conv-${index}`});
  assert.equal(streams.length, 6);
  runtime.setTransportSuspended(true);
  assert(streams.every(stream => stream.closed));
  await runtime.hydrateConversation({id:'late'});
  runtime.invalidateConversation('conv-1');
  runtime.reconcileConversationActivity('conv-1', 'late');
  await tick();
  assert.equal(streams.length, 6, 'new coordinators inherit suspended lifecycle');
  runtime.setTransportSuspended(false);
  assert.equal(streams.length, 13);
  assert.equal(reads, 7);
  runtime.setTransportSuspended(false);
  assert.equal(streams.length, 13, 'repeated activation is idempotent');
  for(const id of ['late', ...Array.from({length:6},(_,i)=>`conv-${i}`)]) runtime.disposeConversation(id);
  assert(streams.every(stream => stream.closed));
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


def test_workspace_navigation_releases_before_lazy_load_and_restores_failure(tmp_path):
    run_node(tmp_path, 'frontend/src/application-shell/page-host.ts', r'''
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<body></body>', {url:'http://localhost/#/chat'});
global.window = dom.window; global.document = dom.window.document;
const {installWorkspaceNavigation, showWorkspace} = require(BUILT);
const events=[];
const dispose = installWorkspaceNavigation(async page => {
  events.push(`load:${page}`); throw new Error('offline');
}, () => events.push('error'), page => events.push(`visible:${page}`));
window.history.replaceState(null,'','#/evolution');
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
(async()=>{
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(events.slice(-4), ['visible:evolution','load:evolution','visible:chat','error']);
  showWorkspace('evolution');
  assert.equal(events.at(-1),'visible:evolution');
  showWorkspace('chat');
  assert.equal(events.at(-1),'visible:chat');
  dispose();
  const count=events.length;
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(events.length,count);
  dom.window.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
