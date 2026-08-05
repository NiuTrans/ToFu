/* ═══════════════════════════════════════════════════════════════════
   local-control — the SINGLE "let Tofu act on my machine" surface.

   Merges what used to be two toolbar rows (#browserToggle + #desktopToggle),
   one setup modal (#browserModal) and one blind flag flip (toggleDesktop with
   no status check at all). From the user's side browser-tabs and
   computer-control are one concept; two rows, two modals and two status dots
   were strictly more cognitive load than one.

   What did NOT merge, deliberately: the two backing flags `browserEnabled`
   and `desktopEnabled` stay separate on the wire. They gate different tool
   families with genuinely different risk tiers (reading a tab vs running a
   shell command), and `lib/tools/registry/_build.py` builds them from two
   independent ToolContext fields. Only the SURFACE is merged.

   ── The one rule this file exists to enforce ──
   Each capability row shows exactly ONE next action, chosen by DETECTED
   state. Never a menu of every possible path, and never an instruction the
   user cannot act on from where they are. The desktop choice is made by the
   BACKEND (`setup_state` on /api/v1/desktop/status) because only the server
   process can see `sys.frozen` — the frontend must not re-derive it.

   ── …and the FLOOR that rule stands on ──
   "Chosen by detected state" used to mean "rendered only after detection":
   the modal opened showing `local.checking` ("正在检查…") over an EMPTY setup
   box, and the install instructions appeared one or two network round-trips
   later. Every user paid a wait to be told the thing that is true for almost
   all of them — and the failure modes were worse than the wait: if the status
   call errored the box was blanked back to empty, and if `Api` was not yet
   defined `_lcRefresh` returned without painting anything at all, leaving
   "正在检查…" and an empty box on screen permanently.

   So detection now UPGRADES an instruction that is already on screen rather
   than being the thing that puts one there. `_lcPaintFloor` runs
   synchronously on open with the guidance that holds regardless of what the
   probe finds (download the extension / install the desktop app), and the
   renderers replace it with something MORE specific once the payload lands.
   The floor is never "loading" and never empty, so the worst case is an
   instruction that is merely generic — never one that is absent.

   Corollary, and the reason the download markup lives in `_lcBrowserDownload`
   rather than inline: the floor and the detected `download` state are the
   SAME instruction, so they must be ONE authoring. Two copies of it would
   drift, and a drifted floor is a wrong instruction shown first.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. No imports/exports.
   ═══════════════════════════════════════════════════════════════════ */

/* Poll cadence while the modal is OPEN. `is_desktop_agent_connected()` is a
 * 15s window (lib/desktop/bridge.py::_CONNECTED_WINDOW_S) and enabling the
 * tray agent takes a couple of seconds, so a user who turns it on WHILE
 * looking at this dialog must see the dot flip without reopening it. The old
 * _checkBrowserStatus was one-shot-on-open; that limitation is not carried
 * over. Cleared on close so a background tab never polls. */
var _LC_POLL_MS = 3000;
var _lcPollTimer = null;

/* Last CONFIRMED reachability per capability, written only by the renderers
 * from a real status response. `null` = never confirmed, which is NOT the same
 * as "unreachable" — an unchecked capability must never be presented as
 * broken. Read by the switch repaint (which would otherwise have to guess) and
 * by the badge. */
var _lcReach = { browser: null, desktop: null };

/* Signature of the last desktop-setup render (see the gate in
 * _lcRenderDesktop). `null` forces a render; openLocalControlModal resets
 * it so a reopened modal never inherits a stale skip. */
var _lcDesktopSigLast = null;

/* The render inputs that justify a setup-box rewrite. Anything NOT in here
 * changing is not a reason to touch the DOM the user is interacting with. */
function _lcDesktopSignature(d) {
  function fp(rows) {
    return (Array.isArray(rows) ? rows : []).map(function (p) {
      return [(p && p.filename) || '', (p && p.size) || 0,
              (p && p.preseed_url) || ''].join(':');
    }).join('|');
  }
  var lang = (typeof _i18nLang !== 'undefined') ? _i18nLang : '';
  return [d.setup_state, !!d.connected, d.server_url || '',
          d.bridge_token_required, fp(d.downloads),
          fp(d.agent_downloads), lang,
          d.server_url_reachability || '', d.bridge_tokens_issued || 0
         ].join('~');
}

/* Paired-but-nothing-arrived: bridge tokens exist yet no agent polls.
 *
 * Recovery belongs to the AGENT, not the user (owner decree 2026-08-04 —
 * no UI may send anyone to open an ssh tunnel by hand): the agent's resume
 * path re-probes its saved address and re-runs the discovery ladder
 * (loopback → LAN → ssh-config candidates → self-built tunnel) keeping the
 * ORIGINAL token, so a dead route heals itself. This line says exactly
 * that, plus the ONE fallback that always works: re-pair from the tray
 * with a fresh code. Its predecessor told the user to mint a second
 * connect line from a tunnel address — measured dead-end advice. */
function _lcAwaitingAgentHtml(d) {
  if (!d || d.connected || !(d.bridge_tokens_issued > 0)) return '';
  return '<p class="lc-step lc-await">' + _lcEsc(_lcT('local.awaitingAgent',
    '已发出配对凭证，等待受控端连入……它会自己寻找服务器并自动重试（本机 → 局域网 → 自动隧道），一般一分钟内变绿；迟迟未连上时，在受控端托盘选「连接到另一个 Tofu…」用新配对码重配一次。')) + '</p>';
}

/* The pairing block — the ONE primary attach action in BOTH install
 * branches (docs/DESKTOP_AGENT_DIST_DESIGN.md §11). Authored ONCE so the
 * branches cannot drift (a drifted copy is a wrong instruction shown
 * first). The ids are fixed (lcPairBtn / lcPairBox): exactly one branch
 * is on screen at a time, so they never collide. `withStep` prefixes the
 * numbered ② line for the remote branch's ①②③ flow; the role-labeled
 * branch and the stale-while-build fallback already name the flow, so
 * they render the bare block. */
function _lcPairBlockHtml(withStep) {
  return (withStep
      ? '<p class="lc-step">' + _lcEsc(_lcT('local.agentStepPair',
        '② 点「配对这台电脑」（6 位码，可复制），填进受控端首次启动：')) + '</p>'
      : '') +
    '<button type="button" class="btn btn-primary btn-sm" id="lcPairBtn">' +
      _lcEsc(_lcT('local.pairBtn', '配对这台电脑')) + '</button>' +
    '<div id="lcPairBox" style="display:none"></div>';
}

/* The demoted connect-line fallback. Suppressed ENTIRELY when the panel is
 * reached through a public host: there the line's address half is an SSO
 * edge the agent can never cross (owner incident 2026-08-03), so offering
 * it is offering a measured dead end — the pairing code above covers every
 * case the line could, because the agent discovers the route itself. */
function _lcConnectDetailsHtml(reachability) {
  if (reachability === 'public') return '';
  return '<details class="lc-details"><summary>' +
      _lcEsc(_lcT('local.connectLineToggle',
        '高级：连接行（配对码不可用时兜底）')) + '</summary>' +
    '<button type="button" class="btn btn-primary btn-sm" id="lcMintBtn">' +
      _lcEsc(_lcT('local.mintToken', '生成连接行')) + '</button>' +
    '<code class="lc-copy" id="lcTokenBox" style="display:none"></code>' +
  '</details>';
}

/* Bind the attach actions a branch just rendered (ids are branch-unique). */
function _lcWireAttach(serverUrl) {
  var pair = document.getElementById('lcPairBtn');
  if (pair) pair.onclick = function () { _lcPairCode(); };
  var mint = document.getElementById('lcMintBtn');
  if (mint) mint.onclick = function () { _lcMintToken(serverUrl); };
}

function openLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (!el) return;
  el.classList.add('open');
  _lcDesktopSigLast = null;
  _lcPaintFloor();
  _lcRefresh();
  if (_lcPollTimer) clearInterval(_lcPollTimer);
  _lcPollTimer = setInterval(_lcRefresh, _LC_POLL_MS);
}

/* Put a real, followable instruction in BOTH rows before anything is fetched.
 *
 * Runs synchronously on open, so the first frame the user sees already tells
 * them what to do. Everything here is derivable with ZERO backend knowledge:
 * downloading the extension ZIP and installing the desktop app are the steps
 * that hold whatever the probe later reports. The renderers then narrow this
 * to the state-specific instruction (load-unpacked with the on-disk path, the
 * tray toggle, the remote connect line) or clear it outright when connected.
 *
 * Status text is painted too. It reads "not installed" / "not running" rather
 * than "checking": for a user who has not set this up — the only user who
 * needs this dialog — that is both the honest answer and the one the poll is
 * about to confirm, and it does not go stale if the poll never answers. */
function _lcPaintFloor() {
  _lcSetStatus('lcBrowserStatus', false, _lcT('local.notInstalled', '尚未安装'));
  _lcSetStatus('lcDesktopStatus', false, _lcT('local.notRunning', '未运行'));
  _lcBrowserDownload();
  var d = document.getElementById('lcDesktopSetup');
  if (d) {
    // No download link yet: the URL comes from the backend's UPDATE_REPO and
    // must not be re-derived here (a fork's build would get the wrong link).
    // _lcRenderDesktop adds it a beat later. A named step with no shortcut is
    // still actionable; a shortcut pointing at the wrong repo would not be.
    d.innerHTML = '<p class="lc-step">' + _lcEsc(_lcT('local.desktopFloor',
      '安装桌面版后，即可在系统托盘一键开启「Enable Computer Control」，让 AI 操作这台电脑。')) + '</p>';
  }
}

function closeLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (el) el.classList.remove('open');
  if (_lcPollTimer) { clearInterval(_lcPollTimer); _lcPollTimer = null; }
}

/* The architecture THIS machine runs, as reported by the browser itself.
 *
 * `navigator.userAgentData.getHighEntropyValues(['architecture'])` is the only
 * practical source of this fact. The UA string cannot supply it on macOS — an
 * Apple Silicon Mac reports "Intel Mac OS X", Chrome and Safari alike — and
 * the `Sec-CH-UA-Arch` request header is sent only AFTER a server has already
 * answered once with an `Accept-CH` opt-in, so the very first page load (the
 * one that renders the download button) would be arch-blind.
 *
 * `null` while unresolved and `''` when the browser refuses to say — both mean
 * "do not narrow", and the backend then returns BOTH macOS DMGs. That ambiguous
 * answer is CORRECT: guessing wrong hands the user a download that cannot open.
 * Resolved once per page (the answer cannot change) and never awaited by the
 * paint path, so a browser without the API costs nothing. */
var _lcArch = null;

function _lcResolveArch() {
  if (_lcArch !== null) return Promise.resolve(_lcArch);
  var uad = (typeof navigator !== 'undefined') ? navigator.userAgentData : null;
  if (!uad || typeof uad.getHighEntropyValues !== 'function') {
    _lcArch = '';
    return Promise.resolve(_lcArch);
  }
  return Promise.resolve(uad.getHighEntropyValues(['architecture']))
    .then(function (v) {
      _lcArch = (v && v.architecture) ? String(v.architecture) : '';
      return _lcArch;
    })
    .catch(function () { _lcArch = ''; return _lcArch; });
}

/* Fetch both capabilities' state and repaint. Each side is independent —
 * one backend hiccup must not blank the other row. */
function _lcRefresh() {
  if (typeof Api === 'undefined' || !Api.browser || !Api.desktop) return;
  Promise.resolve(Api.browser.status())
    .then(_lcRenderBrowser)
    .catch(function (e) { _lcRenderBrowser(null, e); });
  _lcResolveArch().then(function (arch) {
    return Promise.resolve(Api.desktop.status(arch))
      .then(_lcRenderDesktop)
      .catch(function (e) { _lcRenderDesktop(null, e); });
  });
}

function _lcT(key, fallback) {
  if (typeof t === 'function') {
    var v = t(key);
    if (v && v !== key) return v;
  }
  return fallback;
}

function _lcEsc(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Paint one row's dot + label. */
function _lcSetStatus(rowId, connected, label) {
  var box = document.getElementById(rowId);
  if (!box) return;
  var dot = box.querySelector('.browser-status-dot');
  if (dot) {
    dot.classList.toggle('connected', !!connected);
    dot.classList.toggle('disconnected', !connected);
  }
  var txt = box.querySelector('.lc-status-text');
  if (txt) txt.textContent = label;
}

/* Paint one row's switch (reflects the real wire flag, not modal-local state).
 *
 * `reachable` gates turning the capability ON. Switching it on while nothing
 * is connected is the ORIGINAL silent-failure bug in a new costume:
 * `lib/tools/registry/_build.py` ships ZERO tools for an unconnected bridge,
 * so the toggle would light up and the AI would still have nothing. A control
 * that cannot achieve what it claims must not invite the click. Once the agent
 * connects the live poll re-enables it within one beat, so it is never a dead
 * end.
 *
 * ── The gate is ONE-WAY, and that asymmetry is the point ──
 * Turning OFF is ALWAYS allowed, even while disconnected. Gating both
 * directions meant a capability enabled while the agent was up became
 * unrevokable the moment that agent dropped: the flag stayed ON on the wire
 * (it persists per-conversation and is sent to the server), the switch showed
 * ON and greyed out, and the one action a worried user wants — withdraw access
 * to their own machine — was the one action the UI refused. A safety control
 * must never be harder to switch off than on. */
function _lcSetSwitch(switchId, on, reachable) {
  var sw = document.getElementById(switchId);
  if (!sw) return;
  var canEnable = (reachable === undefined) ? true : !!reachable;
  var can = canEnable || !!on;   // already on ⇒ always revocable
  sw.classList.toggle('on', !!on);
  sw.setAttribute('aria-checked', on ? 'true' : 'false');
  sw.disabled = !can;
  sw.classList.toggle('lc-switch-off', !can);
  /* Flag a capability that is ON while nothing is connected: the AI is getting
   * zero tools from it, so leaving it looking healthy repeats the original
   * lie in a quieter form. */
  sw.classList.toggle('lc-switch-stale', !!on && !canEnable);
  if (!can) {
    sw.title = _lcT('local.switchBlocked',
      '连接成功后才能开启 —— 现在打开，AI 也拿不到任何工具。');
  } else if (!!on && !canEnable) {
    sw.title = _lcT('local.switchStale',
      '已开启，但当前未连接 —— AI 现在拿不到这项能力的任何工具。可随时关闭。');
  } else {
    sw.removeAttribute('title');
  }
}

/* Render the "what does this actually give the AI" line for one row.
 *
 * Users are being asked to grant real access to their browser session and
 * their machine; "Browser tabs / This computer" alone does not let them make
 * that call. Kept to ONE short line per row — a full tool list would be the
 * menu-of-everything this merge exists to remove — and phrased as concrete
 * actions rather than tool names, since the tool names are an implementation
 * detail the user never types. */
function _lcSetAbout(rowId, text) {
  var host = document.getElementById(rowId);
  if (!host) return;
  host.textContent = text;
}

// ══════════════════════════════════════════════════════
//  Browser tabs
// ══════════════════════════════════════════════════════

/* Which ONE instruction the browser row shows.
 *
 * BOTH inputs come from the backend's own detection, and BOTH are required
 * for the 'load_unpacked' branch:
 *
 *   - `extensionPath` — routes/api_v1/browser.py fills it only for a
 *     loopback peer whose machine also has a drivable browser.
 *   - `localBrowser` — the probe result: which Chromium-family browser this
 *     machine actually has, or null.
 *
 * ── Why the probe and not the path alone ──
 * This branch used to key off `extensionPath` only, and that is exactly how
 * the dead button shipped. The path's own gate was a pure IP test, which a
 * same-host reverse proxy makes vacuously true for public traffic, so a
 * remote user got a button whose click opened a browser window on a headless
 * server — three 404s in the log and no way for the user to tell why. The
 * probe is a fact about the machine that no proxy can forge.
 *
 * Keeping the `&& localBrowser` conjunction here (rather than trusting the
 * backend to have already ANDed them) is deliberate: it is the frontend's own
 * statement of the rule this file exists to enforce, and it means a future
 * payload that carries a path without a browser still cannot produce a button
 * that has nothing to open. */
function _lcBrowserSetupState(d) {
  if (d && d.connected) return 'connected';
  if (d && d.extensionPath && d.localBrowser) return 'load_unpacked';
  return 'download';
}

function _lcRenderBrowser(d, err) {
  var setup = document.getElementById('lcBrowserSetup');
  var connected = !!(d && d.connected);

  if (err || !d) {
    _lcSetStatus('lcBrowserStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    // Falls back to the download instruction, NOT an empty box. Losing the
    // status call says nothing about whether the user needs the extension —
    // and it is precisely when the backend is flaky that wiping the one
    // followable step off the screen is least defensible.
    _lcBrowserDownload();
    return;
  }

  var clients = d.clients || [];
  /* Fleet tri-state (2026-08-04, stranded-fleet fix): the server reports
   * the version a fresh download would carry (servedExtVersion) and the
   * clients whose polls DIED at the bridge gate (lockedOutClients). A
   * stale-but-connected extension still works — the dot stays green and
   * the upgrade is a one-click nudge. A locked-out extension cannot poll
   * at all, so calling it "尚未安装" would be a lie: it is installed,
   * broken, and unable to heal itself — the row must say so and offer
   * the preseeded re-download (zero-config cure). */
  var servedVer = ((d && d.servedExtVersion) || '').trim();
  var staleFrom = '';
  if (connected && servedVer) {
    for (var _ci = 0; _ci < clients.length; _ci++) {
      var _cv = ((clients[_ci] && clients[_ci].ext_version) || '').trim();
      if (_cv && _cv !== servedVer) { staleFrom = _cv; break; }
    }
  }
  var lockedOut = (!connected && d && Array.isArray(d.lockedOutClients))
    ? d.lockedOutClients : [];
  if (connected) {
    var ago = (d.secondsAgo != null) ? d.secondsAgo + 's' : '';
    if (clients.length > 0) {
      window._browserClientId = clients[0].client_id;
    }
    _lcSetStatus('lcBrowserStatus', true,
      clients.length > 1
        ? _lcT('local.connectedN', '已连接').replace('{n}', clients.length)
        : _lcT('local.connected', '已连接') + (ago ? ' · ' + ago : ''));
  } else {
    window._browserClientId = null;
    _lcSetStatus('lcBrowserStatus', false,
      lockedOut.length
        ? _lcT('local.extDead', '已安装但凭证已失效')
        : _lcT('local.notInstalled', '尚未安装'));
  }

  _lcReach.browser = connected;
  _lcSetSwitch('lcBrowserSwitch',
    typeof browserEnabled !== 'undefined' && browserEnabled, connected);
  _lcUpdateBadge();
  _lcSetAbout('lcBrowserAbout', _lcT('local.browserAbout',
    '读取你已打开的标签页内容，并代你点击、填表单、切换页面。'));

  // Chrome 142+ LNA guidance stays keyed on the CONNECTED extension's version.
  if (typeof _applyBrowserLnaWarning === 'function') {
    _applyBrowserLnaWarning(d.chromeMajor);
  }

  if (!setup) return;
  var state = _lcBrowserSetupState(d);
  if (state === 'connected') {
    if (staleFrom) {
      // Outdated but WORKING: nothing is broken, so this is a nudge, not
      // an alarm — one click on the preseeded zip upgrades in place.
      setup.innerHTML =
        '<p class="lc-step">' + _lcEsc(_lcT('local.browserExtOutdated',
          '扩展有新版本（{old} → {new}）——重新下载 ZIP 覆盖加载即可升级（已自动配对，零配置）：')
          .replace('{old}', staleFrom).replace('{new}', servedVer)) + '</p>' +
        _lcExtDownloadAction();
      _lcWireExtDownload();
    } else {
      setup.innerHTML = '';
    }
    return;
  }

  if (lockedOut.length) {
    // Stranded: an installed extension is knocking with a dead credential.
    // This takes precedence over the load_unpacked/download guidance —
    // the user's problem is not getting the folder, it is replacing a
    // broken install with the self-pairing one.
    setup.innerHTML =
      '<p class="lc-step">' + _lcEsc(_lcT('local.browserExtStranded',
        '检测到旧版扩展因凭证失效连不上（它自己无法恢复）——重新下载扩展 ZIP（已自动配对、零配置），加载即恢复：')) + '</p>' +
      _lcExtDownloadAction();
    _lcWireExtDownload();
    return;
  }

  if (state === 'load_unpacked') {
    // Tofu runs on this machine, this machine HAS a browser we can drive, and
    // the unpacked extension is already on disk. One primary action: that
    // button (it also copies the path). What remains — Developer mode, Load
    // unpacked, paste — is inside the browser's sandbox and no web page can
    // do it for the user; the text says so instead of implying one click
    // finishes the install.
    //
    // The browser is named from the PROBE, never hardcoded: ordering an Edge
    // user into Chrome is its own dead instruction.
    var lb = d.localBrowser || {};
    var bname = lb.name || 'Chrome';
    setup.innerHTML =
      '<button type="button" class="btn btn-primary btn-sm" id="lcExtOpenBtn">' +
        _lcEsc(_lcT('local.browserOpenPageBtn',
          '帮我打开扩展管理页（自动复制路径）')) + '</button>' +
      '<p class="lc-step">' + _lcEsc(
        _lcT('local.browserLoadUnpacked',
          '剩下的三步 {browser} 不允许网页代劳：① 打开右上角「开发者模式」→ ② 点「加载已解压的扩展程序」→ ③ 粘贴路径（已自动复制）选择这个文件夹：')
        .replace('{browser}', bname)) + '</p>' +
      '<code class="lc-copy" id="lcExtPath" data-tooltip="' +
        _lcEsc(_lcT('browser.clickToCopy', '点击复制')) + '">' +
        _lcEsc(d.extensionPath) + '</code>' +
      '<p class="lc-substep" id="lcExtOpenNote"></p>';
    var openBtn = document.getElementById('lcExtOpenBtn');
    if (openBtn) {
      openBtn.onclick = function () { _lcOpenExtensionsPage(d.extensionPath); };
    }
    var code = document.getElementById('lcExtPath');
    if (code) {
      code.onclick = function () {
        if (typeof _safeClipboardWrite === 'function') {
          _safeClipboardWrite(d.extensionPath)
            .then(function () { code.classList.add('copied'); })
            .catch(function () {});
        }
      };
    }
    return;
  }

  // The remaining case — either the folder does not exist on the user's
  // machine (remote server), or this machine has no browser we could drive,
  // which means the user is not sitting at it either. Both reduce to the same
  // ONE actionable path: download the ZIP, then load it in YOUR browser.
  //
  // This branch is load-bearing beyond the remote case now: it is what the
  // panel falls through to instead of rendering a button that can only 404,
  // and what the pre-detection floor shows on open — hence one shared
  // authoring in _lcBrowserDownload rather than markup inline here.
  // An empty panel would be worse than a wrong instruction.
  _lcBrowserDownload();
}

/* The download-the-ZIP instruction — authored ONCE.
 *
 * Shown in three situations that are the same instruction: the pre-detection
 * floor, a failed status call, and the detected `download` state. It needs no
 * payload (downloadBrowserExtension is a pure frontend call), which is exactly
 * what makes it usable as the floor. */
/* The download BUTTON — authored once, shared by the plain install
 * instruction, the upgrade nudge and the stranded-rescue branch (three
 * copies of a button would drift; a drifted button is a dead one). */
function _lcExtDownloadAction() {
  return '<button type="button" class="btn btn-primary btn-sm" id="lcExtDownloadBtn">' +
    _lcEsc(_lcT('browser.stepDownloadBtn', '下载扩展 ZIP')) + '</button>';
}

function _lcWireExtDownload() {
  var btn = document.getElementById('lcExtDownloadBtn');
  if (btn) {
    btn.onclick = function () {
      if (typeof downloadBrowserExtension === 'function') downloadBrowserExtension();
    };
  }
}

function _lcBrowserDownload() {
  var setup = document.getElementById('lcBrowserSetup');
  if (!setup) return;
  setup.innerHTML =
    '<p class="lc-step">' + _lcEsc(_lcT('local.browserDownload',
      '下载扩展并解压，然后在 Chrome / Edge 里打开扩展管理页 → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择解压出的文件夹。')) + '</p>' +
    _lcExtDownloadAction();
  _lcWireExtDownload();
}

// ══════════════════════════════════════════════════════
//  This computer
// ══════════════════════════════════════════════════════

function _lcRenderDesktop(d, err) {
  var setup = document.getElementById('lcDesktopSetup');
  if (err || !d) {
    _lcSetStatus('lcDesktopStatus', false, _lcT('local.unreachable', '无法连接服务器'));
    // Keep whatever instruction is on screen (the floor, or the last good
    // state) rather than blanking to an empty box — see _lcPaintFloor.
    return;
  }

  var connected = !!d.connected;
  _lcSetStatus('lcDesktopStatus', connected,
    connected ? _lcT('local.connected', '已连接')
              : _lcT('local.notRunning', '未运行'));
  _lcReach.desktop = connected;
  _lcSetSwitch('lcDesktopSwitch',
    typeof desktopEnabled !== 'undefined' && desktopEnabled, connected);
  _lcUpdateBadge();
  _lcSetAbout('lcDesktopAbout', _lcT('local.desktopAbout',
    '浏览与读写本机文件、截屏、打开应用、运行命令（写入与执行需单独授权）。'));

  /* The permission note explains the TRAY's Permissions submenu. It is only
   * actionable once the agent is actually running there — showing it to a
   * user who has not installed anything is an instruction they cannot follow,
   * competing with the ONE real next action. */
  var perm = document.getElementById('lcPermNote');
  if (perm) {
    var trayReachable = connected || d.setup_state === 'tray';
    perm.style.display = trayReachable ? '' : 'none';
  }

  if (!setup) return;

  /* ── Poll-signature gate (owner-measured 2026-08-03) ──
   * _lcRefresh repaints every 3s so a freshly-connected agent flips the
   * dot — but rewriting setup.innerHTML on every beat also blew away the
   * USER's interaction state: an expanded <details> collapsed seconds
   * after opening, a minted connect line vanished mid-copy. Rewrite only
   * when the render INPUTS changed; the dot/text/switch above still
   * update every beat, so a connecting agent is never delayed. */
  var sig = _lcDesktopSignature(d);
  if (sig === _lcDesktopSigLast) return;
  _lcDesktopSigLast = sig;

  // The backend chose the state — see routes/api_v1/desktop.py::_setup_state.
  // Reading it (rather than re-deriving from the URL) is what keeps the
  // packaged-app case distinguishable from a reverse-proxied remote one.
  switch (d.setup_state) {
    case 'connected':
      setup.innerHTML = '';
      return;

    case 'tray':
      // Packaged desktop app: the agent runs IN-PROCESS. One click, no token,
      // no second program to install.
      setup.innerHTML = '<p class="lc-step">' + _lcEsc(_lcT('local.desktopTray',
        '右键点击系统托盘里的 Tofu 图标 → 勾选「Enable Computer Control」。')) + '</p>';
      return;

    case 'local_source': {
      // Tofu runs from source on this machine. Two audiences land here and
      // the server CANNOT tell them apart (a same-host proxy and an ssh -L
      // tunnel both present as loopback — see _setup_state's docstring), so
      // BOTH installs are shown, role-labeled, nothing collapsed
      // (owner 2026-08-03: the collapsed tunnel hatch was missed entirely,
      // and the prose wall made the one needed action unfindable). The
      // agent block renders only when a built artifact exists; without one
      // the full desktop app is the sole — and sufficient — offer.
      var srvSrc = (d.server_url || '').trim();
      var agentSrc = Array.isArray(d.agent_downloads)
        ? d.agent_downloads : [];
      // Pairing-code primary here too (owner decree 2026-08-04): the code
      // carries NO address, so it works from every reachability class —
      // the agent discovers the route itself. The connect line survives
      // only as the demoted details fallback (never under a public host).
      var pairSrc = _lcPairBlockHtml(false) +
        _lcConnectDetailsHtml(d.server_url_reachability);
      var htmlSrc = '<p class="lc-step">' + _lcEsc(_lcT('local.roleChoose',
          '当前 Tofu 以源码方式运行 —— 按这台电脑的角色选装：')) + '</p>';
      if (agentSrc.length) {
        htmlSrc +=
          '<div class="lc-role lc-role-primary">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.agentRoleHead', '受控端 · 轻量')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.agentRoleNote',
                '—— 从另一台电脑访问（如 ssh 转发）选它：只让服务器操作那台电脑')) + '</span></p>' +
            _lcDownloadLinks(d, 'agent', true) +
            pairSrc +
          '</div>' +
          '<div class="lc-role">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.fullRoleHead', '完整桌面版')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.fullRoleNote',
                '—— 这台电脑就是服务器本机：装它，托盘一键开启')) + '</span></p>' +
            _lcDownloadLinks(d, 'full') +
          '</div>';
      } else {
        // Stale-while-build: no agent artifact yet — the full installer
        // doubles as the controlled endpoint (tray → Connect to remote),
        // so the attach flow must stay reachable, not vanish with the
        // agent block.
        htmlSrc +=
          '<div class="lc-role">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.fullRoleHead', '完整桌面版')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.fullRoleNote',
                '—— 这台电脑就是服务器本机：装它，托盘一键开启')) + '</span></p>' +
            _lcDownloadLinks(d, 'full') +
            pairSrc +
          '</div>';
      }
      setup.innerHTML = _lcAwaitingAgentHtml(d) + htmlSrc;
      _lcWireAttach(srvSrc);
      return;
    }

    default: {
      // Remote server — the machine in front of the user is NOT this
      // machine, so this is the only case that can need a token. Its role
      // in this dialog is to be CONTROLLED (the A3 branch matrix): the
      // agent installer (lightweight, no frontend) is the PRIMARY offer;
      // the full desktop app is a one-line COLLAPSED secondary. When no
      // agent artifact exists yet (a build is in flight), the full
      // installer takes the primary slot with the historical instruction —
      // stale-while-build, never a dead end.
      //
      // The agent flow is numbered like the browser row's (①②③), because
      // an un-ordered "install … then pair" asked the user to discover
      // the sequence from the layout. Two shapes:
      //   * 3-step (default): download → pair with the 6-digit code →
      //     green status (the agent discovers the route itself);
      //   * 2-step (zero-touch): the artifact carries a usable preseed
      //     (backend already filtered loopback) AND the bridge needs no
      //     token — install and it connects by itself. bridge_token_required
      //     absent ⇒ treated as REQUIRED: the 3-step flow also works on an
      //     open bridge, so that is the fail-safe direction.
      var dl = (d.download_url || '').trim();
      var srv = (d.server_url || '').trim();
      var agentPicks = Array.isArray(d.agent_downloads)
        ? d.agent_downloads : [];
      var html;
      if (agentPicks.length) {
        var autoConnect = (d.bridge_token_required === false) &&
          agentPicks.some(function (p) { return p && p.preseed_url; });
        html =
          '<p class="lc-step">' + _lcEsc(_lcT('local.desktopRemoteAgent',
            'Tofu 运行在远程服务器上 —— 让 AI 操作这台电脑：')) + '</p>' +
          '<p class="lc-step">' + _lcEsc(_lcT('local.agentStep1',
            '① 下载并安装受控端（只让服务器操作这台电脑 —— 轻量 · 无界面 · 托盘配置）：')) + '</p>' +
          _lcDownloadLinks(d, 'agent', true) +
          (autoConnect
            ? '<p class="lc-step">' + _lcEsc(_lcT('local.agentStepAuto',
              '② 装完启动即可 —— 安装包已带服务器地址，会自动连上；此处状态变绿就是成功。')) + '</p>'
            : _lcPairBlockHtml(true) +
              '<p class="lc-step">' + _lcEsc(_lcT('local.agentStep3',
              '③ 连上后此处状态变绿 —— 之后它常驻托盘，无需再操作。')) + '</p>') +
          '<details class="lc-details"><summary>' +
            _lcEsc(_lcT('local.fullVersionToggle',
            '这台电脑也想跑 Tofu 本体（服务器+界面）？下载完整桌面版')) +
            '</summary>' +
            _lcDownloadLinks(d, 'full') +
          '</details>' +
          (autoConnect ? '' : _lcConnectDetailsHtml(d.server_url_reachability));
      } else {
        // Stale-while-build: no agent artifact yet — the full installer
        // doubles as the controlled endpoint, and the attach flow is the
        // SAME pairing code, never a bare minted line (its address half
        // is the measured dead end under an SSO edge).
        html =
          '<p class="lc-step">' + _lcEsc(_lcT('local.desktopRemote',
            'Tofu 运行在远程服务器上。在你自己的电脑安装桌面版，再把 6 位配对码填进它的首次启动：')) + '</p>' +
          _lcDownloadLinks(d) +
          _lcPairBlockHtml(false) +
          _lcConnectDetailsHtml(d.server_url_reachability);
      }
      setup.innerHTML = _lcAwaitingAgentHtml(d) + html;
      _lcWireAttach(srv);
      return;
    }
  }
}

/* Mint a bridge token and render it as a COMPLETE, copy-paste-ready connect
 * line — never a naked secret.
 *
 * The token alone is unusable: it has to be paired with the address of the
 * server the agent should poll, and nothing on the user's machine knows that
 * address. `serverUrl` comes from the backend (the request's own host, i.e. an
 * address the user demonstrably reaches this server on), so one copy carries
 * everything. Reuses POST /api/v1/desktop/token — the raw secret is returned
 * exactly once, so it is rendered here and never re-fetched. */
function _lcMintToken(serverUrl, btnId, boxId) {
  var btn = document.getElementById(btnId || 'lcMintBtn');
  var box = document.getElementById(boxId || 'lcTokenBox');
  if (btn) btn.disabled = true;
  Promise.resolve(Api.desktop.mintToken('local-control'))
    .then(function (r) {
      if (btn) btn.disabled = false;
      if (!r || !r.token) {
        if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
        return;
      }
      if (!box) return;
      var line = _lcConnectLine(serverUrl, r.token);
      box.style.display = '';
      box.textContent = line;
      box.setAttribute('data-tooltip', _lcT('browser.clickToCopy', '点击复制'));
      box.onclick = function () {
        if (typeof _safeClipboardWrite === 'function') {
          _safeClipboardWrite(line)
            .then(function () { box.classList.add('copied'); })
            .catch(function () {});
        }
      };
      if (btn) btn.style.display = 'none';
      /* The line exists to be pasted ONCE — don't make the user discover
       * the click-to-copy affordance. Runs inside the button's click
       * gesture, so the clipboard is allowed; a refusal falls back to the
       * visible box, which still copies on click. */
      if (typeof _safeClipboardWrite === 'function') {
        _safeClipboardWrite(line)
          .then(function () {
            box.classList.add('copied');
            if (typeof showToast === 'function') {
              showToast(_lcT('local.mintCopied',
                '连接行已复制 —— 粘贴到受控端的连接框即可'));
            }
          })
          .catch(function () {});
      }
    })
    .catch(function () {
      if (btn) btn.disabled = false;
      if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
    });
}

/* Mint a PAIRING CODE (P2, docs/DESKTOP_AGENT_DIST_DESIGN.md §11) and render
 * it BIG with a copy button + countdown — the ONE primary action of the
 * remote branch. The user types 6 digits into the agent's first-run dialog;
 * the agent exchanges the code for a bridge token (no bearer, no address,
 * no SSH command). This replaces the mint-connect-line flow as the primary
 * path because the connect line's address half is necessarily wrong under
 * an SSO proxy (owner incident 2026-08-03); the code carries no address at
 * all — the agent discovers the server itself (§11.2.1 ladder).
 *
 * The code is shown EXACTLY once (it is one-shot + 5-minute TTL); the
 * countdown keeps the TTL honest so a code that quietly expired is never
 * pasted. Reuses POST /api/v1/desktop/pair-code. */
function _lcPairCode(btnId, boxId) {
  var btn = document.getElementById(btnId || 'lcPairBtn');
  var box = document.getElementById(boxId || 'lcPairBox');
  if (btn) btn.disabled = true;
  Promise.resolve(Api.desktop.mintPairCode())
    .then(function (r) {
      if (btn) btn.disabled = false;
      if (!r || !r.code) {
        if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
        return;
      }
      if (!box) return;
      var code = String(r.code);
      var expiresAt = Number(r.expires_at || 0);
      box.style.display = '';
      box.innerHTML =
        '<div class="lc-pair-code">' +
          '<span class="lc-pair-digits">' + _lcEsc(code) + '</span>' +
          '<button type="button" class="btn btn-primary btn-sm" id="lcPairCopy">' +
            _lcEsc(_lcT('browser.clickToCopy', '点击复制')) + '</button>' +
        '</div>' +
        '<p class="lc-substep" id="lcPairCountdown">' +
          _lcEsc(_lcT('local.pairHint',
            '把这 6 位数字填进受控端首次启动 —— 它自己找服务器并完成配对（无需地址、无需隧道）。')) +
        '</p>';
      var copyBtn = document.getElementById('lcPairCopy');
      if (copyBtn) {
        copyBtn.onclick = function () {
          if (typeof _safeClipboardWrite === 'function') {
            _safeClipboardWrite(code)
              .then(function () {
                copyBtn.textContent = _lcT('local.copied', '已复制');
                if (typeof showToast === 'function') {
                  showToast(_lcT('local.pairCopied',
                    '配对码已复制 —— 粘贴到受控端首次启动'));
                }
              })
              .catch(function () {});
          }
        };
      }
      // Countdown: keep the TTL honest. Refresh once a second until expiry;
      // past-zero the box greys out and offers to re-mint (the button
      // stays — clicking it again just mints a fresh code).
      var cd = document.getElementById('lcPairCountdown');
      var started = Date.now();
      var iv = setInterval(function () {
        var left = Math.max(0, Math.round(expiresAt - Date.now() / 1000));
        var m = Math.floor(left / 60), s = left % 60;
        if (cd) {
          cd.textContent = _lcT('local.pairExpires',
            '配对码 {mm}:{ss} 后过期').replace('{mm}', m)
            .replace('{ss}', (s < 10 ? '0' : '') + s);
        }
        if (left <= 0) {
          clearInterval(iv);
          if (cd) cd.textContent = _lcT('local.pairExpired',
            '配对码已过期 —— 再点一次生成新码');
        }
      }, 1000);
    })
    .catch(function () {
      if (btn) btn.disabled = false;
      if (typeof showToast === 'function') showToast(_lcT('devices.mintFailed', '生成失败'));
    });
}

/* The ONE action of the on-disk browser case: ask the server to open this
 * machine's browser at its extensions page, and copy the extension path from
 * the FRONTEND (navigator.clipboard — a headless server has no clipboard, so
 * this half must happen here). Both fire together. The three remaining clicks
 * live inside the browser's sandbox — the note never claims the install is
 * finished.
 *
 * This handler is only reachable when the backend probe already found a
 * drivable browser (see _lcBrowserSetupState), so "no browser installed" is
 * no longer one of the outcomes it has to explain — that case never renders
 * the button in the first place. What remains is a genuine launch failure, so
 * the note says what to do by hand rather than guessing at a cause. */
function _lcOpenExtensionsPage(path) {
  var btn = document.getElementById('lcExtOpenBtn');
  var note = document.getElementById('lcExtOpenNote');
  if (btn) btn.disabled = true;
  var copied = (path && typeof _safeClipboardWrite === 'function')
    ? Promise.resolve(_safeClipboardWrite(path)).catch(function () {})
    : Promise.resolve();
  var opened = (typeof Api !== 'undefined' && Api.browser &&
                typeof Api.browser.openExtensions === 'function')
    ? Promise.resolve(Api.browser.openExtensions()).catch(function () { return null; })
    : Promise.resolve(null);
  Promise.all([copied, opened]).then(function (results) {
    if (btn) btn.disabled = false;
    var r = results[1];
    if (!note) return;
    if (r && r.ok) {
      note.textContent = _lcT('local.browserPageOpened',
        '已在你的浏览器打开扩展管理页，路径已复制 —— 剩下三步只能你来点。');
    } else {
      note.textContent = _lcT('local.browserPageOpenFailed',
        '没能替你打开 —— 请自己打开浏览器的扩展管理页，路径已复制。');
    }
  });
}

/* Human-readable size for a download label (bytes → '115 MB'). */
function _lcFmtSize(bytes) {
  var n = Number(bytes);
  if (!isFinite(n) || n <= 0) return '';
  var mb = n / 1048576;
  return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + ' MB';
}

/* Re-base a server-built same-origin URL onto the CURRENT proxy base path.
 *
 * `downloads[].url` is built by the backend from request.host_url — which
 * under a path-prefixed cloud-IDE proxy (…/proxy/15000/) is the origin
 * WITHOUT the prefix: the proxy strips the prefix before forwarding, so the
 * backend structurally cannot see it. Clicking such a link hits the
 * gateway's default route and returns "not found" without the request ever
 * reaching Tofu (the access log shows zero /desktop/download hits). Same
 * failure class as the paper PDF URL (pdf_viewer.js _resolvePaperPdfUrl):
 * strip back to the canonical /api/... tail and re-apply the LIVE base path
 * via apiUrl(). URLs with no /api/ marker (the releases-page escape hatch)
 * pass through untouched, and so does everything when apiUrl is absent. */
function _lcResolveDlUrl(url) {
  if (!url || typeof apiUrl !== 'function') return url;
  var i = url.indexOf('/api/');
  if (i < 0) return url;
  return apiUrl(url.slice(i));
}

/* The download instruction — authored ONCE for both install branches.
 *
 * ── Why per-platform links instead of the releases page ──
 * `download_url` alone points at `…/releases/latest`, a page carrying FIVE
 * assets (two DMGs, an .exe, a .tar.gz, SHA256SUMS). Handing that to a user who
 * asked "how do I install this" makes them identify their own OS and CPU
 * architecture from a list of filenames. The backend already knows the OS from
 * the request, so `downloads` carries the installer(s) this visitor can
 * actually run, each with a direct-download URL.
 *
 * ── Why this may render TWO links, and why that is correct ──
 * On macOS the architecture is genuinely unknowable unless the browser tells
 * us: an Apple Silicon Mac reports "Intel Mac OS X" in its UA. When
 * `getHighEntropyValues` is unavailable (Safari, older browsers) the backend
 * returns BOTH DMGs, and each is labelled with its chip so the user can pick in
 * one glance. Guessing one would give roughly half of Mac users a download
 * that refuses to open — a silent dead end far worse than a two-item choice.
 *
 * Always keeps the releases-page link as a secondary "all downloads" escape
 * hatch: it is the only thing that still works for an unrecognised platform, a
 * release missing an asset, or an unreachable GitHub API. */
function _lcDownloadLinks(d, kind, suppressPage) {
  kind = kind || 'full';
  var page = ((d && d.download_url) || '').trim();
  var raw = (kind === 'agent')
    ? (d && d.agent_downloads) : (d && d.downloads);
  var picks = Array.isArray(raw) ? raw : [];
  var labelKey = (kind === 'agent')
    ? 'local.agentDownloadFor' : 'local.desktopDownloadFor';
  var labelFb = (kind === 'agent') ? '受控端·轻量' : '下载桌面版';
  var html = '';
  if (picks.length) {
    html += '<p class="lc-dl-row">';
    for (var i = 0; i < picks.length; i++) {
      var p = picks[i] || {};
      if (!p.url) continue;
      // The label names the CHIP, not just the OS — the whole point of the
      // two-DMG case is telling the user which one is theirs. Size goes in
      // the label: a 100+ MB installer with no size shown is a bad surprise.
      html += '<a class="lc-dl-link lc-dl-direct" href="' +
        _lcEsc(_lcResolveDlUrl(p.url)) +
        '" target="_blank" rel="noopener noreferrer" title="' +
        _lcEsc(p.filename || '') + '">' +
        _lcEsc(_lcT(labelKey, labelFb) + ' · ' +
               (p.label || p.arch || '') +
               (p.size ? ' · ' + _lcFmtSize(p.size) : '')) + '</a>' +
        // Provenance: an artifact served by THIS server (not the public
        // GitHub network) is the fast/reliable path — say so, or the user
        // cannot tell why this link is preferable to the releases page.
        (p.hosted === 'server'
          ? '<span class="lc-dl-hosted">' +
            _lcEsc(_lcT('local.desktopHosted', '服务器直连')) + '</span>'
          : '');
    }
    html += '</p>';
    if (picks.length > 1) {
      // Say WHY there are two, or the choice reads as a UI defect.
      html += '<p class="lc-substep">' + _lcEsc(_lcT('local.desktopArchAmbiguous',
        '浏览器没告诉我们这台 Mac 的芯片型号（Apple Silicon 也会自称 Intel）。' +
        'Apple 芯片（M1/M2/M3…）选 arm64，Intel 芯片选 x86_64；' +
        '在「关于本机」里可以看到。')) + '</p>';
    }
  }
  if (page && !suppressPage) {
    html += '<p class="lc-substep"><a class="lc-dl-link" id="lcDesktopDownload" href="' +
      _lcEsc(page) + '" target="_blank" rel="noopener noreferrer">' +
      _lcEsc(picks.length
        ? _lcT('local.desktopDownloadAll', '查看全部下载 ↗')
        : _lcT('local.desktopDownload', '下载桌面版 ↗')) + '</a></p>';
  }
  return html;
}

/* Build the one line the user pastes into the desktop app's connect field.
 * Both halves are required — the server address is what makes the token
 * usable, so they travel together in a single copy. */
function _lcConnectLine(serverUrl, token) {
  var srv = (serverUrl || '').trim().replace(/\/+$/, '');
  return srv ? (srv + '  ' + token) : token;
}

// ══════════════════════════════════════════════════════
//  Switches — flip the REAL wire flags, one per capability
// ══════════════════════════════════════════════════════

function toggleBrowserFromLocalModal() {
  var sw = document.getElementById('lcBrowserSwitch');
  if (sw && sw.disabled) return;   // not connected — turning it on grants nothing
  if (typeof _applyBrowserUI === 'function') _applyBrowserUI(!browserEnabled);
  if (typeof _saveConvToolState === 'function') _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcBrowserSwitch', browserEnabled, _lcReach.browser !== false);
  _lcUpdateBadge();
}

function toggleDesktopFromLocalModal() {
  var sw = document.getElementById('lcDesktopSwitch');
  if (sw && sw.disabled) return;   // no agent — turning it on grants nothing
  if (typeof _applyDesktopUI === 'function') _applyDesktopUI(!desktopEnabled);
  if (typeof _saveConvToolState === 'function') _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcDesktopSwitch', desktopEnabled, _lcReach.desktop !== false);
  _lcUpdateBadge();
}

/* ONE summary badge on the merged toolbar entry, counting whichever
 * capabilities are on. The merged row is `active` when either is.
 *
 * ── Enabled ≠ working, and the badge must not blur the two ──
 * `_build_browser` / `_build_desktop` return [] when their bridge is not
 * connected, so a flag that is ON while the bridge is down contributes
 * literally zero tools. Counting it the same as a live one restates the very
 * claim this merge exists to stop making — the user closes the modal, sees a
 * confident badge, and reasonably concludes the AI can reach their machine.
 * A capability confirmed unreachable is marked, not hidden: hiding it would
 * lose the fact that the flag is still ON and still travelling to the server.
 * `null` (never probed — the modal has not been opened this session) counts as
 * live, because presenting an unverified capability as broken is its own lie. */
function _lcUpdateBadge() {
  var bOn = (typeof browserEnabled !== 'undefined' && browserEnabled);
  var dOn = (typeof desktopEnabled !== 'undefined' && desktopEnabled);
  var n = (bOn ? 1 : 0) + (dOn ? 1 : 0);
  var stale = ((bOn && _lcReach.browser === false) ? 1 : 0)
            + ((dOn && _lcReach.desktop === false) ? 1 : 0);
  var badge = document.getElementById('localControlBadge');
  if (badge) {
    badge.textContent = n > 0 ? String(n) : '';
    badge.style.display = n > 0 ? '' : 'none';
    badge.classList.toggle('visible', n > 0);
    badge.classList.toggle('lc-badge-stale', stale > 0);
    if (stale > 0) {
      badge.title = _lcT('local.badgeStale',
        '已开启，但当前未连接 —— AI 实际拿不到这些工具。');
    } else {
      badge.removeAttribute('title');
    }
  }
  var row = document.getElementById('localControlToggle');
  if (row) {
    row.classList.toggle('active', n > 0);
    row.classList.toggle('lc-row-stale', stale > 0);
  }
}

if (typeof window !== 'undefined') {
  window.openLocalControlModal = openLocalControlModal;
  window.closeLocalControlModal = closeLocalControlModal;
  window.toggleBrowserFromLocalModal = toggleBrowserFromLocalModal;
  window.toggleDesktopFromLocalModal = toggleDesktopFromLocalModal;
  window._lcUpdateBadge = _lcUpdateBadge;
  window._lcBrowserSetupState = _lcBrowserSetupState;
  window._lcPaintFloor = _lcPaintFloor;
  window._lcBrowserDownload = _lcBrowserDownload;
  window._lcConnectLine = _lcConnectLine;
  window._lcDownloadLinks = _lcDownloadLinks;
  window._lcResolveArch = _lcResolveArch;
  window._lcSetAbout = _lcSetAbout;
  window._lcRenderBrowser = _lcRenderBrowser;
  window._lcRenderDesktop = _lcRenderDesktop;
}
