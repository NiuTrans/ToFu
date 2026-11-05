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
const roles = {'globalRouter.entry':'总负责人', 'routerWatchdog.entry':'故障巡检',
  'projectCoordination.coordinator':'项目负责人'};
const translate = key => roles[key] || key;
// The runtime prelude binds the catalog the same way.
global._conversationDisplayTitle = (title, fallback) => conversationDisplayTitle(title, fallback, translate);
global.renderConversationList = window.renderConversationList = () => {};
check('sentinel title localized',
  conversationDisplayTitle('New Chat', '新对话') === '新对话');
check('real title unchanged',
  conversationDisplayTitle('Release notes', '新对话') === 'Release notes');
check('server role titles localized',
  conversationDisplayTitle('Global Router', '新对话', translate) === '总负责人'
  && conversationDisplayTitle('Watchdog', '新对话', translate) === '故障巡检'
  && conversationDisplayTitle('Project coordinator', '新对话', translate) === '项目负责人');
check('role titles keep their stored text before the catalog loads',
  conversationDisplayTitle('Global Router', '新对话', key => key) === 'Global Router');
check('only exact role titles are mapped',
  conversationDisplayTitle('constructor', '新对话', translate) === 'constructor'
  && conversationDisplayTitle('Global Router notes', '新对话', translate) === 'Global Router notes');
check('today localized', formatConvTime(Date.now()).includes('今天'));
const topbar = document.getElementById('topbarTitle');
window.dispatchEvent(new window.CustomEvent('tofu:language-change'));
check('active default topbar repainted', topbar.textContent === '新对话');
conversations.push({id:'conv-router', title:'Global Router'}, {id:'conv-real', title:'Release notes'});
global.activeConvId = 'conv-router';
topbar.textContent = 'Global Router';
window.dispatchEvent(new window.CustomEvent('tofu:language-change'));
check('active role topbar repainted', topbar.textContent === '总负责人');
global.activeConvId = 'conv-real';
topbar.textContent = 'Renamed while open';
window.dispatchEvent(new window.CustomEvent('tofu:language-change'));
check('ordinary topbar left to its own writers', topbar.textContent === 'Renamed while open');
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
        expect_pass=9,
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
