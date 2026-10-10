const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

const moduleUnderTest = { exports: {} };
new Function(
  'module',
  'exports',
  ts.transpileModule(
    fs.readFileSync('frontend/src/core/turn-command.ts', 'utf8'),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText,
)(moduleUnderTest, moduleUnderTest.exports);

const {
  buildTurnSubmitRequest,
  buildTurnOperationRequest,
  resumeCtxInputUpdate,
} = moduleUnderTest.exports;

const SNAPSHOT = { model: 'kimi-k3', toolsLabel: 'Tools' };

test('an uploaded image uses its durable ref within the turn storage budget', () => {
  const base64 = 'A'.repeat(4_500_000);
  const uploaded = {
    base64, preview: `data:image/png;base64,${base64}`,
    mediaType: 'image/png', url: '/api/images/uploaded.png',
  };
  const input = { images: [uploaded] };
  const message = { text: 'Describe this', images: [uploaded] };
  const request = buildTurnSubmitRequest(input, {}, { message });
  assert.ok(Buffer.byteLength(JSON.stringify({ inputTurn: input, message })) > 8_384_512);
  assert.ok(Buffer.byteLength(JSON.stringify(request)) < 1024);
  assert.deepEqual(request.message.images[0], {
    preview: uploaded.url, mediaType: uploaded.mediaType, url: uploaded.url,
  });
  assert.deepEqual(request.inputTurn.images[0], request.message.images[0]);
  assert.equal(uploaded.base64, base64, 'the pending draft remains retryable');
});

test('an image without a durable upload keeps its inline fallback', () => {
  const image = { base64: 'ABC', preview: 'data:image/png;base64,ABC' };
  const request = buildTurnSubmitRequest(null, {}, { message: { images: [image] } });
  assert.deepEqual(request.message.images[0], image);
});

test('continue with live context sends an integer input-projection fence', () => {
  const inputUpdate = resumeCtxInputUpdate(SNAPSHOT);
  assert.deepEqual(inputUpdate, {
    contextSnapshot: { blockId: 'turn-context', snapshot: SNAPSHOT },
  });
  const request = buildTurnOperationRequest(
    { conversationId: 'conv-a', projectionRevision: 4 },
    'continue',
    { model: 'kimi-k3' },
    {
      commandId: 'continue:turn-a:4',
      inputUpdate,
      expectedInputProjectionRevision: 7,
    },
  );
  assert.equal(request.expectedProjectionRevision, 4);
  assert.equal(request.expectedInputProjectionRevision, 7);
  assert.equal(typeof request.expectedInputProjectionRevision, 'number');
  assert.ok(Number.isInteger(request.expectedInputProjectionRevision));
  assert.equal(
    JSON.parse(JSON.stringify(request)).expectedInputProjectionRevision,
    7,
  );
});

test('resume inputUpdate without a fence still serializes as integer 0', () => {
  const request = buildTurnOperationRequest(
    { conversationId: 'conv-a', projectionRevision: 2 },
    'continue',
    { model: 'kimi-k3' },
    {
      commandId: 'continue:turn-a:2',
      inputUpdate: resumeCtxInputUpdate(SNAPSHOT),
    },
  );
  assert.equal(request.expectedInputProjectionRevision, 0);
  assert.ok(!Object.values(request).some((value) => value === undefined));
  assert.equal(
    JSON.parse(JSON.stringify(request)).expectedInputProjectionRevision,
    0,
  );
});

test('resumeCtxInputUpdate omits the patch when no live snapshot exists', () => {
  assert.equal(resumeCtxInputUpdate(null), null);
  assert.equal(resumeCtxInputUpdate(['not-an-object']), null);
});
