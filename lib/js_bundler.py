"""JS bundler — concatenate app scripts into a single bundle at startup.

Eliminates the HTTP/1.1 waterfall problem where browsers limit to 6 concurrent
connections per host, causing 18 JS files to download in 3-4 serial waves.
With the bundle, the browser fetches 1 file (gzip ~250KB) in a single request.

The bundle is rebuilt at startup and whenever any source file changes.
No npm/webpack/build step required — pure Python concatenation + a
conservative, dependency-free minify pass (``_minify_js``, see below).
"""
import hashlib
import os
import shutil
import subprocess
import time

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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE_DIR, 'static', 'js')

# ── Load order MUST match index.html (dependencies flow top → bottom) ──
_BUNDLE_FILES = [
    'i18n.js',         # MUST be first — t() is used by all other modules
    'core/icons.js',   # Icon()/IconDot() SVG registry — used by many modules; load early
    'idb-cache.js',
    'core.js',
    # ── core/ subpackage (split 2026-05-28 from monolithic core.js) ──
    # 11 files extracted from core.js (3877 LOC). The slim core.js shell
    # above declares foundational module state (apiUrl, BASE_PATH, TAB_ID,
    # conversations, _folders, config, serverModel, getActiveConv,
    # generateId, _ensureMsgId, scrollToBottom, ...) BEFORE these load,
    # so each extracted file can reference them at module-load time.
    # Symbols share window scope; no exports/imports needed.
    # See `.tofu/skills/frontend-core-decomposition.md` for rationale.
    'core/folders.js',
    'core/cost.js',
    'core/debug_panel.js',
    'core/escape_html.js',
    'core/safe_html.js',   # after escape_html.js (uses escapeHtml), before ui/ consumers
    'core/error_envelope.js',
    'core/cross_tab_sync.js',
    'core/conversations.js',
    # Frontend per-(conv,msg) in-flight translate guard (mirrors the backend
    # lib/translate/inflight.py). Only references window/Map at load; CALLED at
    # runtime by translation.js + message_actions.js. Leaf module.
    'core/translate_guard.js',
    'core/cache_stats.js',
    'core/markdown.js',
    'core/health_stream_timer.js',
    'core/toast.js',
    'core/dialog.js',  # themed confirm/alert/prompt — after toast (same window scope)
    # Unified API client — owns every backend HTTP call. Depends on
    # apiUrl() from core.js, consumed by every feature module below.
    'api.js',
    'push.js',         # after core.js (uses apiUrl), before ui.js (uses pushSubscribe)
    'export-images.js',
    'branch.js',
    # Artifacts panel — depends on core.js (renderMarkdown, escapeHtml,
    # apiUrl) but is consumed by ui.js's chip-rendering path, so it
    # MUST come before ui.js.
    'artifacts.js',
    # ── ui/ subpackage (split 2026-05-28 from monolithic ui.js) ──
    # The 11 files below were extracted from ui.js (8917 LOC). Concatenated
    # in load order — symbols share window scope so no exports needed.
    # IMPORTANT: this list MUST stay in dependency order.
    # See `.tofu/skills/ui-decomposition.md` for the rationale.
    'ui/conversation_list.js',
    'ui/streaming_render.js',
    'ui/chat_render.js',
    'ui/popups.js',
    'ui/finish_info.js',
    'ui/tool_rounds.js',
    'ui/message_actions.js',
    'ui/edit_message.js',
    'ui/turn_nav.js',
    # Swarm "Parallel Execution" panel rendering + stuck-panel reconciler,
    # extracted from ui/streaming_ui.js (2026-06-27). Leaf cluster; its
    # builders are called from streaming_ui.js + tool_rounds.js via shared
    # window scope. Load BEFORE streaming_ui.js for clear intent.
    'ui/streaming_swarm_panel.js',
    'ui/streaming_ui.js',
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
    'image-gen.js',
    'paper-reader.js',
    'project.js',
    'memory.js',
    'skills.js',
    'preferences.js',
    'orchestration.js',
    # Task Mode viewer — references _ORCH_ICONS from orchestration.js at
    # runtime, so it MUST come after it.
    'task-mode.js',
    'scheduler.js',
    'optimizer.js',
    'update.js',
    'timer.js',
    'myday.js',
    # settings.js is now a slim head (var _serverConfig = null;
    # var _keyStatsCache = {...}; var _keyStatsLoading = false;) followed
    # by a pointer comment. It MUST come BEFORE the settings/ subpackage
    # so the head's `var` initialisers run first — extracted files in
    # settings/ assume those globals exist + start as null/empty.
    'settings.js',
    # ── settings/ subpackage (split 2026-05-28 from monolithic settings.js) ──
    # The 15 files below were extracted from settings.js (4755 LOC).
    # Concatenated in load order; symbols share window scope.
    # See `.tofu/skills/frontend-settings-decomposition.md`.
    'settings/branding.js',
    'settings/provider_templates.js',
    'settings/auto_setup.js',
    'settings/local_endpoints.js',
    'settings/core_panel.js',
    'settings/provider_render.js',
    'settings/key_stats.js',
    'settings/balance.js',
    'settings/template_actions.js',
    'settings/model_edit.js',
    'settings/access_matrix.js',
    'settings/visibility_defaults.js',
    'settings/chip_input.js',
    'settings/other_tabs.js',
    'settings/auth_sources.js',
    'settings/save_export.js',
    'settings/system_prompt_editor.js',
    'settings/oauth.js',
    'settings/mcp.js',
    # relay-admin.js intentionally NOT bundled — it loads only on the
    # standalone /admin page (static/admin.html), not in index.html.
    # ── main/ subpackage (split 2026-05-28 from monolithic main.js) ──
    # The 8 files below were extracted from main.js. They must come BEFORE
    # main.js so the boot IIFE in main.js can reference their symbols.
    # See `.tofu/skills/frontend-main-decomposition.md` for the rationale.
    'main/main_conv_lifecycle.js',
    'main/main_translating_bubble.js',
    'main/main_send_pipeline.js',
    'main/main_regen_continue.js',
    'main/main_toolbar_ui.js',
    'main/main_folders_mobile.js',
    'main/main_input_handling.js',
    'main/main_init_tasks.js',
    # Orchestrator (MUST be last) — boot IIFE that wires the app
    'main.js',
    # Post-orchestrator UI widgets (depend on conversations/activeConvId/config
    # globals declared in core.js + main.js, so they MUST come after main.js).
    'compaction-viewer.js',
    'context-bar.js',
    # Cross-conversation live-presence strip — pure render subscriber on the
    # 'presence' push channel. Reads activeConvId / conversations /
    # getActiveConv (main.js) + _getConvProjectPath (project.js) + t (i18n.js)
    # at runtime, so it MUST come after main.js. No raw fetch (pushSubscribe
    # only).
    'presence.js',
    # Project Brain — Pillar #1 cross-conversation Activity Feed tab. Reads
    # loadConversation (main.js) + Api/pushSubscribe + Icon/t at RUNTIME only,
    # so it MUST come after main.js. No raw fetch (Api.project.feed +
    # pushSubscribe only). Independent tab, not a toggle.
    'project-brain.js',
    # Project Brain — Team/Peers column. The cohesion surface: LIVE sibling
    # roster (presence ⋈ task ⋈ claimed-epic via Api.project.brainPeers) + the
    # peer-message thread (extracted from the feed). Reads Api/Icon/t/
    # loadConversation + window.ProjectBrain._state at RUNTIME, so it MUST come
    # after project-brain.js (which owns _state). No raw fetch.
    'project-brain-peers.js',
    # Per-turn context note builder/renderer. Reads projectState + toolbar
    # globals + config to snapshot each turn's context, so it MUST come
    # after main.js. Consumed by ui/chat_render.js (renderTurnCtxNote) and
    # main/main_send_pipeline.js (buildTurnCtxSnapshot).
    'info-rail.js',
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

# Global state
_bundle_filename = None   # e.g. 'bundle-a3f8b2c1.js'
_bundle_mtime = 0         # max mtime of source files when bundle was built


def _source_max_mtime():
    """Get the newest mtime among all source JS files."""
    max_mt = 0
    for name in _BUNDLE_FILES:
        path = os.path.join(JS_DIR, name)
        try:
            mt = os.path.getmtime(path)
            if mt > max_mt:
                max_mt = mt
        except OSError as e:
            logger.debug('[Bundle] Cannot stat %s: %s', name, e)
    return max_mt


def _clean_old_bundles(keep_filename):
    """Remove old bundle-*.js files."""
    try:
        for f in os.listdir(JS_DIR):
            if f.startswith('bundle-') and f.endswith('.js') and f != keep_filename:
                try:
                    os.remove(os.path.join(JS_DIR, f))
                except OSError as e:
                    logger.debug('[Bundle] Failed to remove old bundle %s: %s', f, e)
    except OSError as e:
        logger.debug('Failed to clean old bundles: %s', e)


def build_bundle():
    """Concatenate all app JS files into a single bundle with content hash.

    Returns:
        The bundle filename (e.g. 'bundle-a3f8b2c1.js') or None on failure.
    """
    global _bundle_filename, _bundle_mtime

    t0 = time.time()
    parts = []
    total_size = 0
    missing = []
    corrupt = []

    included = 0
    for name in _BUNDLE_FILES:
        path = os.path.join(JS_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            if name in _CRITICAL_FILES:
                logger.critical('[Bundle] CRITICAL source file %s is MISSING (%s) — '
                                'refusing to ship a crippled bundle; falling back to '
                                'individual <script> tags', name, e)
                return None
            logger.warning('[Bundle] Missing source file %s: %s', name, e)
            missing.append(name)
            continue

        # Reject a corrupt source file BEFORE it can poison the bundle. A
        # single conflict marker / truncated file would otherwise glue a
        # stray token into the concatenation and white-screen every user
        # (the "Uncaught SyntaxError: Unexpected token" install failure).
        reason = _scan_source_corruption(name, content)
        if reason:
            if name in _CRITICAL_FILES:
                logger.critical('[Bundle] CRITICAL source file %s is CORRUPT (%s) — '
                                'refusing to ship a crippled bundle; falling back to '
                                'individual <script> tags', name, reason)
                return None
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

        # Wrap each file in a comment header + newline separator
        # This helps with debugging stack traces.
        parts.append(f'// ═══ {name} ═══\n')
        parts.append(emit)
        # Boundary guard: a leading newline ensures a trailing line-comment
        # in `content` can't swallow the next file's header, and the `;`
        # terminates any statement whose file forgot a trailing semicolon so
        # adjacent files can never glue into one broken expression.
        parts.append('\n;\n')
        total_size += len(emit)
        included += 1

    # A missing file is almost always a stale manifest entry (a JS file
    # renamed / removed without updating _BUNDLE_FILES). We deliberately
    # DON'T abort the whole bundle for that — returning None forces the
    # dev-fallback path in routes/common.py, which strips every app
    # <script> tag and ships a blank UI. Instead we skip the missing
    # files (loud WARNING above) and bundle whatever remains, so one
    # stale entry degrades to "that one module is absent" rather than
    # "the entire app fails to boot". The manifest↔index.html parity
    # tests (tests/test_artifacts_bundle_registration.py) catch genuine
    # omissions at test time. Only a totally empty result is fatal.
    if missing:
        logger.error('[Bundle] %d file(s) missing from _BUNDLE_FILES, '
                     'building without them: %s',
                     len(missing), ', '.join(missing))
    if corrupt:
        logger.error('[Bundle] %d source file(s) were CORRUPT and skipped: %s '
                     '— re-run the installer/self-update or `git checkout` them',
                     len(corrupt), ', '.join(corrupt))
    if included == 0:
        logger.error('[Bundle] Cannot build bundle — no source files found')
        return None

    bundle_content = ''.join(parts)

    # Content hash for cache busting (first 8 chars of SHA-256)
    content_hash = hashlib.sha256(bundle_content.encode('utf-8')).hexdigest()[:8]
    filename = f'bundle-{content_hash}.js'
    bundle_path = os.path.join(JS_DIR, filename)

    # Skip write if unchanged
    if filename == _bundle_filename and os.path.exists(bundle_path):
        logger.debug('[Bundle] Already up to date: %s', filename)
        return filename

    # Write the bundle
    try:
        with open(bundle_path, 'w', encoding='utf-8') as f:
            f.write(bundle_content)
    except OSError as e:
        logger.error('[Bundle] Failed to write %s: %s', bundle_path, e)
        return None

    # Final syntax gate (best-effort — no-op when node is absent). If the
    # concatenation is somehow still not parseable, DON'T serve it: a broken
    # bundle white-screens the app with no recovery, whereas returning None
    # makes routes/common.py fall back to the individual <script> tags in
    # index.html (a slower but WORKING page). Loud CRITICAL so the exact
    # offending line is diagnosable from logs/error.log.
    ok, detail = _node_syntax_ok(bundle_path)
    if not ok:
        logger.critical('[Bundle] Built bundle %s FAILED syntax check — refusing to '
                        'serve it (falling back to individual scripts). Detail: %.500s',
                        filename, detail)
        try:
            os.remove(bundle_path)
        except OSError as e:
            logger.debug('[Bundle] could not remove bad bundle %s: %s', bundle_path, e)
        return None

    _clean_old_bundles(filename)
    _bundle_filename = filename
    _bundle_mtime = _source_max_mtime()

    elapsed = time.time() - t0
    logger.info('[Bundle] Built %s (%d files, %dKB minified) in %.1fms',
                filename, len(_BUNDLE_FILES), total_size // 1024, elapsed * 1000)
    return filename


def get_bundle_filename():
    """Get the current bundle filename, rebuilding if source files changed.

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

    # Rebuild
    return build_bundle()


def get_bundle_script_tag():
    """Get the HTML script tag for the bundle.

    Returns:
        HTML string like '<script defer src="static/js/bundle-a3f8b2c1.js" ...></script>'
        or None if bundle is not available.
    """
    filename = get_bundle_filename()
    if not filename:
        return None
    return (f'<script defer src="static/js/{filename}"'
            f' onload="_onScriptLoad()" onerror="_onScriptError(event)"></script>')
