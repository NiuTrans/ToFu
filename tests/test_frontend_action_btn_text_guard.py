"""General guard for the icon-only ``.action-btn`` square.

WHY (supersedes tests/test_frontend_orphan_resume_btn_class.py)
---------------------------------------------------------------
The orphan-resume affordance was the concrete instance of a general bug class:
a TEXT-bearing button was emitted as ``<button class="orphan-resume-btn
action-btn">…</button>``. ``.action-btn`` (static/styles.css) is the project's
fixed-size ICON-ONLY square — ``width:32px;height:32px``, no padding — so under
the global ``box-sizing:border-box`` the square won over the size-less text
class and squashed the button into a tiny box (the CJK label wrapped one glyph
per line, the icon overflowed).

The orphan-resume button itself is GONE now (the capability was replaced by
typed conversation wake recovery), so the old single-selector
guard rotted to zero hits. But the INVARIANT it taught us is still real and
still worth guarding, and it is bigger than one button:

    a button that carries visible text must NOT wear the bare ``.action-btn``
    square — text needs room, the square has none.

This guard is therefore GENERAL and scans every shipped JS file. It is resolved
by the bundle manifest (``lib/js_bundler._BUNDLE_FILES + _CLASSIC_ASSET_FILES``) —
the single source of truth for what actually reaches a user — NOT by a
hardcoded file path, so it survives the next module split and never scans code
that no user can reach.

What counts as a violation
--------------------------
A ``<button>`` whose class list contains the bare token ``action-btn`` AND
whose inner content carries visible text (after stripping ``<svg>`` / ``<i>``
icons, ``Icon(...)`` interpolations and HTML entities) AND whose opening tag
does NOT inline-override the square (``width:auto`` / ``height:auto``). The
inline override is the legitimate escape hatch the codebase already uses for
text+icon action buttons (e.g. the welcome-screen Retry / New Chat buttons),
so those are NOT flagged.

NEGATIVE CONTROL: ``test_nc_text_button_with_bare_action_btn_goes_red`` builds
a source with exactly the original orphan-resume shape (text button, bare
``action-btn``, no override) and asserts the detector flags it — proving the
guard has teeth. The shipped tree is untouched.

Pure source-level (no node/jsdom needed).
"""

from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FRONTEND_SRC = Path(ROOT) / 'frontend' / 'src'

# A <button> opening tag + its inner content. Shipped JS emits these as HTML
# strings inside template literals / concatenations.
_BUTTON_RE = re.compile(r'<button\b(?P<attrs>[^>]*?)>(?P<inner>.*?)</button>', re.DOTALL)
_CLASS_RE = re.compile(r'class="(?P<cls>[^"]*)"')
# The legitimate escape: an inline size override that defeats the fixed square.
_SIZE_OVERRIDE_RE = re.compile(r'(?:width|height)\s*:\s*auto')


def _shipped_js_files() -> list[Path]:
    """Scan the Vite source graph rather than the retired classic manifest."""
    return sorted(
        path for path in FRONTEND_SRC.rglob('*')
        if path.is_file() and path.suffix in {'.js', '.ts'}
    )


def _inner_has_visible_text(inner: str) -> bool:
    """True when the button's inner content still has visible text after the
    icon/template/entity stripping — i.e. it is a TEXT-bearing button, not an
    icon-only one."""
    s = re.sub(r'<svg\b.*?</svg>', '', inner, flags=re.DOTALL)
    s = re.sub(r'<i\b.*?</i>', '', s, flags=re.DOTALL)
    s = re.sub(r'\$\{[^}]*\}', '', s)          # ${Icon(...)} / ${escapeHtml(...)}
    s = re.sub(r'&nbsp;?', '', s)
    s = re.sub(r'&[a-zA-Z]+;', '', s)
    s = re.sub(r'<[^>]+>', '', s)              # any residual tag
    return bool(s.strip())


def find_text_buttons_with_bare_action_btn(source: str, rel: str) -> list[tuple[str, list[str], str]]:
    """All (rel, classes, inner-preview) buttons in *source* that wear the bare
    ``action-btn`` square AND carry visible text AND have no inline size
    override. Returns [] when clean."""
    violations = []
    for m in _BUTTON_RE.finditer(source):
        attrs, inner = m.group('attrs'), m.group('inner')
        cm = _CLASS_RE.search(attrs)
        if not cm:
            continue
        classes = cm.group('cls').split()
        if 'action-btn' not in classes:
            continue                       # msg-action-btn / conv-action-btn are different classes
        if not _inner_has_visible_text(inner):
            continue                       # icon-only square — the intended use
        if _SIZE_OVERRIDE_RE.search(attrs):
            continue                       # text button that defeats the square inline — legitimate
        violations.append((rel, classes, ' '.join(inner.split())[:60]))
    return violations


def _scan_all_shipped() -> list[tuple[str, list[str], str]]:
    out = []
    for path in _shipped_js_files():
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        with path.open(encoding='utf-8') as f:
            out.extend(find_text_buttons_with_bare_action_btn(f.read(), rel))
    return out


def test_no_text_button_wears_bare_action_btn_square():
    """No shipped text-bearing button wears the fixed 32×32 .action-btn square
    without an inline size override. Regression-locks the squash bug class."""
    violations = _scan_all_shipped()
    assert not violations, (
        'text-bearing button(s) wear the bare 32x32 .action-btn icon square '
        '(they will be squashed). Use a text-button class or inline-override '
        'the size (width:auto;height:auto):\n'
        + '\n'.join(f'  {rel}: class={classes} inner={inner!r}'
                    for rel, classes, inner in violations)
    )


def test_nc_text_button_with_bare_action_btn_goes_red():
    """NC: the detector flags the ORIGINAL orphan-resume shape (text button,
    bare action-btn, no size override) — proving the guard has teeth. The
    shipped tree is untouched (detector runs on an in-memory source)."""
    # The exact bug shape from the original incident: a text+icon button with
    # the bare action-btn class and NO inline size override.
    buggy = ('<button class="orphan-resume-btn action-btn" '
             'onclick="_resumeOrphanTurn()">${Icon(\'rocket\', 13)} 继续回答</button>')
    violations = find_text_buttons_with_bare_action_btn(buggy, '<nc>')
    assert violations, 'NC precondition: the buggy shape was not detected — guard has no teeth'

    # And the same button WITHOUT action-btn (or with a size override) must NOT
    # be flagged — the guard discriminates correctly.
    clean_no_action = buggy.replace(' action-btn', '')
    assert not find_text_buttons_with_bare_action_btn(clean_no_action, '<nc>'), \
        'false positive: button without action-btn was flagged'
    clean_override = buggy.replace('onclick=',
                                   'style="width:auto;height:auto;padding:8px 16px" onclick=')
    assert not find_text_buttons_with_bare_action_btn(clean_override, '<nc>'), \
        'false positive: size-overridden text button was flagged'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
