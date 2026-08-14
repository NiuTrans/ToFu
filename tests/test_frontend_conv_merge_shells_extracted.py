"""Wire-parity guards for pt_3879f00e sub-part 2 slice 7 — extract the
contiguous ``_serverConvCount`` + ``mergeServerConvShells`` pair from
static/js/core/conversations.js into a dedicated leaf
static/js/core/conv_merge_shells.js.

Both helpers are the id-keyed shell-merge path invoked by three call
sites across the bundle:

  * ``loadConversationsFromServer`` (inside conversations.js itself,
    2 remaining call sites of ``_serverConvCount``)
  * ``folders.js`` — ``loadFolderMembers`` calls ``mergeServerConvShells``
  * ``ui/conversation_list.js`` — infinite-scroll pagination calls
    ``mergeServerConvShells``

Two existing behavioural tests drive the extracted body under node:

  * ``test_frontend_folder_members_load.py`` surgically extracts the
    contiguous ``_serverConvCount`` + ``mergeServerConvShells`` region.
    Slice 7 must re-point that extract at the leaf.
  * ``test_frontend_sidebar_shell_count_keys.py`` drives
    ``loadConversationsFromServer`` under node; because that function
    still calls ``_serverConvCount`` inline, the harness must eval
    the new leaf in addition to conversations.js.

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
LEAF_JS = pathlib.Path(runtime_section_path('core/conv_merge_shells.js'))
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. leaf module exists and defines BOTH helpers at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_both_helpers_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted '
        '_serverConvCount + mergeServerConvShells pair from conversations.js')
    src = LEAF_JS.read_text()
    import re
    for name in ('_serverConvCount', 'mergeServerConvShells'):
        m = re.search(
            r'^function\s+' + re.escape(name) + r'\s*\(',
            src, re.MULTILINE)
        assert m, (
            f'{name} must be a top-level `function` in the leaf so bundle-'
            'concat exposes it via the shared window scope')


def test_leaf_defines_serverConvCount_before_mergeServerConvShells():
    """Slice 7 preserves the SOURCE ORDER of the two functions —
    ``test_frontend_folder_members_load.py`` extracts them CONTIGUOUSLY
    (grabs from ``_serverConvCount`` start to end of
    ``mergeServerConvShells``) and would break if the pair were split
    or reordered."""
    src = LEAF_JS.read_text()
    idx_a = src.index('function _serverConvCount(')
    idx_b = src.index('function mergeServerConvShells(')
    assert idx_a < idx_b, (
        '_serverConvCount must be defined BEFORE mergeServerConvShells '
        'in the leaf so folder-members-load can contiguously extract them')


def test_leaf_carries_pivotal_body_lines():
    """The extracted bodies must preserve the load-bearing behaviour —
    NEUTER-detection for a stealth stub."""
    src = LEAF_JS.read_text()
    # _serverConvCount: 3-key coalescing (messageCount || msgCount || msg_count)
    for tok in ('messageCount', 'msgCount', 'msg_count'):
        assert tok in src, f'_serverConvCount must coalesce {tok!r}'
    # mergeServerConvShells: id-keyed map + never-overwrite discipline
    assert 'const localMap = new Map' in src, (
        'mergeServerConvShells must build an id-keyed Map of local convs')
    assert '_applySettingsToConv' in src, (
        'mergeServerConvShells must call _applySettingsToConv on new shells')
    assert '_needsLoad' in src, (
        'mergeServerConvShells must set _needsLoad on new shells')
    # NEW shells return counter: `added++` and function returns count
    assert 'added++' in src, (
        'mergeServerConvShells must increment `added` for each new shell')
    assert 'return added' in src, (
        'mergeServerConvShells must return the count of new shells added')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares either function inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_serverConvCount_inline():
    src = CONV_JS.read_text()
    import re
    m = re.search(
        r'^function\s+_serverConvCount\s*\(', src, re.MULTILINE)
    assert m is None, (
        '_serverConvCount must live in core/conv_merge_shells.js, not '
        'inline in conversations.js')


def test_conversations_js_no_longer_declares_mergeServerConvShells_inline():
    src = CONV_JS.read_text()
    import re
    m = re.search(
        r'^function\s+mergeServerConvShells\s*\(', src, re.MULTILINE)
    assert m is None, (
        'mergeServerConvShells must live in core/conv_merge_shells.js, not '
        'inline in conversations.js')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so
    ``loadConversationsFromServer``'s bare-name calls to
    ``_serverConvCount`` resolve via bundle-level window scope, and
    so ``folders.js`` / ``conversation_list.js`` (both loaded LATER
    in the manifest via the ui/ block) find ``mergeServerConvShells``."""
    owners = runtime_section_names()
    idx_leaf = owners.index('core/conv_merge_shells.js')
    idx_conv = owners.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_merge_shells.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')


# ---------------------------------------------------------------------------
# 4. The page shell contains no raw app-script inventory
# ---------------------------------------------------------------------------
def test_index_html_has_no_raw_script_tag_for_leaf():
    src = INDEX_HTML.read_text()
    assert 'static/js/core/conv_merge_shells.js' not in src
    assert '<!-- TOFU_APP_ASSETS -->' in src
