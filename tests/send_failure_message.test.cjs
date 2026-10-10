const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const moduleUnderTest = { exports: {} };
new Function('module', 'exports', ts.transpileModule(
  fs.readFileSync('frontend/src/conversation/presentation/send-failure-message.ts', 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(moduleUnderTest, moduleUnderTest.exports);
const { sendFailureToastDetail } = moduleUnderTest.exports;

test('server 500 keeps public detail and complete request/conversation handles once', () => {
  const requestId = 'request-id-with-a-long-suffix-0123456789abcdef';
  const conversationId = 'mufmucgu8c8ljb';
  const detail = sendFailureToastDetail({
    message: `Internal server error\nPublic detail\nRequest ID: ${requestId}`,
    requestId,
    body: { error: { message: 'Internal server error' } },
  }, conversationId, 500);
  assert.equal(detail, `Internal server error\nPublic detail\nRequest ID: ${requestId}\nConversation ID: ${conversationId}`);
});

test('500 without transport text still quotes request id, not private body detail', () => {
  assert.equal(sendFailureToastDetail({
    requestId: 'server-id',
    body: { error: { message: 'Failed', private_trace: 'secret' } },
  }, 'conversation', 500), 'Failed\nRequest ID: server-id\nConversation ID: conversation');
});

test('non-500 retains explicit server error; network failure preserves draft guidance', () => {
  assert.equal(sendFailureToastDetail({
    message: 'transport text', body: { error: { message: 'Invalid model' } },
  }, 'conversation', 400), 'Invalid model\nConversation ID: conversation');
  assert.equal(sendFailureToastDetail(new Error('offline'), 'conversation', 0),
    'offline\nConversation ID: conversation\nDraft kept; retry is safe');
});
