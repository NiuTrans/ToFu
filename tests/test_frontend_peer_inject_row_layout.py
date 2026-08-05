#!/usr/bin/env python3
"""The peer-message inbox row must (a) attribute the sender by conversation
TITLE, not a raw id, and (b) carry NO "模型原文" (model-view) button anywhere
— the whole model-view mechanism was removed on 2026-07-28 per owner
directive ("其信息不如调试面板有用"); the round-scoped debug entry is the
surviving way to inspect what a tool call saw.

WHY THIS TEST EXISTS
--------------------
The reported bug (screenshot): a round-boundary peer injection ("收到 N 条对话
消息") rendered the sender as a bare truncated conv id (``mrnaj25i``). Every
human cares WHO sent the message — a title, not an opaque id.

FIX (static/js/ui/tool_rounds.js ``_renderPeerInjectRow``)
  1. The sender is resolved through the shared ``convTitleById`` seam into a
     ``.sw-peer-from-bubble`` TITLE bubble (raw id only in the tooltip).
  2. The per-card ``.sw-card-raw`` toggle and the explicit ``.sw-inbox-row-chev``
     span are gone (a CSS ``::after`` caret handles the collapse affordance).
  3. (2026-07-28) The header ``[data-tc-preview-text]`` model-view button is
     gone with the rest of the mechanism; this suite now PINS ITS ABSENCE so
     the affordance is not re-introduced row-by-row.
  4. (2026-08-04) The bubble is a ``<button data-conv-jump>`` — clicking jumps
     to the source conversation (delegated handler → ``convFullIdById`` →
     ``loadConversation``); the tooltip carries the FULL title + raw id + jump
     hint since the visible label ellipsizes. A single-sender injection no
     longer repeats the header's bubble inside the body card (the "来源重复"
     screenshot); per-card attribution is kept only for multi-sender rows.

This test EXTRACTS the real shipped helpers + ``_renderPeerInjectRow`` and evals
them in node, asserting the title bubble, the absence of the raw id from the
visible label, and the absence of any model-view control. NC neuters
``convTitleById`` so the bubble falls back — proving the title lookup is
load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')


def _pull_fn(src: str, name: str) -> str:
    """Brace-balanced extraction of a top-level `function <name>(...) {...}`."""
    i = src.index('function ' + name + '(')
    depth = 0
    j = src.index('{', i)
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f'could not extract {name}')


_HARNESS = r"""
const fs = require('fs');
let src = fs.readFileSync(process.env.TR_JS, 'utf8');
const NEUTER = process.env.NEUTER === '1';

global.escapeHtml = s => String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
global.Icon = () => '<svg></svg>';
global.renderMarkdown = s => '<p>' + global.escapeHtml(s) + '</p>';
global.t = (k, d) => (d !== undefined ? d : k);
const _TITLES = { mrnaj25i: '修复显示层 Bug', sibaaaa1: '队列注入', sibbbbb2: 'inbox 重建', sibccccc3: '前缀缓存', sibddddd4: '桌面发布' };
global.convTitleById = NEUTER
  ? (cid => 'Untitled chat')                       // NC: lookup always falls back
  : (cid => _TITLES[cid] || '');
global.window = {};

function pull(name){const i=src.indexOf('function '+name+'(');let d=0,j=src.indexOf('{',i);
  for(let k=j;k<src.length;k++){if(src[k]==='{')d++;else if(src[k]==='}'){d--;if(d===0)return src.slice(i,k+1);}}}
eval(pull('_peerFromBubble'));
eval(pull('_peerFromBubbleGroup'));
eval(pull('_renderPeerInjectRow'));

// Count occurrences of a substring.
function nOcc(hay, needle){ return hay.split(needle).length - 1; }
// Header = the <summary> only (attribution lives there, body cards excluded).
function headerOf(html){ const i=html.indexOf('<summary'); const j=html.indexOf('</summary>'); return html.slice(i, j); }
// Body = everything after the </summary> (the collapsible card region).
function bodyOf(html){ const j=html.indexOf('</summary>'); return html.slice(j); }
// Raw-id-as-visible-label detector: `>sibX<` or `>mrnaj25i<` between tags.
function hasRawIdLabel(html){ return /(>|\[)(sib[a-z0-9]+|mrnaj25i)(<|,|\s|\])/.test(html.replace(/title="[^"]*"/g,'')); }

const out = [];
function check(name, cond){ out.push((cond?'PASS ':'FAIL ')+name); }

const html = _renderPeerInjectRow({
  roundNum: 9000002, peerCount: 1,
  peerPreviews: [{ fromConv: 'mrnaj25i', text: 'Heads up: fixing the bug' }],
});

if (!NEUTER) {
  check('title_bubble_present', html.includes('修复显示层 Bug'));
  check('title_bubble_class', html.includes('sw-peer-from-bubble'));
  // The raw id must NOT be a visible label — only inside attributes.
  check('raw_id_not_a_label', !html.includes('>mrnaj25i<'));
  // Jump affordance: the bubble is a <button> carrying data-conv-jump, and its
  // tooltip carries the FULL title + the raw id + the jump hint (the visible
  // label ellipsizes — the tooltip is where the full title lives).
  check('bubble_is_jump_button',
    html.includes('<button') && html.includes('data-conv-jump="mrnaj25i"'));
  check('tooltip_full_title_and_hint',
    /title="[^"]*修复显示层 Bug[^"]*mrnaj25i[^"]*Open conversation"/.test(html));
  // The model-view affordance is GONE (removed 2026-07-28 per owner) and must
  // not creep back — neither the toolContent-backed nor the registry-backed
  // variant, in the header or the body.
  check('no_modelview_btn',
    !html.includes('data-tc-preview') && !html.includes('tc-preview-btn'));
  // The dead chevron span is gone (CSS ::after handles collapse now).
  check('no_chevron_span', !html.includes('sw-inbox-row-chev'));
  // No per-card raw toggle inside the body anymore.
  check('no_percard_raw_toggle', !html.includes('sw-card-raw'));
  // Single-sender dedup: the header already carries the sender bubble, so the
  // body card must NOT repeat the identical bubble (the reported "来源重复").
  check('single_body_no_dup_bubble', !bodyOf(html).includes('sw-peer-from-bubble'));

  // ── MULTI-SENDER: header must show a title bubble PER distinct sender,
  //    never a raw id list. 5 senders → cap 3 bubbles + a "+2" overflow. ──
  const multi = _renderPeerInjectRow({
    roundNum: 9000003, peerCount: 5,
    peerPreviews: [
      { fromConv: 'sibaaaa1', text: 'a' }, { fromConv: 'sibbbbb2', text: 'b' },
      { fromConv: 'sibccccc3', text: 'c' }, { fromConv: 'sibddddd4', text: 'd' },
      { fromConv: 'mrnaj25i', text: 'e' },
    ],
  });
  const mhead = headerOf(multi);
  check('multi_header_has_2plus_bubbles', nOcc(mhead, 'sw-peer-from-bubble') >= 2);
  check('multi_header_has_group', mhead.includes('sw-peer-from-group'));
  check('multi_header_overflow_chip',
    mhead.includes('sw-peer-from-more') && mhead.includes('+2'));
  // NO bare conv id as a visible label anywhere in the header.
  check('multi_no_raw_id_label', !hasRawIdLabel(mhead));
  // The old raw-id fallback markup is fully gone.
  check('multi_no_sw_inbox_row_ids', !multi.includes('sw-inbox-row-ids'));
  // The first 3 senders render as real titles (mrnaj25i is 5th → in +2 overflow).
  check('multi_titles_shown',
    mhead.includes('队列注入') && mhead.includes('inbox 重建') && mhead.includes('前缀缓存'));
  // Multi-sender: per-card attribution is KEPT in the body (only the
  // single-sender case dedups against the header bubble).
  check('multi_body_keeps_percard_bubbles', nOcc(bodyOf(multi), 'sw-peer-from-bubble') >= 2);
} else {
  // NEUTER: convTitleById always falls back → the specific title is gone,
  // proving the title lookup (not a hardcoded string) produced the bubble.
  check('nc_title_falls_back',
    !html.includes('修复显示层 Bug') && html.includes('Untitled chat'));
  // Even with titles neutered the MULTI-sender header still uses bubbles
  // (never a raw id list) — the fallback is a localized label, not the id.
  const multi = _renderPeerInjectRow({
    roundNum: 9000003, peerCount: 2,
    peerPreviews: [{ fromConv: 'sibaaaa1', text: 'a' }, { fromConv: 'sibbbbb2', text: 'b' }],
  });
  const mhead = headerOf(multi);
  check('nc_multi_still_bubbles',
    nOcc(mhead, 'sw-peer-from-bubble') >= 2 && !hasRawIdLabel(mhead));
}

console.log(out.join('\n'));
"""


def _run(neuter: bool) -> str:
    env = dict(os.environ, TR_JS=TR_JS, NEUTER='1' if neuter else '0')
    proc = subprocess.run(['node', '-e', _HARNESS], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_peer_inject_row_title_bubble_and_no_modelview():
    out = _run(neuter=False)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'peer-inject row layout failures:\n' + out
    assert out.count('PASS') >= 15, f'expected >=15 PASS, got:\n{out}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_multi_sender_header_uses_title_bubbles_not_raw_ids():
    """The multi-sender header (this project's normal concurrent-sibling case)
    must render one title bubble per distinct sender + a +K overflow chip, and
    NEVER a bare `[sib1, sib2 …]` id list."""
    out = _run(neuter=False)
    for key in ('multi_header_has_2plus_bubbles', 'multi_header_has_group',
                'multi_header_overflow_chip', 'multi_no_raw_id_label',
                'multi_no_sw_inbox_row_ids', 'multi_titles_shown'):
        assert f'PASS {key}' in out, f'multi-sender check failed ({key}):\n' + out


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_neutered_convtitle_loses_the_title_bubble():
    out = _run(neuter=True)
    assert 'PASS nc_title_falls_back' in out, (
        'NC control failed — the title bubble is not sourced from convTitleById:\n' + out)
    assert 'PASS nc_multi_still_bubbles' in out, (
        'NC control failed — multi-sender header fell back to raw ids:\n' + out)


def test_source_uses_title_bubble_and_no_modelview():
    """Cheap source guard (runs without node): the peer row builds a sender
    bubble, emits NO model-view button (removed 2026-07-28), and no longer
    emits a chevron span or a raw-id list."""
    src = open(TR_JS, encoding='utf-8').read()
    peer = _pull_fn(src, '_renderPeerInjectRow')
    assert '_peerFromBubbleGroup' in peer, 'peer row header no longer uses the title-bubble group'
    assert '_perCardAttribution' in peer, (
        'the single-sender body dedup was removed — the duplicate sender bubble regression is back')
    bubble = _pull_fn(src, '_peerFromBubble')
    assert 'data-conv-jump' in bubble and '<button' in bubble, (
        'the sender bubble lost its click-to-jump affordance')
    assert 'convFullIdById' in src and 'loadConversation(' in src, (
        'the delegated jump handler (bubble → loadConversation) is gone')
    assert '_tcModelViewBtnForText' not in peer, (
        'the model-view button crept back into the peer row header')
    assert 'sw-inbox-row-chev' not in peer, 'the dead chevron span was re-introduced'
    assert 'sw-inbox-row-ids' not in peer, 'the raw-id fallback list was re-introduced in the peer row'


if __name__ == '__main__':
    if not shutil.which('node'):
        print('SKIP — node not available')
    else:
        print(_run(neuter=False))
        print(_run(neuter=True))
    test_source_uses_title_bubble_and_no_modelview()
    print('PASS source guard')
