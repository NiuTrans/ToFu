"""lib/i18n_boot_keys.py — discover the boot-critical i18n key set.

Epic-E sub-part 1 slice 3 (proposed): the current per-lang pack ships the WHOLE
3041-key dictionary (~211 KB). Only ~9% of those keys are referenced during
first paint (data-i18n* attributes in index.html + t('...') calls reachable
from the boot IIFE). This module is the ATOM the next slice needs: a
verifiable static scan that returns the union of keys the first paint needs.

WHY STATIC ANALYSIS IS DEFENSIBLE HERE (checked, not assumed)
-------------------------------------------------------------
A regex-based key scan is only safe when every t()-in-boot call uses a
LITERAL string as its first argument — dynamic template literals or
identifier calls would silently miss keys. The current codebase HAS four
documented ``t('prefix.' + variable)`` concatenation sites (net.state.,
update.phase., finishInfo.cb., finishInfo.cbState.). Those are covered by
:data:`T_CALL_DYNAMIC_PREFIX_RE` and :func:`expand_dynamic_prefixes`, which
enumerate every source-dict key starting with a discovered prefix.

Template-literal calls (``t(`foo.${x}`)``) do NOT exist in the current core
bundle — the module docstring pins that as a machine-checkable precondition,
and any drift shows up in the tests below as a boot-critical key that isn't
in the source dict.

WHAT COUNTS AS "BOOT-CRITICAL"
------------------------------
A key is boot-critical if the browser could render / need to read it BEFORE
the deferred rest-pack fetch could plausibly complete. That is the union of:

  1. Every ``data-i18n``, ``data-i18n-placeholder``, ``data-i18n-title``,
     ``data-i18n-aria-label``, ``data-i18n-html`` attribute value in
     ``index.html`` (walked verbatim by ``_applyI18n()`` on DOMContentLoaded).
  2. Every literal-string ``t('key')`` / ``t("key")`` call in the files that
     ship in the CORE bundle (``lib/js_bundler.py::_BUNDLE_FILES``) — those
     files execute during boot; the deferred bundle is only loaded on user
     action, so its ``t()`` calls happen AFTER the rest-pack could land.
  3. For every dynamic ``t('prefix.' + x)`` call in the CORE bundle: every
     key in the source dict starting with ``prefix.`` (expanded via
     :func:`expand_dynamic_prefixes` when ``source_keys`` is supplied).

Ceiling: boot pack size is bounded by "keys referenced by files in the core
bundle" — the deferred bundle's keys go into the rest-pack, so a large keyset
in ``paper-reader.js`` doesn't inflate first paint.

INTERFACE
---------
``discover_boot_keys(repo_root, source_keys=None)`` returns
``{'html': [...], 'js': [...], 'dynamic_prefixes': [...], 'union': [...]}``.
When ``source_keys`` is provided, ``union`` already includes the dynamic
expansion; when omitted, the caller can expand later via
:func:`expand_dynamic_prefixes`.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'HTML_ATTR_KEY_RE',
    'T_CALL_KEY_RE',
    'T_CALL_DYNAMIC_PREFIX_RE',
    'discover_boot_keys',
    'discover_boot_keys_from_bundle_manifest',
    'expand_dynamic_prefixes',
]


# Matches:  data-i18n="key"  |  data-i18n-placeholder="key" | data-i18n-title="key"
# etc.  Values are always simple keys (no interpolation); the shape has held
# for every existing site in index.html.
HTML_ATTR_KEY_RE = re.compile(r'data-i18n(?:-[a-z-]+)?="([^"]+)"')

# Matches a literal-string t() call:  t('foo.bar')  or  t("foo.bar", args)
# — captures the KEY. Requires the first argument to be a single-quoted or
# double-quoted string literal FOLLOWED IMMEDIATELY BY ``)`` or ``,`` (no
# concatenation operator). This is the key discipline: ``t('prefix.' + x)``
# is a DYNAMIC call whose actual key we cannot know statically — a naive
# regex without the lookahead would happily emit ``prefix.`` as a boot key,
# ship it to the pack, and the runtime would render the empty prefix
# because the source dict has no such entry.
#
# The (?:i18n\.)? prefix is intentional: t() is called both as t('...') and
# occasionally as i18n.t('...') from a subset of files, so match both spellings.
T_CALL_KEY_RE = re.compile(
    r"""(?<![A-Za-z0-9_.])(?:i18n\.)?t\(\s*['"]([A-Za-z][A-Za-z0-9_.]*)['"]\s*(?=[,)])""",
)

# Matches a DYNAMIC t() call whose first argument is a string literal
# CONCATENATED with something:  ``t('prefix.' + x)`` — captures the
# constant prefix (which ends in ``.``). Boot-pack completion for these keys
# is: enumerate every key in the source dict starting with the captured
# prefix and add it to the boot subset. That is the honest treatment — a
# static scan cannot know the runtime suffix.
T_CALL_DYNAMIC_PREFIX_RE = re.compile(
    r"""(?<![A-Za-z0-9_.])(?:i18n\.)?t\(\s*['"]([A-Za-z][A-Za-z0-9_.]*\.)['"]\s*\+""",
)


def _iter_html_keys(html_path: str) -> Iterable[str]:
    with open(html_path, encoding='utf-8') as f:
        src = f.read()
    for m in HTML_ATTR_KEY_RE.finditer(src):
        yield m.group(1)


def _iter_js_keys(js_path: str) -> Iterable[str]:
    """Extract t('key') / t("key") literals from a JS source file.

    Comments containing the pattern are matched too — that is fine, because
    over-inclusion is safe (a key that is only in a comment would be a
    harmless boot-pack row). Under-inclusion is the failure mode we care
    about: dynamic t() calls, which are routed through
    :func:`_iter_js_dynamic_prefixes` instead.
    """
    try:
        with open(js_path, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        logger.debug('[bootKeys] unreadable %s: %s', js_path, e)
        return
    for m in T_CALL_KEY_RE.finditer(src):
        yield m.group(1)


def _iter_js_dynamic_prefixes(js_path: str) -> Iterable[str]:
    """Extract dynamic t('prefix.' + …) call PREFIXES from a JS source file.

    Every match's captured group is a static string ending in ``.`` that the
    runtime concatenates with an unknown suffix to form the real key. The
    only sound completion is "every source-dict key that starts with this
    prefix is boot-critical" — done by :func:`expand_dynamic_prefixes`.
    """
    try:
        with open(js_path, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        logger.debug('[bootKeys] unreadable %s: %s', js_path, e)
        return
    for m in T_CALL_DYNAMIC_PREFIX_RE.finditer(src):
        yield m.group(1)


def expand_dynamic_prefixes(prefixes: Iterable[str],
                            source_keys: Iterable[str]) -> list[str]:
    """Return every key in ``source_keys`` matching any of ``prefixes``.

    A dynamic ``t('prefix.' + x)`` call may resolve to ANY key starting with
    ``prefix.``; the boot pack has to carry the full set or first paint
    can render a raw key string. ``source_keys`` is typically
    ``extract_dictionary().keys()`` — the ground truth of what exists.

    Prefixes with NO matching keys in the source dict are silently omitted;
    the caller can log them separately if needed (the runtime would fall
    through to the tripwire on such calls anyway).
    """
    prefixes = tuple(prefixes)
    if not prefixes:
        return []
    out: set[str] = set()
    for k in source_keys:
        for p in prefixes:
            if k.startswith(p):
                out.add(k)
                break
    return sorted(out)


def discover_boot_keys(repo_root: str,
                       source_keys: Iterable[str] | None = None
                       ) -> dict[str, list[str]]:
    """Return the boot-critical key breakdown for the whole app.

    Sources scanned:
      * ``index.html`` — all ``data-i18n*`` attributes;
      * every ``.js`` file listed in ``lib.js_bundler._BUNDLE_FILES`` —
        literal-string ``t('...')`` calls AND dynamic ``t('prefix.' + x)``
        call PREFIXES (each prefix expands via ``source_keys`` when given).

    Args:
        repo_root: Absolute path of the repo root (contains index.html).
        source_keys: Optional iterable of every key in the source ``_i18n``
            dictionary (typically ``extract_dictionary().keys()``). When
            given, dynamic prefixes discovered in the core bundle expand to
            every matching key in this set — the boot pack THEN carries the
            full namespace for a ``t('prefix.' + x)`` call. When omitted
            (fast unit-test path), the ``dynamic_prefixes`` list is
            returned in the breakdown and the caller can expand later.

    Returns:
        ``{'html': [...], 'js': [...], 'dynamic_prefixes': [...],
           'union': [...]}`` — each list sorted, deduped; the union is what
        the boot-pack should carry (already includes the dynamic expansion
        when ``source_keys`` was provided).
    """
    from lib import js_bundler
    html_keys = sorted({
        k for k in _iter_html_keys(os.path.join(repo_root, 'index.html'))
    })
    js_dir = os.path.join(repo_root, 'static', 'js')
    js_keys: set[str] = set()
    dynamic_prefixes: set[str] = set()
    for name in js_bundler._BUNDLE_FILES:
        # i18n.js is the DEFINITION of t() — its calls are for its own
        # implementation (fallback, tripwire), which live in the pack itself,
        # not in the dictionary. Skip to avoid polluting the boot key set
        # with implementation-detail strings.
        if name == 'i18n.js':
            continue
        p = os.path.join(js_dir, name)
        for k in _iter_js_keys(p):
            js_keys.add(k)
        for pref in _iter_js_dynamic_prefixes(p):
            dynamic_prefixes.add(pref)

    expanded: list[str] = []
    if source_keys is not None:
        expanded = expand_dynamic_prefixes(dynamic_prefixes, source_keys)

    union = sorted(set(html_keys) | js_keys | set(expanded))
    return {
        'html': html_keys,
        'js': sorted(js_keys),
        'dynamic_prefixes': sorted(dynamic_prefixes),
        'union': union,
    }


def discover_boot_keys_from_bundle_manifest() -> dict[str, list[str]]:
    """Convenience wrapper using the repo root derived from THIS file."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return discover_boot_keys(root)
