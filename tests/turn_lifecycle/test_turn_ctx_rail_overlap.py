"""Context rails share a gutter, including user and assistant in one round.

Entry point: the real-app ``page`` fixture and the public
``renderTurnCtxNote`` action. Only message shells/reply heights are synthetic;
all rail/fold markup, layout, observers and scroll handling are production.

Protect parked/scroll/handoff separation, narrow-pane recovery, zero-space
paint suppression/recovery, and an assistant rail inserted without resizing
its host. Assertions inspect rendered geometry/content, never call the private
follower or require its classes, transforms or height-budget implementation.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_WIDE = {'width': 1600, 'height': 900}
_NARROW = {'width': 1000, 'height': 900}
_RAIL_HOSTS = ('u1', 'a1', 'u2')
_EPSILON = 1.0
_TOP_EDGE_RANGE_PX = 12

# Deliberately taller than the first, one-line user message. Otherwise a
# follower which ignores the same-round assistant can pass while still wrong.
_SNAPSHOT = {
    'model': 'claude-opus-5',
    'depth': 'think',
    'tools': [
        {'label': 'Search', 'tone': 'search'},
        {'label': 'Fetch', 'tone': 'net'},
        {'label': 'Memory', 'tone': 'ai'},
    ],
    'roots': [{'short': 'fixture/project', 'path': '/fixture/project', 'ro': False}],
    'git': {'branch': 'fixture/rail-overlap'},
    'todos': [
        {'status': 'in_progress' if i == 0 else 'pending',
         'content': f'Check context rail boundary {i + 1}'}
        for i in range(8)
    ],
}

_PLANT = """({snapshot, assistantRail}) => {
    const inner = document.querySelector('.chat-inner');
    const render = window.TofuModules.resolveAction('renderTurnCtxNote');
    if (!inner || typeof render !== 'function') return 'missing app surface';
    const parts = render(snapshot);
    if (!parts?.rail || !parts?.fold) return 'empty context renderer';
    const message = (key, user, withRail) => {
        const node = document.createElement('div');
        node.className = 'message' + (user ? ' user-msg' : '');
        node.dataset.railOverlapHost = key;
        if (!user) {
            node.style.height = '1400px';
            // An unrailed reply must keep its real spacer height offscreen,
            // not the virtualizer's estimate. Railed hosts keep production CSS.
            if (!withRail) node.style.contentVisibility = 'visible';
        }
        node.innerHTML = '<div class="message-avatar"></div>'
            + '<div class="message-content"><div class="message-header">'
            + '<span class="message-role">' + (user ? 'You' : 'Assistant')
            + '</span></div>' + (withRail ? parts.fold : '')
            + '<div class="message-body"><div class="md-content'
            + (user ? ' user-content' : '') + '">'
            + (user ? 'Short question ' : 'Reply ') + key
            + '</div></div></div>' + (withRail ? parts.rail : '');
        return node;
    };
    inner.append(message('u1', true, true), message('a1', false, assistantRail),
                 message('u2', true, true), message('a2', false, false));
    return 'ok';
}"""

_MEASURE = """() => {
    const cont = document.querySelector('.chat-container');
    const cr = cont.getBoundingClientRect();
    const rect = (r) => ({top: r.top, bottom: r.bottom, left: r.left,
                         right: r.right, width: r.width, height: r.height});
    // Range boxes alone still exist under visibility:hidden and outside a
    // scrollport. Intersect actual text/SVG boxes with ancestor clips so a
    // zero-height rail with overflowing padding/text cannot pass as empty.
    const paintedBounds = (root) => {
        const painted = [];
        const add = (element, raw) => {
            let top = Math.max(raw.top, 0), bottom = Math.min(raw.bottom, innerHeight);
            let left = Math.max(raw.left, 0), right = Math.min(raw.right, innerWidth);
            const leafStyle = getComputedStyle(element);
            if (leafStyle.visibility !== 'visible') return;
            for (let node = element; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.display === 'none' || Number(style.opacity) === 0
                    || style.contentVisibility === 'hidden') return;
                const r = node.getBoundingClientRect();
                if (/^(auto|scroll|hidden|clip)$/.test(style.overflowY)) {
                    top = Math.max(top, r.top + node.clientTop);
                    bottom = Math.min(bottom, r.top + node.clientTop + node.clientHeight);
                }
                if (/^(auto|scroll|hidden|clip)$/.test(style.overflowX)) {
                    left = Math.max(left, r.left + node.clientLeft);
                    right = Math.min(right, r.left + node.clientLeft + node.clientWidth);
                }
            }
            if (bottom > top && right > left) painted.push({top, bottom, left, right});
        };
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const text = walker.currentNode;
            if (!text.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(text);
            for (const r of range.getClientRects()) add(text.parentElement, r);
        }
        for (const svg of root.querySelectorAll('svg')) add(svg, svg.getBoundingClientRect());
        if (!painted.length) return null;
        return {
            top: Math.min(...painted.map(r => r.top)),
            bottom: Math.max(...painted.map(r => r.bottom)),
            left: Math.min(...painted.map(r => r.left)),
            right: Math.max(...painted.map(r => r.right)),
        };
    };
    const hosts = {}, rails = {}, folds = {};
    for (const host of cont.querySelectorAll('[data-rail-overlap-host]')) {
        const key = host.dataset.railOverlapHost;
        const hr = host.getBoundingClientRect();
        hosts[key] = {...rect(hr), docTop: hr.top - cr.top + cont.scrollTop};
        const rail = host.querySelector(':scope > .turn-ctx');
        if (rail) {
            const style = getComputedStyle(rail);
            rails[key] = {...rect(rail.getBoundingClientRect()),
                display: style.display, visibility: style.visibility,
                scrollHeight: rail.scrollHeight, text: rail.textContent.trim(),
                paint: paintedBounds(rail)};
        }
        const fold = host.querySelector('.message-content .tctx-fold');
        if (fold) folds[key] = {...rect(fold.getBoundingClientRect()),
            display: getComputedStyle(fold).display, text: fold.textContent.trim()};
    }
    return {pane: rect(cr), scrollTop: cont.scrollTop, scrollHeight: cont.scrollHeight,
            innerHeight: cont.querySelector('.chat-inner').getBoundingClientRect().height,
            hosts, rails, folds};
}"""


def _settle(page):
    # Allow scroll/ResizeObserver/MutationObserver delivery and the coalesced
    # frame, without invoking any production scheduler or private function.
    page.evaluate("""() => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    })""")


def _measure(page):
    return page.evaluate(_MEASURE)


def _scroll_to(page, y):
    page.evaluate("(y) => { document.querySelector('.chat-container').scrollTop = y; }", y)
    _settle(page)
    return _measure(page)


def _boot(page, *, assistant_rail=True):
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function(
        "typeof window.TofuModules?.resolveAction('renderTurnCtxNote') === 'function'",
        timeout=20000)
    page.set_viewport_size(_WIDE)
    page.evaluate("""async () => {
        // Measure final pane geometry, not the furniture's cosmetic transition.
        for (const node of document.querySelectorAll('.sidebar, .input-inner')) {
            node.style.transition = 'none';
        }
        document.querySelector('.sidebar')?.classList.add('collapsed');
        await document.fonts.ready;
    }""")
    assert page.evaluate(_PLANT, {
        'snapshot': _SNAPSHOT, 'assistantRail': assistant_rail,
    }) == 'ok'
    page.wait_for_function("""() => {
        const rail = document.querySelector('[data-rail-overlap-host="u1"] > .turn-ctx');
        return rail && rail.getBoundingClientRect().width > 0;
    }""", timeout=5000)
    _settle(page)
    state = _measure(page)
    # App startup can leave earlier content above the probes. Align the first
    # user below the pane edge rather than assuming the chat was empty.
    return _scroll_to(page, max(0, state['hosts']['u1']['docTop'] - 48))


def _assert_disjoint(state, phase):
    rails = state['rails']
    assert set(rails) == set(_RAIL_HOSTS), f'{phase}: missing probe rails: {state}'
    for key in _RAIL_HOSTS:
        rail = rails[key]
        assert rail['display'] != 'none' and rail['visibility'] == 'visible', (
            f'{phase}: hiding a rail is not a separation fix: {key}: {rail}')
        assert rail['width'] > 0 and rail['height'] > 0 and rail['text'], (
            f'{phase}: empty rail: {key}: {rail}')
    for previous, following in zip(_RAIL_HOSTS, _RAIL_HOSTS[1:]):
        before, after = rails[previous], rails[following]
        assert before['bottom'] <= after['top'] + _EPSILON, (
            f'{phase}: {previous} overlaps {following}: {before} / {after}')
        if before['paint']:
            assert before['paint']['bottom'] <= after['top'] + _EPSILON, (
                f'{phase}: {previous} text/SVG escapes its rail into {following}: '
                f'{before} / {after}')


def _assert_pinned(state, key):
    rail = state['rails'][key]
    assert state['hosts'][key]['top'] < state['pane']['top'], state
    assert 0 <= rail['top'] - state['pane']['top'] <= _TOP_EDGE_RANGE_PX, (
        f'{key} did not follow its scrolled-away host: {state}')
    assert rail['paint'], f'{key} is positioned but paints no context: {state}'


def test_same_round_user_and_assistant_rails_do_not_overlap(page):
    parked = _boot(page)
    _assert_disjoint(parked, 'parked')
    user, assistant = parked['rails']['u1'], parked['rails']['a1']
    gap = parked['hosts']['a1']['top'] - parked['hosts']['u1']['top']
    assert user['scrollHeight'] > gap + _EPSILON, (
        f'fixture must exercise a rail taller than the short user: {parked}')
    assert user['paint'] and assistant['paint'], parked
    for key in _RAIL_HOSTS:
        assert abs(parked['rails'][key]['top'] - parked['hosts'][key]['top']) <= _EPSILON

    # Both same-round rails are on screen while the short user departs.
    state = _scroll_to(page, parked['hosts']['a1']['docTop'] - 46)
    _assert_disjoint(state, 'user-to-assistant handoff')
    assert state['rails']['u1']['paint'] and state['rails']['a1']['paint'], state

    state = _scroll_to(page, parked['hosts']['a1']['docTop'] + 350)
    _assert_disjoint(state, 'scroll inside assistant reply')
    _assert_pinned(state, 'a1')
    assert state['rails']['u1']['paint'] is None, state

    # The assistant must also yield to the next human, not merely avoid u1.
    state = _scroll_to(page, parked['hosts']['u2']['docTop'] - 46)
    _assert_disjoint(state, 'assistant-to-next-round handoff')
    assert state['rails']['a1']['paint'] and state['rails']['u2']['paint'], state
    assert abs(state['rails']['a1']['bottom'] - state['rails']['u2']['top']) <= 2, (
        f'handoff leaves an early gap instead of continuously yielding: {state}')

    state = _scroll_to(page, parked['hosts']['u2']['docTop'] + 350)
    _assert_disjoint(state, 'scroll inside next round')
    _assert_pinned(state, 'u2')
    assert state['rails']['a1']['paint'] is None, state


def test_same_round_rails_fold_and_restore_without_overlap(page):
    parked = _boot(page)
    state = _scroll_to(page, parked['hosts']['a1']['docTop'] + 350)
    _assert_pinned(state, 'a1')

    page.set_viewport_size(_NARROW)
    _settle(page)
    narrow = _measure(page)
    assert set(narrow['folds']) == set(_RAIL_HOSTS), narrow
    for key in _RAIL_HOSTS:
        rail, fold = narrow['rails'][key], narrow['folds'][key]
        assert rail['display'] == 'none' and rail['paint'] is None, narrow
        assert fold['display'] != 'none' and fold['width'] > 0 and fold['height'] > 0, narrow
        assert fold['text'], f'{key} lost its context in fold mode: {narrow}'

    # Resize alone must re-grant the rail, including its current follow state.
    page.set_viewport_size(_WIDE)
    _settle(page)
    restored = _measure(page)
    _assert_disjoint(restored, 'wide again while scrolled')
    _assert_pinned(restored, 'a1')
    assert all(fold['display'] == 'none' for fold in restored['folds'].values()), restored

    state = _scroll_to(page, parked['scrollTop'])
    _assert_disjoint(state, 'parked after fold/unfold')
    assert state['rails']['u1']['paint'] and state['rails']['a1']['paint'], state
    assert abs(state['rails']['u1']['height'] - parked['rails']['u1']['height']) <= _EPSILON


def test_zero_space_rail_paints_nothing_and_recovers(page):
    parked = _boot(page)
    _assert_disjoint(parked, 'before zero space')
    assert parked['rails']['u1']['paint'], parked

    page.evaluate("""() => {
        const user = document.querySelector('[data-rail-overlap-host="u1"]');
        const assistant = document.querySelector('[data-rail-overlap-host="a1"]');
        assistant.style.position = 'relative';
        assistant.style.top = (user.getBoundingClientRect().top
            - assistant.getBoundingClientRect().top) + 'px';
    }""")
    # A relative top change does not resize the chat. Use a real scroll event
    # to reconcile it; neither the hidden state nor the follower is fabricated.
    zero = _scroll_to(page, parked['scrollTop'] + 1)
    assert abs(zero['hosts']['u1']['top'] - zero['hosts']['a1']['top']) <= _EPSILON, zero
    assert zero['rails']['a1']['paint'], f'the successor must remain visible: {zero}'
    assert zero['rails']['u1']['paint'] is None, (
        f'zero free track still paints the preceding rail content: {zero}')

    page.evaluate("""() => {
        document.querySelector('[data-rail-overlap-host="a1"]').style.removeProperty('top');
    }""")
    recovered = _scroll_to(page, parked['scrollTop'])
    _assert_disjoint(recovered, 'space restored')
    assert recovered['rails']['u1']['paint'], (
        f'the zero-space rail was permanently hidden or clipped: {recovered}')
    assert abs(recovered['rails']['u1']['height'] - parked['rails']['u1']['height']) <= _EPSILON


def test_new_assistant_rail_reclamps_without_scroll_or_resize(page):
    before = _boot(page, assistant_rail=False)
    assert set(before['rails']) == {'u1', 'u2'}, before
    assert before['rails']['u1']['paint'], before
    assert before['rails']['u1']['bottom'] > before['hosts']['a1']['top'] + _EPSILON, (
        f'fixture must initially lend the assistant gutter to the user rail: {before}')

    inserted = page.evaluate("""(snapshot) => {
        const host = document.querySelector('[data-rail-overlap-host="a1"]');
        const parts = window.TofuModules.resolveAction('renderTurnCtxNote')(snapshot);
        if (!parts?.rail || !parts?.fold) return false;
        host.querySelector('.message-header').insertAdjacentHTML('afterend', parts.fold);
        host.insertAdjacentHTML('beforeend', parts.rail);
        return true;
    }""", _SNAPSHOT)
    assert inserted, 'production renderer did not create the dynamic assistant rail'
    # No manual event, viewport change, or scheduler call: an absolute rail
    # and a wide-pane hidden fold do not change the observed content height.
    _settle(page)
    after = _measure(page)
    assert abs(after['scrollTop'] - before['scrollTop']) <= _EPSILON, (before, after)
    assert abs(after['scrollHeight'] - before['scrollHeight']) <= _EPSILON, (before, after)
    assert abs(after['innerHeight'] - before['innerHeight']) <= _EPSILON, (before, after)
    assert abs(after['pane']['width'] - before['pane']['width']) <= _EPSILON, (before, after)
    for key in before['hosts']:
        assert abs(after['hosts'][key]['top'] - before['hosts'][key]['top']) <= _EPSILON
        assert abs(after['hosts'][key]['height'] - before['hosts'][key]['height']) <= _EPSILON
    _assert_disjoint(after, 'assistant rail dynamically inserted')
    assert after['rails']['u1']['paint'] and after['rails']['a1']['paint'], after
    assert after['rails']['u1']['height'] < before['rails']['u1']['height'], (before, after)
