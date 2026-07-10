"""Order-independence of the live segment-translation stamp.

The translate `done` push (carrying `segmentsByRound`) and the chat `done` event
(which projects `committedMessage.segments` onto the message) are near-concurrent.
Because the deliverable reuses a CACHED round-segment translation, the translate
frame often wins the race and lands BEFORE `msg.segments` exists. The stamp must
therefore be order-independent:

  • translate `done` first  → stash the {round:中文} map on `_pendingSegTranslations`;
    the chat `done` handler replays it right after projecting segments.
  • chat `done` first        → segments already present → stamp immediately.

This test extracts the pure `_stampSegTranslations` function from the SHIPPED
`static/js/translation.js` (no jsdom needed — the function only touches the msg
object) and drives both orderings + a neuter that proves the stash is load-bearing.
"""

import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
TRANSLATION_JS = os.path.join(ROOT, 'static', 'js', 'translation.js')


def _has_node():
    return shutil.which('node') is not None


def _extract_stamp_fn(src: str) -> str:
    """Pull the `function _stampSegTranslations(...) { ... }` block verbatim."""
    start = src.index('function _stampSegTranslations(')
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError('could not extract _stampSegTranslations body')


def _run(node_body: str, *, neuter: bool = False) -> str:
    src = open(TRANSLATION_JS, encoding='utf-8').read()
    fn = _extract_stamp_fn(src)
    if neuter:
        # Remove the stash branch → a translate-frame-first arrival is dropped.
        fn = fn.replace('msg._pendingSegTranslations = byRound;\n    return;',
                        'return;')
        assert 'msg._pendingSegTranslations = byRound;' not in fn, 'neuter failed'
    prog = fn + '\n' + node_body
    proc = subprocess.run(['node', '-e', prog], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


_SEGS = """
function makeSegs() {
  return [
    { type: 'text', llmRound: 0, text: 'First segment.' },
    { type: 'tool_use', llmRound: 0 },
    { type: 'text', llmRound: 1, text: 'Second segment.' },
    { type: 'tool_use', llmRound: 1 },
    { type: 'text', llmRound: 2, text: 'The final answer.', deliverable: true },
  ];
}
const BY_ROUND = { '0': 'ZH0', '1': 'ZH1', '2': 'ZHFINAL' };
"""


@pytest.mark.skipif(not _has_node(), reason='node not installed')
def test_chat_done_first_stamps_immediately():
    out = _run(_SEGS + """
      const msg = { segments: makeSegs() };
      _stampSegTranslations(msg, BY_ROUND);
      const narr = msg.segments.filter(s => s.type === 'text' && !s.deliverable);
      const ok = narr[0].translatedText === 'ZH0'
              && narr[1].translatedText === 'ZH1'
              // deliverable narration is NOT stamped (it renders as the bilingual body)
              && !msg.segments.find(s => s.deliverable).translatedText
              && !msg._pendingSegTranslations;
      console.log(ok ? 'PASS' : 'FAIL ' + JSON.stringify(msg.segments));
    """)
    assert out.endswith('PASS'), out


@pytest.mark.skipif(not _has_node(), reason='node not installed')
def test_translate_done_first_stashes_then_replays():
    out = _run(_SEGS + """
      // Translate frame wins the race: segments not present yet.
      const msg = {};
      _stampSegTranslations(msg, BY_ROUND);
      const stashed = msg._pendingSegTranslations === BY_ROUND;
      // Chat `done` now projects segments and replays the stash.
      msg.segments = makeSegs();
      _stampSegTranslations(msg, msg._pendingSegTranslations);
      const narr = msg.segments.filter(s => s.type === 'text' && !s.deliverable);
      const ok = stashed
              && narr[0].translatedText === 'ZH0'
              && narr[1].translatedText === 'ZH1'
              && !msg._pendingSegTranslations;   // cleared after successful stamp
      console.log(ok ? 'PASS' : 'FAIL stashed=' + stashed + ' ' + JSON.stringify(msg.segments));
    """)
    assert out.endswith('PASS'), out


@pytest.mark.skipif(not _has_node(), reason='node not installed')
def test_neuter_without_stash_drops_translate_first_arrival():
    """Negative control: strip the stash branch → a translate-frame-first arrival
    is lost (segments never get translatedText even after they arrive)."""
    out = _run(_SEGS + """
      const msg = {};
      _stampSegTranslations(msg, BY_ROUND);       // no segments yet → dropped
      msg.segments = makeSegs();
      _stampSegTranslations(msg, msg._pendingSegTranslations);  // undefined → no-op
      const narr = msg.segments.filter(s => s.type === 'text' && !s.deliverable);
      const anyStamped = narr.some(s => s.translatedText);
      console.log(anyStamped ? 'STAMPED' : 'DROPPED');
    """, neuter=True)
    assert out.endswith('DROPPED'), out
