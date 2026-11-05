"""tests/test_page_asset_paths.py — every page must survive a prefixed mount.

Why this exists
---------------
This plugin's pages are served by the Tofu host, and the host is routinely
reached through a path-prefixing reverse proxy (VS Code / Codespaces /
Gitpod / JupyterHub port-forwarding — ``server.py::_detect_reverse_proxy``
enumerates them, and ``VSCODE_PROXY_URI`` on the dev box is literally
``https://…/proxy/{{port}}/``). Under such a mount a page lives at
``<prefix>/<page>.html``, so a *root-absolute* ref like
``/trading-static/trading.css`` resolves against the ORIGIN, drops the
prefix and 404s, while the page-relative ``trading-static/trading.css``
resolves against the document and hits the blueprint route.

The failure mode is total and SILENT: every stylesheet 404s, the browser
paints the DOM with no CSS at all, and the page looks like a badly-designed
wall of text rather than a broken one. Nothing errors. The host's own
``index.html`` has used 100% relative asset paths from the start for exactly
this reason; ``trading.html`` diverged when it was extracted into this
plugin (0a5041c) and was never relative until 29457bd.

Why it scans a DIRECTORY rather than one file
---------------------------------------------
The bug only manifests behind a prefix, and the whole test suite runs
direct-to-localhost — so nothing about a *new* page would reveal it either.
Pinning one filename would leave the identical trap open for page #2. These
guards therefore enumerate every tracked HTML template and hold all of them
to the same contract.

Enumeration uses ``git ls-files``, NOT ``os.walk``. Measured: a walk of this
repo finds 3 ``trading.html`` files — the real one plus two stale copies
under ``.tofu_trash/`` (recovery snapshots), one of which still carries the
3 pre-fix absolute refs. A walk-based guard would be permanently, unfixably
red on a file nobody ships. (``git ls-files`` is also the charter-mandated
enumeration here: ``os.walk`` times out on the FUSE mount.)

Discipline
----------
These assert the RESULT ("no ref can lose a mount prefix"), not the
implementation — renaming ``trading-static``, adding a stylesheet, adding a
page, or reordering the ``<head>`` must not make them red, while
reintroducing a single root-absolute ref must.
"""

import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

# Refs the browser resolves against the DOCUMENT url — i.e. the ones a mount
# prefix applies to. <a href> is included: a root-absolute nav target escapes
# the deployment just as thoroughly as a root-absolute stylesheet, it just
# fails on click instead of on load.
_ASSET_RE = re.compile(
    r'<(?:link|script|img|source|iframe)\b[^>]*?\b(?:href|src)\s*=\s*"([^"]+)"', re.I)
_NAV_RE = re.compile(r'<a\b[^>]*?\bhref\s*=\s*"([^"]+)"', re.I)


def _tracked_html():
    """Every HTML template git actually ships, as absolute paths."""
    out = subprocess.run(['git', 'ls-files', '*.html'], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [os.path.join(ROOT, p) for p in out.split('\n') if p.strip()]


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _is_prefix_losing(url: str) -> bool:
    """True when the browser would resolve ``url`` against the ORIGIN.

    Root-absolute (``/x``) refs discard any mount prefix. Protocol-relative
    (``//host/x``), absolute (``https://…``), inline (``data:``), fragment
    and non-navigating schemes are origin- or self-contained, so a mount
    prefix never applied to them in the first place.
    """
    if url.startswith(('data:', 'http://', 'https://', '//', '#',
                       'mailto:', 'tel:', 'javascript:', 'blob:')):
        return False
    return url.startswith('/')


def _refs(html: str):
    """(asset_refs, nav_refs) for one document."""
    return ([m.group(1) for m in _ASSET_RE.finditer(html)],
            [m.group(1) for m in _NAV_RE.finditer(html)])


@pytest.mark.unit
def test_scan_surface_report():
    """Source path guards must inspect at least one tracked page.

    Emitted CSS/module presence is checked through actual native HTTP in
    tests/frontend/test_plugin_frontend_delivery.py, including a proxy mount.
    """
    assert _tracked_html(), 'no tracked HTML found; path guards would inspect nothing'


@pytest.mark.unit
def test_no_asset_ref_loses_a_mount_prefix():
    """Every asset every page loads must resolve under a prefixed mount."""
    bad = []
    for p in _tracked_html():
        rel = os.path.relpath(p, ROOT)
        bad += [f'{rel}: {r}' for r in _refs(_read(p))[0] if _is_prefix_losing(r)]
    assert not bad, (
        'root-absolute asset ref(s) found:\n  ' + '\n  '.join(bad) +
        '\n\nServed at <prefix>/<page>.html these resolve against the ORIGIN, '
        'dropping the prefix, so they 404 behind the reverse proxy this project '
        'is routinely deployed under (VSCODE_PROXY_URI=…/proxy/{{port}}/). '
        'For stylesheets the result is a page painted with NO CSS at all — a '
        'silent failure that looks like bad design, not like an error. '
        'Drop the leading "/" to make the ref page-relative.')


@pytest.mark.unit
def test_no_nav_link_escapes_the_mount():
    """In-app navigation must not jump out of the deployment.

    Same bug class, different symptom: ``href="/"`` under a ``/proxy/15000/``
    mount lands on the PROXY's root rather than the Tofu home page, so the
    only way back is the browser Back button.
    """
    bad = []
    for p in _tracked_html():
        rel = os.path.relpath(p, ROOT)
        bad += [f'{rel}: {r}' for r in _refs(_read(p))[1] if _is_prefix_losing(r)]
    assert not bad, (
        'root-absolute nav link(s) found:\n  ' + '\n  '.join(bad) +
        '\n\nUnder a prefixed mount these leave the Tofu deployment entirely. '
        'Use a page-relative target (e.g. "./" or "index.html").')
