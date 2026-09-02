"""Composer stop behavior over authoritative attempt identities."""

from __future__ import annotations

from functools import lru_cache
import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEND_BUTTON = runtime_section_path('ui/send_button.js')
INPUT_HANDLING = runtime_section_path('main/main_input_handling.js')
COMPOSER_CONTROLS = os.path.join(
    ROOT, 'frontend', 'src', 'conversation', 'ui',
    'composer-send-controls.ts',
)
COMPOSER_CSS = os.path.join(
    ROOT, 'frontend', 'src', 'styles', 'application',
    '02-messages-composer.css',
)
TOFU_CSS = os.path.join(
    ROOT, 'frontend', 'src', 'styles', 'application',
    '11-tofu-theme-foundation.css',
)
MOBILE_CSS = os.path.join(
    ROOT, 'frontend', 'src', 'styles', 'application',
    '15-queue-mobile-my-day.css',
)
_NODE = shutil.which('node')
_ESBUILD = os.path.isfile(os.path.join(ROOT, 'node_modules', '.bin', 'esbuild'))


@lru_cache(maxsize=1)
def _composer_controls_bundle() -> str:
    return native_module_path('composer-send-controls.js', COMPOSER_CONTROLS)

_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.console = {log(){}, warn(){}, error(){}, info(){}, debug(){}};
const timers = [];
global.setTimeout = (fn) => { timers.push(fn); return timers.length; };

class FakeTextArea {
  constructor() {
    this.tagName = 'TEXTAREA';
    this.value = '';
    this.listeners = new Map();
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  emit(name) {
    for (const listener of this.listeners.get(name) || []) {
      listener({type:name, target:this});
    }
  }
  listenerCount(name) { return (this.listeners.get(name) || []).length; }
}
global.HTMLTextAreaElement = FakeTextArea;

function makeClassList() {
  const names = new Set();
  return {
    add(name) { names.add(name); },
    remove(name) { names.delete(name); },
    contains(name) { return names.has(name); },
  };
}
function makeButton(id) {
  const attributes = Object.create(null);
  return {
    id,
    tagName:'BUTTON',
    type:'button',
    className:'',
    innerHTML:'',
    title:'',
    hidden:false,
    style:{},
    onclick:null,
    parentElement:null,
    setAttribute(name, value) { attributes[name] = String(value); },
    getAttribute(name) { return attributes[name] ?? null; },
    removeAttribute(name) { delete attributes[name]; },
  };
}
const elements = Object.create(null);
const actionRight = {
  classList:makeClassList(),
  children:[],
  insertBefore(child, reference) {
    const index = this.children.indexOf(reference);
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
    child.parentElement = this;
    elements[child.id] = child;
  },
};
const button = makeButton('sendBtn');
button.parentElement = actionRight;
actionRight.children.push(button);
elements.sendBtn = button;
const composerInput = new FakeTextArea();
elements.userInput = composerInput;
global.document = {
  getElementById: (id) => elements[id] || null,
  createElement: (tagName) => tagName.toLowerCase() === 'button'
    ? makeButton('') : null,
};
global.renderConversationList = () => {};
let sendCalls = 0;
global.sendMessage = () => { sendCalls += 1; };
const labels = {
  'orch.ai.send':'Send',
  'paper.send':'Send (Enter)',
  'branch.stopGen':'Stop generating',
  'stop.stopping':'Stopping…',
};
global.t = (key) => labels[key] || key;
global.pendingImages = [];
global.pendingPdfTexts = [];
global.pendingVideos = [];
const toasts = [];
global.showToast = (message, kind) => toasts.push({message, kind});
let current = null;
global.getActiveConv = () => current;
global.convIsBusy = (conv) => Boolean(conv && (conv._activeAttemptId
  || conv._activeBranchAttemptIds?.size || conv._forceBusy));
global._dispatchableQueueCount = (convId) => convId === 'main' ? 2 : 0;
const agentLocks = [];
global._setAgentModeLocked = (busy) => agentLocks.push(busy);
let abortConvCalls = 0;
const abortedAttempts = [];
const hydrated = [];
global.Api = {chat:{
  abortConv: async () => { abortConvCalls += 1; },
}};
global.ConversationTurnStore = {
  abortAttempt: async (attemptId) => { abortedAttempts.push(attemptId); },
  hydrateConversation: async (conv) => { hydrated.push(conv.id); },
};
global.ConversationTurnRead = {
  activeAttemptIds(conv) {
    const ids = [];
    if (conv?._activeAttemptId) ids.push(conv._activeAttemptId);
    for (const attemptId of conv?._activeBranchAttemptIds || []) ids.push(attemptId);
    return ids;
  },
};
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));

(async () => {
  current = {id:'main', _activeAttemptId:'a1'};
  const mainConversation = current;
  const separateStopAbsent = () => !elements.composerStopBtn;
  updateSendButton();
  const initial = {
    className:button.className,
    title:button.title,
    ariaLabel:button.getAttribute('aria-label'),
    separateStopAbsent:separateStopAbsent(),
  };

  /* One control, one state: a draft typed mid-turn never splits the composer
   * into Send + Stop — the single control stays Stop (queueing a follow-up
   * mid-turn stays on the Enter key path). */
  composerInput.value = 'follow up while streaming';
  composerInput.emit('input');
  const busyDraft = {
    sendClass:button.className,
    sendTitle:button.title,
    sendAriaLabel:button.getAttribute('aria-label'),
    queueBadge:button.innerHTML.includes('queue-badge'),
    parentClass:actionRight.classList.contains('composer-dual-actions'),
    separateStopAbsent:separateStopAbsent(),
    inputListenerCount:composerInput.listenerCount('input'),
  };
  button.onclick();
  const stopping = {
    sendClass:button.className,
    sendTitle:button.title,
    sendAriaBusy:button.getAttribute('aria-busy'),
  };
  button.onclick();
  const duplicateCount = abortedAttempts.length;
  timers.shift()();
  await Promise.resolve();
  await Promise.resolve();

  const retry = {
    sendClass:button.className,
  };
  composerInput.value = '';
  composerInput.emit('input');
  const cleared = {
    sendClass:button.className,
    parentClass:actionRight.classList.contains('composer-dual-actions'),
    separateStopAbsent:separateStopAbsent(),
  };

  pendingImages.push({name:'draft.png'});
  updateSendButton();
  const attachmentDraft = {
    sendClass:button.className,
    separateStopAbsent:separateStopAbsent(),
  };
  pendingImages.length = 0;
  updateSendButton();

  current = null;
  updateSendButton();
  const idle = {
    sendClass:button.className,
    title:button.title,
    ariaLabel:button.getAttribute('aria-label'),
    separateStopAbsent:separateStopAbsent(),
  };

  const main = {
    initial,
    busyDraft,
    sendCalls,
    aborted: abortedAttempts.slice(),
    stopping,
    duplicateCount,
    latchReleased: !mainConversation._finishingStream
      && mainConversation._finishingAttemptId === null,
    authorityPreserved: mainConversation._activeAttemptId === 'a1',
    retry,
    cleared,
    attachmentDraft,
    idle,
    warned: toasts.length === 1 && toasts[0].kind === 'warning',
    hydrated: hydrated.slice(),
  };

  current = {id:'branch', _activeBranchAttemptIds:new Set(['b1', 'b2'])};
  button.className = '';
  updateSendButton();
  button.onclick();
  const branch = {
    aborted: abortedAttempts.slice(1),
    stopping: button.className.includes('is-stopping'),
    latch: current._finishingAttemptId,
  };

  let startupAborts = 0;
  current = {id:'startup', _genStartCtrl:{abort(){ startupAborts += 1; }}};
  composerInput.value = 'draft typed while the first request connects';
  button.className = '';
  updateSendButton();
  const startupProjection = {
    buttonClass:button.className,
    separateStopAbsent:separateStopAbsent(),
  };
  button.onclick();
  await Promise.resolve();
  const startup = {
    projection:startupProjection,
    controllerAborts: startupAborts,
    serverAborts: abortConvCalls,
    released: current._genStartCtrl === null,
    buttonClass: button.className,
  };
  composerInput.value = '';
  composerInput.emit('input');

  current = {id:'unknown', _forceBusy:true};
  button.className = '';
  updateSendButton();
  button.onclick();
  await Promise.resolve();
  const unknown = {hydrated: hydrated.includes('unknown')};

  let keyboardCalls = 0;
  global._getSendMode = () => 'enter';
  global._doSendOrGenerate = () => { keyboardCalls += 1; };
  const keyEvent = {
    key:'Enter', keyCode:13, isComposing:false,
    shiftKey:false, ctrlKey:false, metaKey:false, altKey:false,
    target:composerInput,
    prevented:false,
    preventDefault() { this.prevented = true; },
  };
  handleKeyDown(keyEvent);
  const keyboard = {calls:keyboardCalls, prevented:keyEvent.prevented};

  process.stdout.write(JSON.stringify({main, branch, startup, unknown, keyboard}));
})().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(not _NODE or not _ESBUILD,
                    reason='node + esbuild not available')
def test_stop_click_feedback_and_idempotency():
    proc = subprocess.run(
        [_NODE, '-e', _HARNESS, SEND_BUTTON, _composer_controls_bundle(),
         INPUT_HANDLING],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['main'] == {
        'initial': {
            'className': 'send-btn stop-btn',
            'title': 'Stop generating',
            'ariaLabel': 'Stop generating',
            'separateStopAbsent': True,
        },
        'busyDraft': {
            'sendClass': 'send-btn stop-btn',
            'sendTitle': 'Stop generating',
            'sendAriaLabel': 'Stop generating',
            'queueBadge': True,
            'parentClass': False,
            'separateStopAbsent': True,
            'inputListenerCount': 1,
        },
        'sendCalls': 0,
        'aborted': ['a1'],
        'stopping': {
            'sendClass': 'send-btn stop-btn is-stopping',
            'sendTitle': 'Stopping…',
            'sendAriaBusy': 'true',
        },
        'duplicateCount': 1,
        'latchReleased': True,
        'authorityPreserved': True,
        'retry': {
            'sendClass': 'send-btn stop-btn',
        },
        'cleared': {
            'sendClass': 'send-btn stop-btn',
            'parentClass': False,
            'separateStopAbsent': True,
        },
        'attachmentDraft': {
            'sendClass': 'send-btn stop-btn',
            'separateStopAbsent': True,
        },
        'idle': {
            'sendClass': 'send-btn',
            'title': 'Send (Enter)',
            'ariaLabel': 'Send',
            'separateStopAbsent': True,
        },
        'warned': True,
        'hydrated': ['main'],
    }
    assert result['branch'] == {
        'aborted': ['b1', 'b2'],
        'stopping': True,
        'latch': 'b1',
    }
    assert result['startup'] == {
        'projection': {
            'buttonClass': 'send-btn stop-btn',
            'separateStopAbsent': True,
        },
        'controllerAborts': 1,
        'serverAborts': 1,
        'released': True,
        'buttonClass': 'send-btn',
    }
    assert result['unknown'] == {'hydrated': True}
    assert result['keyboard'] == {'calls': 1, 'prevented': True}


@pytest.mark.skipif(not _ESBUILD, reason='esbuild not available')
def test_busy_draft_single_stop_control_fits_390px(browser):
    css = '\n'.join(
        open(path, encoding='utf-8').read()
        for path in (COMPOSER_CSS, TOFU_CSS, MOBILE_CSS)
    )
    context = browser.new_context(
        viewport={'width': 390, 'height': 844},
        is_mobile=True,
        has_touch=True,
    )
    page = context.new_page()
    try:
        page.set_content(f'''<!doctype html>
<html data-theme="tofu"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
*{{box-sizing:border-box}}html,body{{width:100%;margin:0;overflow-x:hidden}}
{css}
</style></head><body>
<div class="input-area"><div class="input-inner"><div class="input-box">
  <textarea id="userInput">follow up while streaming</textarea>
  <div class="input-actions">
    <button class="action-btn attach-btn" type="button">+</button>
    <div class="input-actions-scroll">
      <div class="input-group" id="modelGroup">
        <button class="preset-toggle" type="button"><span class="ps-label">Claude Sonnet Long Model</span></button>
      </div>
    </div>
    <div class="input-actions-right">
      <button class="mobile-more-btn" type="button">•••</button>
      <div class="search-mode-toggle" role="button"></div>
      <button class="send-btn" id="sendBtn" type="button"></button>
    </div>
  </div>
</div></div></div>
</body></html>''')
        page.add_script_tag(path=_composer_controls_bundle())
        result = page.evaluate('''() => {
          const state = updateComposerSendControls({
            document,
            translating:false,
            startupConnecting:false,
            turnBusy:true,
            stopping:false,
            hasAttachmentDraft:false,
            queueCount:1,
            labels:{
              send:'Send', sendTitle:'Send (Enter)',
              stop:'Stop generating', stopping:'Stopping…',
            },
            onSend() {}, onStop() {}, requestRefresh() {},
          });
          const toolbar = document.querySelector('.input-actions');
          const send = document.getElementById('sendBtn');
          const stop = document.getElementById('composerStopBtn');
          const model = document.getElementById('modelGroup');
          const tr = toolbar.getBoundingClientRect();
          const sr = send.getBoundingClientRect();
          const mr = model.getBoundingClientRect();
          return {
            state,
            toolbarFits:toolbar.scrollWidth <= toolbar.clientWidth,
            pageFits:document.documentElement.scrollWidth <= 390,
            separateStopAbsent:!stop,
            controlInside:sr.left >= tr.left && sr.right <= tr.right,
            controlTouchTarget:sr.width >= 40 && sr.height >= 40,
            modelStillVisible:mr.width > 0,
            controlAria:send.getAttribute('aria-label'),
            controlTitle:send.title,
          };
        }''')
    finally:
        context.close()

    assert result == {
        'state': {'busy': True, 'hasDraft': True, 'splitControls': False},
        'toolbarFits': True,
        'pageFits': True,
        'separateStopAbsent': True,
        'controlInside': True,
        'controlTouchTarget': True,
        'modelStillVisible': True,
        'controlAria': 'Stop generating',
        'controlTitle': 'Stop generating',
    }


def test_stop_control_has_no_task_protocol_fallback():
    source = open(SEND_BUTTON, encoding='utf-8').read()
    assert 'activeTaskId' not in source
    assert 'abortTask' not in source
    assert '_authoritativeActiveTaskIds' not in source
    assert 'finishStream' not in source


def test_composer_control_i18n_keys_exist():
    locale_root = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    for language in ('zh', 'en'):
        with open(os.path.join(locale_root, language + '.json'),
                  encoding='utf-8') as handle:
            catalog = json.load(handle)
        for key in ('orch.ai.send', 'paper.send', 'branch.stopGen',
                    'stop.stopping'):
            assert catalog.get(key)
