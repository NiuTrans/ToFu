"""Frontend guards for the Goals lane (Project Brain Status & Focus).

REPOINTED 2026-07-30 — a goal no longer travels through the charter at all.

This file was written for the design in which a goal was COPIED into the
charter's north-star column, so it guarded the machinery that copy required: a
three-state badge, a replacement-preview card, and a version gate. The owner
then directed that goals work WITHOUT being in the charter ("goals are goals").
With one copy instead of two, none of that machinery exists — so the tests that
asserted it are reversed in place rather than deleted, because a deleted test
lets the next person rebuild the duplication.

What is guarded NOW, and why it still needs node rather than a source scan: the
frontend decides whether a charter-writing button is offered at all. The
original measured bug was branch logic in ``_buildWatchActions`` (it gated the
promote button on ``!item.promoted``, handing a diverged goal a button that
silently overwrote whichever side the human had just edited). The current
invariant is the sharper descendant of that: a GOAL must never be offered a
charter-writing button, in any state. No Python assertion can see it — it is a
property of the shipped renderer.

Each test loads the REAL ``static/js/project-brain-status.js`` into a node
harness with a minimal DOM and drives the exported ``buildWatchItem``.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, 'static', 'js', 'project-brain-status.js')

_HARNESS = r"""
// ── Minimal DOM good enough for the builders under test ──────────────
function mkEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: '', textContent: '', hidden: false, disabled: false,
    type: '', rows: 0, value: '', placeholder: '', title: '',
    children: [], _attrs: {}, _listeners: {}, parentNode: null,
    appendChild(c) { c.parentNode = el; el.children.push(c); return c; },
    removeChild(c) { el.children = el.children.filter(x => x !== c); return c; },
    setAttribute(k, v) { el._attrs[k] = String(v); },
    getAttribute(k) { return el._attrs[k] === undefined ? null : el._attrs[k]; },
    addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
    click() { (el._listeners['click'] || []).forEach(fn => fn({})); },
    closest(sel) {
      const want = sel.replace(/^\./, '');
      let n = el;
      while (n) { if ((n.className || '').split(/\s+/).includes(want)) return n; n = n.parentNode; }
      return null;
    },
    querySelector(sel) {
      const want = sel.replace(/^\./, '');
      const walk = (n) => {
        for (const c of n.children) {
          if ((c.className || '').split(/\s+/).includes(want)) return c;
          const hit = walk(c); if (hit) return hit;
        }
        return null;
      };
      return walk(el);
    },
    get innerHTML() { return ''; },
    set innerHTML(_v) { el.children = []; },
  };
  return el;
}
const _byId = {};
global.document = {
  createElement: mkEl,
  createDocumentFragment: () => mkEl('fragment'),
  getElementById: (id) => _byId[id] || null,
};
global.window = {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;                       // labels come back as their key
global.Icon = () => '';
global.Api = { project: {
  brainWatchAddress: () => Promise.resolve({}),
  brainWatchUpdate: () => Promise.resolve({}),
  brainWatchPromote: (...a) => { global.__promoteCalls.push(a); return Promise.resolve({}); },
  brainWatchList: () => Promise.resolve({ items: [] }),
} };
global.__promoteCalls = [];

require(process.env.PB_STATUS_JS);
const PBS = global.window.ProjectBrainStatus;

// Collect every text node in a subtree (class list + textContent).
function flatten(node, out) {
  out = out || [];
  out.push({ cls: node.className || '', text: node.textContent || '' });
  (node.children || []).forEach(c => flatten(c, out));
  return out;
}
function hasClass(node, cls) { return flatten(node).some(n => (n.cls || '').split(/\s+/).includes(cls)); }
function textsOf(node) { return flatten(node).map(n => n.text).filter(Boolean); }

global.__probe = { PBS, flatten, hasClass, textsOf, mkEl };
"""


def _run(script: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')
    src = _HARNESS + '\n' + script
    env = dict(os.environ, PB_STATUS_JS=_JS)
    proc = subprocess.run([node, '-e', src], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, (
        f'node harness failed\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


_CTX = '{charterVersion: 7}'  # charterContent is no longer sent


def _item(**kw):
    base = {'item_id': 'w1', 'kind': 'goal', 'text': 'NEW GOAL TEXT',
            'status': 'open', 'promotionState': 'none', 'divergedSide': '',
            'injected': True, 'promotedAudit': False, 'responses': []}
    base.update(kw)
    return json.dumps(base)


def test_an_open_goal_says_it_is_live_and_offers_no_charter_button():
    """An OPEN goal is in every sibling's prompt because it exists, so it states
    that as a fact — and must never be offered a charter-writing button. There
    is nothing to promote, and a button implying otherwise would suggest the
    charter is why the goal works (it is not), which would then make Resolve
    look like it should not withdraw it."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_item()}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        promoteBtn: __probe.hasClass(card, 'pb-watch-btn-promote'),
        replaceCard: __probe.hasClass(card, 'pb-watch-replace'),
        texts: __probe.textsOf(card),
      }}));
    """)
    assert out['live'] is True
    assert out['promoteBtn'] is False, (
        'a goal must never be offered a charter-writing button')
    assert out['replaceCard'] is False
    assert 'projectBrain.watchGoalLive' in out['texts']


def test_a_resolved_goal_does_not_claim_to_be_live():
    """Resolve is the withdrawal lever, so a resolved goal must stop saying every
    conversation reads it — the badge follows the backend's `injected` fact."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem(
        {_item(status='resolved', injected=False)}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        promoteBtn: __probe.hasClass(card, 'pb-watch-btn-promote'),
      }}));
    """)
    assert out['live'] is False, 'a resolved goal must not claim to be injected'
    assert out['promoteBtn'] is False


def test_the_replacement_preview_machinery_is_gone():
    """REVERSED IN PLACE. Three tests here used to assert this machinery worked:
    that clicking a goal opened a two-column replacement preview, that it never
    called promote directly, and that confirming submitted the rendered
    charterVersion as a hard gate.

    All three were correct FOR THAT DESIGN — the charter's content column is a
    single slot, so overwriting it needed confirmation and a version gate. With
    a goal no longer copied there, the preview has nothing to compare and the
    version gate nothing to protect. Asserting their ABSENCE is what keeps the
    duplication from being rebuilt: if a future change reintroduces a
    goal→charter write, it will have to reintroduce these symbols and this test
    goes red."""
    from tests._source_scan import strip_comments
    with open(_JS, encoding='utf-8') as f:
        # Comments MUST be stripped FIRST. The change that removed this
        # machinery necessarily EXPLAINS it, naming the very symbols banned
        # below — so a raw substring scan is satisfied by its own explanation
        # and reports a clean tree as dirty. This is the recurring trap this
        # project logs (charter #24): the guard tripped by the comment that
        # documents why the guard exists. Measured here on the first run.
        src = strip_comments(f.read(), lang='js')
    for gone in ('_buildGoalReplaceCard', '_divergedHint',
                 'pb-watch-replace', 'watchSetAsGoal', 'watchReadopt',
                 'charterContent'):
        assert gone not in src, (
            f'{gone!r} is back in project-brain-status.js — the goal→charter '
            f'duplication (and the diverged state it forces) is being rebuilt')
    # And no goal-side code may call the promote endpoint any more.
    assert 'brainWatchPromote' in src, 'concern/question still need the bridge'


def test_a_goal_never_reaches_the_promote_endpoint(monkeypatch):
    """Behavioural complement to the source scan: drive every reachable button
    on a goal card and assert the promote API is never called."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_item()}, {_CTX});
      const flat = __probe.flatten(card);
      // Click EVERY button-ish node on the card.
      const clickAll = (n) => {{
        if ((n._listeners || {{}}).click) {{ try {{ n.click(); }} catch (e) {{}} }}
        (n.children || []).forEach(clickAll);
      }};
      clickAll(card);
      console.log(JSON.stringify({{
        promoteCalls: global.__promoteCalls.length,
        classes: flat.map(n => n.cls).filter(Boolean),
      }}));
    """)
    assert out['promoteCalls'] == 0, (
        'a goal card reached the promote endpoint — goals must never write the '
        'charter, in any state or via any button')


def test_concern_keeps_the_direct_decision_path():
    """A concern is NOT a goal: appends commute, so it needs no replacement
    preview and must still promote in one click."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem(
        {_item(kind='concern', text='Artifacts may desync')}, {_CTX});
      card.querySelector('.pb-watch-btn-promote').click();
      console.log(JSON.stringify({{
        calls: global.__promoteCalls.length,
        previewOpened: __probe.hasClass(card, 'pb-watch-replace'),
      }}));
    """)
    assert out['calls'] == 1, 'a concern must promote directly (appends commute)'
    assert out['previewOpened'] is False


def test_renderer_never_reads_the_stale_promoted_boolean():
    """The stored flag records that a promotion once happened; it stays true
    after the charter is deleted. Rendering it is how the badge came to lie."""
    out = _run(f"""
      // promotedAudit=true but the item is a CONCERN whose verdict says 'none'.
      const card = __probe.PBS.buildWatchItem(
        {_item(kind='concern', promotionState='none', promotedAudit=True, injected=False)}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        diverged: __probe.hasClass(card, 'pb-watch-diverged'),
      }}));
    """)
    assert out['live'] is False, (
        'the badge must follow the computed verdict, not the stored boolean')
    assert out['diverged'] is False


def test_source_has_no_bare_promoted_branch():
    """Complement to the behavioural tests: the renderer must not regrow a
    branch on the stale boolean. `promotedAudit` (the renamed audit field) is
    allowed to APPEAR in the payload mapping; what is banned is branching on a
    bare `item.promoted`."""
    with open(_JS, encoding='utf-8') as f:
        src = f.read()
    assert 'item.promoted)' not in src, (
        'project-brain-status.js branches on the stale `item.promoted` boolean '
        '— render item.promotionState (the computed verdict) instead')
    assert 'item.promotionState' in src, 'the computed verdict must be rendered'
