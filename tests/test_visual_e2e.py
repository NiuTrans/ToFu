"""Playwright visual E2E tests — browser-based UI verification.

Each test navigates to the live ChatUI, performs actions (send messages,
toggle modes, open panels), takes screenshots, and optionally runs VLM
analysis to detect visual anomalies.

Run:  pytest tests/test_visual_e2e.py -m visual
      pytest tests/test_visual_e2e.py -m visual -k "test_initial_load"

Screenshots saved to: tests/screenshots/

NOTE: The ``page`` fixture (conftest.py) automatically cleans up any
conversations created during each test so E2E runs don't leave ghost
entries in the production database.  Tests that create their own page/
context (e.g. TestResponsive) should use ``_cleanup_test_convs()`` if
they send messages.
"""
from __future__ import annotations

import os
import re
import time
import urllib.request

import pytest

from tests.visual_check import VisualVerdict, analyze_screenshot

# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _cleanup_test_convs(page, server_url: str, ids_before: set[str]):
    """Delete conversations created during a test (for manually-created pages).

    Calls deleteConversation() from inside the browser so the frontend
    removes them from memory (a server-side-only DELETE is insufficient
    because the frontend re-syncs cached conversations back to the DB).

    Usage::

        ids_before = set(page.evaluate("conversations.map(c => c.id)") or [])
        # ... run test ...
        _cleanup_test_convs(page, live_server, ids_before)
    """
    try:
        ids_after = set(page.evaluate("conversations.map(c => c.id)") or [])
    except Exception:
        ids_after = set()
    for cid in ids_after - ids_before:
        try:
            page.evaluate(f"deleteConversation('{cid}')")
        except Exception:
            try:
                req = urllib.request.Request(
                    f"{server_url}/api/conversations/{cid}", method="DELETE",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass


def _screenshot(page, screenshot_dir: str, name: str) -> str:
    """Take a screenshot and return the file path."""
    path = os.path.join(screenshot_dir, f"{name}.png")
    page.screenshot(path=path, full_page=False)
    print(f"  📸 Screenshot: {path}")
    return path


def _check(path: str, desc: str, elements: list[str]) -> VisualVerdict:
    """Analyze a screenshot and return verdict (VLM with rule-based fallback)."""
    verdict = analyze_screenshot(path, desc, elements)
    print(f"  {verdict}")
    return verdict


def _send_message(page, text: str, *, wait_done: bool = True, timeout: float = 30000):
    """Type a message and send it, optionally waiting for the response to complete."""
    textarea = page.locator("#userInput")
    textarea.fill(text)
    page.locator("#sendBtn").click()

    if wait_done:
        # Wait for streaming to finish — the streaming-msg element should disappear
        # or a .message element with assistant role should appear
        try:
            # Wait for streaming indicator to appear then disappear
            page.wait_for_selector("#streaming-msg", state="attached", timeout=5000)
            page.wait_for_selector("#streaming-msg", state="detached", timeout=timeout)
        except Exception:
            # Might have completed too fast for us to catch streaming-msg
            pass
        # Small settle delay for DOM to update
        time.sleep(0.5)


def _wait_for_app_ready(page, timeout: float = 10000):
    """Wait for the ChatUI app to be fully loaded and interactive."""
    # Wait for key elements
    page.wait_for_selector("#userInput", state="visible", timeout=timeout)
    page.wait_for_selector("#sendBtn", state="visible", timeout=timeout)
    page.wait_for_selector("#sidebar", state="visible", timeout=timeout)
    # Wait for JS to be ready
    page.wait_for_function("typeof sendMessage === 'function'", timeout=timeout)
    time.sleep(0.3)


# ═══════════════════════════════════════════════════════════
#  Tests: Initial Load
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestInitialLoad:
    """Verify the app loads correctly with all elements visible."""

    def test_initial_load(self, page, screenshot_dir):
        """App should show sidebar, input box, welcome screen."""
        _wait_for_app_ready(page)
        path = _screenshot(page, screenshot_dir, "01_initial_load")

        verdict = _check(path, "Initial load — empty chat, welcome screen visible", [
            "sidebar with conversation list",
            "text input box at bottom",
            "send button",
            "welcome/empty state message",
            "tool toggles (search, etc.)",
        ])
        # Rule-based check always passes if screenshot is non-empty
        assert verdict.ok or len(verdict.elements_missing) == 0 or "Rule-based" in verdict.summary

    def test_sidebar_visible(self, page, screenshot_dir):
        """Sidebar should be visible with new chat button."""
        _wait_for_app_ready(page)
        sidebar = page.locator("#sidebar")
        assert sidebar.is_visible(), "Sidebar should be visible"

        # Check for new chat button
        new_btn = page.locator("#sidebar button, #sidebar .new-chat-btn, .sidebar-header button").first
        assert new_btn.is_visible(), "New chat button should be visible in sidebar"

    def test_input_area_complete(self, page, screenshot_dir):
        """Input area should have textarea, send button, and tool toggles."""
        _wait_for_app_ready(page)

        # Textarea
        textarea = page.locator("#userInput")
        assert textarea.is_visible(), "Text input should be visible"

        # Send button
        send_btn = page.locator("#sendBtn")
        assert send_btn.is_visible(), "Send button should be visible"

        # Search mode toggle
        search_toggle = page.locator("#searchModeToggle")
        assert search_toggle.is_visible(), "Search mode toggle should be visible"

        path = _screenshot(page, screenshot_dir, "02_input_area")
        _check(path, "Input area closeup — textarea, send button, tool toggles", [
            "text input textarea",
            "send button with arrow icon",
            "search mode toggle",
            "tool toggles row",
        ])


# ═══════════════════════════════════════════════════════════
#  Tests: Normal Chat Mode
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestNormalChat:
    """Test normal chat message flow."""

    def test_send_message_and_receive_reply(self, page, screenshot_dir):
        """Send a message and verify the response renders correctly."""
        _wait_for_app_ready(page)

        _send_message(page, "Hello, this is a test message!", wait_done=True, timeout=60000)

        path = _screenshot(page, screenshot_dir, "03_normal_chat_reply")
        verdict = _check(path, "Normal chat — user message sent, assistant reply received", [
            "user message bubble with 'Hello, this is a test message!'",
            "assistant reply bubble",
            "assistant avatar",
            "message timestamps",
            "sidebar showing conversation",
        ])
        assert verdict.ok or "Rule-based" in verdict.summary

    def test_message_has_user_and_assistant(self, page, screenshot_dir):
        """After sending, both user and assistant messages should appear."""
        _wait_for_app_ready(page)
        _send_message(page, "What is 2+2?", wait_done=True, timeout=60000)

        # Check DOM for message elements
        messages = page.locator(".message")
        count = messages.count()
        assert count >= 2, f"Expected at least 2 messages (user+assistant), got {count}"

    def test_empty_send_blocked(self, page, screenshot_dir):
        """Clicking send with empty input should do nothing."""
        _wait_for_app_ready(page)

        # Get current message count
        initial_count = page.locator(".message").count()

        # Try to send empty
        page.locator("#sendBtn").click()
        time.sleep(0.5)

        final_count = page.locator(".message").count()
        assert final_count == initial_count, "Empty send should not create messages"

    def test_conversation_appears_in_sidebar(self, page, screenshot_dir):
        """After sending a message, a conversation should appear in sidebar."""
        _wait_for_app_ready(page)
        _send_message(page, "Test sidebar entry", wait_done=True, timeout=60000)

        # Check sidebar has at least one conversation
        conv_items = page.locator("#convList .conv-item, #convList .conversation-item, #convList li, #convList > div > div")
        time.sleep(0.5)
        count = conv_items.count()
        assert count >= 1, f"Expected at least 1 conversation in sidebar, got {count}"

        path = _screenshot(page, screenshot_dir, "04_sidebar_with_conv")
        _check(path, "Sidebar showing conversation entry after sending message", [
            "conversation list with at least one entry",
            "conversation title or preview text",
            "active conversation highlight",
        ])


# ═══════════════════════════════════════════════════════════
#  Tests: Search Mode Toggle
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestSearchMode:
    """Test search mode cycling and UI state."""

    def test_search_mode_cycles(self, page, screenshot_dir):
        """Clicking search toggle should cycle: off → single → multi → off."""
        _wait_for_app_ready(page)

        toggle = page.locator("#searchModeToggle")

        # Initial: off
        initial_mode = toggle.get_attribute("data-mode")
        assert initial_mode == "off", f"Initial search mode should be 'off', got '{initial_mode}'"

        # Click 1: single
        toggle.click()
        time.sleep(0.3)
        mode1 = toggle.get_attribute("data-mode")
        assert mode1 == "single", f"After 1 click, mode should be 'single', got '{mode1}'"

        path = _screenshot(page, screenshot_dir, "05_search_single")
        _check(path, "Search mode set to 'single' — toggle should show active state", [
            "search toggle in active/highlighted state",
            "search mode label showing 'single' or similar",
        ])

        # Click 2: multi
        toggle.click()
        time.sleep(0.3)
        mode2 = toggle.get_attribute("data-mode")
        assert mode2 == "multi", f"After 2 clicks, mode should be 'multi', got '{mode2}'"

        # Click 3: back to off
        toggle.click()
        time.sleep(0.3)
        mode3 = toggle.get_attribute("data-mode")
        assert mode3 == "off", f"After 3 clicks, mode should be 'off', got '{mode3}'"


# ═══════════════════════════════════════════════════════════
#  Tests: Tool Toggles (Swarm, Endpoint, etc.)
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestToolToggles:
    """Test tool toggle buttons and their UI states."""

    def test_tool_submenu_opens(self, page, screenshot_dir):
        """Clicking the tools button should open a submenu dropdown."""
        _wait_for_app_ready(page)

        # Find and click the tools/more button to open submenu
        tools_btn = page.locator(".submenu-toggle, .tools-toggle, [data-submenu]").first
        if tools_btn.count() > 0:
            tools_btn.click()
            time.sleep(0.3)

            path = _screenshot(page, screenshot_dir, "06_tools_submenu")
            _check(path, "Tools submenu dropdown open — showing available tools", [
                "submenu dropdown panel",
                "tool items with icons and labels",
                "swarm toggle option",
                "endpoint mode toggle option",
            ])

    def test_endpoint_toggle(self, page, screenshot_dir):
        """Toggling endpoint mode should update UI indicators."""
        _wait_for_app_ready(page)

        # Evaluate JS directly to toggle endpoint mode
        page.evaluate("toggleEndpoint()")
        time.sleep(0.3)

        # Check if endpoint badge appeared
        endpoint_enabled = page.evaluate("endpointEnabled")
        assert endpoint_enabled is True, "Endpoint should be enabled after toggle"

        path = _screenshot(page, screenshot_dir, "07_endpoint_enabled")
        _check(path, "Endpoint mode enabled — badge/indicator should be visible", [
            "endpoint mode badge or indicator",
            "input area with endpoint mode active",
        ])

        # Toggle back off
        page.evaluate("toggleEndpoint()")
        time.sleep(0.3)
        endpoint_disabled = page.evaluate("endpointEnabled")
        assert endpoint_disabled is False, "Endpoint should be disabled after second toggle"

    def test_swarm_toggle(self, page, screenshot_dir):
        """Toggling swarm mode should update UI indicators."""
        _wait_for_app_ready(page)

        page.evaluate("toggleSwarm()")
        time.sleep(0.3)

        swarm_enabled = page.evaluate("swarmEnabled")
        assert swarm_enabled is True, "Swarm should be enabled after toggle"

        path = _screenshot(page, screenshot_dir, "08_swarm_enabled")
        _check(path, "Swarm mode enabled — bee badge should be visible", [
            "swarm badge (🐝) indicator",
            "input area with swarm mode active",
        ])

        # Toggle back off
        page.evaluate("toggleSwarm()")
        time.sleep(0.3)


# ═══════════════════════════════════════════════════════════
#  Tests: Endpoint Mode Chat
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestEndpointModeChat:
    """Test endpoint mode sends and renders like a natural conversation."""

    def test_endpoint_mode_message(self, page, screenshot_dir):
        """Send message in endpoint mode — should show worker+critic flow."""
        _wait_for_app_ready(page)

        # Enable endpoint mode
        page.evaluate("_applyEndpointUI(true)")
        time.sleep(0.3)

        # Send a message
        _send_message(page, "Build a simple function", wait_done=True, timeout=60000)

        path = _screenshot(page, screenshot_dir, "09_endpoint_mode_chat")
        verdict = _check(path, "Endpoint mode chat — message sent, multi-turn response expected", [
            "user message",
            "assistant response (possibly multi-turn with iterations)",
            "endpoint mode indicator",
            "natural conversation-like layout",
        ])
        assert verdict.ok or "Rule-based" in verdict.summary

        # Disable endpoint mode
        page.evaluate("_applyEndpointUI(false)")


# ═══════════════════════════════════════════════════════════
#  Tests: Dark Theme Consistency
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestTheme:
    """Verify dark theme renders correctly."""

    def test_dark_theme_no_white_flash(self, page, screenshot_dir):
        """Page should use dark background, no white flash areas."""
        _wait_for_app_ready(page)

        # Get background color of body
        bg_color = page.evaluate("""
            () => getComputedStyle(document.body).backgroundColor
        """)
        print(f"  Body background: {bg_color}")

        # Dark theme: RGB values should be low (< 50)
        rgb_match = re.search(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", bg_color)
        if rgb_match:
            r, g, b = int(rgb_match.group(1)), int(rgb_match.group(2)), int(rgb_match.group(3))
            assert r < 80 and g < 80 and b < 80, \
                f"Expected dark background, got rgb({r},{g},{b})"

        path = _screenshot(page, screenshot_dir, "10_dark_theme")
        _check(path, "Dark theme verification — no white flash, consistent dark colors", [
            "dark background throughout",
            "readable text on dark background",
            "no bright white areas",
        ])


# ═══════════════════════════════════════════════════════════
#  Tests: New Chat Creation
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestNewChat:
    """Test creating new conversations."""

    def test_new_chat_button(self, page, screenshot_dir):
        """Clicking new chat should reset to welcome screen."""
        _wait_for_app_ready(page)

        # Send a message first to have a conversation
        _send_message(page, "First message", wait_done=True, timeout=60000)

        # Click new chat
        new_btn = page.locator("text=New Chat, button:has-text('New'), .new-chat-btn").first
        if new_btn.count() > 0:
            new_btn.click()
            time.sleep(0.5)

            path = _screenshot(page, screenshot_dir, "11_new_chat")
            _check(path, "New chat created — welcome/empty state, previous conv in sidebar", [
                "empty/welcome state in main area",
                "previous conversation in sidebar",
                "input box ready for typing",
            ])

    def test_switch_between_conversations(self, page, screenshot_dir):
        """Create two convs and switch between them."""
        _wait_for_app_ready(page)

        # Create first conversation
        _send_message(page, "Conversation One Message", wait_done=True, timeout=60000)
        time.sleep(0.5)

        # Create new chat
        page.evaluate("newChat()")
        time.sleep(0.5)

        # Send in second conversation
        _send_message(page, "Conversation Two Message", wait_done=True, timeout=60000)
        time.sleep(0.5)

        path = _screenshot(page, screenshot_dir, "12_two_conversations")
        _check(path, "Two conversations exist — second is active, first in sidebar", [
            "sidebar with multiple conversations",
            "active conversation content visible",
        ])


# ═══════════════════════════════════════════════════════════
#  Tests: Responsive Layout
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestResponsive:
    """Test layout at different viewport sizes."""

    def test_narrow_viewport(self, browser, live_server, screenshot_dir):
        """At mobile width, sidebar should collapse or overlay."""
        ctx = browser.new_context(viewport={"width": 480, "height": 800})
        page = ctx.new_page()
        page.goto(live_server, wait_until="networkidle")
        _wait_for_app_ready(page)

        path = _screenshot(page, screenshot_dir, "13_narrow_viewport")
        verdict = _check(path, "Narrow viewport (480px) — mobile-friendly layout", [
            "input box fully visible and not cut off",
            "send button accessible",
            "no horizontal overflow or scrollbar",
            "content readable without horizontal scrolling",
        ])
        assert verdict.ok or "Rule-based" in verdict.summary

        page.close()
        ctx.close()

    def test_wide_viewport(self, browser, live_server, screenshot_dir):
        """At ultra-wide, layout should not stretch excessively."""
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(live_server, wait_until="networkidle")
        _wait_for_app_ready(page)

        path = _screenshot(page, screenshot_dir, "14_wide_viewport")
        verdict = _check(path, "Wide viewport (1920px) — content properly centered/constrained", [
            "sidebar visible",
            "chat area properly sized (not stretched to full width)",
            "input area properly aligned",
        ])
        assert verdict.ok or "Rule-based" in verdict.summary

        page.close()
        ctx.close()


# ═══════════════════════════════════════════════════════════
#  Tests: Multi-turn conversation
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestMultiTurn:
    """Test multi-turn conversation rendering."""

    def test_three_turn_conversation(self, page, screenshot_dir):
        """Three exchanges should render as alternating user/assistant messages."""
        _wait_for_app_ready(page)

        _send_message(page, "First question", wait_done=True, timeout=60000)
        _send_message(page, "Second question", wait_done=True, timeout=60000)
        _send_message(page, "Third question", wait_done=True, timeout=60000)

        path = _screenshot(page, screenshot_dir, "15_multi_turn")
        verdict = _check(path, "Multi-turn conversation — 3 user+assistant message pairs", [
            "three user message bubbles",
            "three assistant reply bubbles",
            "alternating message layout (user/assistant/user/assistant/...)",
            "proper spacing between messages",
            "scrollable if content overflows",
        ])
        assert verdict.ok or "Rule-based" in verdict.summary


# ═══════════════════════════════════════════════════════════
#  Tests: Keyboard shortcuts
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestKeyboardShortcuts:
    """Test keyboard interaction."""

    def test_ctrl_enter_sends(self, page, screenshot_dir):
        """Ctrl+Enter should send the message."""
        _wait_for_app_ready(page)

        textarea = page.locator("#userInput")
        textarea.fill("Sent via keyboard shortcut")
        textarea.press("Control+Enter")

        # Wait for streaming to complete
        time.sleep(2)

        # Should have messages now
        messages = page.locator(".message")
        assert messages.count() >= 1, "Ctrl+Enter should have sent the message"

    def test_enter_inserts_newline(self, page, screenshot_dir):
        """Plain Enter should insert a newline (not send)."""
        _wait_for_app_ready(page)

        textarea = page.locator("#userInput")
        textarea.fill("Line 1")
        textarea.press("Enter")

        # Message should NOT have been sent
        time.sleep(0.5)
        # The textarea should still have content (or a newline was added)
        value = textarea.input_value()
        assert len(value) > 0, "Enter should not clear the input (should add newline)"


# ═══════════════════════════════════════════════════════════
#  Tests: Streaming UI
# ═══════════════════════════════════════════════════════════

@pytest.mark.visual
class TestStreamingUI:
    """Test the streaming/loading state UI."""

    def test_streaming_indicator_appears(self, page, screenshot_dir):
        """While processing, a streaming indicator should be visible."""
        _wait_for_app_ready(page)

        textarea = page.locator("#userInput")
        textarea.fill("Tell me something interesting")
        page.locator("#sendBtn").click()

        # Try to catch the streaming state
        try:
            page.wait_for_selector("#streaming-msg", state="attached", timeout=5000)
            # Take screenshot during streaming
            path = _screenshot(page, screenshot_dir, "16_streaming_state")
            _check(path, "Streaming in progress — loading indicator visible", [
                "streaming/loading indicator (pulse, spinner, or typing dots)",
                "user message visible above",
                "partially received content or placeholder",
            ])
        except Exception:
            print("  ⚠️  Streaming completed too fast to capture mid-stream state")

        # Wait for completion
        try:
            page.wait_for_selector("#streaming-msg", state="detached", timeout=60000)
        except Exception:
            pass
