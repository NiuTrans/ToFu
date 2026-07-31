"""Static guard: Project Brain panel readability + zh chrome purity.

WHY (2026-07-25, owner report): the Project Brain dashboard on a LIGHT theme
(tofu paper / light) had three readability defects —

  1. STATUS HUES tuned for dark surfaces washed out on paper: the
     "auto-starts ~30s" amber badge (#eab308 text on cream ≈ 1.6:1) and the
     green board-tab accent (#22c55e ≈ 2.3:1) were nearly unreadable. The
     fix deepens --pb-green/--pb-red/--pb-amber under [data-theme=light|tofu]
     ONLY (dark theme keeps the bright hues).
  2. The long-text clamp fade was 2.4em tall — it covered the whole last
     visible line, so a clamped epic's tail read as washed-out grey (the
     "some characters are hard to read" complaint). Now 1.15em: only the
     bottom edge fades, as a "there is more" hint.
  3. zh chrome labels mixed English "epic" into Chinese sentences
     ("新建 epic", "epic 标题"). The board's own tab is 任务板, so the zh
     noun is 任务 — these pins keep the zh values free of the English word.

NATURE OF THESE TESTS — contract pins (golden strings) + one REAL WCAG
contrast computation. They are not behavioural proof; they exist so a future
restyle cannot silently re-introduce the washed-out light-theme palette, the
line-covering fade, or the mixed-language labels. If a deliberate redesign
changes the tokens, update the pins IN THE SAME COMMIT.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_STYLES = os.path.join(ROOT, 'static', 'styles.css')
_I18N = os.path.join(ROOT, 'static', 'js', 'i18n.js')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── WCAG contrast ──────────────────────────────────────────────────
def _luminance(hexcolor: str) -> float:
    h = hexcolor.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def test_light_theme_pb_status_tokens_exist_and_contrast():
    """The light/tofu override block must exist and every deepened status hue
    must hold ≥ 4.0:1 against WHITE (the strictest light ground — cream/paper
    is darker, so white-passing hues pass everywhere on light themes)."""
    css = _read(_STYLES)
    block_re = re.compile(
        r'\[data-theme="light"\]\s+\.project-brain-overlay,\s*'
        r'\[data-theme="tofu"\]\s+\.project-brain-overlay\s*\{([^}]*)\}')
    m = block_re.search(css)
    assert m, ('the light/tofu .project-brain-overlay status-token override '
               'block is missing — light themes fall back to the washed-out '
               'dark-theme hues (#22c55e/#ef4444/#eab308)')
    body = m.group(1)
    tokens = dict(re.findall(r'--pb-(green|red|amber)\s*:\s*(#[0-9a-fA-F]{6})', body))
    assert set(tokens) == {'green', 'red', 'amber'}, \
        f'the override must deepen all three status hues: {tokens}'
    for name, hexv in tokens.items():
        ratio = _contrast(hexv, '#ffffff')
        assert ratio >= 4.0, \
            f'--pb-{name} {hexv} contrasts only {ratio:.2f}:1 on white — ' \
            f'the light-theme badge/tab text would be unreadable again'


def test_dark_theme_pb_tokens_untouched():
    """The DEFAULT (dark) overlay tokens keep the original bright hues — the
    deepening is scoped to light themes only."""
    css = _read(_STYLES)
    m = re.search(r'\.project-brain-overlay\{(.*?)\n\}', css, re.S)
    assert m, 'base .project-brain-overlay token block not found'
    assert '--pb-green:#22c55e' in m.group(1), 'dark green token drifted'
    assert '--pb-amber:#eab308' in m.group(1), 'dark amber token drifted'


def test_clamp_fade_is_a_hint_not_a_cover():
    """The collapsed-clamp gradient fade must stay short (≤1.3em): the 2.4em
    fade covered the entire last visible line — the "hard to read" complaint."""
    css = _read(_STYLES)
    m = re.search(r'\.pb-clamp:not\(\.pb-clamp-open\)::after\{([^}]*)\}', css)
    assert m, 'pb-clamp ::after fade rule not found'
    hm = re.search(r'height\s*:\s*([0-9.]+)em', m.group(1))
    assert hm, 'fade height not declared'
    assert float(hm.group(1)) <= 1.3, \
        f'fade height {hm.group(1)}em covers a whole text line again (was 2.4em)'


def test_board_card_status_edges_present():
    """Every board lane status colour-codes its card's leading edge (the
    scan-by-colour language held/awaiting already had)."""
    css = _read(_STYLES)
    for status in ('open', 'claimed', 'blocked', 'done', 'held'):
        sel = f'.pb-board-card.pb-board-{status}'
        assert sel in css, f'missing status-edge rule for {status}'
        rule = re.search(re.escape(sel) + r'\{([^}]*)\}', css)
        assert rule and 'border-left' in rule.group(1), \
            f'{sel} lost its leading-edge colour coding'


def test_zh_chrome_labels_do_not_mix_english_epic():
    """The board's zh noun is 任务 (the tab is 任务板) — zh chrome strings must
    not fall back to the English word "epic". (The peerAdvancing {epic} token
    is a template VARIABLE name, not display text — excluded.)"""
    src = _read(_I18N)
    keys = [
        'collab.epicsInProgress',
        'projectBrain.autoStartTitle',
        'projectBrain.newEpic',
        'projectBrain.newEpicNoConv',
        'projectBrain.newEpicPrompt',
        'projectBrain.boardUntitled',
    ]
    for key in keys:
        m = re.search(re.escape("'" + key + "'") +
                      r"\s*:\s*\{\s*zh:\s*'([^']*)'", src)
        assert m, f'i18n key {key} not found'
        assert 'epic' not in m.group(1).lower(), \
            f'{key} zh value mixes the English word again: {m.group(1)}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
