"""The chat shell repaints default chrome after asynchronous i18n startup."""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit


_TITLE_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const conversations = [{id:'conv-default', title:'New Chat'}];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html:'<!doctype html><html lang="zh-CN"><body>'
    + '<div id="convList"></div><div id="topbarTitle">New Chat</div>'
    + '</body></html>',
  targets:[process.argv[2], process.argv[4]],
  globals:{
    activeConvId:'conv-default', conversations, paperMode:false,
    sidebarSearchQuery:'',
    t(key) {
      return ({
        'chat.newConversation':'新对话',
        'sidebar.dateToday':'今天',
        'sidebar.dateYesterday':'昨天',
      })[key] || key;
    },
    runtimeScope:{ConversationTurnRead:{ordered(){return [];}}},
    escapeHtml(value){return String(value ?? '');},
  },
});
global._conversationDisplayTitle = conversationDisplayTitle;
global.renderConversationList = window.renderConversationList = () => {};
check('sentinel title localized',
  conversationDisplayTitle('New Chat', '新对话') === '新对话');
check('real title unchanged',
  conversationDisplayTitle('Release notes', '新对话') === 'Release notes');
check('today localized', formatConvTime(Date.now()).includes('今天'));
window.dispatchEvent(new window.CustomEvent('tofu:language-change'));
check('active default topbar repainted',
  document.getElementById('topbarTitle').textContent === '新对话');
report();
"""


def test_default_conversation_title_and_date_are_presentation_localized():
    localization_bundle = native_module_path(
        'shell-localization.js',
        'frontend/src/conversation/presentation/shell-localization.ts',
    )
    run_harness(
        localization_bundle,
        _TITLE_HARNESS,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'conversation_list.js')],
        expect_pass=4,
        label='shell localization',
    )


def test_composer_chrome_repaints_when_the_locale_chunk_arrives():
    branch_source = runtime_section('main/conversation_turn_store.js')
    hint_source = runtime_section('main/main_folders_mobile.js')

    assert "t('chat.messagePlaceholder')" in branch_source
    assert (
        "'tofu:language-change', _renderNativeBranchComposerChrome"
        in branch_source
    )
    assert (
        "window.addEventListener('tofu:language-change', refreshInputSendHint)"
        in hint_source
    )
