"""Wire-parity guards for pt_3879f00e sub-part 2 slice 6 — extract
hydrateSidebarFromCache from static/js/core/conversations.js into its
own leaf module static/js/core/conv_hydrate_cache.js.

hydrateSidebarFromCache is the cold-boot cache-first sidebar paint
(reads the IndexedDB ConvCache mirror + opened-conv metas, seeds
`conversations` with shells before any server round-trip). It has ONE
call site — bootstrap in static/js/main.js — plus behavioural tests
(``test_frontend_cache_hydrate_boot.py`` and
``test_frontend_sidebar_fulllist_hydrate.py``) that drive the extracted
function body under node.

Failing-first: written BEFORE the extraction; each guard turns RED
until the leaf lands and conversations.js delegates.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from _runtime_sections import runtime_section_names, runtime_section_path  # noqa: E402

CONV_JS = pathlib.Path(runtime_section_path('core/conversations.js'))
LEAF_JS = pathlib.Path(runtime_section_path('core/conv_hydrate_cache.js'))
INDEX_HTML = ROOT / 'index.html'


def _extract_fn(src: str, name: str) -> str:
    import re
    m = re.search(r'async\s+function\s+' + re.escape(name) + r'\s*\(', src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


# ---------------------------------------------------------------------------
# 1. leaf module exists and defines the function at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_hydrator_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted '
        'hydrateSidebarFromCache from conversations.js')
    src = LEAF_JS.read_text()
    # top-level async function OR var/const bound to one (top-level only).
    import re
    has_async_def = re.search(
        r'^async\s+function\s+hydrateSidebarFromCache\s*\(',
        src, re.MULTILINE)
    assert has_async_def, (
        'hydrateSidebarFromCache must be a top-level `async function` in '
        'the leaf so bundle-concat exposes it in the shared window scope')


def test_leaf_carries_pivotal_body_lines():
    """The extracted body must preserve the load-bearing behavior chunks —
    NEUTER-detection for a stealth stub."""
    src = LEAF_JS.read_text()
    # Full-list mirror precedence: getSidebarList tried BEFORE getAllMeta
    # in the CODE, not just the docstring — key on the actual `await
    # ConvCache.<...>` call sites so a comment mentioning getAllMeta
    # first doesn't fool the guard.
    idx_full = src.find('await ConvCache.getSidebarList')
    idx_meta = src.find('await ConvCache.getAllMeta')
    assert idx_full != -1, 'leaf must call await ConvCache.getSidebarList()'
    assert idx_meta != -1, 'leaf must call await ConvCache.getAllMeta() as fallback'
    assert idx_full < idx_meta, (
        'the full-list mirror must be preferred (called BEFORE getAllMeta) '
        '— reordering would silently regress the cold-boot full-sidebar paint')
    # CAS-rev adoption for anti-resurrect
    assert '_serverRev' in src, (
        'must adopt m.rev as _serverRev for id-keyed merge / CAS base')
    # Shell markers
    for tok in ('_needsLoad', '_fromCache', '_pendingSyncAt'):
        assert tok in src, f'leaf must preserve {tok!r} shell wiring'
    # Poller kick when any hydrated shell carries a stranded pending tail
    assert '_startPendingSyncPolling' in src
    assert '_flushPendingSyncs' in src


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the function inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_hydrator_inline():
    """After extraction the async function definition must be GONE from
    conversations.js — call sites keep bare `hydrateSidebarFromCache()`
    calls resolved via bundle-level window scope."""
    src = CONV_JS.read_text()
    import re
    # No top-level async function definition.
    top_def = re.search(
        r'^async\s+function\s+hydrateSidebarFromCache\s*\(',
        src, re.MULTILINE)
    assert top_def is None, (
        'hydrateSidebarFromCache must live in core/conv_hydrate_cache.js, '
        'not inline in conversations.js')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so main.js's
    bootstrap call to hydrateSidebarFromCache() resolves via the shared
    bundle scope."""
    owners = runtime_section_names()
    idx_leaf = owners.index('core/conv_hydrate_cache.js')
    idx_conv = owners.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_hydrate_cache.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv}) so bundle scope resolves '
        'hydrateSidebarFromCache before conversations.js\'s own code refers '
        'to helpers it needs (in fact conversations.js exports many '
        'helpers hydrator needs — leaf-BEFORE is the safe order)')


# ---------------------------------------------------------------------------
# 4. The page shell contains no raw app-script inventory
# ---------------------------------------------------------------------------
def test_index_html_has_no_raw_script_tag_for_leaf():
    src = INDEX_HTML.read_text()
    assert 'static/js/core/conv_hydrate_cache.js' not in src
    assert '<!-- TOFU_APP_ASSETS -->' in src


# ---------------------------------------------------------------------------
# 5. main.js still calls hydrateSidebarFromCache at bootstrap
# ---------------------------------------------------------------------------
def test_main_js_still_bootstraps_hydrator():
    """Callsite MUST survive the extraction — bare-name call resolved via
    bundle-level window scope (leaf loaded BEFORE main.js because leaf
    lives in core/, main.js in the root)."""
    main_js = pathlib.Path(runtime_section_path('main.js'))
    src = main_js.read_text()
    assert 'hydrateSidebarFromCache()' in src, (
        'main.js must still bootstrap hydrateSidebarFromCache() — '
        'that call is the cache-first sidebar paint entry point')
