"""JS bundler — concatenate app scripts into a single bundle at startup.

Eliminates the HTTP/1.1 waterfall problem where browsers limit to 6 concurrent
connections per host, causing 18 JS files to download in 3-4 serial waves.
With the bundle, the browser fetches 1 file (gzip ~250KB) in a single request.

The bundle is rebuilt at startup and whenever any source file changes.
No npm/webpack/build step required — pure Python concatenation + a
conservative, dependency-free minify pass (``_minify_js``, see below).

When a ``node`` toolchain WITH ``esbuild`` happens to be present, an OPTIONAL
stronger minify pass (``_esbuild_minify``) is layered on top of the concatenated
bundle — it mangles function-local identifiers and shrinks syntax for a further
~12% gzip / ~19% raw reduction. It is strictly best-effort and fail-open: absent
or broken esbuild → the dependency-free ``_minify_js`` output is served
unchanged, so a bare ``python server.py`` (e.g. a Mac with no node) is byte-for
-byte identical to before. See ``_esbuild_minify`` for the safety argument (why
script-mode esbuild never renames the top-level globals index.html's inline
``onclick=`` handlers depend on, and never tree-shakes a top-level definition).
"""
import ast
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

try:
    import fcntl  # POSIX advisory locks — serialize concurrent bundle builds
except ImportError:  # pragma: no cover - non-POSIX (Windows); temp+rename still safe
    fcntl = None

from lib.log import get_logger

logger = get_logger(__name__)


def _minify_js(src: str) -> str:
    r"""Conservatively strip comments + non-semantic whitespace from JS.

    A single forward char-scan that tracks lexical state — string literals
    (``'`` / ``"``), template literals (`` ` `` incl. ``${ }`` interpolation
    with its own brace depth), and regex literals — so a ``//`` or ``/* */``
    that lives INSIDE one of those is never mistaken for a comment. Outside
    those contexts it drops line (``//``) and block (``/* */``) comments.

    LINE-PRESERVING by design: newlines are kept (only blank lines, leading
    indentation, and trailing whitespace are dropped). Keeping ``\n`` means the
    transform can only ever DELETE comment bytes and horizontal whitespace —
    never move code across a line boundary — so it introduces NO
    Automatic-Semicolon-Insertion hazard and can never fuse two tokens. This is
    the same fail-safe philosophy as ``lib/css_bundler._minify_css``; the
    regex-vs-divide call is deliberately conservative (when unsure, treat ``/``
    as division and leave the bytes intact) so a misjudgement can only leave a
    comment un-stripped, never corrupt real code.

    Returns the source UNCHANGED on any unexpected condition (never raises).
    ``build_bundle`` still runs the ``node --check`` gate on the concatenated
    result, so even a latent minifier bug degrades to "serve the un-minified
    fallback", never a white screen.
    """
    out = []
    i = 0
    n = len(src)
    quote = ''            # inside '...' / "..." → the delimiter char
    in_template = 0       # template-literal nesting depth
    template_expr = []    # per open template: brace depth inside ${ } (0 = raw text)
    in_regex = False
    in_line_comment = False
    in_block_comment = False
    last_sig = ''         # last emitted significant char (regex-vs-divide hint)

    def _regex_allowed(prev):
        # A '/' begins a regex (not division) when the previous significant
        # token is one after which a VALUE is expected. Conservative: unknown
        # → treat as divide (safe — leaves bytes intact).
        if prev == '':
            return True
        return prev in '(,=:[!&|?{};+-*%^~<>'

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''

        if in_line_comment:
            if c == '\n':
                in_line_comment = False
                out.append(c)
            i += 1
            continue
        if in_block_comment:
            if c == '*' and nxt == '/':
                in_block_comment = False
                i += 2
            else:
                if c == '\n':
                    out.append('\n')   # keep line count stable
                i += 1
            continue
        if quote:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if c == quote:
                quote = ''
                last_sig = c
            i += 1
            continue
        if in_regex:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if c == '/':
                in_regex = False
                last_sig = c
            i += 1
            continue
        if in_template:
            depth = template_expr[-1]
            if depth == 0:
                # Raw template text — preserve verbatim.
                out.append(c)
                if c == '\\' and i + 1 < n:
                    out.append(nxt)
                    i += 2
                    continue
                if c == '`':
                    in_template -= 1
                    template_expr.pop()
                    last_sig = c
                    i += 1
                    continue
                if c == '$' and nxt == '{':
                    out.append(nxt)
                    template_expr[-1] = 1
                    i += 2
                    continue
                i += 1
                continue
            else:
                # Inside ${ ... } — ordinary JS. Track brace depth + nested
                # templates, then fall through to shared token handling.
                if c == '{':
                    template_expr[-1] += 1
                    out.append(c)
                    last_sig = c
                    i += 1
                    continue
                if c == '}':
                    template_expr[-1] -= 1
                    out.append(c)
                    last_sig = c
                    i += 1
                    continue
                if c == '`':
                    in_template += 1
                    template_expr.append(0)
                    out.append(c)
                    last_sig = c
                    i += 1
                    continue
                # else: shared handling below

        # ── Not inside string/regex/comment (or inside a template ${} expr) ──
        if c == '/' and nxt == '/':
            in_line_comment = True
            i += 2
            continue
        if c == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue
        if c in '"\'':
            quote = c
            out.append(c)
            last_sig = c
            i += 1
            continue
        if c == '`':
            in_template += 1
            template_expr.append(0)
            out.append(c)
            last_sig = c
            i += 1
            continue
        if c == '/':
            if _regex_allowed(last_sig):
                in_regex = True
                out.append(c)
                i += 1
                continue
            out.append(c)
            last_sig = c
            i += 1
            continue
        out.append(c)
        if not c.isspace():
            last_sig = c
        i += 1

    stripped = ''.join(out)

    # Per-line cleanup: drop blank lines + leading/trailing whitespace. Pure
    # per-line — cannot fuse tokens across a line boundary.
    lines = [s for s in (ln.strip() for ln in stripped.split('\n')) if s]
    return '\n'.join(lines)

# Git merge-conflict markers. An interrupted / conflicted self-update
# `git pull` can leave one of these embedded in a JS source file; glued
# into the bundle it produces the classic "Uncaught SyntaxError:
# Unexpected token '<'/'='/'-'" that white-screens the whole app.
_CONFLICT_MARKERS = ('<<<<<<< ', '=======\n', '>>>>>>> ')


def _scan_source_corruption(name, content):
    """Detect corruption classes that would break the concatenated bundle.

    These are the failure modes that ship a syntactically broken bundle from
    an otherwise-healthy install: a git merge-conflict marker left by an
    interrupted `git pull`, or a NUL byte from a truncated / partial file
    write. A file flagged here is SKIPPED (not glued into the bundle), so one
    corrupt file degrades to "that module is absent" instead of
    "the entire app fails to boot".

    Args:
        name: Source file name (for logging).
        content: The file's text content.

    Returns:
        A human-readable reason string if corrupt, else None.
    """
    if '\x00' in content:
        return 'contains NUL byte (truncated / partial write)'
    # A bare conflict marker at line start is unambiguous corruption. Check
    # line-anchored so a legitimate string like ">>>>>>> " inside code is not
    # false-flagged unless it actually begins a line.
    for line in content.splitlines():
        if line.startswith('<<<<<<< ') or line.startswith('>>>>>>> '):
            return 'git merge-conflict marker (%.20s...)' % line
        if line == '=======':
            return 'git merge-conflict marker (=======)'
    return None


def _node_syntax_ok(bundle_path):
    """Best-effort syntax gate on the final bundle using `node --check`.

    Returns (ok, detail). If node is not installed (the common case on a
    fresh install), returns (True, '') — we do NOT fail the bundle just
    because we cannot validate it; the per-source corruption scan is the
    dependency-free primary defense.
    """
    node = shutil.which('node')
    if not node:
        return True, ''
    try:
        proc = subprocess.run(
            [node, '--check', bundle_path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.debug('[Bundle] node --check unavailable: %s', e)
        return True, ''
    if proc.returncode == 0:
        return True, ''
    detail = (proc.stderr or proc.stdout or '').strip()
    return False, detail


def _find_syntax_broken_sources(names):
    """Per-file ``node --check`` over the given bundle-relative source files.

    This is the ATTRIBUTION pass for a failure class ``_scan_source_corruption``
    structurally cannot see: a file that is scanner-clean (no conflict markers,
    no NUL bytes) yet does not parse — the 2026-08-04 sidebar incident, where an
    interrupted agent edit left a duplicated ``function renderMessage(`` line in
    chat_render.js (brace imbalance → EOF mid-construct). It runs ONLY after the
    whole-bundle gate failed, so healthy builds pay nothing for it.

    Best-effort, mirroring ``_node_syntax_ok``: no node → ``[]`` (the gate that
    would have triggered this pass is equally vacuous then). Returns the ordered
    list of names that fail to parse; ``[]`` means the failure is a cross-file
    gluing bug, not attributable to any single source.
    """
    node = shutil.which('node')
    if not node:
        return []
    broken = []
    for name in names:
        path = os.path.join(JS_DIR, name)
        try:
            proc = subprocess.run(
                [node, '--check', path],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            logger.debug('[Bundle] node --check unavailable for %s: %s', name, e)
            continue
        if proc.returncode != 0:
            broken.append(name)
    return broken


def _resolve_esbuild():
    """Locate an ``esbuild`` binary, preferring the project's local install.

    Checks ``node_modules/.bin/esbuild`` first (populated by ``npm ci`` /
    ``npm install`` per package.json), then falls back to ``esbuild`` on PATH.
    Deliberately never uses ``npx`` — an unresolved ``npx esbuild`` would try to
    DOWNLOAD the package at server-startup time, which is exactly the network
    surprise a self-hosted launcher must not incur. Returns the path or None.
    """
    local = os.path.join(BASE_DIR, 'node_modules', '.bin',
                         'esbuild.cmd' if os.name == 'nt' else 'esbuild')
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which('esbuild')


def _esbuild_minify(src):
    """Optional stronger minify via esbuild — best-effort, fail-open.

    Mirrors the ``_node_syntax_ok`` philosophy exactly: when esbuild is present
    AND its output passes a ``node --check`` gate, return the esbuild-minified
    string; otherwise return None so the caller keeps the dependency-free
    ``_minify_js`` output. A bare install with no node/esbuild is therefore
    byte-identical to before.

    SAFETY (why this can't break the app): the bundle has NO ``import`` /
    ``export`` (verified) so esbuild processes it in SCRIPT mode, where every
    top-level ``var`` / ``function`` / ``const`` / ``let`` is an observable
    global and is NEVER renamed — so the names index.html's inline ``onclick=``
    handlers rely on (``loadConversation``, ``closeSettings``, …) survive intact.
    Only function-LOCAL identifiers are mangled, and those are private. No
    bundling/tree-shaking is requested, so no top-level definition is dropped.
    The trade-off vs ``_minify_js``: esbuild collapses everything to one line, so
    the per-file ``// ═══ name ═══`` debug headers are lost (acceptable for a
    minified artifact); the line-preserving ``_minify_js`` fallback keeps them.
    """
    esb = _resolve_esbuild()
    if not esb:
        return None
    try:
        proc = subprocess.run(
            [esb, '--minify', '--loader=js'],
            input=src, capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        logger.debug('[Bundle] esbuild unavailable: %s', e)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        logger.warning('[Bundle] esbuild minify failed (exit=%s), keeping _minify_js: %.300s',
                       proc.returncode, (proc.stderr or '').strip())
        return None
    out = proc.stdout
    # Validate esbuild's own output before trusting it — a latent esbuild bug
    # degrades to the _minify_js bundle, never to a broken served file.
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as tf:
            tf.write(out)
            tmp_path = tf.name
    except OSError as e:
        logger.debug('[Bundle] esbuild temp write failed: %s', e)
        return None
    try:
        ok, detail = _node_syntax_ok(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError as e:
            logger.debug('[Bundle] could not remove esbuild temp %s: %s', tmp_path, e)
    if not ok:
        logger.warning('[Bundle] esbuild output failed syntax check, keeping '
                       '_minify_js: %.300s', detail)
        return None
    return out

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE_DIR, 'static', 'js')

# Built (content-hashed) bundle outputs ONLY: bundle-<8hex>.js / feature-<8hex>.js.
# Deliberately anchored to the 8-hex hash so a SOURCE file that merely starts
# with 'feature-' (e.g. feature-loader.js) is NOT matched by the stale-bundle
# cleaner. Cf. the corruption-guard skill: a runtime-assembled artifact must
# never delete its own source.
# Built artifacts: core/deferred bundles AND the single-language i18n packs
# emitted by lib/i18n_packs.py (Epic-E sub-part 1 slice 2). Kept in lockstep
# with tests/test_bundle_manifest_parity.py::_BUILT_BUNDLE_RE — the parity
# test's disk-orphan edge treats anything NOT matching this as a source file.
_BUILT_BUNDLE_RE = re.compile(
    r'^(?:(?:bundle|feature)-[0-9a-f]{8}|i18n-(?:zh|en)-[0-9a-f]{8})\.js$')

# The i18n-pack subset of _BUILT_BUNDLE_RE, CAPTURING the language — a stale
# pack can only ever heal to the current pack of the same language, so the
# resolver needs the language, not just "is a pack".
_PACK_LANG_RE = re.compile(r'^i18n-(zh|en)-[0-9a-f]{8}\.js$')

# How long a built artifact is immune to another process's cleanup (seconds).
# Covers the longest realistic serve overlap: a browser holding an old
# index.html (bfcache / long-lived tab / caching proxy) plus an i18n pack
# fetch. 2h by default so an hourly deploy cadence can never 404 an in-flight
# page; 0 restores the pre-grace behaviour (keep-set only).
_BUILT_ARTIFACT_GRACE_S = int(
    os.environ.get('TOFU_BUNDLE_ARTIFACT_GRACE_S', '7200'))

# ── Load order MUST match index.html (dependencies flow top → bottom) ──
_BUNDLE_FILES = [
    'i18n.js',         # MUST be first — t() is used by all other modules
    'core/icons.js',   # Icon()/IconDot() SVG registry — used by many modules; load early
    'idb-cache.js',
    'core.js',
    # Browser console → logs/frontend.log relay (pt_cfdfd30c8699407b).
    # Native fetch/localStorage only; loads right after the core.js shell so
    # every later module's console lines are captured. apiUrl resolved at
    # call time.
    'core/client_log_relay.js',
    # ── core/ subpackage (split 2026-05-28 from monolithic core.js) ──
    # 11 files extracted from core.js (3877 LOC). The slim core.js shell
    # above declares foundational module state (apiUrl, BASE_PATH, TAB_ID,
    # conversations, _folders, config, serverModel, getActiveConv,
    # generateId, _ensureMsgId, scrollToBottom, ...) BEFORE these load,
    # so each extracted file can reference them at module-load time.
    # Symbols share window scope; no exports/imports needed.
    # See `.tofu/memories/frontend-core-decomposition.md` for rationale.
    'core/folders.js',
    'core/cost.js',
    'core/debug_panel.js',
    'core/request_inspector.js',  # after debug_panel.js (calls showMessagesInDebug at runtime)
    'core/escape_html.js',
    'core/safe_html.js',   # after escape_html.js (uses escapeHtml), before ui/ consumers
    'core/error_envelope.js',
    # Shared bytes->human size formatter (formatFileSize) — de-dupes
    # image-gen.js _formatFileSize + skills.js _skillsFmtSize. Load before them.
    'core/format_size.js',
    # Capability taxonomy — window.isChatModel + CHAT_EXCLUDED_CAPS. Consumed
    # by main_toolbar_ui.js / paper/report.js / settings/visibility_defaults.js
    # / settings/template_actions.js so all of them load after this. Only
    # touches window at load; server-config payload is applied at runtime.
    # See lib/model_info/capability_taxonomy.py for the SSOT.
    'core/model_caps.js',
    # THE brand mascot URL (2026-07-29). Owns the cache-bust token (icons ship
    # with max-age=86400, so a bare path made a logo change invisible for 24h —
    # that is how a rollback once looked like it "didn't happen") AND the
    # runtime try-on skin, so a candidate logo can be WORN in-product before
    # anyone commits to shipping it. Consumed by ui/chat_render.js,
    # main/main_conv_lifecycle.js, settings/core_panel.js and main.js boot —
    # all load after this. Leaf module (window only at load).
    'core/brand_logo.js',
    # THE model-availability judgment (2026-07-28, pt_464f2baf). A logical
    # model is served by a POOL of (wire id × key) slots and the dispatcher
    # rotates over all of them, so the ONLY correct rule is "any usable slot
    # ⇒ usable model". The rule it replaces summed the pool's requests/errors
    # and divided, which paints a model with 8 dead slots + 1 healthy one as
    # ~11% (red) even though the dispatcher serves it fine — one redeployed
    # upstream made whole cards permanently red. Same function folds BOTH
    # axes (runtime dispatch-health rows + active probe cells) so the passive
    # and active signals can never disagree. Consumed by
    # settings/key_stats.js; leaf module (window only at load), so its
    # ordering requirement is just "before its consumers".
    'core/model_health.js',
    # THE model-grouping rule (2026-07-28, pt_464f2baf). The toolbar picker
    # grouped by provider_id — so moving Claude to the Anthropic-native face
    # (same gateway, same keys, just a different wire protocol) split the
    # dropdown into two "Meituan" sections, leaking a backend detail to the
    # user. The settings preset tab grouped by brand and never split. Two
    # lists of the SAME data must not disagree about grouping; this is the
    # single key/label both use. Also owns the brand-name table (previously
    # duplicated verbatim twice in visibility_defaults.js). Consumed by
    # main_toolbar_ui.js + settings/visibility_defaults.js (both load after).
    'core/model_group.js',
    # pt_679d064f68ac4dd6 (2026-07-25) — boot-time tenant identity probe.
    # Defines initCurrentUserId(), which main.js awaits (as a promise chain)
    # BEFORE wiring the push subscribers so the four multi-user gates
    # (conv_state_reducer::_frameIsOurs, cross_tab_sync::_onConvNotifyPush /
    # _onFoldersChangedPush, conv_sync_push::_onConvSyncPush) have an
    # identity to compare frame.userId against. Leaf module (touches only
    # window + Api at CALL time, never at load), so its only ordering
    # requirement is "before main.js" — which every entry here satisfies.
    'core/current_user.js',
    # Shared OS-file .zip drag/drop wiring (attachZipDropZone) — de-dupes the
    # memory + skills install dropzones. Load before memory_skill_install.js
    # and skills_install.js (both call it at runtime; core loads first anyway).
    'core/zip_drop_zone.js',
    # Bounded-concurrency task runner (runWithConcurrency) — caps the
    # reconnect "thundering herd" (all N conv reattach/probe calls firing at
    # once on wake). Leaf module (window only); load before its consumers
    # core/cross_tab_sync.js + core/health_stream_timer.js.
    # pt_conv_state_ssot P2 (2026-07-24) — pure reducer for server-
    # authoritative conv busy state (applyRunningTaskIdsFrame /
    # applyConvStateSnapshot / computeConvBusy / pickAuthoritativeTaskIdForReconnect).
    # Consumed by ui/conversation_list.js (convIsBusy union read),
    # core/cross_tab_sync.js (notify + conv_state_snapshot frame dispatch),
    # main/main_conv_lifecycle.js (reconnect target picker). Leaf module
    # (window only); MUST load before every consumer.
    # pt_turn_settlement C1 (2026-07-24) — canonical JS port of the
    # turn-settlement verdict (computeTurnSettlement / _tsScanKeptRounds).
    # Consumed by ui/chat_render.js (Continue-button affordance),
    # ui/finish_info.js (interrupt bubble label),
    # main/main_regen_continue.js (Continue executor). Pure leaf module
    # (window only); MUST load before every consumer. Behaviour-locked with
    # lib/conversations/turn_settlement.py via
    # tests/test_frontend_turn_settlement_equivalence.py.
    'core/turn_settlement.js',
    # THE ordered-insert primitive for #chatInner (2026-07-26). Owns the head
    # and tail anchors that step over lazy-window furniture
    # (#_lazyLoadSentinel / #_lazyLoadSentinelBottom), plus the RENDER_CONTRACT
    # Invariant-1 runtime tripwire. Exists because the head-anchor rule first
    # shipped as a CLOSURE inside renderChat, which ConvView could not reach —
    # so the identical bug reappeared at the tail (a sent message rendering
    # BELOW the bottom sentinel). Leaf module (document + console only); MUST
    # load before ui/chat_render.js and conv_view.js.
    # Order pinned by tests/test_frontend_lazy_sentinel_anchor.py.
    'core/chatinner_dom.js',
    # pt_679d064f68ac4dd6 follow-up — the multi-user identity gate's WATCHDOG.
    # Owns the fail-open latch + reporter + a self-owned flush, and depends on
    # NOTHING it watches. MUST load BEFORE core/conv_state_reducer.js: the
    # degrade it reports is "the reducer failed to load", so hosting it inside
    # the reducer (as it was until 2026-07-26) made it structurally unable to
    # fire on its own trigger — latch, reader and the probe timer that ships it
    # all vanished with the predicate. Leaf module (window + Api at CALL time).
    # Order pinned by tests/test_frontend_identity_gate_parity.py.
    'core/identity_gate_tripwire.js',
    'core/conv_state_reducer.js',
    'core/async_pool.js',
    # Settings-column adopter extracted 2026-07-27 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 5):
    # _applySettingsToConv — 8 call sites inside conversations.js AND
    # 1 cross-file call site inside cross_tab_sync.js's
    # _handleConvNotifyPush. Load-order constraint: MUST come before
    # cross_tab_sync (the earlier consumer) — the leaf must precede
    # BOTH consumers so the bare-name call resolves via bundle-level
    # window scope. Pure helper: reads settings, writes onto conv.
    'core/conv_apply_settings.js',
    # core/cross_tab_sync.js — MOVED to _DEFERRED_FILES 2026-07-31 (Epic-E
    # pt_3879f00e sub-part 3 slice A, 53KB out of the render-blocking core).
    # Its core prerequisites (conv_apply_settings / conv_state_reducer /
    # async_pool / current_user) all stay here and therefore always load
    # first; its window-exposed entry point _wireConvSyncPush is stubbed by
    # feature-loader.js (see _DEFERRED_ENTRY_POINTS) so main.js's
    # typeof-guarded boot call still wires the conv-sync push subscription.
    # The 3 hot-path _broadcastToTabs call sites (conv_save.js,
    # main_conv_lifecycle.js ×2) are typeof-guarded at their call sites.
    # Option A relocation (BroadcastChannel listener owned by the module
    # itself) landed earlier — test_frontend_cross_tab_sync_deferrable.py.
    # Pure conversation reducers extracted 2026-07-25 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 1):
    # convAutoTranslate / assistantTailIsPriorTurn /
    # pollWriteWouldClobberSettledTail / convTitleById /
    # convAutoTranslateEffective. Leaf module (window only, no runtime
    # state); load BEFORE core/conversations.js so downstream reads
    # inside its heavier functions still resolve the bare names.
    'core/conv_reducers.js',
    # Local-persistence primitives extracted 2026-07-31 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 13):
    # saveConversations (with the LOAD-BEARING flicker guard against
    # active streams + 2s sidebar-refresh throttle) +
    # syncConversationToServerDebounced (rapid-toggle coalescer) +
    # the private _syncDebounceTimers map. Reads conversations /
    # activeStreams / _convSorter / _broadcastToTabs /
    # renderConversationList / syncConversationToServer at CALL time
    # via bundle-level window scope. Load BEFORE core/conversations.js
    # so its heavier functions can call these bare names at runtime.
    'core/conv_save.js',
    # Pending-sync retry cluster extracted 2026-07-25 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 2):
    # markConvPendingSync / _clearPendingSyncMarkers / convHasPendingSync
    # / _startPendingSyncPolling / _flushPendingSyncs plus the two
    # state variables (_pendingSyncInterval, _PENDING_SYNC_POLL_MS).
    # Reads ConvCache / Api.health / activeStreams / conversations /
    # loadConversationMessages / syncConversationToServer at CALL time
    # via bundle-level window scope. Load BEFORE core/conversations.js
    # so its still-in-file writer (_clearPendingSyncMarkers call inside
    # syncConversationToServer's success branch) resolves.
    'core/pending_sync.js',
    # Persist / freshness / rebase helpers extracted 2026-07-25 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 3):
    # _stripUsageTransient / _trimMsgForPersist /
    # _serverHasSegmentsLocalLacks / _serverHasTranslationLocalLacks /
    # _isErrorOnlyAssistant / _rebaseUnackedTail + the
    # _USAGE_TRANSIENT_KEYS module-level constant. Pure helpers — every
    # dependency is read at CALL time via bundle-level window scope.
    # Load BEFORE core/conversations.js so syncConversationToServer
    # (which calls _trimMsgForPersist and _rebaseUnackedTail) and
    # loadConversationMessages (which calls both freshness signals)
    # still resolve the bare names at runtime.
    'core/conv_persist_helpers.js',
    # Per-conv image base64 hydrator extracted 2026-07-26 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 4):
    # _hydrateImageBase64. Pure helper — fetches base64 for images
    # arriving from DB with base64 stripped (post-restart), stashes a
    # promise on conv._hydratePromise for downstream awaits. Reads
    # apiUrl() at CALL time via bundle-level window scope. Load BEFORE
    # core/conversations.js so its two call sites inside
    # loadConversationMessages still resolve the bare name at runtime.
    'core/conv_image_hydrate.js',
    # Cold-boot cache-first sidebar paint extracted 2026-07-28 from
    # core/conversations.js (pt_3879f00e sub-part 2, slice 6):
    # hydrateSidebarFromCache. Reads ConvCache (getSidebarList /
    # getAllMeta), seeds `conversations` with lightweight shells before
    # any server round-trip so first paint has zero network dependency.
    # Called ONCE from main.js's bootstrap. Load BEFORE
    # core/conversations.js so main.js's bare-name call resolves via
    # bundle-level window scope; the leaf itself calls `_serverConvCount`
    # (still in conversations.js), `_applySettingsToConv`,
    # `_startPendingSyncPolling` / `_flushPendingSyncs`, `_convSorter`,
    # `renderConversationList` — all resolved AT CALL TIME via bundle
    # scope, so the leaf-before-conversations order is safe.
    'core/conv_hydrate_cache.js',
    # ── conv_merge_shells.js: `_serverConvCount` (3-key coalescing) +
    # `mergeServerConvShells` (id-keyed shell merge with never-overwrite
    # discipline) — the pair `folders.js` / `ui/conversation_list.js`
    # / conversations.js's `loadConversationsFromServer` all call.
    # Must load BEFORE conversations.js so the two remaining
    # `_serverConvCount` call sites inside `loadConversationsFromServer`
    # resolve via bundle window scope (pt_3879f00e slice 7). Kept
    # CONTIGUOUS as a single leaf so `test_frontend_folder_members_load`'s
    # source extract (start of `_serverConvCount` → end of
    # `mergeServerConvShells`) still succeeds.
    'core/conv_merge_shells.js',
    # ── conv_rescue_tail.js: `_rescuableLocalTail(localMsgs, serverMsgs)` —
    # pure verdict that answers whether a server reply's shortfall is a
    # legitimate delete (empty rescue → overwrite) or the signature of a
    # lost-race whole-blob write (non-empty rescue → keep local, push back).
    # ONE call site inside `loadConversationMessages` (conversations.js
    # ~L1453). Load BEFORE conversations.js so the surviving call resolves
    # via bundle-level window scope. Pure seam — no DOM, no globals, no
    # state (pt_3879f00e slice 8).
    'core/conv_rescue_tail.js',
    # ── conv_disaster_recovery.js: forceRecoverFromServer /
    # auditConversations / recoverAll — console-invokable last-resort
    # rescue trio. Zero cross-file callers (grep-verified); the three
    # reach each other inside this leaf. Load BEFORE conversations.js so
    # the trio is available on window scope for console use, and AFTER
    # conv_apply_settings.js because forceRecoverFromServer calls
    # `_applySettingsToConv` (pt_3879f00e slice 9).
    'core/conv_disaster_recovery.js',
    # ── conv_verify_visibility.js: _setCacheVerifying (DOM decoration)
    # + _openConvMayHoldOrphanGhost (ghost predicate). Two pure helpers
    # on the cache-verify visibility path. Load BEFORE conversations.js
    # so the 11 bare-name call sites (9 for the visibility toggle, 2
    # for the ghost predicate) resolve via bundle-level window scope
    # (pt_3879f00e slice 10). The bounded self-heal retry cluster
    # remains in conversations.js — it calls into the still-unextracted
    # _verifyActiveConvFromServer path.
    'core/conv_verify_visibility.js',
    # ── conv_verify_retry.js: bounded self-heal retry cluster
    # (_CONV_VERIFY_RETRY_DELAYS_DEFAULT + _convVerifyRetryTimers +
    # _convVerifyRetryDelays + _scheduleConvVerifyRetry).  Load BEFORE
    # conversations.js so its three surviving call sites inside
    # loadConversationMessages resolve via bundle-level window scope
    # (pt_3879f00e slice 11).  The cluster REACHES BACK into
    # conversations.js's `_verifyActiveConvFromServer` at CALL time via
    # bundle-level window scope — the typeof guard makes the reference
    # safe when hot-reloaded out of order.
    'core/conv_verify_retry.js',
    'core/conversations.js',
    # Shared SSE fetch-response read/decode/buffer loop (readSSEStream) —
    # extracted 2026-07-11 from branch.js / paper-reader.js / ui/sse_pipeline.js.
    # Leaf module (touches only response.body.getReader + TextDecoder); load
    # before all three consumers below.
    'core/sse_reader.js',
    # Frontend per-(conv,msg) in-flight translate guard (mirrors the backend
    # lib/translate/inflight.py). Only references window/Map at load; CALLED at
    # runtime by translation.js + message_actions.js. Leaf module.
    'core/translate_guard.js',
    # Canonical msg.translation model + displayContent resolver + bidirectional
    # legacy projection (decoupling step 1). Pure, no DOM. After translate_guard,
    # before translation.js (which will delegate to it in a later increment).
    'core/translation_model.js',
    'core/cache_stats.js',
    'core/markdown.js',
    # core/health_stream_timer.js — MOVED to _DEFERRED_FILES 2026-08-01
    # (Epic-E pt_3879f00e sub-part 3 slice B, 62KB out of the render-
    # blocking core; see docs/EPIC_E_SIZE_LEDGER.md). Every external
    # consumer is typeof-guarded: twUpdate/twStart call sites across the
    # SSE handlers (60 guarded, census 2026-08-01), the 5 compound-line
    # twStop abort-path sites gated in the same commit, streamHealth*
    # (net-latency.js), _probeAllStuckStreamsOnWake (backend_offline_
    # monitor.js), _seedStreamTimerStart (sse_poll_fallback.js). NO
    # feature-loader stub by design: there is no one-time boot wiring to
    # miss (unlike _wireConvSyncPush) — the module self-initializes per
    # stream on first twStart, the idle prefetch lands it ~2s after boot,
    # and the gates degrade to "no elapsed badge until then".
    'core/toast.js',
    'core/dialog.js',  # themed confirm/alert/prompt — after toast (same window scope)
    # Unified API client — owns every backend HTTP call. Depends on
    # apiUrl() from core.js, consumed by every feature module below.
    'api.js',
    'push.js',         # after core.js (uses apiUrl), before ui.js (uses pushSubscribe)
    # Login-wall cookie-capture consent banner. Subscribes pushSubscribe
    # (push.js, directly above) + Api.authSources (api.js) at runtime;
    # everything else is typeof-guarded. Self-inits on DOMContentLoaded.
    'cookie_capture_consent.js',
    # Global backend-liveness watch + prominent offline banner. Subscribes
    # pushOnLatency (push.js) + probes via Api.health (api.js) — both loaded
    # directly above; every other app symbol (showToast / recovery fns) is
    # referenced only inside function bodies and typeof-guarded.
    'core/backend_offline_monitor.js',
    # On-demand loader for the DEFERRED feature bundle (_DEFERRED_FILES). Must
    # be in the CORE bundle (installs the lazy stubs for the deferred entry
    # points before main.js boots). Only references document/debugLog/toast/t
    # at RUNTIME. See lib/js_bundler.py _DEFERRED_FILES + routes/common.py
    # (__FEATURE_BUNDLE_SRC__ injection).
    'feature-loader.js',
    'export-images.js',
    'branch.js',
    # Branch SSE/poll transport + reconnect (extracted from branch.js 2026-07).
    # Shares _branchStreams/_activeBranch/_branchKey with branch.js at runtime
    # (window scope); load order among the two is free — both before main.js.
    'branch_stream.js',
    # Artifacts panel — depends on core.js (renderMarkdown, escapeHtml,
    # apiUrl) but is consumed by ui.js's chip-rendering path, so it
    # MUST come before ui.js.
    'artifacts.js',
    # ── ui/ subpackage (split 2026-05-28 from monolithic ui.js) ──
    # The 11 files below were extracted from ui.js (8917 LOC). Concatenated
    # in load order — symbols share window scope so no exports needed.
    # IMPORTANT: this list MUST stay in dependency order.
    # See `.tofu/memories/ui-decomposition.md` for the rationale.
    # Shared image fullscreen/download helpers (_openImageFullscreen /
    # _downloadGenImage). CORE because chat_render.js + tool_rounds.js call
    # them via inline onclick= on image thumbnails; image-gen.js (their old
    # home) is DEFERRED, so keeping them here guarantees they exist before
    # Image-Gen mode is ever opened. Leaf module (DOM APIs only) — load early.
    'ui/image_fullscreen.js',
    'ui/conversation_list.js',
    'ui/streaming_render.js',
    'ui/chat_render.js',
    # Translate progress/error indicator, extracted from chat_render.js
    # (decoupling step 3). Reads msg.translation via the canonical model; called
    # at runtime by renderMessage, so load order beyond "bundled" is free.
    'ui/translation_indicator.js',
    # Translation → DOM repaint subscriber (decoupling step 4): the 4 relocated
    # painters + emitMessageChanged. Calls renderMessage, so load AFTER
    # chat_render.js; consumed by translation.js, so load BEFORE it.
    'ui/translation_render.js',
    'ui/popups.js',
    # ui/finish_info.js — SLIMMED 2026-08-01 (Epic-E sub-8): the
    # cost-popover family (_buildCostPopover + interaction cluster,
    # ~24KB) moved to DEFERRED ui/finish_info_rich.js and builds LAZILY
    # on first open from the _costCtxByMsg stash (renderFinishInfo no
    # longer embeds pre-built popover HTML per message). The cache-break
    # phrase family stays (the collapsed bar's warn tooltip renders at
    # paint). _toggleCostPopover is a feature-loader entry point.
    'ui/finish_info.js',
    # ui/tool_rounds.js — SLIMMED 2026-08-01 (Epic-E sub-4): the conv-meta
    # rich-render family + Timer Watcher block + ticker moved to DEFERRED
    # ui/tool_rounds_rich.js. The core remainder is boot-critical (first
    # paint restore goes through chat_render.js → renderToolRoundsHTML).
    'ui/tool_rounds.js',
    'ui/message_actions.js',
    'ui/edit_message.js',
    'ui/turn_nav.js',
    # Swarm "Parallel Execution" panel rendering + stuck-panel reconciler,
    # extracted from ui/streaming_ui.js (2026-06-27). Leaf cluster; its
    # builders are called from streaming_ui.js + tool_rounds.js via shared
    # window scope. Load BEFORE streaming_ui.js for clear intent.
    # ui/streaming_swarm_panel.js — MOVED to _DEFERRED_FILES 2026-08-01
    # (Epic-E pt_3879f00e sub-5B, 55KB out of the core). The seven call
    # sites (streaming_ui.js ×5, chat_render.js, tool_rounds.js) are all
    # typeof-guarded in the same commit: absence degrades swarm rounds to
    # _renderUnifiedToolLine's generic line and drops the inbox chips;
    # the panel self-heals on the next SSE event once the idle prefetch
    # lands. Its two tickers only touch DOM the module itself rendered.
    # Suite: tests/test_frontend_swarm_panel_deferred.py.
    'ui/streaming_ui.js',
    # RENDER_CONTRACT Phase 3.5 §7 live stream session — the phase home
    # (convId-keyed runtime slice; replaces streamBufs). Zero deps; load
    # BEFORE the reducer/handlers/pipeline that read+write it.
    'ui/stream_session.js',
    # RENDER_CONTRACT Phase 3 pure stream reducer — the single {content,
    # thinking,toolRounds} projection all four apply paths fold through. Pure
    # (no DOM/globals); load BEFORE the handlers + pipeline that consume it.
    'ui/stream_reducer.js',
    # Stall watch (pt_e0ea29f2): grades heartbeat self-ticks vs real progress
    # and drives the "no unannounced freeze" banner. Leaf module (document/
    # window only); load BEFORE the pipeline (feed seam) + streaming_ui
    # (render seam) that consume it. NOTE: streaming_ui.js sits ABOVE this
    # list — the render seam resolves stallWatchState lazily at runtime via
    # window.*, so load order is intent-only, not a hard dependency.
    'ui/stall_watch.js',
    # Property-only SSE handlers extracted from dispatchSSEEvent (2026-06).
    # Plain hoisted functions taking (ev, ctx-snapshot); the dispatcher in
    # sse_pipeline.js calls them. Load BEFORE sse_pipeline.js for clear intent.
    'ui/sse_handlers_tool.js',
    'ui/sse_handlers_swarm.js',
    'ui/sse_handlers_io.js',
    'ui/sse_handlers_misc.js',
    'ui/sse_handlers_lifecycle.js',
    'ui/sse_pipeline.js',
    # Split out of sse_pipeline.js (2026-06): window-scope siblings with no
    # _trySSE closure capture. Load AFTER sse_pipeline.js (connectToTask
    # calls _pollFallback at runtime; updateSendButton is global).
    'ui/sse_poll_fallback.js',
    # Cross-turn swarm panel updates via /api/push (settles the "N running
    # async" badge after the spawning turn ends). Needs the swarm SSE
    # handlers (sse_handlers_swarm.js) + pushSubscribe (push.js) + renderChat
    # (ui/chat_render.js) — all loaded above. Pure runtime subscriber.
    'ui/swarm_push.js',
    'ui/send_button.js',
    # Stream lifecycle + finalize (showStreamingUIForConv / finishStream /
    # HG-translate helpers), extracted from ui/streaming_ui.js (2026-06-27).
    # Downstream caller of the render path — calls updateStreamingUI /
    # renderMessage / ConvView.finalizeStreaming / _attachAutopilotFollowup /
    # _checkForQueuedTask at RUNTIME, so it MUST load AFTER ui/streaming_ui.js
    # (and after conv_view.js's deps are present at call time).
    'ui/stream_lifecycle.js',
    # Unified chatInner controller — depends on renderMessage,
    # _surgicalTruncateDOM, _convRenderFingerprint, renderChat from
    # the ui/ subpackage plus _ensureMsgId from core.js, so it MUST
    # come after ui/. Consumed by main.js and downstream feature modules.
    'conv_view.js',
    # Feature modules (order-independent, but keep stable for cache)
    'log-clean.js',
    'toolset-apply.js',  # tool-schema latch "apply on next conversation" banner
    'translation.js',
    'upload.js',
    # Attachment + tool-content preview modals (extracted from upload.js 2026-07).
    # Window-scope siblings; called at runtime from onclick / main.js / chat_render.js
    # so load order is free — anywhere before main.js.
    'upload_preview.js',
    # Voice input (speech-to-text) — mic button + MediaRecorder capture.
    # Leaf composer feature: uses Api.audio.* at RUNTIME and its initVoiceInput()
    # is called from main.js's boot, so it only needs to load before main.js.
    'voice.js',
    # image-gen.js — MOVED to _DEFERRED_FILES (lazy-loaded on first entry into
    # Image-Gen mode; ~11KB gzip). No load-time side effect (its only load-time
    # core read is `const _escapeHtmlBasic = escapeHtml`, and core loads first).
    # See feature-loader.js.
    # paper-reader.js — MOVED to _DEFERRED_FILES (lazy-loaded on first Paper
    # Reader open; ~54KB gzip). See feature-loader.js.
    # project.js — SPLIT 2026-08-01 (Epic-E pt_3879f00e sub-7): the STATE
    # subset (conv-project path helpers, _applyProjectData SSE entry,
    # loadProjectStatus boot restore, project-bar render + bar
    # interactions) lives in project_state.js BELOW (core); the PANEL
    # (folder modal, browser, recent, apply-code, drop zones,
    # approval/stdin/HG submit handlers) moved to _DEFERRED_FILES as
    # project.js with 13 feature-loader stubs (openProjectModal and the
    # chat-rendered approval/stdin/HG/undo/apply handlers).
    'project_state.js',
    # memory.js — MOVED to _DEFERRED_FILES 2026-08-01 (Epic-E sub-9,
    # settings-panel six-pack ~123KB; full census note below at timer.js).
    # Skill-package (.zip) drag/drop install (extracted from memory.js 2026-07).
    'memory_skill_install.js',
    # skills.js — MOVED to _DEFERRED_FILES 2026-08-01 (Epic-E sub-9).
    # Skills-tab zip drag/drop + upload transport (extracted from skills.js 2026-07).
    'skills_install.js',
    # preferences.js — MOVED to _DEFERRED_FILES 2026-08-01 (Epic-E sub-9).
    # orchestration.js + task-mode.js — MOVED to _DEFERRED_FILES (lazy-loaded
    # on first Orchestration Studio / Task Mode open; ~48KB gzip combined).
    # task-mode.js reads _ORCH_ICONS from orchestration.js only at RUNTIME
    # (typeof-guarded), and both load together in the feature bundle, so the
    # ordering constraint is preserved within _DEFERRED_FILES. See
    # feature-loader.js.
    # optimizer.js + update.js + timer.js (+ memory.js / skills.js /
    # preferences.js above) — MOVED to _DEFERRED_FILES 2026-08-01 (Epic-E
    # pt_3879f00e sub-9, ~123KB of user-triggered settings panels out of
    # the render-blocking core). Census: every panel opens via topbar
    # badge / settings tab / mobile sheet; ZERO boot-path bare calls —
    # settings/core_panel.js typeof-gates the three tab populates, and
    # with stubs installed the gates DISPATCH (gate+stub composition).
    # Boot-wiring hazards fixed in the same slice: update.js's version
    # check rides _onReady (a deferred module lands AFTER window 'load',
    # so the old listener would never fire); timer/optimizer polling
    # self-arms at bundle land; mobile_panels.js's toggle wraps are
    # re-runnable + identity-tracked and re-wrap on
    # 'tofu:feature-bundle-loaded' (the real toggle clobbers the wrapper
    # installed over the stub); skills_install.js's post-install
    # _populateSkillsTab is typeof-gated. Suite:
    # tests/test_frontend_settings_panels_deferred.py.
    # myday.js + myday_tasks.js — MOVED to _DEFERRED_FILES 2026-08-01
    # (Epic-E pt_3879f00e sub-6, 65KB out of the core). Census: ZERO
    # external JS callers (openDailyReport/closeDailyReport/
    # _mydayTriggerGenerate are referenced only from index.html inline
    # onclicks — they become feature-loader stubs); the _myday state is
    # private to the two modules (they move together, order preserved);
    # both load-time boot blocks branch on document.readyState so they
    # fire directly when the feature bundle lands after DOMContentLoaded
    # (the digest boot is setTimeout(2500) by design — deferral aligns
    # with the module's own 'never competes with first paint' intent).
    # Suite: tests/test_frontend_myday_deferred.py.
    # settings.js is now a slim head (var _serverConfig = null;
    # var _keyStatsCache = {...}; var _keyStatsLoading = false;) followed
    # by a pointer comment. It MUST come BEFORE the settings/ subpackage
    # so the head's `var` initialisers run first — extracted files in
    # settings/ assume those globals exist + start as null/empty.
    'settings.js',
    # ── settings/ subpackage (split 2026-05-28 from monolithic settings.js) ──
    # The 15 files below were extracted from settings.js (4755 LOC).
    # Concatenated in load order; symbols share window scope.
    # See `.tofu/memories/frontend-settings-decomposition.md`.
    # settings/branding.js — STAYS IN CORE 2026-08-02 (Epic-E sub-10
    # boundary fix): it is NOT settings-only. main.js:88 + main.js:349
    # call _modelShortName() BARE on the boot/model-switch path
    # (_applyModelUI) — deferring branding breaks the boot model paint
    # with ReferenceError. Its brand helpers (_detectBrand/_brandSvg/
    # _providerDisplayName/…) are also consumed by the deferred family
    # (visibility_defaults ×12, local_endpoints, template_actions) —
    # deferred→core is the safe direction. finish_info.js's cold
    # finish-bar calls are typeof-gated but hot-path; keeping branding
    # core keeps them always-satisfied.
    'settings/branding.js',
    # ── ENTIRE settings/ subpackage + widgets/chip_input.js — MOVED to
    # _DEFERRED_FILES 2026-08-01 (Epic-E pt_3879f00e sub-10, ~455KB out
    # of the core, the line-closer slice). Census: the whole family
    # renders ONLY inside the user-triggered Settings modal; boot config
    # load (_loadServerConfigAndPopulate, main_toolbar_ui.js:391) calls
    # ZERO settings/ functions (one-way dependency);
    # visibility_defaults.js has no load-time side effects and no boot
    # callers (branding.js DOES — main.js:88/349 bare — so it STAYS);
    # oauth/key_stats have no boot readers; every programmatic
    # openSettings/switchSettingsTab caller is typeof-guarded
    # (onboarding.js:271, main_toolbar_ui.js:382/537,
    # skills_install.js:70) — gate+stub composition (sub-9 pattern).
    # settings.js (the head above) STAYS: var _serverConfig /
    # _keyStatsCache / _keyStatsLoading are read by
    # main_input_handling.js. 4 stubs (openSettings/closeSettings/
    # saveSettings/switchSettingsTab); local_endpoints.js's metrics
    # setInterval self-arms on land. Suite:
    # tests/test_frontend_settings_family_deferred.py.
    # settings/providers/access_matrix.js — MOVED to _DEFERRED_FILES
    # 2026-08-01 (Epic-E pt_3879f00e sub-5A, 55KB out of the core). All
    # three external call sites are already typeof-guarded
    # (core_panel.js / provider_render.js ×2; the matrix toggle button
    # itself only renders when the module is present), _stgMatrixOpen
    # moves with it (read guarded), and its only load-time side effect
    # is a self-contained window-resize IIFE — zero new guards, zero
    # stubs. Suite: tests/test_frontend_access_matrix_deferred.py.
    # relay-admin.js intentionally NOT bundled — it loads only on the
    # standalone /admin page (static/admin.html), not in index.html.
    # ── main/ subpackage (split 2026-05-28 from monolithic main.js) ──
    # The 8 files below were extracted from main.js. They must come BEFORE
    # main.js so the boot IIFE in main.js can reference their symbols.
    # See `.tofu/memories/frontend-main-decomposition.md` for the rationale.
    'main/main_conv_lifecycle.js',
    'main/main_translating_bubble.js',
    'main/main_send_pipeline.js',
    'main/main_regen_continue.js',
    'main/main_toolbar_ui.js',
    'main/main_folders_mobile.js',
    'main/main_input_handling.js',
    'main/main_init_tasks.js',
    # Server→client history_rewrite alignment: applies the backend reconcile
    # verdict push ('conv' channel) in place so no manual refresh is needed.
    # MUST load BEFORE main.js (mirrors core/cross_tab_sync.js): the bundler
    # concatenates every file into ONE shared lexical scope, and main.js's
    # synchronous boot IIFE calls _wireConvHistoryRewritePush(), which reads the
    # module-level `let _convSyncPushChannelWired`. A `let` is hoisted but stays
    # in the TDZ until its own line executes — so if this file came AFTER
    # main.js the boot IIFE would hit `ReferenceError: Cannot access
    # '_convSyncPushChannelWired' before initialization` (the hoisted function
    # is callable, its `let` is not yet initialized) and abort the whole init.
    # Only touches window/Map at load; conversations/Api/renderChat/pushSubscribe
    # are referenced only inside function bodies (runtime, after boot).
    'conv_sync_push.js',
    # Client half of windowed conversation reads (tail-N first-open + scroll-up
    # pagination). MUST load BEFORE main.js for the SAME TDZ reason as
    # conv_sync_push.js above: main.js's synchronous boot IIFE calls
    # wireConvWindowScrollLoader(), whose body reads the module-level
    # `let _scrollUpWired`. If this file came AFTER main.js the boot IIFE would
    # hit `ReferenceError: Cannot access '_scrollUpWired' before
    # initialization` and abort the whole init (observed 2026-07-13). Only
    # defines functions + attaches window.* at load; conversations/activeConvId/
    # renderChat/Api/document are referenced only inside function bodies
    # (runtime, after boot). Inert unless the server returns windowed:true.
    'conv_window.js',
    # One-click diagnostics collector (window.__tofuCollectDiagnostics). Only
    # defines a window.* global inside an IIFE at load; reads app state
    # (conversations/activeConvId/convWindowParam/BASE_PATH) lazily inside the
    # collector body at RUNTIME, so it can load anytime before main.js. The
    # tofu-android WebScreen.kt "Copy diagnostics" FAB invokes it via
    # evaluateJavascript() and writes the JSON to the native clipboard.
    'diag_collect.js',
    # Orchestrator (MUST be last) — boot IIFE that wires the app
    'main.js',
    # Post-orchestrator UI widgets (depend on conversations/activeConvId/config
    # globals declared in core.js + main.js, so they MUST come after main.js).
    'compaction-viewer.js',
    'context-bar.js',
    # tofu-pet.js + tofu-scene.js — MOVED to _DEFERRED_FILES 2026-08-01
    # (Epic-E pt_3879f00e sub-part 3 slice C, the ~160KB decorative
    # family out of the render-blocking core; see docs/EPIC_E_SIZE_LEDGER.md).
    # Zero gates needed: the pair has ZERO external JS callers (the only
    # cross-references are between the two and all window-guarded), the
    # sole external reference (index.html sceneSwitchBtn onclick) is
    # natively absence-safe (window.TofuPet&&…), the app→pet signal seam
    # is fire-and-forget dispatchEvent (absent listeners = no-op), and
    # both IIFEs self-boot through the readyState guard whenever the
    # feature bundle lands. NO feature-loader stub by design (same
    # no-one-time-wiring argument as health_stream_timer). The idle
    # prefetch lands the pair ~2s after boot; the mount target
    # #projectBar starts display:none, so no layout shift.
    # Cross-conversation live-presence strip — pure render subscriber on the
    # 'presence' push channel. Reads activeConvId / conversations /
    # getActiveConv (main.js) + _getConvProjectPath (project.js) + t (i18n.js)
    # at runtime, so it MUST come after main.js. No raw fetch (pushSubscribe
    # only).
    'presence.js',
    # NOTE: the Project Brain cluster (project-brain.js + -peers + -status +
    # -i18n) was MOVED to _DEFERRED_FILES (2026-07-09). It is a self-contained
    # panel opened only by a user action (openProjectBrain / toggleProjectBrain /
    # openProjectBrainInfluence — the collab-bar click + conv-scoped deep-link);
    # the only core caller, projectBrainRefresh (main.js:637, on conv-switch), is
    # typeof-guarded and no-ops until the panel has been opened, so deferring the
    # cluster does NOT trigger the feature fetch on boot/conv-switch. See
    # _DEFERRED_FILES below + feature-loader.js.
    # Per-turn context note builder/renderer. Reads projectState + toolbar
    # globals + config to snapshot each turn's context, so it MUST come
    # after main.js. Consumed by ui/chat_render.js (renderTurnCtxNote) and
    # main/main_send_pipeline.js (buildTurnCtxSnapshot).
    'info-rail.js',
    # Merged "Local Control" surface (browser bridge + desktop agent in ONE
    # toolbar entry + ONE setup modal). Reads the toolbar globals
    # (browserEnabled / desktopEnabled) and calls _applyBrowserUI /
    # _applyDesktopUI / _saveConvToolState, so it MUST come after main.js and
    # main/main_toolbar_ui.js.
    'local-control.js',
    # First-run setup wizard (API key vs subscription chooser + API probe
    # path). Drives openSettings / switchSettingsTab / _oauthLogin /
    # Api.providers.probe / Api.serverConfig.update and is entered from
    # _maybeAutoOpenSettings — all runtime calls, so it only needs to come
    # after the settings modules and main/main_toolbar_ui.js.
    'onboarding.js',
    # Real-time network-latency signal indicator in the topbar. Pure runtime
    # subscriber on push.js's RTT probe (pushOnLatency) + reads t() at render
    # time, so it MUST come after main.js (and after push.js, loaded far above).
    'net-latency.js',
    # Mobile popover portaling (timer/optimizer) + mobile flow picker.
    # MUST come after timer.js / optimizer.js / main_toolbar_ui.js — it wraps
    # their globals (toggleTimerPanel / toggleOptimizerPanel / setActiveFlow).
    'mobile_panels.js',
]

# ── Load-bearing modules ──────────────────────────────────────────────
# If ANY of these is corrupt or missing, a bundle built from "whatever
# remains" boots into a silently-crippled app (e.g. a skipped push.js kills
# live updates, the "N running" badge, and translate status with NO error and
# nothing actionable in the logs). For a critical file we therefore REFUSE to
# ship a partial bundle and return None, so routes/common.py falls back to the
# individual <script> tags where index.html's load-guard SURFACES the failure
# to the user. Non-critical files keep the skip-and-continue degradation.
# INVARIANT: every entry MUST be in _BUNDLE_FILES (guarded by
# tests/test_bundle_corruption_guard.py) so a rename can't silently empty it.
_CRITICAL_FILES = frozenset({
    'i18n.js',   # t() — used by every module
    'core.js',   # foundational module state (apiUrl, conversations, config, …)
    'api.js',    # unified backend HTTP client (Api.*)
    'push.js',   # live server-push channel (pushSubscribe / notifications)
    'main.js',   # boot orchestrator IIFE
})

# ── DEFERRED feature bundle ───────────────────────────────────────────
# Heavy, rarely-first-used feature modules that are NOT needed for first
# paint or chat. They are built into a SEPARATE bundle (feature-<hash>.js)
# that the browser fetches ON DEMAND — the first time the user opens the
# feature — via static/js/feature-loader.js (in the core bundle), which
# installs a lazy stub for each entry point and swaps in the real function
# once the feature bundle loads. Both bundles share window scope (plain
# concatenated <script>s, NOT ES modules), so the dependency ordering
# WITHIN this list still matters (task-mode.js reads orchestration.js's
# _ORCH_* at runtime → orchestration.js first).
#
# SAFE-TO-DEFER criteria (audited 2026-07-05): nothing in the core bundle
# references a deferred module's symbols at IIFE/LOAD time (only inside
# function bodies, all typeof-guarded), and each has a clean user-triggered
# onclick= entry point in index.html. Modules with load-time side effects
# are deliberately KEPT in the core bundle: scheduler/optimizer/timer
# (badge-polling IIFEs at load), and myday (its `_mydayScheduleReminder()`
# auto-runs at load — myday.js:1326). NOTE: image-gen.js was previously listed
# here as blocked by "its core-owned `imageGenMode` global" — that was WRONG:
# `imageGenMode` is declared in core.js:145 and every load-time reader
# (`_applyImageGenUI`, conv-restore at main.js:597) lives in main.js/core, NOT
# in image-gen.js, whose own top-level is only var/let/const declarations. It
# is now correctly DEFERRED (below).
_DEFERRED_FILES = [
    'orchestration-catalog.js',  # role/control/glyph/icon catalog + icon-URL helpers; read at runtime by orchestration.js AND task-mode.js → load FIRST
    'orchestration.js',   # Orchestration Studio (openOrchestration) — ~36KB gz
    'task-mode.js',       # Task Mode viewer (openTaskMode) — reads _ORCH_* at runtime → AFTER orchestration.js
    # paper-reader.js decomposition (Epic E, 2026-07-11). Cohesive leaf siblings
    # load BEFORE paper-reader.js; all window-scope var (no load-time cross-read).
    'paper/push_transport.js',  # shared push-vs-poll transport: paperAttachPush/paperDetachPush + the seq-gated exactly-once ingest (paperIngestEvent), used by report/qa/recommend → window-scope leaf, MUST load before every paper/* consumer
    'paper/reader_prefs.js',  # reader comfort prefs (text-size + width); leaf
    'paper/arxiv.js',     # arXiv search + describe-recommend + fetch; owns _recStream (read by core KaTeX hook at runtime) → load before paper-reader.js
    'paper/qa.js',        # Q&A tab render+send+poll; QA state + _ensurePaperText stay in core → load before paper-reader.js
    'paper/pdf_viewer.js',  # pdf.js load/render/zoom pipeline; owns _paperResizeObserver/_paperZoomDebounce → BEFORE pdf_responsive.js (calls paperFitWidth) + paper-reader.js
    'paper/pdf_responsive.js',  # draggable divider + foldable/tablet responsive-crossing IIFE (self-contained; self-inits on DOMContentLoaded)
    'paper/report.js',    # Report + Review Mode (task/poll/render/export + 7 load-time listeners); report/review STATE stays in core → load before paper-reader.js
    'paper/reading_xp.js',  # Reading-experience rail (anchored insight cards / recap / cost breakdown); seams INTO report.js via window._paperXp* → load AFTER report.js, before paper-reader.js
    'paper/deepen.js',      # On-demand section depth (P3): heading/formula deepen buttons + drawer; hooked from reading_xp's after-render seam → load AFTER reading_xp.js
    'paper/notes.js',       # Reader margin notes (P4): selection → popover → paper_notes CRUD + highlight/chip/orphan-tray decoration; hooked from reading_xp's seam → AFTER deepen.js
    'paper/focus_mode.js',  # Focus mode (P4): one-paragraph spotlight + j/k nav; hooked from reading_xp's seam → AFTER notes.js
    'paper/babel.js',     # Babel PDF-translation tab; owns _babelTranslatedPages (read by core library-persist at runtime) → load before paper-reader.js
    'paper/library.js',   # Paper Library (bookshelf) cache+CRUD+render; owns _paperLibrary state (extracted from paper-reader.js 2026-07) → runtime cross-refs, order free; before paper-reader.js
    'paper/podcast.js',   # Paper Podcast tab (player + transcript + sleep timer); reads _paperHash/Api.paper.podcast* at RUNTIME only → before paper-reader.js
    'paper/video.js',     # Paper Video Abstract tab (player + scene grid + per-scene regen); reads _paperHash/Api.motion* at RUNTIME only → before paper-reader.js
    'paper/research.js',  # Auto-research console (direction → scored ideas). Reads NO paper state (pre-paper capability); reached from paper-reader.js:708's describe-box button → MUST load before paper-reader.js
    'paper-reader.js',    # Paper Reader (togglePaperMode) — ~54KB gz; init via _onReady (feature-loader.js)
    # Image-Gen mode (enterImageGenMode + panel controls) — ~11KB gz. No
    # load-time side effect; only load-time core read is `escapeHtml` (present,
    # core loads first). Independent of the three above (no cross-read).
    'image-gen.js',
    # Gacha (batch) image generation (extracted from image-gen.js 2026-07).
    # _igGenerateBatch/_igBatchModels; called at runtime from generateImageDirect,
    # shares _igGenerating/_IG_ALL_MODELS via window scope → load order free.
    'image-gen-batch.js',
    # Project Brain cluster (~18KB gz standalone) — the full three-column
    # coordination panel. DEFERRED 2026-07-09: no load-time side effect (each
    # file's top level is only decls + window.* exposes; project-brain.js's
    # pushSubscribe lives INSIDE openFeed(), never at module scope). Opened only
    # by a user action (the 3 openers below). The one core caller,
    # projectBrainRefresh (main.js:637, conv-switch), and closeProjectBrain
    # (overlay onclick) are typeof-guarded and deliberately NOT deferred entry
    # points — leaving them absent-at-boot means conv-switch NEVER triggers the
    # feature fetch (refresh has nothing to refresh, close nothing to close when
    # the panel was never opened). Ordering: peers/status/i18n read
    # window.ProjectBrain._state at RUNTIME → MUST come after project-brain.js.
    'project-brain.js',
    'project-brain-attention.js',
    'project-brain-peers.js',
    'project-brain-status.js',
    'project-brain-i18n.js',
    # Cross-tab/cross-device sync (53KB) — deferred 2026-07-31 (Epic-E
    # pt_3879f00e sub-part 3 slice A). Boot wiring survives via the
    # _wireConvSyncPush feature-loader stub (main.js calls it
    # typeof-guarded at boot); the module's own load-time side effects
    # (BroadcastChannel creation + listener, _scheduleNextReconcile()
    # self-start, window exposes) only touch browser globals / window,
    # and every core symbol it reads (conversations, ConvCache,
    # loadConversationsFromServer, pushSubscribe, …) is in the core
    # bundle which always loads first. See the _BUNDLE_FILES moved-note.
    'core/cross_tab_sync.js',
    # Stream health/elapsed timers (62KB) — deferred 2026-08-01 (Epic-E
    # sub-part 3 slice B). Gates + idle prefetch only (no stub — see the
    # _BUNDLE_FILES moved-note for the no-one-time-wiring argument). Its
    # load-time side effects (visibilitychange/pageshow listeners, window
    # exposes) touch only browser globals; every core symbol it reads is
    # in the core bundle which always loads first.
    'core/health_stream_timer.js',
    # The decorative pet family (~160KB) — deferred 2026-08-01 (Epic-E
    # sub-part 3 slice C). Zero gates + zero stubs (see the _BUNDLE_FILES
    # moved-note for the census); tofu-pet.js first to preserve the
    # core-bundle relative order, though every cross-reference between
    # the two is window-guarded so the order is free.
    'tofu-pet.js',
    'tofu-scene.js',
    # Rich tool-round renderers (~58KB) — deferred 2026-08-01 (Epic-E
    # sub-4, split OUT of ui/tool_rounds.js which STAYS in core for the
    # first-paint restore path). The conv-meta family (Project Brain
    # board/charter/feed/peer/digest/commit cards) + the Timer Watcher
    # block + its countdown ticker render rounds that only exist in convs
    # which used Project Brain / scheduler tools. Core dispatch in
    # _renderUnifiedToolLine is typeof-guarded (generic ptool-line until
    # this lands via idle prefetch) and the module's load-time upgrade
    # pass re-renders the active conv once if it holds such rounds.
    'ui/tool_rounds_rich.js',
    # Access matrix (55KB) — deferred 2026-08-01 (Epic-E sub-5A). The
    # per-provider model×key health grid renders only inside Settings →
    # Providers after a user toggle; every external call site was already
    # typeof-guarded (see the _BUNDLE_FILES moved-note).
    'settings/providers/access_matrix.js',
    # Swarm panel (55KB) — deferred 2026-08-01 (Epic-E sub-5B). Renders
    # only for convs with swarm activity; guarded generic-line fallback
    # until it lands (see the _BUNDLE_FILES moved-note).
    'ui/streaming_swarm_panel.js',
    # My Day report modal (65KB) — deferred 2026-08-01 (Epic-E sub-6).
    # Opens only via the topbar button (stub: openDailyReport); zero
    # external JS callers, _myday state private to the pair, boot blocks
    # late-load-safe (see the _BUNDLE_FILES moved-note). myday.js FIRST
    # — myday_tasks.js shares its state object.
    'myday.js',
    'myday_tasks.js',
    # Project PANEL (67KB after the sub-7 split) — deferred 2026-08-01.
    # The state subset stays in core as project_state.js (loaded at the
    # panel's old position, far above). The panel calls the state subset
    # at RUNTIME via window scope; the two reverse bare calls from the
    # state subset (saveRecentProject / closeProjectModal / _mpFolders
    # reset) are typeof-guarded. Entry-point stubs cover the bar's
    # openProjectModal and every chat-rendered submit handler.
    'project.js',
    # Cost popover (24KB) — deferred 2026-08-01 (Epic-E sub-8). Builds
    # lazily on first open from the _costCtxByMsg stash (see the
    # _BUNDLE_FILES moved-note); legacy embedded content wins when
    # present (mixed-shape bundles safe).
    'ui/finish_info_rich.js',
    # Settings-panel six-pack (123KB) — deferred 2026-08-01 (Epic-E
    # sub-9). All user-triggered (topbar badge / settings tab / mobile
    # sheet); see the _BUNDLE_FILES moved-note for the boot-wiring fixes
    # (_onReady conversion / mobile re-wrap / install gate).
    'memory.js',
    'skills.js',
    'preferences.js',
    'optimizer.js',
    'update.js',
    'timer.js',
    # ENTIRE settings/ subpackage (~455KB) — deferred 2026-08-01 (Epic-E
    # sub-10, the line-closer). Renders only inside the user-triggered
    # Settings modal (see the _BUNDLE_FILES moved-note for the census);
    # settings.js head + settings/branding.js STAY in core (see the
    # _BUNDLE_FILES boundary note: main.js:88/349 boot callers).
    # Order preserved from the core manifest (section_requires before
    # core_panel; provider_faces before provider_render; chip_input
    # before its two consumers).
    'settings/provider_templates.js',
    'settings/auto_setup.js',
    'settings/local_endpoints.js',
    'settings/section_requires.js',
    'settings/core_panel.js',
    'settings/provider_faces.js',
    'settings/provider_render.js',
    'settings/key_stats.js',
    'settings/balance.js',
    'settings/template_actions.js',
    'settings/model_edit.js',
    'settings/visibility_defaults.js',
    'widgets/chip_input.js',
    'settings/other_tabs.js',
    'settings/speech.js',
    'settings/auth_sources.js',
    'settings/private_hosts.js',
    'settings/credentials_vault.js',
    'settings/save_export.js',
    'settings/system_prompt_editor.js',
    'settings/oauth.js',
    'settings/mcp.js',
    'settings/devices.js',
]

# The entry-point functions the feature bundle DEFINES. feature-loader.js
# installs a lazy stub for each; index.html's inline pre-boot LoadGuard also
# stubs them. Kept here so the parity test can assert the two lists agree.
_DEFERRED_ENTRY_POINTS = (
    'openOrchestration', 'openTaskMode', 'togglePaperMode',
    # image-gen.js onclick entry points (derived from every image-gen-defined
    # onclick target in index.html — the toolbar mode button + panel controls).
    # enterImageGenMode is the real load trigger; the rest only become
    # clickable after the panel opens, but are stubbed for defense-in-depth.
    'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect',
    'selectIgAspect', 'selectIgCount', 'selectIgResolution', 'toggleIgModelDropdown',
    # Project Brain openers (deferred 2026-07-09). ONLY the user-triggered
    # openers are stubbed — these are the only fns invocable while the bundle is
    # absent (collab-bar click → openProjectBrain; conv-scoped deep-link →
    # openProjectBrainInfluence; topbar toggle → toggleProjectBrain). Deliberately
    # ABSENT: projectBrainRefresh (main.js:637 conv-switch) + closeProjectBrain
    # (overlay onclick) — a loading stub there would fetch the bundle on every
    # conv-switch, negating the deferral. They are typeof-guarded at their call
    # sites and safely no-op until the panel is first opened.
    'openProjectBrain', 'toggleProjectBrain', 'openProjectBrainInfluence',
    # Cross-tab sync boot wiring (deferred 2026-07-31, Epic-E sub-part 3
    # slice A). main.js calls _wireConvSyncPush typeof-guarded at boot;
    # the stub loads the feature bundle and dispatches to the real fn, so
    # the conv-sync push subscription wires right after boot instead of
    # never. Keep in sync with feature-loader.js's _DEFERRED_ENTRY_POINTS.
    '_wireConvSyncPush',
    # My Day modal (deferred 2026-08-01, Epic-E sub-6). openDailyReport is
    # the genuine entry (topbar button = always-visible static HTML);
    # closeDailyReport + _mydayTriggerGenerate are only reachable inside
    # the open modal, stubbed for defense-in-depth (image-gen precedent).
    'openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate',
    # Project panel (deferred 2026-08-01, Epic-E sub-7). openProjectModal
    # is the always-visible project-bar opener; the rest are
    # chat-rendered interactive handlers (write-approval buttons, stdin
    # input/EoF, human-guidance choice/free-text, undo/redo modification
    # cards, apply-code modal) — a click while the panel is in flight
    # must load the bundle and dispatch, never ReferenceError.
    'openProjectModal', 'closeProjectModal',
    'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
    'submitHumanGuidanceChoice', 'submitHumanGuidanceFreeText',
    'undoConvModifications', 'undoAllModifications', 'redoConvModifications',
    'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
    # Cost popover (deferred 2026-08-01, Epic-E sub-8) — the cost tag is
    # chat-rendered on every assistant message, so its onclick must load
    # the feature bundle and build+show the popover, never ReferenceError.
    '_toggleCostPopover',
    # Settings modal (deferred 2026-08-01, Epic-E sub-10) — openSettings is
    # the genuine early entry (sidebar gear / mobile sheet / onboarding /
    # toolbar flows); switchSettingsTab follows it in every flow (same
    # window); closeSettings + saveSettings are defense-in-depth
    # (image-gen precedent). Modal-internal handlers (system-prompt
    # editor, _mcp*) deliberately NOT stubbed (Project Brain precedent).
    'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
    # Settings-panel six-pack (deferred 2026-08-01, Epic-E sub-9).
    # Badge/tab/mobile-sheet entries + the three settings-core-panel tab
    # populates (gate+stub: the typeof gate passes on the stub, which
    # loads the bundle and dispatches instead of silently skipping the
    # tab fill). The memory-modal pair is defense-in-depth (reachable
    # only inside the open modal, myday precedent).
    'openUpdateDialog', 'toggleTimerPanel', 'toggleOptimizerPanel',
    'toggleMemory', 'openMemoryModal', 'closeMemoryModal',
    'toggleMemoryAddForm', 'toggleMemoryFromModal',
    '_populateSkillsTab', '_populatePreferencesTab',
    '_renderSettingsUpdatePill',
    # Defense-in-depth close-out (same slice): static panel onclicks
    # clickable in the settings-open → bundle-land window — updateModal
    # overlay closer, skills scope tabs + search, memory create form,
    # preferences reload/save (image-gen precedent: stub every control
    # reachable from server-spliced static panel HTML).
    'closeUpdateModal', '_skillsSetScope', '_skillsFilter',
    'openMemoryCreateForm', 'refreshPreferences', 'savePreferences',
)

# ── Bundle-manifest freshness (2026-07-24 / 2026-07-31 incident class) ──
# The four manifests above are bound ONCE at import. A long-running server
# whose manifest file changes AFTER process start (a deploy, a sibling edit)
# used to keep rebuilding from that import-time-frozen binding: the rebuild
# gate (_source_max_mtime stats THIS file) correctly fired, but the build
# then assembled the OLD list — silently dropping every newly-added file
# from the shipped bundle (2026-07-24 core/model_caps.js → every model
# picker threw ReferenceError; 2026-07-31 core/conv_save.js → ReferenceError
# at 108 call sites, with core/conv_verify_retry.js missing from the same
# artifact). The fix: build_bundle() re-reads the manifests from DISK via
# _refresh_manifest() below, so the build can never lag the file. Keep the
# four assignments PLAIN module-level literals: _extract_manifest_from_source
# parses them with ast.literal_eval, and a smarter expression (concat /
# comprehension / conditional import) makes the refresh fail LOUDLY (ERROR
# log + last-known-good kept), never silently. Guarded by
# tests/test_bundle_manifest_freshness.py.
def _extract_manifest_from_source(path):
    """Re-parse the four bundle manifests from this module's on-disk source.

    Uses ast (never exec), so refreshing can never re-run module-level side
    effects. Returns ``(bundle_files, deferred_files, entry_points,
    critical_files)`` as fresh container objects. Raises (loudly) when any
    of the four is not a plain literal assignment.
    """
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    wanted = ('_BUNDLE_FILES', '_DEFERRED_FILES', '_DEFERRED_ENTRY_POINTS',
              '_CRITICAL_FILES')
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in wanted:
            continue
        value = node.value
        if target.id == '_CRITICAL_FILES':
            # frozenset({...}) — peel the call, literal-eval the set body.
            if (not isinstance(value, ast.Call)
                    or not isinstance(value.func, ast.Name)
                    or value.func.id != 'frozenset' or len(value.args) != 1):
                raise ValueError(
                    '_CRITICAL_FILES must stay a plain frozenset({...}) literal')
            found[target.id] = frozenset(ast.literal_eval(value.args[0]))
        else:
            found[target.id] = ast.literal_eval(value)
    missing = [w for w in wanted if w not in found]
    if missing:
        raise ValueError(
            f'{path}: manifest assignment(s) missing or no longer plain '
            f'module-level literals: {missing}')
    return (list(found['_BUNDLE_FILES']), list(found['_DEFERRED_FILES']),
            tuple(found['_DEFERRED_ENTRY_POINTS']), found['_CRITICAL_FILES'])


try:
    _manifest_source_mtime = os.path.getmtime(__file__)
except OSError as e:  # module file unreadable at import — refresh keeps retrying
    logger.debug('[Bundle] cannot stat %s at import (%s) — refresh will retry',
                 __file__, e)
    _manifest_source_mtime = 0.0


def _refresh_manifest():
    """Re-bind the four manifests from disk when this file changed.

    mtime-gated: a no-op when nothing changed, so callers on hot paths pay
    one stat. Fail-safe: any read/parse error keeps the last-known-good
    lists (a stale-but-working bundle beats no bundle) and logs ERROR.
    Returns True when the manifests were actually re-read.
    """
    global _BUNDLE_FILES, _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS, _CRITICAL_FILES
    global _manifest_source_mtime
    try:
        current = os.path.getmtime(__file__)
    except OSError as e:
        logger.warning('[Bundle] cannot stat %s (%s) — keeping last-known-good manifest',
                       __file__, e)
        return False
    if current <= _manifest_source_mtime:
        return False
    try:
        fresh = _extract_manifest_from_source(__file__)
    except Exception as e:
        logger.error('[Bundle] manifest re-parse failed: %s — keeping last-known-good manifest',
                     e, exc_info=True)
        return False
    _BUNDLE_FILES, _DEFERRED_FILES, _DEFERRED_ENTRY_POINTS, _CRITICAL_FILES = fresh
    _manifest_source_mtime = current
    logger.info('[Bundle] bundle manifests re-read from disk: %d core + %d deferred files, %d entry points',
                len(fresh[0]), len(fresh[1]), len(fresh[2]))
    return True


# Global state
_bundle_filename = None    # e.g. 'bundle-a3f8b2c1.js'  (core)
_feature_filename = None   # e.g. 'feature-b7c1d2e3.js' (deferred; None if empty/failed)
_bundle_mtime = 0          # max mtime of source files when bundle was built
_pack_filenames = {}       # {'zh': 'i18n-zh-<hash>.js', 'en': ...} — EMPTY when the
                           # current bundle CONTAINS i18n.js (dual fallback), set when
                           # it excludes it. The two are ONE atomic fact: a bundle
                           # without i18n.js must NEVER be served without its pack.
_bundle_includes_i18n = True


def _source_max_mtime():
    """Get the newest mtime among all source JS files (core + deferred).

    Includes THIS module's own file (``lib/js_bundler.py``) in the max, because
    the load ORDER and membership of the bundle live in ``_BUNDLE_FILES`` here —
    NOT in any ``.js`` source. A reorder (e.g. moving a module before ``main.js``
    to fix a TDZ crash) or an add/remove leaves every ``.js`` mtime untouched, so
    without this the rebuild gate (`get_bundle_filename`) would keep serving the
    stale bundle built from the OLD order. Stat'ing the manifest file makes any
    ordering/membership change trigger a rebuild automatically.
    """
    max_mt = 0
    for name in (*_BUNDLE_FILES, *_DEFERRED_FILES):
        path = os.path.join(JS_DIR, name)
        try:
            mt = os.path.getmtime(path)
            if mt > max_mt:
                max_mt = mt
        except OSError as e:
            logger.debug('[Bundle] Cannot stat %s: %s', name, e)
    try:
        mt = os.path.getmtime(__file__)
        if mt > max_mt:
            max_mt = mt
    except OSError as e:
        logger.debug('[Bundle] Cannot stat manifest module %s: %s', __file__, e)
    return max_mt


def _clean_old_bundles(keep_core, keep_feature, keep_packs=()):
    """Remove stale built bundles (keep the current set + anything YOUNG).

    Matches ONLY the content-hashed output filenames — ``bundle-<hash>.js`` /
    ``feature-<hash>.js`` / ``i18n-<lang>-<hash>.js`` — so a SOURCE file like
    ``feature-loader.js`` (which also starts with ``feature-``) is never
    deleted. (Deleting feature-loader.js would silently break the lazy loader.)
    ``keep_packs`` is the CURRENT i18n pack filenames; any other pack-shaped
    artifact is stale.

    ★ CROSS-PROCESS AGE GRACE: an artifact younger than
    ``_BUILT_ARTIFACT_GRACE_S`` is NEVER deleted, whatever the keep-set says.
    The keep-set is per-process, but the directory is shared — a second
    builder (an xdist worker, a supervisor-restart overlap, a sibling agent
    running the bundler tests) computes its own keep-set and would delete the
    bundle/pack THIS process is currently serving: an old index.html still
    references it, and an ``i18n-<lang>-<hash>.js`` has no stale-resolver
    heal — its 404 blanks the whole UI through t()'s silent fallback. The
    build lock serializes builder-vs-builder, never builder-vs-reader, so the
    only safe reap clock is the filesystem's own mtime (process-independent).
    The grace bounds the disk the same way a TTL does: stale artifacts are
    still reaped, just never while they can still be in anyone's serve path.
    """
    keep = {keep_core, keep_feature, *keep_packs}
    now = time.time()
    try:
        for f in os.listdir(JS_DIR):
            if f in keep:
                continue
            if not _BUILT_BUNDLE_RE.match(f):
                continue
            path = os.path.join(JS_DIR, f)
            try:
                if now - os.path.getmtime(path) < _BUILT_ARTIFACT_GRACE_S:
                    continue  # another process may have just published + be serving this
            except OSError as e:
                logger.debug('[Bundle] cannot stat %s during reap (%s) — skipping', f, e)
                continue
            try:
                os.remove(path)
            except OSError as e:
                logger.debug('[Bundle] Failed to remove old bundle %s: %s', f, e)
    except OSError as e:
        logger.debug('Failed to clean old bundles: %s', e)


def _assemble_bundle(files, prefix, critical):
    """Scan → minify → concat → hash → write → node-gate one bundle.

    Args:
        files: ordered list of source file names (relative to JS_DIR).
        prefix: output filename prefix ('bundle-' for core, 'feature-' for deferred).
        critical: if True, a MISSING/CORRUPT file in ``_CRITICAL_FILES`` is FATAL
            (returns None so routes/common.py falls back to individual <script>
            tags). Only the core bundle passes critical=True — the deferred
            bundle has no critical files (its failure degrades to "that feature
            fails to open", surfaced by feature-loader.js).

    Returns:
        ``(filename, total_minified_bytes)`` on success, or ``(None, 0)`` on
        failure / empty. A syntactically-broken result is deleted + None so a
        broken bundle is never served — with ONE refinement: when the
        whole-bundle gate fails, ``_find_syntax_broken_sources`` attributes
        the breakage per-file and the bundle is re-assembled WITHOUT the
        broken NON-critical sources (degrade to "module absent", the same
        contract as the corruption scan). None is returned only when a
        CRITICAL file is broken or the re-assembled bundle still fails.
    """
    chunks = []
    total_size = 0
    missing = []
    corrupt = []
    included = 0

    for name in files:
        path = os.path.join(JS_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            if critical and name in _CRITICAL_FILES:
                logger.critical('[Bundle] CRITICAL source file %s is MISSING (%s) — '
                                'refusing to ship a crippled bundle; falling back to '
                                'individual <script> tags', name, e)
                return None, 0
            logger.warning('[Bundle] Missing source file %s: %s', name, e)
            missing.append(name)
            continue

        # Reject a corrupt source file BEFORE it can poison the bundle. A
        # single conflict marker / truncated file would otherwise glue a
        # stray token into the concatenation and white-screen every user
        # (the "Uncaught SyntaxError: Unexpected token" install failure).
        reason = _scan_source_corruption(name, content)
        if reason:
            if critical and name in _CRITICAL_FILES:
                logger.critical('[Bundle] CRITICAL source file %s is CORRUPT (%s) — '
                                'refusing to ship a crippled bundle; falling back to '
                                'individual <script> tags', name, reason)
                return None, 0
            logger.error('[Bundle] Skipping CORRUPT source file %s: %s', name, reason)
            corrupt.append(name)
            continue

        # Conservatively minify (comment + whitespace strip). Fail-open: any
        # minifier edge case falls back to the raw content for THAT file, so
        # one tricky file can never blank the app — and the final node --check
        # gate still validates the concatenated result either way. Runs AFTER
        # the corruption scan (which must see the original bytes).
        try:
            emit = _minify_js(content)
        except Exception as e:
            logger.warning('[Bundle] minify failed for %s, using raw: %s', name, e)
            emit = content

        # Wrap each file in a comment header + newline separator (helps
        # debugging stack traces). Kept as (name, chunk) pairs — NOT a flat
        # string list — so the syntax-bisect retry below can re-assemble the
        # bundle WITHOUT the broken file(s) at file granularity.
        # Boundary guard: a leading newline ensures a trailing line-comment
        # in `content` can't swallow the next file's header, and the `;`
        # terminates any statement whose file forgot a trailing semicolon so
        # adjacent files can never glue into one broken expression.
        chunks.append((name, f'// ═══ {name} ═══\n' + emit + '\n;\n'))
        total_size += len(emit)
        included += 1

    # A missing file is almost always a stale manifest entry. We DON'T abort
    # for that — skip it (loud WARNING) and bundle whatever remains, so one
    # stale entry degrades to "that one module is absent" rather than "the
    # entire app fails to boot". Only a totally empty result is fatal.
    if missing:
        logger.error('[Bundle] %d file(s) missing from %s manifest, building without them: %s',
                     len(missing), prefix.rstrip('-'), ', '.join(missing))
    if corrupt:
        logger.error('[Bundle] %d source file(s) were CORRUPT and skipped: %s '
                     '— re-run the installer/self-update or `git checkout` them',
                     len(corrupt), ', '.join(corrupt))
    if included == 0:
        # For the DEFERRED bundle an empty result is legitimate (e.g. all
        # deferred files removed) — return None WITHOUT an error so the core
        # bundle still ships and feature-loader.js just has nothing to load.
        if critical:
            logger.error('[Bundle] Cannot build core bundle — no source files found')
        else:
            logger.info('[Bundle] Deferred bundle is empty — nothing to defer')
        return None, 0

    # ── Publish loop: at most ONE syntax-bisect retry ──
    # Round 1 assembles every scanner-clean file. If the whole-bundle node gate
    # STILL fails, the breakage is a class _scan_source_corruption cannot see —
    # e.g. a brace-unbalanced file left by an interrupted agent edit (2026-08-04:
    # a duplicated `function renderMessage(` line in chat_render.js killed every
    # user's conversation-opening because the whole-bundle refusal pushed ALL
    # traffic onto the dev-fallback, where the same broken file was served raw).
    # The bisect (node --check per source) attributes the failure to its file(s);
    # round 2 re-assembles WITHOUT them so one broken module degrades to "module
    # absent" — the same contract the corruption scanner already keeps for
    # conflict markers / NUL bytes. A broken CRITICAL file stays fatal (the
    # dev-fallback's load guard surfaces it); a gate that still fails after
    # exclusion means a cross-file gluing bug and also refuses.
    for _attempt in (1, 2):
        bundle_content = ''.join(chunk for _, chunk in chunks)

        # Optional stronger minification via esbuild (mangle locals + shrink
        # syntax) when a node toolchain is present. Fail-open: absent/broken →
        # keep the dependency-free _minify_js output. Hashing the RESULT below
        # means the content-hash (cache-buster) always reflects the bytes
        # actually served.
        enhanced = _esbuild_minify(bundle_content)
        if enhanced is not None:
            bundle_content = enhanced

        content_hash = hashlib.sha256(bundle_content.encode('utf-8')).hexdigest()[:8]
        filename = f'{prefix}{content_hash}.js'
        bundle_path = os.path.join(JS_DIR, filename)

        # Short-circuit: a bundle of THIS content-hash already sits on disk
        # (built by us earlier or by a concurrent builder that won the flock).
        # The hash is over the exact bytes served, so an existing file is
        # byte-identical — no rebuild, no node-gate, no write. This makes
        # concurrent builders converge on the same artifact instead of racing
        # to (re)create it.
        if os.path.exists(bundle_path):
            return filename, total_size

        # Atomic publish: write to a UNIQUE temp file in the SAME dir, gate
        # THAT, and only os.rename() it into the hash path on success.
        # os.rename within a dir is atomic, so a reader (node --check on a
        # sibling worker, or a browser fetch) never observes a partial write,
        # and a failed gate deletes only the private temp — never a hash path
        # another process may be using. This kills both the truncated-read
        # SyntaxError and the MODULE_NOT_FOUND deletion race.
        tmp_fd = None
        tmp_path = None
        try:
            # Suffix MUST be .js: the node --check gate infers the module type
            # from the extension and hard-errors (ERR_UNKNOWN_FILE_EXTENSION)
            # on .tmp. The leading dot + random stem keep it private and out
            # of the served bundle set (_BUILT_BUNDLE_RE only matches
            # bundle-/feature-<hash>.js).
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f'.{prefix}{content_hash}.', suffix='.js', dir=JS_DIR)
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                tmp_fd = None  # fdopen took ownership; don't double-close below
                f.write(bundle_content)
        except OSError as e:
            logger.error('[Bundle] Failed to write temp bundle for %s: %s', filename, e)
            if tmp_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
            return None, 0

        # Final syntax gate on the TEMP file (best-effort — no-op when node is
        # absent). A broken bundle white-screens (core) / breaks the feature
        # (deferred) with no recovery, so DON'T publish it: drop the temp.
        ok, detail = _node_syntax_ok(tmp_path)
        if ok:
            # Another builder may have published the identical hash between our
            # existence check and now (byte-identical content). If so, adopt
            # theirs and drop our temp — never rename over a file a peer may
            # already be serving.
            if os.path.exists(bundle_path):
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
                return filename, total_size
            try:
                os.rename(tmp_path, bundle_path)
            except OSError as e:
                # Lost the publish race (peer renamed first) → identical.
                if os.path.exists(bundle_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)
                    return filename, total_size
                logger.error('[Bundle] Failed to publish bundle %s: %s', filename, e)
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
                return None, 0
            return filename, total_size

        # ── Gate FAILED — the concatenation does not parse. ──
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        logger.critical('[Bundle] Built bundle %s FAILED syntax check. Detail: %.500s',
                        filename, detail)
        if _attempt == 2:
            logger.critical('[Bundle] %s still fails after excluding the broken '
                            'source(s) — a cross-file gluing bug, not a bad module; '
                            'refusing to serve it', filename)
            return None, 0
        broken = _find_syntax_broken_sources([name for name, _ in chunks])
        if not broken:
            logger.critical('[Bundle] %s fails as a bundle but every source parses '
                            'individually — a cross-file gluing bug; refusing to '
                            'serve it', filename)
            return None, 0
        crit_bad = [b for b in broken if critical and b in _CRITICAL_FILES]
        if crit_bad:
            logger.critical('[Bundle] CRITICAL source file(s) %s are syntactically '
                            'broken — refusing to ship a crippled bundle; falling '
                            'back to individual <script> tags', crit_bad)
            return None, 0
        logger.critical('[Bundle] Excluding %d syntactically-broken source file(s) '
                        'from %s and rebuilding WITHOUT them: %s — those modules '
                        'will be absent, the rest of the app stays bundled',
                        len(broken), prefix.rstrip('-'), ', '.join(broken))
        chunks = [(n, c) for n, c in chunks if n not in set(broken)]

    return None, 0  # unreachable: every attempt path above returns explicitly


# Cross-process build lock. Serializes concurrent `build_bundle()` calls (many
# processes importing/starting at once, or racing `GET /` requests on first
# boot) so only one runs the scan→gate→publish at a time; the others block,
# then hit the content-hash short-circuit in _assemble_bundle and adopt the
# just-published artifact instead of rebuilding it. Lives next to the sources
# so it is shared across every process that bundles THIS tree. Advisory
# (fcntl.flock) — a foreign reader is never blocked; only our own builders
# coordinate. Fail-open: if the lock can't be taken (no fcntl / OSError), the
# build still proceeds — the temp+rename atomicity keeps it correct, just
# without the serialization optimization.
_BUILD_LOCK_PATH = os.path.join(JS_DIR, '.bundle-build.lock')


@contextlib.contextmanager
def _build_lock():
    """Best-effort cross-process advisory lock around a bundle build."""
    if fcntl is None:
        yield
        return
    fd = None
    try:
        fd = os.open(_BUILD_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        logger.debug('[Bundle] build lock unavailable (%s) — proceeding unlocked', e)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def build_bundle():
    """Build BOTH the core boot bundle and the deferred feature bundle.

    The core bundle (``bundle-<hash>.js``) is required — a None result forces
    routes/common.py's dev-fallback (individual <script> tags). The deferred
    bundle (``feature-<hash>.js``) is optional — a None result just means
    feature-loader.js has nothing to lazily load (the deferred modules then
    simply aren't present; their onclick stubs report a load failure).

    Returns:
        The CORE bundle filename (e.g. 'bundle-a3f8b2c1.js') or None on
        failure. The feature filename is stored in the module global
        ``_feature_filename`` (read via ``get_feature_bundle_filename``).
    """
    global _bundle_filename, _feature_filename, _bundle_mtime
    global _pack_filenames, _bundle_includes_i18n

    t0 = time.time()

    with _build_lock():
        # Re-read the manifest from disk BEFORE anything in this build can
        # consume it (pack extraction below reads _BUNDLE_FILES per call via
        # lib/i18n_boot_keys). Without this, a long-running process whose
        # module was imported before the last manifest edit assembles the
        # import-time-frozen list — the rebuild gate fires, yet the shipped
        # bundle silently lacks every file added since process start.
        _refresh_manifest()

        # i18n single-language packs (Epic-E sub-part 1 slice 2) — emit FIRST,
        # before assembling the core bundle, so the bundle's shape (with vs
        # without i18n.js) can be decided by whether packs exist. FAIL-OPEN:
        # emission needs node (extraction + the roundtrip gate); when it fails
        # we assemble the bundle WITH i18n.js exactly as before and serve no
        # packs — the status quo. A pack failure must never take the bundle
        # down, because a served broken pack is invisible in production (t()
        # silently falls back), whereas a missing pack just means "no split".
        pack_names = ()
        pack_map = {}
        try:
            from lib.i18n_packs import emit_pack_files
            # Extract from THIS tree's i18n.js, never the global default —
            # the pack and the bundle must always derive from the same
            # sources (and tests monkeypatch JS_DIR to a temp tree).
            pack_map = emit_pack_files(
                JS_DIR, source_path=os.path.join(JS_DIR, 'i18n.js'))
            pack_names = tuple(pack_map.values())
        except Exception as e:  # noqa: BLE001 — fail-open by design (see above)
            logger.warning('[Bundle] i18n pack emission failed; serving '
                           'dual-language i18n.js as before: %s', e)

        # Only exclude i18n.js from the core bundle when its replacement packs
        # actually exist. The bundle content and _pack_filenames are ONE
        # atomic fact (set together below): a bundle without i18n.js must
        # never be served alongside an empty pack set.
        core_files = ([f for f in _BUNDLE_FILES if f != 'i18n.js']
                      if pack_map else list(_BUNDLE_FILES))

        core_name, core_size = _assemble_bundle(core_files, 'bundle-', critical=True)
        if not core_name:
            return None

        # Deferred bundle — non-fatal. If it fails to build, ship core alone.
        feature_name, feature_size = _assemble_bundle(_DEFERRED_FILES, 'feature-', critical=False)

        _clean_old_bundles(core_name, feature_name, keep_packs=pack_names)
        # PUBLISH ORDER IS LOAD-BEARING. Readers of this manifest
        # (get_i18n_pack_tag / get_i18n_pack_urls) are lock-free, so they can
        # observe a partially-updated manifest. Publish the i18n pair FIRST and
        # the `_bundle_filename` pointer that advertises it LAST: then a reader
        # either sees the old pointer (with a pack pair that is valid for it too,
        # since both packs are hash-named and still on disk) or the new pointer
        # with its own pair. The reverse order let a reader pair the NEW bundle
        # (i18n.js excluded) with the STALE `_bundle_includes_i18n = True`, so it
        # injected neither the dictionary nor a pack and the whole UI rendered
        # raw i18n keys with no error.
        _pack_filenames = pack_map
        _bundle_includes_i18n = not pack_map
        _bundle_mtime = _source_max_mtime()
        _feature_filename = feature_name
        _bundle_filename = core_name

    elapsed = time.time() - t0
    if feature_name:
        logger.info('[Bundle] Built %s (%d files, %dKB) + deferred %s (%d files, %dKB) in %.1fms',
                    core_name, len(_BUNDLE_FILES), core_size // 1024,
                    feature_name, len(_DEFERRED_FILES), feature_size // 1024, elapsed * 1000)
    else:
        logger.info('[Bundle] Built %s (%d files, %dKB minified) in %.1fms — no deferred bundle',
                    core_name, len(_BUNDLE_FILES), core_size // 1024, elapsed * 1000)
    return core_name


# ── Background rebuild (keeps a slow build off the request thread) ──
# `get_bundle_filename()` rebuilds INLINE when a source file changed. That is
# fine at startup, but it must NEVER run on the `GET /` request thread: a stale
# in-memory manifest (e.g. a source file renamed under a still-running process)
# turns every first page load into a full node-gate + minify + hash cycle,
# which — during a reconnect storm — stalled the event loop and produced a 95s
# `GET /`. The non-blocking accessor below serves the last-good bundle
# immediately and schedules the rebuild in a daemon thread instead.
_bg_rebuild_lock = threading.Lock()
_bg_rebuild_active = False


def _schedule_background_rebuild():
    """Kick off build_bundle() in a daemon thread, deduplicated.

    A no-op when a background rebuild is already in flight (only one runs at a
    time; concurrent callers just adopt the last-good bundle until it lands).
    """
    global _bg_rebuild_active
    with _bg_rebuild_lock:
        if _bg_rebuild_active:
            return
        _bg_rebuild_active = True

    def _run():
        global _bg_rebuild_active
        try:
            build_bundle()
        except Exception as e:
            logger.error('[Bundle] background rebuild failed: %s', e, exc_info=True)
        finally:
            with _bg_rebuild_lock:
                _bg_rebuild_active = False

    t = threading.Thread(target=_run, name='tofu-bundle-rebuild', daemon=True)
    t.start()


def reset_manifest_for_tests():
    """Drop the in-process bundle/pack manifest so the next read REBUILDS it.

    ★ WHY THIS EXISTS — the save/restore trap.

    ``_pack_filenames`` / ``_bundle_includes_i18n`` are PUBLISHED as a side
    effect of building (see build_bundle), so a test that snapshots them,
    mutates them, and replays the snapshot does NOT restore the world: its
    snapshot was taken BEFORE the build it triggered, so replaying it stamps
    the pre-build values (``{}`` / ``True``) over the real published state.
    The module then reports "packs inactive" for the rest of the process and
    every later test asking for a pack tag silently gets None — a
    cross-file poisoning that NEITHER file reveals alone.

    That is the same defect shape as the production bug this module was just
    fixed for: a snapshot/restore pair reinstating a stale fact.

    So the ONLY supported way to undo manifest mutation is to invalidate it
    and let the next reader rebuild from the real sources. Tests MUST call
    this instead of assigning the private globals back.

    Cheap: the content-hash short-circuit means a rebuild re-publishes the
    same filenames without rewriting any artifact.
    """
    global _bundle_filename, _feature_filename, _bundle_mtime
    global _pack_filenames, _bundle_includes_i18n
    with _build_lock():
        _bundle_filename = None
        _feature_filename = None
        # 0 forces the staleness gate in get_bundle_filename* to miss, so a
        # reader cannot be fooled into serving the cleared manifest.
        _bundle_mtime = 0
        _pack_filenames = {}
        _bundle_includes_i18n = True


def get_bundle_filename_nonblocking():
    """Like get_bundle_filename() but NEVER runs build_bundle() on the caller.

    Serves the current bundle immediately. When source files changed since the
    last build, it schedules a background rebuild and returns the still-valid
    last-good bundle — so the browser gets the OLD bundle for one more load and
    picks up the new one on the next page load (the hashed filename makes that
    cache-safe). The only case that still blocks is a genuine cold start with NO
    serviceable bundle on disk (startup normally builds it first, so this is the
    dev/first-boot fallback, not the request-path hot case).

    Returns:
        Bundle filename string, or None if no bundle could be produced.
    """
    current_mtime = _source_max_mtime()
    have_current = (
        _bundle_filename
        and current_mtime <= _bundle_mtime
        and os.path.exists(os.path.join(JS_DIR, _bundle_filename))
    )
    if have_current:
        return _bundle_filename

    # Stale (or missing) — but if the last-good bundle is still on disk we can
    # serve it now and rebuild off-thread. Only block when there is nothing
    # serviceable to hand back.
    if _bundle_filename and os.path.exists(os.path.join(JS_DIR, _bundle_filename)):
        _schedule_background_rebuild()
        return _bundle_filename

    return build_bundle()


def get_feature_bundle_filename_nonblocking():
    """Non-blocking companion to get_feature_bundle_filename().

    Resolves the pair via get_bundle_filename_nonblocking() (which may schedule
    a background rebuild) and returns the currently-published feature filename.
    """
    get_bundle_filename_nonblocking()
    return _feature_filename


def get_bundle_script_tag_nonblocking():
    """Non-blocking companion to get_bundle_script_tag() for the request path."""
    filename = get_bundle_filename_nonblocking()
    if not filename:
        return None
    return (f'<script defer src="static/js/{filename}"'
            f' onload="_onScriptLoad()" onerror="_onScriptError(event)"></script>')
def get_i18n_pack_tag(lang):
    """Script tag for the single-language i18n pack, or None.

    Returns a tag ONLY when the currently-served core bundle EXCLUDES i18n.js
    (i.e. packs were emitted in the same build). When the bundle contains
    i18n.js (dual fallback after a failed emission), returns None so the
    caller injects nothing — the dictionary is already in the bundle.

    The ``_bundle_includes_i18n`` / ``_pack_filenames`` pair is published
    BEFORE the ``_bundle_filename`` pointer inside build_bundle(), and read
    into locals in ONE snapshot here, so a tag can never be handed out for a
    bundle that already carries the dictionary.
    """
    filename = get_bundle_filename_nonblocking()
    includes_i18n, packs = _bundle_includes_i18n, _pack_filenames
    if not filename or includes_i18n:
        return None
    pack = packs.get(lang) or packs.get('zh')
    if not pack:
        return None
    # _onI18nPackError (index.html), NOT the generic _onScriptError: in pack
    # mode this file is the only copy of the dictionary, so its failure gets a
    # retry + an explicit banner rather than a silent wall of raw i18n keys.
    return (f'<script defer src="static/js/{pack}"'
            f' onload="_onScriptLoad()" onerror="_onI18nPackError(event)"></script>')


def get_i18n_pack_urls():
    """{lang: 'static/js/<pack>'} for setLanguage()'s on-demand fetch, or None.

    Injected into the page by routes/common.py as ``window.__I18N_PACK_URLS__``.
    None when packs are inactive (dual bundle) — setLanguage then needs no
    fetch because the dictionary already carries both languages.
    """
    filename = get_bundle_filename_nonblocking()
    includes_i18n, packs = _bundle_includes_i18n, _pack_filenames
    if not filename or includes_i18n or not packs:
        return None
    return {lang: f'static/js/{name}' for lang, name in packs.items()}


def get_bundle_filename():
    """Get the current CORE bundle filename, rebuilding if source files changed.

    Returns:
        Bundle filename string, or None if bundling failed.
    """
    global _bundle_filename, _bundle_mtime

    # Check if any source file is newer than the bundle
    current_mtime = _source_max_mtime()
    if _bundle_filename and current_mtime <= _bundle_mtime:
        # Bundle path might have been deleted (e.g., manual cleanup)
        if os.path.exists(os.path.join(JS_DIR, _bundle_filename)):
            return _bundle_filename

    # Rebuild (rebuilds both core + deferred)
    return build_bundle()


def get_feature_bundle_filename():
    """Get the current DEFERRED feature bundle filename (or None).

    Ensures the pair is built/up-to-date first (via get_bundle_filename),
    then returns the feature filename. None means there is nothing to defer
    or the deferred bundle failed to build (core still ships).
    """
    get_bundle_filename()   # keeps the pair coherent; sets _feature_filename
    return _feature_filename


def resolve_stale_bundle(filename):
    """Map a requested built-bundle filename to the CURRENT one if it is stale.

    A client holding a stale ``index.html`` (bfcache / long-lived tab /
    caching proxy) asks for a ``bundle-<hash>.js`` / ``feature-<hash>.js`` whose
    hash was already deleted by ``_clean_old_bundles`` on the last rebuild →
    404 → the LoadGuard banner. This resolver lets the 404 handler self-heal
    such a request by redirecting to the current bundle of the SAME KIND.

    Args:
        filename: the bare filename requested (e.g. ``'bundle-95e8203d.js'``),
            with no directory or query string.

    Returns:
        The current bundle filename (e.g. ``'bundle-3af2a182.js'``) when
        ``filename`` is a genuinely-built bundle of the same kind but a
        DIFFERENT (stale) hash and the current one is available; otherwise
        None. Returns None when the request already names the current file
        (let it serve normally) or is not a built-bundle name at all (a real
        404 — must NOT be masked).

    ★ KIND IS THREE-WAY, NOT TWO-WAY. ``_BUILT_BUNDLE_RE`` also admits the
    single-language ``i18n-<lang>-<hash>.js`` packs, and a pack must heal to
    the current pack OF THE SAME LANGUAGE. A two-way ``bundle-`` / else-
    ``feature-`` split sent every stale pack request to the FEATURE bundle:
    the browser then ran the feature bundle in the pack's place, so the core
    bundle (which EXCLUDES i18n.js whenever packs are active) never got
    ``_i18n`` / ``_i18nLang`` / ``t()`` at all — the whole UI rendered raw
    i18n keys — while the doubly-executed feature bundle threw
    ``Identifier already declared``, killing the rest of boot.
    """
    if not filename or not _BUILT_BUNDLE_RE.match(filename):
        return None
    pack = _PACK_LANG_RE.match(filename)
    if pack:
        # Keeps the pair coherent: _pack_filenames is published inside
        # build_bundle() together with the bundle pointer this resolves against.
        get_bundle_filename()
        current = _pack_filenames.get(pack.group(1))
    elif filename.startswith('bundle-'):
        current = get_bundle_filename()
    else:  # 'feature-'
        current = get_feature_bundle_filename()
    if not current or filename == current:
        return None
    return current


def get_bundle_script_tag():
    """Get the HTML script tag for the CORE bundle.

    Returns:
        HTML string like '<script defer src="static/js/bundle-a3f8b2c1.js" ...></script>'
        or None if bundle is not available.
    """
    filename = get_bundle_filename()
    if not filename:
        return None
    return (f'<script defer src="static/js/{filename}"'
            f' onload="_onScriptLoad()" onerror="_onScriptError(event)"></script>')
