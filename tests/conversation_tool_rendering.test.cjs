/** Retained bridge decorates one visible row and reuses immutable sibling input. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname,
  '../frontend/src/runtime/sections/main/conversation_turn_store.js'), 'utf8');
const start = source.indexOf('  renderToolBlockHtml(block, turn) {');
const end = source.indexOf('  renderInjectionBlockHtml(', start);

test('visible rows reuse sibling identity without decorating full historical rounds', () => {
  let decorations = 0;
  let localAnswer = '';
  const calls = [];
  const context = {
    humanGuidancePresentation: { decorate(_conversationId, round) {
      decorations += 1;
      return localAnswer && round.guidanceId ? { ...round, _hgUserResponse: localAnswer } : round;
    } },
    _renderToolSlot(round, siblings) { calls.push({ round, siblings }); return 'rendered'; },
  };
  vm.createContext(context);
  vm.runInContext(`globalThis.ports = {${source.slice(start, end)}}`, context);
  const rounds = Array.from({ length: 1000 }, (_, index) => ({
    toolCallId: `call-${index}`, toolName: 'read_files', status: 'done',
    toolArgs: { files: ['same.txt'], range: index }, attemptId: 'old', taskId: 'old-task',
  }));
  rounds[0] = { ...rounds[0], toolName: 'ask_human', guidanceId: 'guidance', status: 'awaiting_human' };
  const turn = { turnId: 'turn', status: 'completed', attemptId: 'latest', taskId: 'latest-task',
    source: { conversationId: 'conversation', projection: { toolRounds: rounds },
      settlement: { resumeOptions: [{ operation: 'answer_guidance', anchor: { guidanceId: 'guidance' } }] } } };
  context.ports.renderToolBlockHtml({ round: rounds[0] }, turn);
  localAnswer = 'Fresh presentation answer';
  context.ports.renderToolBlockHtml({ round: rounds[0] }, turn);
  context.ports.renderToolBlockHtml({ round: rounds[999] }, turn);
  assert.equal(decorations, 3);
  assert.ok(calls.every(call => call.siblings === rounds));
  assert.equal(calls[0].round._hgAnswerGuidance, true);
  assert.equal(calls[1].round._hgUserResponse, 'Fresh presentation answer');
  assert.equal(calls[2].round._taskId, 'old-task');
  assert.equal(calls[2].round._turnSettled, true);
  assert.equal(rounds[0]._hgUserResponse, undefined);
  assert.equal(rounds[999]._turnId, undefined);
});
