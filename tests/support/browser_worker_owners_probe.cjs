/* Exercise the shipped owner classes through their lifecycle ports. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = vm.createContext({TextEncoder});
for (const name of ['result-outbox', 'command-execution']) {
  const file = path.join(process.argv[3], name + '.js');
  vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename: file});
}
const Outbox = vm.runInContext('BrowserResultOutbox', context);
const Executor = vm.runInContext('BrowserCommandExecutor', context);
const a = {serverUrl: 'https://a.test', clientId: 'device', profile: ''};
const b = {...a, serverUrl: 'https://b.test'};
function drain(outbox, connection) {
  const rows = [];
  while (true) {
    const receipt = outbox.begin(connection);
    rows.push(...receipt.items); receipt.acknowledge();
    if (!receipt.items.length) return rows;
  }
}
function receipts() {
  const outbox = new Outbox({maxResults: 2, maxBytes: 1024});
  outbox.select(a);
  for (const id of ['a1', 'a2', 'a3']) outbox.enqueue(a, {id, result: id});
  const first = outbox.begin(a), leased = outbox.size;
  outbox.select(b); outbox.enqueue(b, {id: 'b1', result: 'other-server'});
  const others = drain(outbox, b);
  const rolledBack = first.rollback(), duplicateAck = first.acknowledge();
  outbox.select(a);
  const replay = drain(outbox, a);
  const sameServerNewSecret = {...a, headers: {'X-Bridge-Secret': 'renewed'}};
  outbox.enqueue(a, {id: 'credential-renewal'});
  const renewed = drain(outbox, sameServerNewSecret);
  outbox.select(null);
  return {leased, rolledBack, duplicateAck, others, replay, renewed,
    remaining: outbox.size, retainedScopes: outbox.scopes.size};
}
function payloads() {
  const outbox = new Outbox({maxResults: 4, maxBytes: 1024});
  outbox.select(a);
  const circular = {id: 'circular'}; circular.result = circular;
  outbox.enqueue(a, circular);
  outbox.enqueue(a, {id: 'unicode', result: '豆腐'.repeat(300)});
  outbox.enqueue(a, {id: 'small', result: 'ok'});
  const normalized = drain(outbox, a);
  for (let index = 0; index < 4; index++) outbox.enqueue(a, {id: String(index), result: 'ok'});
  const batch = outbox.begin(a), initialSize = batch.items.length;
  const firstBackoff = batch.rejectPayload();
  batch.rollback(); batch.rejectPayload();
  const split = outbox.begin(a), splitIds = split.items.map(row => row.id);
  split.rejectPayload();
  const singleton = outbox.begin(a), singletonId = singleton.items[0].id;
  const singletonBackoff = singleton.rejectPayload();
  const floor = outbox.begin(a), compacted = floor.items[0];
  const floorBackoff = floor.rejectPayload();
  return {normalized, initialSize, firstBackoff, splitIds, singletonId,
    singletonBackoff, compacted, floorBackoff, remaining: drain(outbox, a)};
}
function retention() {
  const outbox = new Outbox();
  let largestLive = 0, delivered = 0;
  for (let index = 0; index < 256; index++) {
    const connection = {...a, serverUrl: `https://server-${index}.test`};
    outbox.select(connection);
    for (let row = 0; row < 32; row++) outbox.enqueue(connection, {id: `${index}:${row}`, result: 'x'.repeat(128)});
    const receipt = outbox.begin(connection);
    largestLive = Math.max(largestLive, outbox.size);
    delivered += receipt.items.length; receipt.acknowledge();
  }
  const scopesBeforeStop = outbox.scopes.size;
  outbox.select(null);
  return {delivered, largestLive, scopesBeforeStop, scopesAfterStop: outbox.scopes.size,
    outstanding: outbox.size};
}
function ports(outbox, overrides = {}) {
  return {outbox, createResources: () => ({close: async () => {}}),
    log() {}, warn() {}, error() {}, now: () => 1, timeout: () => 1000,
    withTimeout: promise => promise, describeError: error => error.message,
    execute: async () => ({ok: true}), settled() {}, ...overrides};
}
async function commandScopes() {
  const outbox = new Outbox(), finishes = [], settlements = [];
  let created = 0, closed = 0;
  const cleanup = [];
  const owner = new Executor(ports(outbox, {
    createResources: () => { created++; return {close: () => new Promise(resolve => {
      cleanup.push(() => { closed++; resolve(); });
    })}; },
    execute: (_command, _resources, connection) => new Promise(resolve => {
      finishes.push(() => resolve({serverUrl: connection.serverUrl}));
    }),
    settled: connection => settlements.push(connection.serverUrl),
  }));
  const command = {id: 'same-id', type: 'test', params: {}};
  const first = owner.execute(command, a);
  await owner.execute(command, a);
  const second = owner.execute(command, b), during = owner.status();
  finishes[0](); finishes[1]();
  await new Promise(resolve => setImmediate(resolve));
  const beforeCleanup = {status: owner.status(), queued: outbox.size};
  for (const finish of cleanup) finish();
  await Promise.all([first, second]);
  return {during, beforeCleanup, after: owner.status(), created, closed, settlements,
    a: drain(outbox, a), b: drain(outbox, b)};
}
async function commandFailures() {
  const cases = {};
  for (const phase of ['create', 'execute', 'cleanup', 'execute+cleanup']) {
    const outbox = new Outbox(); let closed = 0, settled = 0;
    const owner = new Executor(ports(outbox, {
      createResources: () => {
        if (phase === 'create') throw new Error('create failed');
        return {close: async () => { closed++; if (phase.includes('cleanup')) throw new Error('cleanup failed'); }};
      },
      execute: async () => { if (phase.includes('execute')) throw new Error('execute failed'); return {ok: true}; },
      settled: () => { settled++; },
    }));
    await owner.execute({id: phase, type: 'test', params: {}}, a);
    cases[phase] = {closed, settled, status: owner.status(), results: drain(outbox, a)};
  }
  return cases;
}
const probes = {receipts, payloads, retention, commandScopes, commandFailures};
Promise.resolve().then(() => probes[process.argv[2]]()).then(result => {
  process.stdout.write(JSON.stringify(result));
}).catch(error => { console.error(error); process.exitCode = 1; });
