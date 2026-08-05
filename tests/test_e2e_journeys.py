"""Critical-user-journey E2E — the browser 主干道巡检 (P0-3).

设计稿 docs/TESTING_STRATEGY.md §4:业界惯例是 10–50 条关键旅程守 release 闸,
不多养。本文件在 test_e2e_smoke.py 的 3 条(bundle 加载 / 发消息流式渲染 /
工具卡渲染)之上补齐主干道:中止 / 会话恢复 / 侧栏 / 多轮 / 键盘发送 /
新会话 / 主题持久化 / 设置弹窗 / 上传 chip。

Same hermetic contract as the smoke file: real app + real browser + STUB LLM
(the session fixture is imported — one install covers both files), and every
LLM path asserts the stub sentinel ADVANCED so a patch-miss fails loudly
instead of silently passing on real model output.

Journey discipline: each test is ONE user journey against the live DOM — no
re-implementation of render logic, no reaching into internals beyond the
in-memory ``conversations`` model (the same source of truth the UI renders
from). If a journey here breaks, a real user would have hit it.
"""
from __future__ import annotations

import base64
import re
import time

import pytest

# Reuse the smoke file's session stub fixture + helpers. The imported fixture
# object keeps its @pytest.fixture registration, so this module gets the same
# hermetic LLM/search stubs without duplication.
from tests.test_e2e_smoke import (  # noqa: F401  (fixture import is load-bearing)
    _SENTINEL,
    _install_llm_stubs,
    _wait_app_ready,
)

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


def _send_and_wait_done(page, text, expect_assistant=1, timeout=30000):
    """One full user turn: fill → click send → wait until the Nth stub reply
    is in the conversations model — and PROVE the stub (not a real model)
    answered. ``expect_assistant`` must track the turn number in multi-turn
    journeys, else the wait is satisfied by the PREVIOUS turn's reply and the
    hermetic guard fires before this turn's stream even starts."""
    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill(text)
    page.locator('#sendBtn').click()
    page.wait_for_function(
        """(n) => {
            if (typeof conversations === 'undefined') return false;
            const c = conversations.find(c => c.id === activeConvId);
            if (!c) return false;
            return c.messages.filter(m => m.role === 'assistant'
                && (m.content || '').includes('stubbed model')).length >= n;
        }""",
        arg=expect_assistant,
        timeout=timeout)
    assert _SENTINEL['stream_calls'] > calls_before, (
        'stream_llm_response stub never ran — a real model may have streamed; '
        'the journey is no longer hermetic.'
    )


def _fresh_chat(page):
    _wait_app_ready(page)
    page.evaluate("newChat()")
    time.sleep(0.4)


# ─── 主干道 1: 中止 ─────────────────────────────────────────────────────

def test_abort_halts_stream_and_keeps_partial(page):
    """Journey: 用户点了停止——流必须真的停、按钮弹回发送态、已流出的部分保留。

    Uses the stub's __e2e_slow__ branch (60 words × 50ms) so the abort click
    lands mid-stream deterministically."""
    _fresh_chat(page)
    page.locator('#userInput').fill('__e2e_slow__ stream please')
    page.locator('#sendBtn').click()
    # the send button must flip to its stop state while streaming
    page.wait_for_selector('#sendBtn.stop-btn', state='visible', timeout=10000)
    # let a few words land, then abort via the SAME button
    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('slow03')",
        timeout=10000)
    page.locator('#sendBtn').click()
    # button must return to send state once the abort lands
    page.wait_for_function(
        "!document.querySelector('#sendBtn').classList.contains('stop-btn')",
        timeout=10000)
    text = page.inner_text('#chatInner')
    idxs = [int(m.group(1)) for m in re.finditer(r'slow(\d\d)', text)]
    assert idxs, f'no partial stream content kept after abort:\n{text[:300]}'
    assert max(idxs) < 55, (
        f'stream ran to (near) completion despite the abort — last word '
        f'slow{max(idxs):02d} of slow59')
    # and it must not resume growing after the abort settled
    later = page.inner_text('#chatInner')
    idxs_later = [int(m.group(1)) for m in re.finditer(r'slow(\d\d)', later)]
    assert max(idxs_later or [0]) <= max(idxs) + 2, (
        'the stream kept growing after the abort settled')


# ─── 主干道 2: 会话恢复(刷新后历史还在) ──────────────────────────────

def test_reload_restores_conversation(page):
    """Journey: 用户刷新页面——会话仍在侧栏,点开后历史完整渲染。"""
    _fresh_chat(page)
    _send_and_wait_done(page, 'Hello reload E2E')
    conv_id = page.evaluate("activeConvId")
    page.reload()
    _wait_app_ready(page)
    item = f'.conv-item[data-conv-id="{conv_id}"]'
    page.wait_for_selector(item, state='attached', timeout=15000)
    page.locator(item).click()
    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('stubbed model')",
        timeout=15000)
    assert 'Hello reload E2E' in page.inner_text('#chatInner'), (
        'user message missing after reload-restore'
    )


# ─── 主干道 3: 侧栏出现新会话 ──────────────────────────────────────────

def test_sidebar_lists_new_conversation(page):
    """Journey: 发完一条消息,新会话出现在侧栏列表且可点回。"""
    _fresh_chat(page)
    _send_and_wait_done(page, 'Hello sidebar E2E')
    conv_id = page.evaluate("activeConvId")
    page.wait_for_selector(
        f'.conv-item[data-conv-id="{conv_id}"]', state='attached', timeout=15000)


# ─── 主干道 4: 多轮对话 ────────────────────────────────────────────────

def test_multi_turn_both_turns_render(page):
    """Journey: 同一会话连发两轮,两轮的用户/助手消息都在。"""
    _fresh_chat(page)
    _send_and_wait_done(page, 'turn one E2E', expect_assistant=1)
    _send_and_wait_done(page, 'turn two E2E', expect_assistant=2)
    page.wait_for_function(
        """() => {
            const c = conversations.find(c => c.id === activeConvId);
            return c && c.messages.filter(m => m.role === 'assistant'
                && (m.content || '').includes('stubbed model')).length >= 2;
        }""",
        timeout=15000)
    body = page.inner_text('#chatInner')
    assert 'turn one E2E' in body and 'turn two E2E' in body


# ─── 主干道 5: 键盘 Enter 发送 ─────────────────────────────────────────

def test_enter_key_sends_message(page):
    """Journey: 用户按 Enter(默认发送模式)即发送,无需点按钮。"""
    _fresh_chat(page)
    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill('Hello enter E2E')
    page.keyboard.press('Enter')
    page.wait_for_function(
        """() => {
            const c = conversations.find(c => c.id === activeConvId);
            return c && c.messages.some(m => m.role === 'assistant'
                && (m.content || '').includes('stubbed model'));
        }""",
        timeout=30000)
    assert _SENTINEL['stream_calls'] > calls_before, (
        'Enter path never reached the stub — keyboard send is broken'
    )


# ─── 主干道 6: 新会话清空视图 ──────────────────────────────────────────

def test_new_chat_clears_chat_view(page):
    """Journey: 用户点新会话——上一轮内容从聊天视图消失。"""
    _fresh_chat(page)
    _send_and_wait_done(page, 'Hello clear E2E')
    assert 'stubbed model' in page.inner_text('#chatInner')
    page.evaluate("newChat()")
    time.sleep(0.5)
    assert 'stubbed model' not in page.inner_text('#chatInner'), (
        'previous conversation still visible after newChat()'
    )


# ─── 主干道 7: 主题设置持久化 ──────────────────────────────────────────

def test_theme_persists_across_reload(page):
    """Journey: 用户换主题——data-theme 立即生效、写 localStorage、刷新后启动
    路径(applyTheme(_getCurrentTheme()))还原同一主题。"""
    _wait_app_ready(page)
    before = page.evaluate("document.documentElement.getAttribute('data-theme')")
    page.evaluate("cycleTheme()")
    chosen = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert chosen != before, 'cycleTheme() did not change data-theme'
    assert page.evaluate("localStorage.getItem('claude_ui_theme')") == chosen, (
        'theme not written to localStorage'
    )
    page.reload()
    _wait_app_ready(page)
    restored = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert restored == chosen, (
        f'theme not restored at boot: chose {chosen}, got {restored} after reload'
    )


# ─── 主干道 8: 设置弹窗开合 ────────────────────────────────────────────

def test_settings_modal_opens_and_closes(page):
    """Journey: 用户打开设置——弹窗渲染;点关闭——弹窗收起。"""
    _wait_app_ready(page)
    page.locator('button[onclick="openSettings()"]').first.click()
    page.wait_for_selector('#settingsModal.open', timeout=10000)
    assert page.locator('#settingsModal.open').count() == 1, (
        'settings modal did not render in open state')
    page.locator('.settings-close-btn').first.click()
    page.wait_for_function(
        "!document.getElementById('settingsModal').classList.contains('open')",
        timeout=5000)
    assert page.locator('#settingsModal.open').count() == 0, (
        'settings modal still open after the close button')


# ─── 主干道 9: 上传附件 chip ───────────────────────────────────────────

def test_upload_image_chip_renders(page, tmp_path):
    """Journey: 用户选了一张图片——预览 chip 立即渲染(optimistic preview)。"""
    _fresh_chat(page)
    png = tmp_path / 'tiny.png'
    png.write_bytes(_TINY_PNG)
    page.set_input_files('#fileInput', str(png))
    page.wait_for_selector('.img-preview', state='attached', timeout=10000)
    assert page.locator('.img-preview').count() >= 1, (
        'no attachment preview chip rendered after file selection')
