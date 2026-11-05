const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const modules = new Map();
function load(file) {
  file = path.resolve(file);
  if (modules.has(file)) return modules.get(file);
  const exports = {};
  modules.set(file, exports);
  const source = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  new Function('exports', 'require', source)(exports, name => load(path.resolve(path.dirname(file), name) + '.ts'));
  return exports;
}
const {createAudioCapabilitiesReader, needsWaveEncoding} = load('frontend/src/features/voice/audio-capabilities.ts');
const {VoiceRecording} = load('frontend/src/features/voice/recording.ts');
const {audioBufferToWave, createWaveEncoder} = load('frontend/src/features/voice/wave-encoder.ts');
const {createAudioEndpoints} = load('frontend/src/api/endpoint-registry/audio.ts');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
};

test('audio facade forwards cancellation while preserving transcription deadline policy', async () => {
  const calls = [], abort = new AbortController(), data = new FormData();
  const audio = createAudioEndpoints({request: async (...args) => calls.push(args), get: async (...args) => calls.push(args)});
  await audio.transcribe(data, {signal:abort.signal}); await audio.capabilities({signal:abort.signal});
  assert.equal(calls[0][0], '/api/v4/audio/transcribe');
  assert.equal(calls[0][1].body,data); assert.equal(calls[0][1].timeout,0);
  assert.equal(calls[0][1].signal,abort.signal); assert.equal(calls[1][1].signal,abort.signal);
  assert.equal(calls[1][1].onError,'null');
});

test('shared capabilities coalesce readers and retire reads preceding a routing commit', async () => {
  const reads = [], published = [], errors = [];
  const owner = createAudioCapabilitiesReader({
    load: signal => {const read = deferred(); reads.push({...read, signal}); return read.promise;},
    report: error => errors.push(error),
  });
  const unsubscribe = owner.subscribe(value => published.push(value));
  const composer = owner.refresh(), settings = owner.refresh();
  assert.equal(composer, settings);
  await tick(); assert.equal(reads.length, 1);
  const committed = owner.routingChanged(4);
  assert.equal(committed, composer);
  reads[0].resolve({available: true}); await tick();
  assert.equal(reads.length, 2); assert.deepEqual(published, [null]);
  assert.equal(owner.routingChanged(4), committed);
  assert.equal(owner.routingChanged(3), committed);
  reads[1].resolve({available: false, models: []});
  assert.equal((await settings).available, false);
  assert.equal(published.length, 2); assert.deepEqual(errors, []);
  unsubscribe(); owner.destroy();
  assert.equal(reads[0].signal.aborted, true);
  assert.equal(await owner.refresh(), null); assert.equal(reads.length, 2);
});

test('stale failure is suppressed, current failure disables availability, disposal fences late reads', async () => {
  const reads = [], failures = [], values = [];
  const owner = createAudioCapabilitiesReader({
    load: () => {const result = deferred(); reads.push(result); return result.promise;},
    report: error => failures.push(error),
  });
  owner.subscribe(value => values.push(value));
  const first = owner.refresh(); await tick();
  owner.invalidate(); reads[0].reject(Error('stale')); await tick();
  assert.equal(failures.length, 0);
  reads[1].reject(Error('current')); assert.equal(await first, null);
  assert.equal(failures[0].message, 'current');
  const last = owner.refresh(); await tick(); owner.destroy();
  const count = values.length;
  reads[2].resolve({available: true}); assert.equal(await last, null);
  assert.equal(values.length, count);
});

test('mixed, unknown and missing model modes preserve conservative WAV conversion', () => {
  for (const value of [null, {models: []}, {models: [{mode:'chat'}]},
      {models:[{mode:'endpoint'}, {mode:'chat'}]}, {models:[null]}, {models:[{}]}]) {
    assert.equal(needsWaveEncoding(value), true);
  }
  assert.equal(needsWaveEncoding({models: [{mode:'endpoint'}, {mode:'endpoint'}]}), false);
});

test('accepted equal-revision authority refreshes health; broken views cannot block other subscribers', async () => {
  let available = false, reads = 0; const errors = [], values = [];
  const owner = createAudioCapabilitiesReader({load: async () => {reads++; return {available};}, report: e => errors.push(e)});
  owner.subscribe(() => {throw Error('detached view');});
  owner.subscribe(value => values.push(value?.available));
  await owner.routingAccepted(5);
  available = true; await owner.routingAccepted(5);
  await owner.routingChanged(5); await owner.routingChanged(4);
  assert.equal(reads,2); assert.equal(values.at(-1),true); assert.equal(errors.length,3);
  owner.destroy();
});

test('routing invalidation in the read-settlement microtask starts a fresh request', async () => {
  let reads = 0, refresh;
  const owner = createAudioCapabilitiesReader({load: async () => ({available: ++reads > 1}), report: error => {throw error;}});
  owner.subscribe(value => {
    if (value?.available === false) queueMicrotask(() => {refresh = owner.invalidate();});
  });
  await owner.refresh(); await refresh;
  assert.equal(reads,2); assert.equal(owner.current.available,true);
  owner.destroy();
});

function recordingFixture({failStart = false, failStop = false, failConstruct = false, encode = false} = {}) {
  const permissions = [], transcripts = [], failures = [], states = [], requests = [], recorders = [], cleanups = [];
  let stops = 0, closed = 0, warmed = 0;
  const encoder = {prewarm: () => warmed++, encode: async () => new Blob(['wav'], {type:'audio/wav'}), destroy: () => closed++};
  const recording = new VoiceRecording({
    acquireStream: () => {const permission = deferred(); permissions.push(permission); return permission.promise;},
    createRecorder: () => {
      if (failConstruct) throw Error('construct');
      const recorder = {state:'inactive', mimeType:'audio/webm',
        start() {if (failStart) throw Error('start'); this.state = 'recording';},
        stop() {
          this.state = 'inactive';
          if (failStop) throw Error('stop');
          this.ondataavailable?.({data: new Blob(['clip'])}); this.onstop?.();
        },
      };
      recorders.push(recorder); return recorder;
    },
    createEncoder: () => encoder, needsEncoding: () => encode,
    blob: (chunks, type) => new Blob(chunks, {type}),
    transcribe: (blob, signal) => {const result = deferred(); requests.push({...result, blob, signal}); return result.promise;},
    stateChanged: state => states.push(state), transcript: text => transcripts.push(text),
    failure: (kind, error) => failures.push({kind, error}), cleanupError: error => cleanups.push(error),
  });
  const stream = {getTracks: () => [{stop: () => stops++}]};
  return {recording, permissions, requests, states, transcripts, failures, recorders, stream, cleanups,
    stopped: () => stops, closed: () => closed, warmed: () => warmed,
    setEncoding: value => {encode = value;}, encoder};
}

test('pending permission is single-owner and retirement releases a late grant', async () => {
  const f = recordingFixture();
  f.recording.toggle(); f.recording.toggle(); assert.equal(f.permissions.length, 1);
  f.recording.retirePermission(); assert.equal(f.states.at(-1), 'idle');
  f.permissions[0].resolve(f.stream); await tick();
  assert.equal(f.stopped(), 1); assert.equal(f.recorders.length, 0);
  f.recording.toggle(); assert.equal(f.permissions.length, 2);
  f.recording.destroy(); f.permissions[1].resolve(f.stream); await tick();
  assert.equal(f.stopped(), 2); assert.equal(f.requests.length, 0);
});

test('permission denial is visible and allows a subsequent recording', async () => {
  const f = recordingFixture(); f.recording.toggle();
  f.permissions[0].reject(Error('denied')); await tick();
  assert.equal(f.failures[0].kind, 'permission'); assert.equal(f.states.at(-1), 'idle');
  f.recording.toggle(); assert.equal(f.permissions.length, 2);
  f.recording.destroy(); f.permissions[1].reject(Error('late denial')); await tick();
  assert.equal(f.failures.length, 1);
});

for (const failure of ['failStart', 'failStop', 'failConstruct']) {
  test(`${failure} releases microphone and prewarmed encoder`, async () => {
    const f = recordingFixture({[failure]: true, encode:true});
    f.recording.toggle(); f.permissions[0].resolve(f.stream); await tick();
    if (failure === 'failStop') f.recording.toggle();
    assert.equal(f.stopped(), 1); assert.equal(f.closed(), 1); assert.equal(f.warmed(), 1);
    assert.equal(f.requests.length, 0); assert.equal(f.states.at(-1), 'idle');
    assert.equal(f.failures[0].kind, 'recorder'); f.recording.destroy();
    assert.equal(f.closed(), 1);
  });
}

test('each track is released even if another track stop fails', async () => {
  const f = recordingFixture(); let second = 0;
  f.recording.toggle(); f.permissions[0].resolve({getTracks: () => [
    {stop() {throw Error('track stop');}}, {stop() {second++;}},
  ]}); await tick(); f.recording.destroy();
  assert.equal(second, 1); assert.equal(f.cleanups.length, 1);
});

test('one recording posts once, retains its encoder until settlement and gates failed responses', async () => {
  const f = recordingFixture({encode:true});
  f.recording.toggle(); f.permissions[0].resolve(f.stream); await tick();
  const onstop = f.recorders[0].onstop;
  f.recording.toggle(); f.recording.toggle(); onstop(); await tick();
  assert.equal(f.requests.length, 1); assert.equal(f.stopped(), 1); assert.equal(f.closed(), 0);
  assert.equal(f.requests[0].blob.type, 'audio/wav');
  f.requests[0].resolve({ok:false, text:'must not inject', error:'upstream failure'}); await tick();
  assert.deepEqual(f.transcripts, []); assert.equal(f.failures[0].kind, 'transcription');
  assert.equal(f.closed(), 1); assert.equal(f.states.at(-1), 'idle');
});

test('teardown aborts transcription and ignores late success without emitting an error', async () => {
  const f = recordingFixture();
  f.recording.toggle(); f.permissions[0].resolve(f.stream); await tick(); f.recording.toggle();
  assert.equal(f.requests[0].blob.type, 'audio/webm');
  f.recording.destroy(); assert.equal(f.requests[0].signal.aborted, true);
  f.requests[0].resolve({ok:true,text:'late'}); await tick();
  assert.deepEqual(f.transcripts, []); assert.deepEqual(f.failures, []);
});

test('teardown during encoding prevents a later upload', async () => {
  const f = recordingFixture({encode:true}), conversion = deferred();
  f.encoder.encode = () => conversion.promise;
  f.recording.toggle(); f.permissions[0].resolve(f.stream); await tick(); f.recording.toggle();
  f.recording.destroy(); conversion.resolve(new Blob(['wave'])); await tick();
  assert.equal(f.requests.length, 0); assert.equal(f.closed(), 1);
});

test('encoding observes current routing modes and successful text is trimmed', async () => {
  const f = recordingFixture();
  f.recording.toggle(); f.permissions[0].resolve(f.stream); await tick();
  f.setEncoding(true); f.recording.toggle(); await tick();
  assert.equal(f.requests[0].blob.type, 'audio/wav');
  f.requests[0].resolve({ok:true,text:'  transcript  '}); await tick();
  assert.deepEqual(f.transcripts, ['transcript']); assert.equal(f.closed(), 1);
});

test('PCM encoder preserves stereo downmix, resampling, clipping and WAV headers', () => {
  const source = {sampleRate:48000,numberOfChannels:2,length:6,
    getChannelData: channel => new Float32Array(channel ? [1,-1,2,-2,0.5,0] : [1,-1,2,-2,0.5,0])};
  const encoded = audioBufferToWave(source, 16000), view = new DataView(encoded);
  assert.equal(Buffer.from(encoded).subarray(0,4).toString(), 'RIFF');
  assert.equal(Buffer.from(encoded).subarray(8,12).toString(), 'WAVE');
  assert.equal(view.getUint32(24,true),16000); assert.equal(view.getUint16(22,true),1);
  assert.equal(view.getUint16(34,true),16); assert.equal(encoded.byteLength,48);
  assert.equal(view.getInt16(44,true),32767); assert.equal(view.getInt16(46,true),-32768);
  const mixed = audioBufferToWave({...source, length:2, getChannelData: c => new Float32Array(c ? [1,-1] : [-1,1])}, 48000);
  assert.equal(new DataView(mixed).getInt16(44,true),0);
});

test('audio context is reused, rejected resume/close are handled and decode failure preserves the raw blob', async () => {
  let created=0,closed=0; const warnings=[], original=new Blob(['original']);
  const encoder = createWaveEncoder({
    createContext: () => {created++; return {state:'suspended',resume:async()=>{throw Error('resume');},
      decodeAudioData: async()=>{throw Error('decode');}, close:async()=>{closed++;throw Error('close');}};},
    blob: data => new Blob([data]), debug:()=>false,now:()=>0,
    log:{warn: (...args)=>warnings.push(args),info(){}},
  });
  encoder.prewarm(); assert.equal(await encoder.encode(original),original);
  encoder.destroy(); encoder.destroy(); await tick();
  assert.equal(created,1); assert.equal(closed,1); assert.equal(warnings.length,3);
  assert.equal(await encoder.encode(original),original);
});
