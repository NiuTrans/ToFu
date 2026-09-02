"""Settings-panel HTML fragment injection.

The settings modal in ``index.html`` historically inlined ~1000 lines of
per-tab panel markup (``<div class="settings-tab-panel" id="settingsTab_*">``).
That made a single settings page hard to locate: its structure lived in
``index.html``, its logic in ``static/js/settings/*.js``, and its styles in
``static/styles.css`` — three distant files.

This module decouples the STRUCTURE half. Each migrated panel moves into its
own fragment file under ``static/settings_panels/<tab>.html`` and ``index.html``
keeps only a one-line marker::

    <!-- SETTINGS_PANEL:translate -->

At page-render time ``routes/common.py`` calls :func:`inject_panels` to splice
each fragment back in where its marker sits. No build step (the project ships
vanilla JS with a server-side ``index.html`` rewrite already — see
``routes/common.py:index_page``); this rides the SAME rewrite pass.

Cache safety: :func:`panels_signature` returns a combined ``(mtime, size)``
digest of every fragment file so ``index_page``'s HTML cache key invalidates
when any fragment changes — otherwise editing a fragment would silently NOT
re-render (the same "silent no-op" trap the JS bundler allowlist guards
against, CLAUDE.md §3.2.1).

The marker↔fragment↔assembled-HTML invariants are guarded structurally by
``tests/test_settings_panels_parity.py`` so a migrated panel can never silently
vanish from the served page.
"""
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANELS_DIR = os.path.join(BASE_DIR, 'static', 'settings_panels')

# Marker the injector replaces. Tab id is restricted to [a-z_] so it maps 1:1
# to a `settingsTab_<tab>` panel id and a `<tab>.html` fragment filename.
_MARKER_RE = re.compile(r'<!--\s*SETTINGS_PANEL:([a-z_]+)\s*-->')

# ── Maintainer-only tabs (hidden from opensource builds) ─────────────────
# Most iteration feedback on this project comes from the maintainer, so the
# settings UI stays deliberately detailed. Tabs in this set are dogfooding
# knobs (e.g. the cost A/B experiment) that public users/developers should
# never see: in opensource builds ``inject_panels`` strips the nav button AND
# the panel marker at render time, and export.py additionally excludes the
# fragment file from the exported tree and bakes the flag default below to
# '1' — belt-and-braces, the same contract as lib/mcp/registry.py's
# ``internal_only`` catalog filter.
MAINTAINER_ONLY_TABS = frozenset({
    'experiments',
})

# export.py's opensource sanitizer flips this default to '1' in the exported
# copy of THIS file (literal replacement, filepath-gated), so the stripped
# render below does not depend on the operator remembering to set the env var.
# An env override is provided for tests / forced opensource runs.
_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '1').strip().lower() in {
    '1', 'true', 'yes', 'on',
}


def is_opensource_build() -> bool:
    """True when running an opensource build (maintainer-only tabs hidden)."""
    return _OPENSOURCE_BUILD


def _strip_nav_button(html, tab):
    """Remove the settings nav ``<button>`` for ``tab`` (opensource builds).

    Loud on drift: if the button is not found exactly once, index.html was
    restructured and the strip would silently leak the tab — log an error so
    the failure is visible (and the parity/opensource-strip tests go red).
    """
    rx = re.compile(
        r'^[ \t]*<button class="settings-tab" data-tab="%s"[^>]*>.*?</button>\n?'
        % re.escape(tab),
        re.S | re.M,
    )
    html, n = rx.subn('', html, count=1)
    if n != 1:
        logger.error('[SettingsPanels] opensource strip: nav button for '
                     'maintainer-only tab=%s matched %d times (expected 1) — '
                     'index.html drifted?', tab, n)
    return html


def _strip_panel_marker(html, tab):
    """Remove the ``<!-- SETTINGS_PANEL:tab -->`` marker line for ``tab``."""
    rx = re.compile(
        r'^[ \t]*<!--\s*SETTINGS_PANEL:%s\s*-->[ \t]*\n?' % re.escape(tab),
        re.M,
    )
    html, n = rx.subn('', html, count=1)
    if n != 1:
        logger.error('[SettingsPanels] opensource strip: panel marker for '
                     'maintainer-only tab=%s matched %d times (expected 1) — '
                     'index.html drifted?', tab, n)
    return html


def strip_maintainer_only_tabs(html):
    """Strip maintainer-only tabs (nav button + panel marker) in opensource builds.

    Runs BEFORE fragment injection inside :func:`inject_panels`, so a stripped
    marker never reaches ``_load_fragment`` — the fragment file may legitimately
    be absent (export.py excludes it from the opensource tree). No-op in
    personal/internal builds.
    """
    if not _OPENSOURCE_BUILD:
        return html
    for tab in sorted(MAINTAINER_ONLY_TABS):
        html = _strip_nav_button(html, tab)
        html = _strip_panel_marker(html, tab)
    return html


def marker_for(tab):
    """Return the canonical marker comment string for a tab id."""
    return '<!-- SETTINGS_PANEL:%s -->' % tab


def fragment_path(tab):
    """Absolute path of the fragment file for ``tab`` (may not exist)."""
    return os.path.join(PANELS_DIR, '%s.html' % tab)


def list_fragment_tabs():
    """Return the set of tab ids that have a fragment file on disk."""
    try:
        return {
            f[:-5] for f in os.listdir(PANELS_DIR)
            if f.endswith('.html')
        }
    except OSError as e:
        logger.debug('[SettingsPanels] Cannot list %s: %s', PANELS_DIR, e)
        return set()


def find_markers(html):
    """Return the list of tab ids referenced by markers in ``html`` (in order)."""
    return _MARKER_RE.findall(html or '')


def panels_signature():
    """Return a digest string of every fragment file's ``(mtime, size)``.

    Used as part of the served-HTML cache key so any fragment edit forces a
    re-render. Returns ``''`` when the directory is absent (no panels migrated
    yet) — a stable value that keeps the cache warm.
    """
    parts = []
    try:
        for name in sorted(os.listdir(PANELS_DIR)):
            if not name.endswith('.html'):
                continue
            try:
                st = os.stat(os.path.join(PANELS_DIR, name))
                parts.append('%s:%d:%d' % (name, int(st.st_mtime), st.st_size))
            except OSError as e:
                logger.debug('[SettingsPanels] stat %s failed: %s', name, e)
    except OSError as _e:
        logger.debug('panels signature: unreadable (%s)', _e)
        return ''
    return '|'.join(parts)


def _load_fragment(tab):
    """Read a fragment file's content, or ``None`` if missing/unreadable."""
    path = fragment_path(tab)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        logger.error('[SettingsPanels] Fragment for tab=%s missing (%s) — '
                     'panel will be ABSENT from the served page', tab, e)
        return None


def inject_panels(html):
    """Replace every ``<!-- SETTINGS_PANEL:tab -->`` marker with its fragment.

    A marker whose fragment file is missing is left in place (and logged as an
    error) rather than silently dropped, so the failure is visible in the page
    source instead of manifesting as a vanished tab.

    Args:
        html: The raw ``index.html`` contents.

    Returns:
        The HTML with all resolvable panel markers spliced in.
    """
    # Opensource builds: maintainer-only tabs (nav button + marker) are removed
    # before injection — see strip_maintainer_only_tabs.
    html = strip_maintainer_only_tabs(html)

    def _sub(m):
        tab = m.group(1)
        frag = _load_fragment(tab)
        if frag is None:
            return m.group(0)  # keep marker visible; error already logged
        return frag.rstrip('\n')

    return _MARKER_RE.sub(_sub, html)
