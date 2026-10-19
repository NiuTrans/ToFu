"""Chromium attribution/order regression; no model calls or production writes.

After ``npm run build:frontend`` in this worktree, run::

    .venv/bin/python -m pytest -q \
        tests/visual/test_visual_translation_compaction_attribution.py --tb=short

Needs shared Node/Python dependencies, cached Chromium, and a matching native
TofuDB engine for ``live_server`` (tests/conftest.py). Prepare it through
``.venv/bin/python -m serverctl_pkg.native_engine --project "$PWD"`` if needed;
never bypass its source-identity check or target a production server. For CJK
screenshots this host's cached conda fontconfig can be selected with
``FONTCONFIG_FILE="$(dirname "$(dirname "$(command -v fc-list)")")/etc/fonts/fonts.conf"``.
Run with ``-k 'not real_app'`` for the native-independent source visual tier.
The first test boots the real local Hypercorn app and shipped frontend. Only catalog/snapshot/SSE inputs
are synthetic; mutation HTTP requests and off-origin requests are blocked.
This verifies presentation/replay, NOT backend receipt or provider execution.
The second uses the established source-build visual tier for the composer owner
(the production instance is private). Screenshots under tests/visual/output
are review evidence, not golden baselines. No existing test files are changed.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401 — pytest fixture

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "visual" / "output"
CONVERSATION = "visual-translation-compaction-fixture"
NOW = 1_790_000_000_000


def _turn(turn_id, ordinal, actor, projection):
    return {
        "turnId": turn_id, "presentationId": turn_id,
        "conversationId": CONVERSATION, "laneId": "main", "ordinal": ordinal,
        "actor": actor, "kind": "message", "runId": "visual-fixture-run",
        "status": "completed", "currentAttemptId": None,
        "projectionRevision": 1, "projection": projection,
        "settlement": {"outcome": "completed", "cause": "completed", "evidence": "provider_finish"},
        "createdAt": NOW + ordinal, "updatedAt": NOW + ordinal,
    }


def _compaction(entry_id, *, historical=False):
    entry = {
        "id": entry_id, "spanId": entry_id, "seq": 1, "occurredAt": NOW,
        "kind": "status", "status": "succeeded", "severity": "info",
        "reasonCode": "context_compaction", "summary": "Context compressed",
        "summaryKey": "activity.compaction.succeeded", "archiveId": "visual-archive",
        "taskId": "visual-main-task", "attemptId": "visual-main-attempt",
        "llmRound": 0, "trigger": "dispatch_guard", "phase": "preflight",

        "detail": "final rendered prompt 84,000 tokens exceeds the admissible ceiling",
        "tokensBefore": 1317447, "tokensAfter": 49648,
        "messagesBefore": 32, "messagesAfter": 6, "durationMs": 1200, "count": 1,
        # Historical model describes main execution, never summary evidence.
        "model": "Sol",
    }
    if not historical:
        entry.update(summaryModel="Luna", taskModel="Sol", anchorPosition="producer-start")
    else:
        entry.pop("phase")
        entry.pop("trigger")
    return entry


def _snapshot():
    human = _turn("visual-input", 0, "human", {
        "content": "Please explain the result.", "originalContent": "请解释结果。",
        "translation": {"status": "completed", "model": "Luna"},
        "segments": [{"type": "text", "blockId": "text:input",
                      "text": "Please explain the result.", "terminal": True, "deliverable": True}],
    })
    turns = [human]
    for turn_id, ordinal, content, translated, historical in [
        ("visual-historical", 1, "Historical answer without a translation receipt.",
         "没有翻译模型记录的历史答复。", True),
        ("visual-answer", 2, "The answer follows preflight compression.",
         "答复位于预检压缩之后。", False),
    ]:
        projection = {
            "content": content, "translatedContent": translated, "model": "Sol",
            "segments": [{"type": "text", "blockId": f"text:{turn_id}", "text": content,
                          "terminal": True, "deliverable": True, "llmRound": 0,
                          "taskId": "visual-main-task", "attemptId": "visual-main-attempt"}],
            "activityTimeline": {"blockId": "activity-timeline", "version": 1,
                                 "entries": [_compaction(f"{turn_id}-compaction", historical=historical)]},
        }
        if not historical:
            projection["translation"] = {"status": "completed", "model": "Luna"}
        turns.append(_turn(turn_id, ordinal, "assistant", projection))
    return {
        "ok": True, "contract": "tofu.conversation-sync.snapshot/v1",
        "scope": {"kind": "conversation", "ownerId": "visual-owner", "threadId": CONVERSATION},
        "conversationId": CONVERSATION, "conversationRevision": 1,
        "syncSeq": 1, "cursor": "visual-cursor-1", "serverBootId": "visual-boot",
        "heartbeatIntervalMs": 15000, "settings": {"model": "Sol", "autoTranslate": False},
        "turns": turns, "attempts": [], "queueItems": [],
    }


def _install_fixture(page, origin, language, theme):
    snapshot = _snapshot()
    blocked_mutations = []
    origin_parts = urlsplit(origin)

    def intercept(route):
        request = route.request
        parts = urlsplit(request.url)
        if (parts.scheme, parts.netloc) != (origin_parts.scheme, origin_parts.netloc):
            route.abort()
        elif parts.path == "/api/v4/logs/client" and request.method == "POST":
            # Diagnostics are not conversation/configuration mutations. Keep
            # them local rather than writing even the disposable app's logs.
            route.fulfill(json={"ok": True})
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            blocked_mutations.append(f"{request.method} {parts.path}")
            route.fulfill(status=403, json={"ok": False, "error": "Visual fixture forbids writes"})
        elif parts.path == "/api/v4/conversations":
            route.fulfill(headers={"X-Total-Count": "1"}, json={"ok": True, "items": [{
                "id": CONVERSATION, "title": "Translation and compaction fixture",
                "settings": {"model": "Sol", "autoTranslate": False},
                "createdAt": NOW, "updatedAt": NOW, "messageCount": 3, "rev": 1,
            }]})
        elif parts.path == f"/api/v4/conversations/{CONVERSATION}/sync":
            route.fulfill(json=snapshot)
        elif parts.path.startswith(f"/api/v4/conversations/{CONVERSATION}"):
            route.fulfill(status=404, json={"ok": False, "error": "Not in bounded fixture"})
        else:
            route.continue_()

    page.route("**/*", intercept)
    # Real hydration/decoder/reducer; only the fixture's stream transport changes.
    script = """(({id, language, theme}) => {
      localStorage.setItem('tofu_ui_lang', language);
      localStorage.setItem('claude_ui_theme', theme);
      sessionStorage.setItem('tofu_activeConvId', id);
      const NativeEventSource = window.EventSource;
      window.__visualConversationSources = [];
      window.EventSource = class FixtureEventSource extends EventTarget {
        constructor(url, options) {
          super();
          if (!String(url).includes('/conversations/' + id + '/events')) {
            return new NativeEventSource(url, options);
          }
          this.url = String(url); this.readyState = 1;
          window.__visualConversationSources.push(this);
          setTimeout(() => { this.onopen?.(new Event('open')); this.dispatchEvent(new Event('open')); }, 0);
        }
        close() { this.readyState = 2; }
      };
      window.__emitVisualConversationEvent = (event) => {
        const source = window.__visualConversationSources.findLast(item => item.readyState === 1);
        if (!source) throw new Error('Real app did not subscribe to fixture conversation');
        source.dispatchEvent(new MessageEvent(event.type, {
          data: JSON.stringify(event), lastEventId: 'visual-cursor-' + event.syncSeq,
        }));
      };
    })"""
    page.add_init_script(script + f"({json.dumps({'id': CONVERSATION, 'language': language, 'theme': theme})});")
    return snapshot, blocked_mutations


def _order_and_attribution(page, language):
    answer = page.locator('[data-turn-id="visual-answer"]')
    historical = page.locator('[data-turn-id="visual-historical"]')

    # Completed-turn process cards are materialized only when history is opened.
    for turn in (answer, historical):
        process = turn.locator('details.conversation-history-process')
        if process.count() and not process.evaluate('node => node.open'):
            process.locator(':scope > summary').click()
    page.wait_for_function("""() => document.querySelector(
      '[data-turn-id="visual-answer"] .conversation-block--text .bilingual-attribution')
    """)
    translation_label = "翻译模型：Luna" if language == "zh" else "Translated by Luna"
    summary_label = "压缩模型：Luna" if language == "zh" else "Summary model: Luna"
    main_label = "主模型：Sol" if language == "zh" else "Main model: Sol"
    badge = answer.locator('.conversation-block--text > .bilingual-attribution')
    assert badge.is_visible()
    assert badge.inner_text() == translation_label
    assert answer.locator('.conversation-block--text .bilingual-model').count() == 1
    footer_text = answer.locator('.conversation-turn-footer').inner_text()
    assert 'Sol' in footer_text and 'Luna' not in footer_text
    card = answer.locator('.activity-event--compaction')
    assert card.is_visible()
    assert summary_label in card.inner_text()
    assert main_label not in card.inner_text()
    assert 'final rendered prompt' not in card.inner_text()
    assert ("发送前请求过大" if language == "zh" else "Request too large before sending") in card.inner_text()
    diagnostics = card.locator('details.activity-event__compaction-details')

    original_focus = page.evaluate_handle('document.activeElement')
    diagnostics.locator('summary').focus()
    page.keyboard.press('Enter')
    assert diagnostics.evaluate('node => node.open')
    assert main_label in card.inner_text()
    assert 'final rendered prompt' in card.inner_text()
    page.keyboard.press('Space')
    assert not diagnostics.evaluate('node => node.open')

    original_focus.evaluate('node => { if (node?.isConnected) node.focus(); }')
    original_focus.dispose()
    assert card.bounding_box()['height'] < (145 if page.viewport_size['width'] < 600 else 95)
    assert card.evaluate('node => node.scrollWidth <= node.clientWidth + 1')

    assert card.locator('[data-conversation-action="open-compaction"]').get_attribute('data-operation') == 'visual-archive'
    # Actual Chromium geometry as well as DOM chronology.
    assert card.bounding_box()["y"] < answer.locator('.conversation-block--text .md-content').first.bounding_box()["y"]
    assert answer.evaluate("""node => {
      const card = node.querySelector('.activity-event--compaction');
      const prose = node.querySelector('.conversation-block--text');
      return !!(card.compareDocumentPosition(prose) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    historical_text = historical.locator('.conversation-block--text .bilingual-attribution').inner_text()
    assert "Luna" not in historical_text and "Sol" not in historical_text
    assert historical_text == ("翻译模型未报告" if language == "zh" else "Translation model not reported")
    historical_card = historical.locator('.activity-event--compaction')
    assert summary_label not in historical_card.inner_text()
    assert ("压缩模型：Sol" if language == "zh" else "Summary model: Sol") not in historical_card.inner_text()
    assert ("压缩模型未记录" if language == "zh" else "Summary model not recorded") in historical_card.inner_text()
    return answer


@pytest.mark.parametrize("language,theme,viewport", [
    ("en", "light", {"width": 1440, "height": 1050}),
    ("zh", "dark", {"width": 1440, "height": 1050}),
    ("en", "dark", {"width": 390, "height": 1000}),
    ("zh", "light", {"width": 390, "height": 1000}),
])
def test_real_app_translation_and_preflight_replay(browser, live_server, language, theme, viewport):
    """Local shipped app + synthetic SSE upsert + authoritative reload."""
    context = browser.new_context(viewport=viewport, device_scale_factor=1)
    page = context.new_page()
    page.set_default_timeout(15000)
    snapshot, blocked_mutations = _install_fixture(page, live_server, language, theme)

    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.goto(live_server, wait_until="domcontentloaded")

        page.wait_for_function("() => window.TofuModules?.version === 3")
        item = page.locator(f'.conv-item[data-conv-id="{CONVERSATION}"]').first
        item.wait_for(state="attached")
        # Invoke the real sidebar handler even when the narrow rail is closed.
        item.evaluate("node => node.click()")
        _order_and_attribution(page, language)
        updated = copy.deepcopy(snapshot["turns"][-1])
        updated["projectionRevision"] = 2
        updated["projection"]["content"] += " Live update verified."
        updated["projection"]["translatedContent"] += " 实时更新已验证。"
        event = {
            "contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
            "conversationId": CONVERSATION, "syncSeq": 2, "occurredAt": NOW + 10,
            "payload": {"conversationRevision": 2, "turns": [updated], "attempts": []},
        }
        page.evaluate("event => window.__emitVisualConversationEvent(event)", event)
        page.wait_for_function("""() => document.querySelector(
          '[data-turn-id="visual-answer"] .conversation-block--text .md-content')?.textContent.includes('实时更新已验证')
        """)
        answer = _order_and_attribution(page, language)
        fold = answer.locator('details.bilingual-block')
        summary = fold.locator('summary')
        summary.focus()
        page.keyboard.press("Enter")
        assert fold.evaluate("node => node.open")
        assert summary.locator('.bilingual-model').count() == 0
        page.keyboard.press("Space")
        assert not fold.evaluate("node => node.open")
        assert page.locator('.composer-translation-status').count() == 0
        OUT.mkdir(parents=True, exist_ok=True)
        stem = f"translation-compaction-{language}-{theme}-{viewport['width']}"
        answer.screenshot(path=str(OUT / f"{stem}-live.png"), animations="disabled")
        snapshot.update(conversationRevision=2, syncSeq=2, cursor="visual-cursor-2")
        snapshot["turns"][-1] = updated
        page.reload(wait_until="domcontentloaded")

        page.wait_for_function("() => window.TofuModules?.version === 3")
        item = page.locator(f'.conv-item[data-conv-id="{CONVERSATION}"]').first
        item.wait_for(state="attached")
        item.evaluate("node => node.click()")
        answer = _order_and_attribution(page, language)
        assert not answer.locator('details.bilingual-block').evaluate("node => node.open")
        answer.screenshot(path=str(OUT / f"{stem}-reload.png"), animations="disabled")
        assert not blocked_mutations, f"Unexpected app write attempts: {blocked_mutations}"

        assert not page_errors, page_errors
    finally:
        context.close()


def _composer_harness(tmp_path):
    bundle = tmp_path / "composer-status.js"
    result = subprocess.run([
        str(ROOT / "scripts/vite_test_bundle.mjs"),
        str(ROOT / "frontend/src/conversation/application/composer-translation-status.ts"),
        "--bundle", "--format=iife", "--platform=browser",
        "--global-name=TranslationStatusOwner", f"--outfile={bundle}",
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return bundle.read_text()


@pytest.mark.parametrize("language,theme", [("en", "light"), ("zh", "dark")])
def test_visual_successful_composer_translation_clears(browser, frontend_visual_origin, tmp_path, language, theme):
    """Visual source tier: pending is visible, success has no stale status."""
    harness = _composer_harness(tmp_path)
    labels = json.loads((ROOT / f"frontend/src/i18n/locales/{language}.json").read_text())
    context = browser.new_context(viewport={"width": 800, "height": 300}, device_scale_factor=1)
    page = context.new_page()
    try:
        page.goto(frontend_visual_origin, wait_until="load")
        page.add_script_tag(content=harness)
        page.evaluate("""({language,theme,labels}) => {
          document.documentElement.lang=language;
          document.documentElement.dataset.theme=theme;
          document.body.innerHTML = `<main class="input-area" style="position:static;max-width:700px;margin:40px auto">
            <div class="input-box"><textarea aria-label="Composer"></textarea></div></main>`;
          window.__translationFixture = TranslationStatusOwner.createComposerTranslationStatus({
            document, activeConversationId: () => 'fixture', now: () => 0,
            label: key => labels[key] || key,
          });
          window.__translationToken = window.__translationFixture.begin('fixture', 'send');
        }""", {"language": language, "theme": theme, "labels": labels})
        status = page.locator('.composer-translation-status')
        assert status.is_visible()
        assert status.locator('.composer-translation-phase').inner_text() == labels['translationWait.send']
        OUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT / f"translation-compaction-composer-{language}-{theme}-pending.png"), animations="disabled")
        page.evaluate("() => window.__translationFixture.finish(window.__translationToken, 'completed', 'Luna')")
        assert status.count() == 0
        page.evaluate("() => window.__translationFixture.destroy()")
        page.screenshot(path=str(OUT / f"translation-compaction-composer-{language}-{theme}-ready.png"), animations="disabled")
    finally:
        context.close()


@pytest.mark.parametrize("language,theme,width", [("en", "light", 1440), ("zh", "dark", 390)])
def test_visual_translation_compaction_owner_graph(browser, frontend_visual_origin, tmp_path, language, theme, width):
    """Visual tier stays runnable even when native app prerequisites are absent.

    Uses the actual surface controller, view model and classic renderers with
    bounded plain-text Markdown/localization ports, not a backend substitute.
    """
    bundle = tmp_path / "conversation-owner.js"
    result = subprocess.run([
        str(ROOT / "scripts/vite_test_bundle.mjs"),
        str(ROOT / "frontend/src/conversation/index.ts"), "--bundle", "--format=iife",
        "--platform=browser", "--global-name=ConversationOwner", f"--outfile={bundle}",
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    labels = json.loads((ROOT / f"frontend/src/i18n/locales/{language}.json").read_text())
    page = browser.new_page(viewport={"width": width, "height": 1050}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(frontend_visual_origin, wait_until="load")
        page.add_script_tag(content=bundle.read_text())
        page.evaluate(r"""({snapshot,labels,language,theme}) => {
          document.documentElement.lang=language;
          document.documentElement.dataset.theme=theme;
          document.body.innerHTML='<main id="fixture" style="width:100%;max-width:800px;padding:16px;box-sizing:border-box;margin:auto"></main>';
          const localizedText=(key,fallback,values={}) => (labels[key]||fallback)
            .replace(/\{(\w+)\}/g, (match,name)=>values[name]??match);
          const renderers=ConversationOwner.createClassicConversationRenderers({
            localizedText,
            renderSafeMarkdownHtml: text => {
              const p=document.createElement('p');p.textContent=text;return p.outerHTML;
            },
          });
          const state={conversationId:snapshot.conversationId, conversationRevision:1,
            transport:'live', turnsById:Object.fromEntries(snapshot.turns.map(turn=>[turn.turnId,turn])),
            laneOrder:{main:snapshot.turns.map(turn=>turn.turnId)}, attemptsById:{},
            queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
          const controller=ConversationOwner.createConversationSurfaceController({
            isActive:id=>id===snapshot.conversationId,
            getContainer:()=>document.getElementById('fixture'), nativeRenderers:renderers,
            schedule:callback=>{const id=requestAnimationFrame(callback);return()=>cancelAnimationFrame(id);},
          });
          window.__ownerFixture={controller,state,snapshot};
          controller.render({id:snapshot.conversationId},state);
        }""", {"snapshot": _snapshot(), "labels": labels, "language": language, "theme": theme})
        answer = _order_and_attribution(page, language)
        # Original-display mode puts one model beside Translation/译文, never
        # on the main execution footer or twice on the translated answer.
        human = page.locator('[data-turn-id="visual-input"]')
        assert human.locator('.bilingual-attribution').count() == 0
        assert human.locator('.bilingual-header .bilingual-model').inner_text() == (
            "翻译模型：Luna" if language == "zh" else "Translated by Luna")
        assert human.locator('.bilingual-model').count() == 1
        assert human.locator('.bilingual-header .bilingual-label').inner_text() == (
            "译文" if language == "zh" else "Translation")
        fold = answer.locator('details.bilingual-block')
        fold.locator('summary').focus()
        page.keyboard.press("Enter")
        assert fold.evaluate("node=>node.open")
        page.evaluate("""() => {
          const f=window.__ownerFixture;
          const turn=structuredClone(f.state.turnsById['visual-answer']);
          turn.projectionRevision++;
          turn.projection.translatedContent+=' 实时更新已验证。';
          f.state={...f.state,conversationRevision:2,
            turnsById:{...f.state.turnsById,[turn.turnId]:turn}};
          f.controller.render({id:f.snapshot.conversationId},f.state);
        }""")
        page.wait_for_function("""()=>document.querySelector(
          '[data-turn-id="visual-answer"] .conversation-block--text > .md-content')?.textContent.includes('实时更新已验证')""")
        _order_and_attribution(page, language)
        assert fold.evaluate("node=>node.open"), "Live commit must preserve keyboard-open fold"
        assert fold.locator('summary').evaluate("node=>node===document.activeElement")
        page.keyboard.press("Space")
        assert not fold.evaluate("node=>node.open")
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        OUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT / f"translation-compaction-owner-{language}-{theme}-{width}.png"), full_page=True, animations="disabled")
        page.evaluate("() => window.__ownerFixture.controller.dispose()")
        assert page.locator('[data-turn-id="visual-answer"]').count() == 0
        assert not errors, errors
    finally:
        page.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
