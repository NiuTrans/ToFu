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
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
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
_BUILT_BUNDLE_RE = re.compile(r'^(?:bundle|feature)-[0-9a-f]{8}\.js$')

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
    # Shared bytes->human size formatter (formatFileSize) — de-dupes
    # image-gen.js _formatFileSize + skills.js _skillsFmtSize. Load before them.
    'core/format_size.js',
    # Shared OS-file .zip drag/drop wiring (attachZipDropZone) — de-dupes the
    # memory + skills install dropzones. Load before memory_skill_install.js
    # and skills_install.js (both call it at runtime; core loads first anyway).
    'core/zip_drop_zone.js',
    'core/cross_tab_sync.js',
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
    'core/health_stream_timer.js',
    'core/toast.js',
    'core/dialog.js',  # themed confirm/alert/prompt — after toast (same window scope)
    # Unified API client — owns every backend HTTP call. Depends on
    # apiUrl() from core.js, consumed by every feature module below.
    'api.js',
    'push.js',         # after core.js (uses apiUrl), before ui.js (uses pushSubscribe)
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
    # See `.tofu/skills/ui-decomposition.md` for the rationale.
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
    # RENDER_CONTRACT Phase 3 pure stream reducer — the single {content,
    # thinking,toolRounds} projection all four apply paths fold through. Pure
    # (no DOM/globals); load BEFORE the handlers + pipeline that consume it.
    'ui/stream_reducer.js',
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
    'project.js',
    'memory.js',
    # Skill-package (.zip) drag/drop install (extracted from memory.js 2026-07).
    'memory_skill_install.js',
    'skills.js',
    # Skills-tab zip drag/drop + upload transport (extracted from skills.js 2026-07).
    'skills_install.js',
    'preferences.js',
    # orchestration.js + task-mode.js — MOVED to _DEFERRED_FILES (lazy-loaded
    # on first Orchestration Studio / Task Mode open; ~48KB gzip combined).
    # task-mode.js reads _ORCH_ICONS from orchestration.js only at RUNTIME
    # (typeof-guarded), and both load together in the feature bundle, so the
    # ordering constraint is preserved within _DEFERRED_FILES. See
    # feature-loader.js.
    'scheduler.js',
    'optimizer.js',
    'update.js',
    'timer.js',
    'myday.js',
    # My Day TODO/stream mutation handlers (extracted from myday.js 2026-07).
    # Window-scope siblings; invoked at runtime from onclick in myday.js render
    # fns, share the _myday state object → load order free (after myday.js).
    'myday_tasks.js',
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
    'settings/speech.js',
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
    # The Tofu pet — a self-driven mascot mounted into #projectBar (tofu theme
    # only via CSS). Queries the DOM + reads localStorage at RUNTIME only, so
    # it can load anytime after main.js. No app-pipeline dependency; exposes
    # window.TofuPet + listens on the 'tofu:activity'/'tofu:react' event seam.
    'tofu-pet.js',
    # The procedural Impressionist canvas backdrop for the project bar (tofu
    # theme only via CSS). Asset-free brush-dab painter; reads the bar's
    # [data-decor] (set by tofu-pet.js) + the app theme at RUNTIME only, no
    # pipeline dependency, so it can load anytime after main.js. Exposes
    # window.TofuScene; listens on the same 'tofu:decor' event seam.
    'tofu-scene.js',
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
    'paper/reader_prefs.js',  # reader comfort prefs (text-size + width); leaf
    'paper/arxiv.js',     # arXiv search + describe-recommend + fetch; owns _recStream (read by core KaTeX hook at runtime) → load before paper-reader.js
    'paper/qa.js',        # Q&A tab render+send+poll; QA state + _ensurePaperText stay in core → load before paper-reader.js
    'paper/pdf_viewer.js',  # pdf.js load/render/zoom pipeline; owns _paperResizeObserver/_paperZoomDebounce → BEFORE pdf_responsive.js (calls paperFitWidth) + paper-reader.js
    'paper/pdf_responsive.js',  # draggable divider + foldable/tablet responsive-crossing IIFE (self-contained; self-inits on DOMContentLoaded)
    'paper/report.js',    # Report + Review Mode (task/poll/render/export + 7 load-time listeners); report/review STATE stays in core → load before paper-reader.js
    'paper/babel.js',     # Babel PDF-translation tab; owns _babelTranslatedPages (read by core library-persist at runtime) → load before paper-reader.js
    'paper/library.js',   # Paper Library (bookshelf) cache+CRUD+render; owns _paperLibrary state (extracted from paper-reader.js 2026-07) → runtime cross-refs, order free; before paper-reader.js
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
    'project-brain-peers.js',
    'project-brain-status.js',
    'project-brain-i18n.js',
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
)

# Global state
_bundle_filename = None    # e.g. 'bundle-a3f8b2c1.js'  (core)
_feature_filename = None   # e.g. 'feature-b7c1d2e3.js' (deferred; None if empty/failed)
_bundle_mtime = 0          # max mtime of source files when bundle was built


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


def _clean_old_bundles(keep_core, keep_feature):
    """Remove stale built bundles (keep the current pair).

    Matches ONLY the content-hashed output filenames — ``bundle-<hash>.js`` /
    ``feature-<hash>.js`` where <hash> is 8 hex chars — so a SOURCE file like
    ``feature-loader.js`` (which also starts with ``feature-``) is never
    deleted. (Deleting feature-loader.js would silently break the lazy loader.)
    """
    try:
        for f in os.listdir(JS_DIR):
            if f in {keep_core, keep_feature}:
                continue
            if _BUILT_BUNDLE_RE.match(f):
                try:
                    os.remove(os.path.join(JS_DIR, f))
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
        broken bundle is never served.
    """
    parts = []
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

    bundle_content = ''.join(parts)

    # Optional stronger minification via esbuild (mangle locals + shrink syntax)
    # when a node toolchain is present. Fail-open: absent/broken → keep the
    # dependency-free _minify_js output. Hashing the RESULT below means the
    # content-hash (cache-buster) always reflects the bytes actually served.
    enhanced = _esbuild_minify(bundle_content)
    if enhanced is not None:
        bundle_content = enhanced

    content_hash = hashlib.sha256(bundle_content.encode('utf-8')).hexdigest()[:8]
    filename = f'{prefix}{content_hash}.js'
    bundle_path = os.path.join(JS_DIR, filename)

    # Short-circuit: a bundle of THIS content-hash already sits on disk (built
    # by us earlier or by a concurrent builder that won the flock). The hash is
    # over the exact bytes served, so an existing file is byte-identical — no
    # rebuild, no node-gate, no write. This makes concurrent builders converge
    # on the same artifact instead of racing to (re)create it.
    if os.path.exists(bundle_path):
        return filename, total_size

    # Atomic publish: write to a UNIQUE temp file in the SAME dir, gate THAT,
    # and only os.rename() it into the hash path on success. os.rename within a
    # dir is atomic, so a reader (node --check on a sibling worker, or a browser
    # fetch) never observes a partial write, and a failed gate deletes only the
    # private temp — never a hash path another process may be using. This kills
    # both the truncated-read SyntaxError and the MODULE_NOT_FOUND deletion race.
    tmp_fd = None
    tmp_path = None
    try:
        # Suffix MUST be .js: the node --check gate infers the module type from
        # the extension and hard-errors (ERR_UNKNOWN_FILE_EXTENSION) on .tmp.
        # The leading dot + random stem keep it private and out of the served
        # bundle set (_BUILT_BUNDLE_RE only matches bundle-/feature-<hash>.js).
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
    # (deferred) with no recovery, so DON'T publish it: drop the temp + None.
    ok, detail = _node_syntax_ok(tmp_path)
    if not ok:
        logger.critical('[Bundle] Built bundle %s FAILED syntax check — refusing to '
                        'serve it. Detail: %.500s', filename, detail)
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return None, 0

    # Another builder may have published the identical hash between our
    # existence check and now (they'd have written byte-identical content).
    # If so, adopt theirs and drop our temp — never rename over a file a peer
    # may already be serving.
    if os.path.exists(bundle_path):
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return filename, total_size

    try:
        os.rename(tmp_path, bundle_path)
    except OSError as e:
        # Lost the publish race (peer renamed first) → their file is identical.
        if os.path.exists(bundle_path):
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            return filename, total_size
        logger.error('[Bundle] Failed to publish bundle %s: %s', filename, e)
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return None, 0

    return filename, total_size


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

    t0 = time.time()

    with _build_lock():
        core_name, core_size = _assemble_bundle(_BUNDLE_FILES, 'bundle-', critical=True)
        if not core_name:
            return None

        # Deferred bundle — non-fatal. If it fails to build, ship core alone.
        feature_name, feature_size = _assemble_bundle(_DEFERRED_FILES, 'feature-', critical=False)

        _clean_old_bundles(core_name, feature_name)
        _bundle_filename = core_name
        _feature_filename = feature_name
        _bundle_mtime = _source_max_mtime()

    elapsed = time.time() - t0
    if feature_name:
        logger.info('[Bundle] Built %s (%d files, %dKB) + deferred %s (%d files, %dKB) in %.1fms',
                    core_name, len(_BUNDLE_FILES), core_size // 1024,
                    feature_name, len(_DEFERRED_FILES), feature_size // 1024, elapsed * 1000)
    else:
        logger.info('[Bundle] Built %s (%d files, %dKB minified) in %.1fms — no deferred bundle',
                    core_name, len(_BUNDLE_FILES), core_size // 1024, elapsed * 1000)
    return core_name


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
    """
    if not filename or not _BUILT_BUNDLE_RE.match(filename):
        return None
    if filename.startswith('bundle-'):
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
