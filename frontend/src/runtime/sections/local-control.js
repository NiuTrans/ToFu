/* ===== migrated source: local-control.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   local-control — the SINGLE "let Tofu act on my machine" surface.

   Merges what used to be two toolbar rows (#browserToggle + #desktopToggle),
   one setup modal (#browserModal) and one blind desktop flag flip (with
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

   This demand-loaded owner contains the modal, probes, downloads and browser
   relay. The retained local-control-state.js owns only the toolbar badge, so
   ordinary coding and writing sessions do not parse this workbench.
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
var LocalControlPresentationState = Object.freeze({ reach: _lcReach });

/* Signatures and explicit entry intent used by the setup renderers.
 * `_lcDesktopSigLast = null` forces a desktop render on reopen. Settings can
 * also open this modal because a site adapter negotiated a concrete browser
 * capability gap even when a legacy extension reports no manifest version;
 * retain that stronger signal for this modal session. */
var _lcDesktopSigLast = null;
var _lcBrowserUpgradeRequested = false;

/* ── Browser-assisted desktop transport (Codelab / web VS Code) ───────
 *
 * A /proxy/<port> URL behind SSO is reachable in this tab but not from the
 * cookieless TofuAgent process.  The agent therefore exposes a loopback-only
 * broker on 15180..15189.  This authenticated page carries each desktop poll
 * through the gateway with `credentials: include` and hands the response back
 * to the agent.  Cookies never leave the browser; the local broker accepts
 * only this configured Tofu Origin and answers Private Network Access
 * preflights itself.
 *
 * The loop survives closing this modal and stops naturally when the Tofu tab
 * closes.  Clicking the agent download starts a 30-minute discovery watch,
 * covering the normal download → install interval.  Existing installs can use
 * the visible button in the SSO hint; the native panel opens this page with
 * #tofu-agent-relay.  We deliberately do not scan localhost merely because a
 * modal opened — Chromium may show a Local Network Access permission prompt.
 */
var _LC_AGENT_RELAY_PORT_START = 15180;
var _LC_AGENT_RELAY_PORT_END = 15189;
var _lcAgentRelay = { base: '', running: false, state: 'idle', detail: '', watchUntil: 0, watchTimer: null,
  attachPushing: false, attachDone: false };

function _lcRelayHintText() {
  if (_lcAgentRelay.state === 'attaching') {
    return _lcT('local.relayAttaching',
      '正在为受控端写入连接信息……');
  }
  if (_lcAgentRelay.state === 'attached') {
    return _lcT('local.relayAttached',
      '连接信息已写入受控端——它将自行连接服务器，本页可以关闭。');
  }
  if (_lcAgentRelay.state === 'connected') {
    return _lcT('local.relayConnected',
      '浏览器安全通道已接通——请保持这个 Tofu 标签页打开。');
  }
  if (_lcAgentRelay.state === 'forwarding') {
    return _lcT('local.relayForwarding',
      '已找到受控端，正在用当前浏览器的登录态接通 Codelab……');
  }
  if (_lcAgentRelay.state === 'error') {
    return _lcT('local.relayError',
      '浏览器通道暂时中断，正在自动重连；请确认当前 Tofu 网页仍处于登录状态。');
  }
  return _lcT('local.relayWaiting',
    '当前地址有 Codelab/SSO 登录墙。安装受控端后保持本 Tofu 标签页打开，网页会自动接力连接，无需配置 SSH。');
}

function _lcPaintRelayHint() {
  var el = document.getElementById('lcAgentRelayHint');
  if (el) el.textContent = _lcRelayHintText();
  var btn = document.getElementById('lcAgentRelayStart');
  if (btn) btn.style.display = _lcAgentRelay.state === 'connected' ? 'none' : '';
}

function _lcRelayHintHtml(d) {
  if (!d || d.server_url_reachability !== 'public') return '';
  return '<p class="lc-step lc-await" id="lcAgentRelayHint">' +
    _lcEsc(_lcRelayHintText()) + '</p>' +
    '<button type="button" class="btn btn-primary btn-sm" id="lcAgentRelayStart" ' +
    'data-tofu-action="_lcEnsureAgentRelay(1800000)">' +
    _lcEsc(_lcT('local.relayStart', '立即通过浏览器连接')) + '</button>';
}

function _lcLocalRelayFetch(url, options, timeoutMs) {
  var controller = (typeof AbortController !== 'undefined')
    ? new AbortController() : null;
  var opts = Object.assign({ mode: 'cors', credentials: 'omit',
    cache: 'no-store', targetAddressSpace: 'local' }, options || {});
  if (controller) opts.signal = controller.signal;
  var timer = controller ? setTimeout(function () { controller.abort(); },
    timeoutMs || 15000) : null;
  return fetch(url, opts).finally(function () {
    if (timer) clearTimeout(timer);
  });
}

function _lcRelayExpectedPollUrl() {
  var path = (typeof apiUrl === 'function')
    ? apiUrl('/api/desktop/poll') : '/api/desktop/poll';
  try { return new URL(path, window.location.href).href; }
  catch (e) { return ''; }
}

function _lcDiscoverAgentRelay() {
  /* Resolves {base, attached} for the first live broker, else null.
   * `attached === false` (the agent's bootstrap mode) means the broker is
   * waiting for this page to push its attach bundle; agents built before
   * the field existed report nothing and read as attached — the legacy
   * relay-only flow. */
  if (typeof fetch !== 'function') return Promise.resolve(null);
  var probes = [];
  for (var p = _LC_AGENT_RELAY_PORT_START;
       p <= _LC_AGENT_RELAY_PORT_END; p++) {
    (function (port) {
      var base = 'http://127.0.0.1:' + port;
      probes.push(_lcLocalRelayFetch(base + '/v1/status', {}, 650)
        .then(function (r) {
          if (!r || !r.ok) return null;
          return r.json().then(function (body) {
            return body && body.kind === 'tofu-agent-browser-relay'
              ? { base: base, attached: body.attached !== false } : null;
          });
        }).catch(function () { return null; }));
    })(p);
  }
  return Promise.all(probes).then(function (rows) {
    for (var i = 0; i < rows.length; i++) if (rows[i]) return rows[i];
    return null;
  });
}

/* The page's own reachable base (origin + proxy prefix) — the ?base= value
 * both zero-config channels pin server-side to the request's Host. */
function _lcPageBase() {
  try {
    var origin = (window.location && window.location.origin) || '';
    // 'null' (file://, opaque origins) must never become a query value.
    if (/^https?:\/\//.test(origin)) {
      return origin + ((typeof BASE_PATH === 'string') ? BASE_PATH : '');
    }
  } catch (e) { /* no usable origin — the backend falls back to host_url */ }
  return '';
}

/* Zero-config attach push — the macOS/Linux counterpart of the personalized
 * Windows installer. The unattached agent's broker gets its routes + a fresh
 * credential from THIS signed-in page (the user downloaded the agent from
 * here, so this page IS the proof of which server is meant). The broker's
 * answer picks the continuation: transport 'browser' keeps this page
 * carrying polls (SSO case); 'direct' means the agent polls on its own and
 * the watch's job is done — probing further would only make the agent
 * prefer this page's transport over its own faster LAN route. */
function _lcPushAttach(base) {
  if (_lcAgentRelay.attachPushing || _lcAgentRelay.attachDone) return;
  _lcAgentRelay.attachPushing = true;
  _lcAgentRelay.state = 'attaching';
  _lcPaintRelayHint();
  var finish = function () { _lcAgentRelay.attachPushing = false; };
  var mint = (typeof Api !== 'undefined' && Api.desktop &&
    typeof Api.desktop.mintAttachBundle === 'function')
    ? Promise.resolve(Api.desktop.mintAttachBundle(_lcPageBase()))
    : Promise.reject(new Error('Api.desktop.mintAttachBundle unavailable'));
  mint.then(function (bundle) {
    if (!bundle || (!bundle.candidates && !bundle.fallback_candidates)) {
      throw new Error('empty attach bundle');
    }
    // The attach handler probes each candidate (up to 2.5 s apiece), so
    // this fetch needs a generous budget — 45 s covers the worst walk.
    return _lcLocalRelayFetch(base + '/v1/attach', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bundle)
    }, 45000);
  }).then(function (resp) {
    if (!resp) throw new Error('no attach response');
    if (resp.status === 404) {
      // An agent build predating /v1/attach: the relay still works — enter
      // it and never retry the push.
      _lcAgentRelay.attachDone = true;
      finish();
      _lcAgentRelayLoop(base);
      return;
    }
    return resp.json().then(function (body) {
      if (resp.ok && body && body.accepted) {
        _lcAgentRelay.attachDone = true;
        finish();
        if (body.transport === 'browser') {
          _lcAgentRelayLoop(base);
          return;
        }
        _lcAgentRelay.watchUntil = 0;
        _lcAgentRelay.state = 'attached';
        _lcPaintRelayHint();
        return;
      }
      if (body && body.reason === 'already_attached') {
        _lcAgentRelay.attachDone = true;
        finish();
        _lcAgentRelayLoop(base);
        return;
      }
      throw new Error('attach refused: ' +
        ((body && body.reason) || resp.status));
    });
  }).catch(function (e) {
    finish();
    _lcAgentRelay.state = 'error';
    _lcAgentRelay.detail = String(e && e.message || e);
    _lcPaintRelayHint();
    // The agent may still be mid-install — keep the watch alive and retry.
    if (Date.now() < _lcAgentRelay.watchUntil) {
      _lcAgentRelay.watchTimer = setTimeout(_lcRelayWatchBeat, 4000);
    }
  });
}

function _lcReturnRelayResult(base, id, status, body) {
  return _lcLocalRelayFetch(base + '/v1/result', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, status: status, body: body })
  }, 5000);
}

async function _lcAgentRelayLoop(base) {
  if (_lcAgentRelay.running) return;
  _lcAgentRelay.running = true;
  _lcAgentRelay.base = base;
  _lcAgentRelay.state = 'forwarding';
  _lcPaintRelayHint();
  try {
    while (_lcAgentRelay.base === base) {
      var take = await _lcLocalRelayFetch(base + '/v1/take', {}, 13000);
      if (take.status === 204) continue;
      if (!take.ok) throw new Error('local relay HTTP ' + take.status);
      var job = await take.json();
      var expected = _lcRelayExpectedPollUrl();
      var target = '';
      try { target = new URL(job.url, window.location.href).href; }
      catch (e) { target = ''; }
      if (!expected || target !== expected) {
        await _lcReturnRelayResult(base, job.id, 409,
          JSON.stringify({ error: 'relay_target_mismatch' }));
        continue;
      }
      try {
        var remote = await Api.desktop.relayPoll(
          job.payload || {},
          job.headers && job.headers['X-Bridge-Secret']);
        var responseBody = await remote.text();
        await _lcReturnRelayResult(base, job.id, remote.status, responseBody);
        _lcAgentRelay.state = remote.ok ? 'connected' : 'error';
        _lcAgentRelay.detail = 'HTTP ' + remote.status;
        _lcPaintRelayHint();
      } catch (remoteErr) {
        await _lcReturnRelayResult(base, job.id, 502,
          JSON.stringify({ error: 'browser_gateway_unreachable' }))
          .catch(function () {});
        _lcAgentRelay.state = 'error';
        _lcAgentRelay.detail = String(remoteErr && remoteErr.message || remoteErr);
        _lcPaintRelayHint();
      }
    }
  } catch (e) {
    _lcAgentRelay.state = 'error';
    _lcAgentRelay.detail = String(e && e.message || e);
    _lcPaintRelayHint();
  } finally {
    _lcAgentRelay.running = false;
    if (_lcAgentRelay.base === base) _lcAgentRelay.base = '';
    if (Date.now() < _lcAgentRelay.watchUntil) {
      setTimeout(_lcRelayWatchBeat, 1000);
    }
  }
}

function _lcRelayWatchBeat() {
  _lcAgentRelay.watchTimer = null;
  if (_lcAgentRelay.running || _lcAgentRelay.attachPushing ||
      Date.now() >= _lcAgentRelay.watchUntil) return;
  _lcDiscoverAgentRelay().then(function (info) {
    if (info && info.base) {
      if (info.attached === false && !_lcAgentRelay.attachDone) {
        // Unattached agent found — push its bundle instead of relaying.
        _lcPushAttach(info.base);
        return;
      }
      _lcAgentRelayLoop(info.base);
    } else if (Date.now() < _lcAgentRelay.watchUntil) {
      _lcAgentRelay.watchTimer = setTimeout(_lcRelayWatchBeat, 1800);
    }
  });
}

function _lcEnsureAgentRelay(durationMs) {
  var duration = Number(durationMs) || (30 * 60 * 1000);
  _lcAgentRelay.watchUntil = Math.max(_lcAgentRelay.watchUntil,
    Date.now() + duration);
  if (!_lcAgentRelay.running && !_lcAgentRelay.watchTimer) {
    _lcRelayWatchBeat();
  }
}

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
          d.visitor_os || '',
          fp(d.downloads), fp(d.agent_downloads), lang,
          d.server_url_reachability || '', d.bridge_tokens_issued || 0,
          d.server_bind || '', d.agent_installer_ready === true
         ].join('~');
}

/* Installer downloaded but nothing has arrived yet. Recovery belongs to the
 * agent, not the user: it re-probes its internal routes by itself. */
function _lcAwaitingAgentHtml(d) {
  if (!d || d.connected || !(d.bridge_tokens_issued > 0)) return '';
  if (d.server_url_reachability === 'public') {
    return '<p class="lc-step lc-await">' + _lcEsc(_lcT(
      'local.awaitingAgentBrowser',
      '正在等待受控端首次连入。此 Codelab 地址有 SSO 登录墙；保持本 Tofu 标签页打开，网页会自动接通受控端。')) + '</p>';
  }
  return '<p class="lc-step lc-await">' + _lcEsc(_lcT('local.awaitingAgent',
    '正在等待受控端首次连入……它会自动寻找服务器并重试，一般一分钟内变绿。')) + '</p>';
}

/* ── The diagnostics inbox (owner ask 2026-08-06) ──
 * A controlled machine that cannot reach this server cannot push its logs
 * anywhere — debugging it blind was the 2026-08-06 incident's blind spot.
 * The agent's window/tray has「复制诊断信息」; the user pastes the bundle
 * HERE and it lands in logs/desktop_client_diag.log on the server, where
 * the operator/assistant reads it directly. Collapsed by default so it
 * never competes with the ONE primary install action. */
function _lcDiagInboxHtml() {
  return '<details class="lc-details lc-diag"><summary>' +
    _lcEsc(_lcT('local.diagTitle', '受控端连不上？把它的诊断信息粘贴到这里')) +
    '</summary>' +
    '<p class="lc-substep">' + _lcEsc(_lcT('local.diagDesc',
      '在受控端窗口点「复制诊断信息」（或托盘菜单同名项），回到这里粘贴提交——服务器会直接存盘，排查时立刻能读到，不用截图不用转述。')) + '</p>' +
    '<textarea id="lcDiagText" class="lc-diag-text" rows="6" spellcheck="false" placeholder="' +
    _lcEsc(_lcT('local.diagPlaceholder', 'Ctrl+V 粘贴诊断信息……')) + '"></textarea>' +
    '<div class="lc-diag-actions">' +
    '<button type="button" id="lcDiagSubmit" class="btn btn-primary btn-sm">' +
    _lcEsc(_lcT('local.diagSubmit', '提交诊断')) + '</button>' +
    '<span id="lcDiagHint" class="lc-diag-hint"></span></div>' +
    '<div id="lcDiagRecent" class="lc-diag-recent"></div>' +
    '</details>';
}

function _lcDiagRefreshRecent() {
  var box = document.getElementById('lcDiagRecent');
  if (!box) return;
  // jsdom harnesses splice the renderers without an Api stub — the
  // refresh is best-effort, never a render-time throw.
  if (typeof Api === 'undefined' || !Api.desktop ||
      typeof Api.desktop.listDiags !== 'function') return;
  Promise.resolve(Api.desktop.listDiags()).then(function (r) {
    var boxNow = document.getElementById('lcDiagRecent');
    if (!boxNow) return;
    if (!r || !Array.isArray(r.entries) || !r.entries.length) {
      boxNow.innerHTML = '';
      return;
    }
    var rows = r.entries.slice(0, 5).map(function (e) {
      var when = e.ts ? new Date(e.ts * 1000).toLocaleString() : '?';
      return '<div class="lc-diag-row">' + _lcEsc(when) + ' · ' +
        _lcEsc(String(e.chars || 0)) + ' chars</div>';
    }).join('');
    boxNow.innerHTML = '<div class="lc-diag-recent-head">' +
      _lcEsc(_lcT('local.diagRecent', '最近提交')) + '</div>' + rows;
  });
}

function _lcWireDiag() {
  var btn = document.getElementById('lcDiagSubmit');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var ta = document.getElementById('lcDiagText');
    var hint = document.getElementById('lcDiagHint');
    var text = ta ? ta.value.trim() : '';
    if (!text) {
      if (hint) hint.textContent = _lcT('local.diagEmpty', '先粘贴诊断信息再提交');
      return;
    }
    btn.disabled = true;
    Promise.resolve(Api.desktop.submitDiag(text)).then(function (r) {
      btn.disabled = false;
      if (!r) {
        if (hint) hint.textContent = _lcT('local.diagFailed', '提交失败——稍后再试');
        return;
      }
      if (ta) ta.value = '';
      if (hint) hint.textContent = _lcT('local.diagDone', '已收到——服务器已存盘，可以去排查了');
      _lcDiagRefreshRecent();
    });
  });
  _lcDiagRefreshRecent();
}

/* The ONE primary attach action — chosen per VISITOR PLATFORM.
 *
 * Windows gets the personalized one-file installer: route candidates and the
 * minted credential live inside the .exe's NSIS trailer; the user never
 * sees, copies, or pastes anything. That personalization is WINDOWS-ONLY BY
 * CONSTRUCTION (lib/desktop_dist/agent_installer.py rewrites an NSIS
 * trailer), so offering the same button to a Mac/Linux visitor downloads a
 * package the machine cannot run — the measured defect: a Mac client's
 * primary action fetched TofuAgent-Setup-*-win64.exe. Non-Windows visitors
 * therefore get the mirrored per-platform agent asset from
 * `d.agent_downloads` (macOS DMG / Linux tarball) via _lcDownloadLinks.
 *
 * The platform itself comes from the BACKEND (`visitor_os` on the status
 * payload — the same _detect_os that picks the download rows), never
 * re-derived here; `osHint` exists only for the floor's first frame, which
 * paints before any status answer (see _lcPaintFloor/_lcFloorOsHint) and is
 * replaced by detection a beat later. An UNRECOGNISED platform ('') never
 * gets the .exe: guessing hands an unrunnable package to exactly the
 * visitor we know least about — the releases page is the honest offer.
 *
 * The `base` query carries the browser's live origin + BASE_PATH (e.g.
 * /proxy/15000 behind a cloud-IDE gateway) — the backend cannot see the
 * prefix (the proxy strips it), and baking request.host_url produced the
 * dead-address class that stranded the fleet (extension "HTTP 405",
 * 2026-08-04). The backend pins the param to the request's Host. */
function _lcAgentInstallerUrl() {
  var u = '/api/v1/desktop/agent-installer';
  if (typeof apiUrl === 'function') u = apiUrl(u);
  var base = _lcPageBase();
  return u + (base ? ('?base=' + encodeURIComponent(base)) : '');
}

function _lcInstallerWaitHtml() {
  // A stale/absent artifact must not render a dead button. Do not offer a
  // multi-file/manual fallback: polling replaces this note when ready.
  return '<p class="lc-substep">' + _lcEsc(_lcT('local.installerRebuilding',
      '受控端安装包正在后台准备，完成后下载按钮会自动出现。')) + '</p>';
}

function _lcAgentInstallerBlockHtml(d, osHint) {
  var os = (d && typeof d.visitor_os === 'string' && d.visitor_os) ||
    osHint || '';
  if (os === 'windows') {
    if (d && d.agent_installer_ready) {
      return '<a class="btn btn-primary btn-sm" id="lcAgentInstallerBtn" href="' +
        _lcEsc(_lcAgentInstallerUrl()) +
        '" data-tofu-action="_lcEnsureAgentRelay(1800000)">' +
        _lcEsc(_lcT('local.agentInstallerBtn',
          '下载受控端安装包')) + '</a>' +
        '<p class="lc-substep">' + _lcEsc(_lcT('local.agentInstallerNote',
          '下载后直接运行即可。服务器地址和连接信息都已内置，无需额外设置。')) + '</p>';
    }
    return _lcInstallerWaitHtml();
  }
  var picks = (d && Array.isArray(d.agent_downloads)) ? d.agent_downloads : [];
  if (picks.length) {
    // The per-platform links carry their own chip labels/sizes; the page
    // link stays with the full-installer details below (one escape hatch,
    // not two identical ones).
    return _lcDownloadLinks(d, 'agent', true);
  }
  if (os) {
    // The mirror has not landed this platform's agent asset yet — the 3 s
    // poll replaces this note when it does. The releases page stays visible
    // as the escape hatch that always works.
    return _lcInstallerWaitHtml() +
      _lcDownloadLinks({ download_url: d && d.download_url, agent_downloads: [] },
        'agent');
  }
  // Unrecognised platform: nothing direct is honest — releases page only.
  return _lcDownloadLinks({ download_url: d && d.download_url, agent_downloads: [] },
    'agent');
}

/* A loopback-bound server behind a proxy cannot be reached DIRECTLY by a
 * remote agent (direct LAN refuses; the SSO edge 401s cookieless calls).
 * Measured 2026-08-05: a platform-injected BIND_HOST=127.0.0.1 quietly
 * overrode the 0.0.0.0 default and the whole attach flow failed silently.
 * The panel says so out loud — the one operator action that actually
 * unblocks direct attach. */
function _lcBindWarnHtml(d) {
  if (!d || d.server_bind !== 'loopback') return '';
  if (d.setup_state !== 'remote' && d.setup_state !== 'local_source') return '';
  return '<p class="lc-step lc-await">' + _lcEsc(_lcT('local.bindLoopbackWarn',
    '⚠️ 服务器当前只监听本机回环（BIND_HOST=127.0.0.1）——受控端无法直连内网地址，将使用浏览器安全通道或自动隧道；去掉该环境变量并重启后可直连，更快更稳。')) + '</p>';
}

function openLocalControlModal(options) {
  var el = document.getElementById('localControlModal');
  if (!el) return;
  _lcBrowserUpgradeRequested = !!(options && options.browserUpgrade);
  el.classList.add('open');
  _lcDesktopSigLast = null;
  _lcPaintFloor();
  _lcRefresh();
  if (_lcPollTimer) clearInterval(_lcPollTimer);
  _lcPollTimer = setInterval(_lcRefresh, _LC_POLL_MS);
}

/* First-frame OS sniff for the FLOOR ONLY. The authoritative platform is
 * the backend's `visitor_os` (one _detect_os owns the rule, shared with the
 * download rows); this exists because the floor paints before any status
 * answer, and offering the Windows .exe to a Mac in that first frame is the
 * very defect the platform gate exists to kill. Detection replaces whatever
 * this picks a beat later. Deliberately narrow — phones and anything
 * unrecognised return '' (no direct offer), mirroring _detect_os. */
function _lcFloorOsHint() {
  try {
    var ua = (navigator.userAgent || '').toLowerCase();
    if (!ua) return '';
    if (ua.indexOf('android') >= 0 || ua.indexOf('iphone') >= 0 ||
        ua.indexOf('ipad') >= 0 || ua.indexOf('ipod') >= 0) return '';
    if (ua.indexOf('windows') >= 0) return 'windows';
    if (ua.indexOf('mac os x') >= 0 || ua.indexOf('macintosh') >= 0) {
      return 'macos';
    }
    if (ua.indexOf('linux') >= 0 || ua.indexOf('x11') >= 0) return 'linux';
  } catch (e) { /* a floor that cannot sniff simply waits for detection */ }
  return '';
}

/* Put a real, followable instruction in BOTH rows before anything is fetched.
 *
 * Runs synchronously on open, so the first frame the user sees already tells
 * them what to do. Everything here is derivable with ZERO backend knowledge:
 * downloading the extension ZIP and installing the controlled-end app are the steps
 * that hold whatever the probe later reports. The renderers then narrow this
 * to the state-specific instruction (load-unpacked with the on-disk path, the
 * tray toggle, the personalized installer) or clear it outright when connected.
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
    /* The ONE primary attach action needs ZERO backend knowledge, so it is
     * on screen in the first frame (owner-measured 2026-08-06: the desktop
     * half of this modal used to pop its install block one status round-trip
     * late, next to a browser half that never waits). The FULL-desktop link
     * still waits for the backend — its URL derives from UPDATE_REPO and a
     * hardcoded one would point a fork at the wrong releases page — but the
     * agent-installer URL is a pure frontend derivation,
     * and the endpoint answers honestly when the installer is not built yet
     * (404/409 JSON with the next step + a rebuild kick), never a silent
     * dead end. The .exe is WINDOWS-ONLY, so the floor's button is gated on
     * a UA sniff (_lcFloorOsHint); a Mac first frame gets the wait note,
     * which detection swaps for the real DMG links. Detection replaces this
     * a beat later with the state-specific instruction (rebuilding note /
     * tray toggle / cleared when connected). Reuse the detected branch's
     * own authoring for the button — two copies of one button drift, and a
     * drifted button is a dead one. */
    d.innerHTML = '<p class="lc-step">' + _lcEsc(_lcT('local.desktopFloorLead',
        '让 AI 操作这台电脑，只需安装轻量受控端：')) + '</p>' +
      _lcAgentInstallerBlockHtml({ agent_installer_ready: true },
        _lcFloorOsHint());
  }
}

function closeLocalControlModal() {
  var el = document.getElementById('localControlModal');
  if (el) el.classList.remove('open');
  _lcBrowserUpgradeRequested = false;
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
  /* Fleet recovery states: the server reports
   * the version a fresh download would carry (servedExtVersion) and the
   * clients whose polls DIED at the bridge gate (lockedOutClients) or strict
   * protocol handshake (incompatibleClients). A
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
  var incompatible = (!connected && d && Array.isArray(d.incompatibleClients))
    ? d.incompatibleClients : [];
  if (connected) {
    var ago = (d.secondsAgo != null) ? d.secondsAgo + 's' : '';
    if (clients.length > 0) {
      runtimeScope._browserClientId = clients[0].client_id;
    }
    /* Name WHICH extension binary is connected, next to the dot — the
     * version is the only at-a-glance evidence of a stale side-load. Only a
     * fleet-wide agreement earns the suffix: mixed versions stay unnamed here
     * because the upgrade nudge below is what names the stale install, and a
     * legacy client reporting no manifest version yields an empty set. */
    var extVers = [];
    for (var _vi = 0; _vi < clients.length; _vi++) {
      var _vv = String((clients[_vi] && clients[_vi].ext_version) || '').trim();
      if (_vv && extVers.indexOf(_vv) === -1) extVers.push(_vv);
    }
    var verSuffix = extVers.length === 1 ? ' · v' + extVers[0] : '';
    _lcSetStatus('lcBrowserStatus', true,
      clients.length > 1
        ? _lcT('local.connectedN', '已连接').replace('{n}', clients.length) + verSuffix
        : _lcT('local.connected', '已连接') + verSuffix + (ago ? ' · ' + ago : ''));
  } else {
    runtimeScope._browserClientId = null;
    _lcSetStatus('lcBrowserStatus', false,
      lockedOut.length
        ? _lcT('local.extDead', '已安装但凭证已失效')
        : incompatible.length
          ? _lcT('local.extUpgradeRequired', '已安装但需要升级')
        : _lcT('local.notInstalled', '尚未安装'));
  }

  _lcReach.browser = connected;
  _lcSetSwitch('lcBrowserSwitch',
    LocalControlShellState.browserEnabled, connected);
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
    if (staleFrom || _lcBrowserUpgradeRequested) {
      // Outdated but WORKING: nothing is broken, so this is a nudge, not
      // an alarm — one click on the preseeded zip upgrades in place. A
      // capability-triggered entry has stronger evidence than version diff:
      // legacy extensions often report no ext_version at all.
      var upgradeLead = staleFrom
        ? _lcT('local.browserExtOutdated',
          '扩展有新版本（{old} → {new}）——重新下载 ZIP 覆盖加载即可升级（已自动配对，零配置）：')
          .replace('{old}', staleFrom).replace('{new}', servedVer)
        : _lcT('local.browserExtCapabilityMismatch',
          '当前扩展已连接，但缺少站点适配器所需能力。重新下载 ZIP 并在扩展管理页覆盖加载，即可完成升级：');
      setup.innerHTML = '<p class="lc-step">' + _lcEsc(upgradeLead) + '</p>' +
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

  if (incompatible.length) {
    // The owner/device authenticated successfully, so re-pairing is the wrong
    // cure. It was rejected before registration because its protocol is old;
    // name that exact state and offer the current binary.
    var rejected = incompatible[0] || {};
    var reportedProtocol = rejected.protocol_version || '?';
    var requiredProtocol = (d && d.protocolVersion) || '?';
    setup.innerHTML =
      '<p class="lc-step">' + _lcEsc(_lcT('local.browserExtProtocolMismatch',
        '检测到浏览器扩展协议过旧（v{old} → v{new}），后端已安全拒绝命令。重新下载扩展 ZIP 并覆盖加载即可恢复：')
        .replace('{old}', reportedProtocol).replace('{new}', requiredProtocol)) + '</p>' +
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

function downloadBrowserExtension() {
  // Carry the browser's OWN base (origin + live BASE_PATH, e.g. /proxy/15000
  // behind a cloud-IDE gateway) so the zip's bridge_preseed pairs the
  // extension with an address this browser demonstrably reaches. A
  // server-side request.host_url loses both external https and proxy prefix.
  var base = encodeURIComponent(window.location.origin + BASE_PATH);
  window.open(apiUrl('/api/browser/download?base=' + base), '_blank');
}

/* Chrome 142+ Local Network Access prompts can fire per site during multi-tab
 * work. Only the Local Control browser card renders this recovery guidance. */
function _applyBrowserLnaWarning(chromeMajor) {
  var box = document.getElementById('browserLnaWarning');
  if (!box) return;
  if (!chromeMajor || chromeMajor < 142) {
    box.style.display = 'none';
    return;
  }
  box.style.display = '';
  var pol = document.getElementById('browserLnaPolicy');
  if (pol && !pol._wired) {
    pol._wired = true;
    pol.onclick = function () {
      if (typeof _safeClipboardWrite === 'function') {
        _safeClipboardWrite(pol.textContent)
          .then(function () { pol.classList.add('copied'); })
          .catch(function () {});
      }
    };
  }
  var pathEl = document.getElementById('browserLnaPath');
  if (pathEl) {
    var ua = (navigator.userAgent || '').toLowerCase();
    var dir = '';
    if (ua.indexOf('windows') >= 0) {
      dir = 'HKLM\\SOFTWARE\\Policies\\Google\\Chrome\\ (via registry / Group Policy)';
    } else if (ua.indexOf('mac os') >= 0 || ua.indexOf('macintosh') >= 0) {
      dir = "defaults write com.google.Chrome LocalNetworkAccessAllowedForUrls -array '*'";
    } else {
      dir = '/etc/opt/chrome/policies/managed/tofu-lna.json';
    }
    var label = (typeof t === 'function')
      ? t('browser.lnaPathLabel') : 'Place it at:';
    pathEl.style.display = '';
    pathEl.innerHTML = label + ' <code>' +
      dir.replace(/</g, '&lt;') + '</code>';
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
    LocalControlShellState.desktopEnabled, connected);
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
   * after opening. Rewrite only
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
      var agentSrc = Array.isArray(d.agent_downloads)
        ? d.agent_downloads : [];
      // The personalized installer carries connection data internally. No
      // pairing or connection-information fallback belongs in this panel.
      var htmlSrc = '<p class="lc-step">' + _lcEsc(_lcT('local.roleChoose',
          '当前 Tofu 以源码方式运行 —— 按这台电脑的角色选装：')) + '</p>';
      if (agentSrc.length) {
        htmlSrc +=
          '<div class="lc-role lc-role-primary">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.agentRoleHead', '受控端 · 轻量')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.agentRoleNote',
                '—— 从另一台电脑访问（如 ssh 转发）选它：只让服务器操作那台电脑')) + '</span></p>' +
            _lcAgentInstallerBlockHtml(d) +
          '</div>' +
          '<div class="lc-role">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.fullRoleHead', '完整桌面版')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.fullRoleNote',
                '—— 这台电脑就是服务器本机：装它，托盘一键开启')) + '</span></p>' +
            _lcDownloadLinks(d, 'full') +
          '</div>';
      } else {
        // The agent build is still in flight. Keep its role visible with one
        // honest wait state; do not redirect the user into manual pairing.
        htmlSrc +=
          '<div class="lc-role lc-role-primary">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.agentRoleHead', '受控端 · 轻量')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.agentRoleNote',
                '—— 从另一台电脑访问（如 ssh 转发）选它：只让服务器操作那台电脑')) + '</span></p>' +
            _lcAgentInstallerBlockHtml(d) +
          '</div>' +
          '<div class="lc-role">' +
            '<p class="lc-role-head">' +
              _lcEsc(_lcT('local.fullRoleHead', '完整桌面版')) +
              '<span class="lc-role-note">' + _lcEsc(_lcT('local.fullRoleNote',
                '—— 这台电脑就是服务器本机：装它，托盘一键开启')) + '</span></p>' +
            _lcDownloadLinks(d, 'full') +
          '</div>';
      }
      setup.innerHTML = _lcBindWarnHtml(d) + _lcRelayHintHtml(d) +
        _lcAwaitingAgentHtml(d) + htmlSrc + _lcDiagInboxHtml();
      _lcWireDiag();
      return;
    }

    default: {
      // Remote server — the machine in front of the user is NOT this
      // machine. Its role in this dialog is to be CONTROLLED: the
      // personalized agent installer (lightweight, no frontend; Windows
      // only — other platforms get the mirrored per-platform agent asset,
      // see _lcAgentInstallerBlockHtml) is primary;
      // the full desktop app is a one-line COLLAPSED secondary. When no
      // agent artifact exists yet (a build is in flight), the full
      // installer takes the primary slot with the historical instruction —
      // stale-while-build, never a dead end.
      //
      // One download, one runnable file; connection details stay internal.
      var agentPicks = Array.isArray(d.agent_downloads)
        ? d.agent_downloads : [];
      var html;
      if (agentPicks.length) {
        html =
          '<p class="lc-step">' + _lcEsc(_lcT('local.desktopRemoteAgent',
            'Tofu 运行在远程服务器上 —— 让 AI 操作这台电脑：')) + '</p>' +
          _lcAgentInstallerBlockHtml(d) +
          '<details class="lc-details"><summary>' +
            _lcEsc(_lcT('local.fullVersionToggle',
            '这台电脑也想跑 Tofu 本体（服务器+界面）？下载完整桌面版')) +
            '</summary>' +
            _lcDownloadLinks(d, 'full') +
          '</details>';
      } else {
        // No agent artifact yet: show preparation, never substitute a manual
        // auth/configuration flow that recreates the user's original burden.
        html =
          '<p class="lc-step">' + _lcEsc(_lcT('local.desktopRemoteAgent',
            'Tofu 运行在远程服务器上 —— 让 AI 操作这台电脑：')) + '</p>' +
          _lcAgentInstallerBlockHtml(d) +
          '<details class="lc-details"><summary>' +
            _lcEsc(_lcT('local.fullVersionToggle',
            '这台电脑也想跑 Tofu 本体（服务器+界面）？下载完整桌面版')) +
            '</summary>' + _lcDownloadLinks(d, 'full') + '</details>';
      }
      setup.innerHTML = _lcBindWarnHtml(d) + _lcRelayHintHtml(d) +
        _lcAwaitingAgentHtml(d) + html + _lcDiagInboxHtml();
      _lcWireDiag();
      return;
    }
  }
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
        _lcEsc(p.filename || '') + '"' +
        // The agent download arms the 30-min attach watch: once the
        // freshly installed broker answers, this page pushes its
        // zero-config bundle (macOS/Linux) or carries SSO polls.
        (kind === 'agent'
          ? ' data-tofu-action="_lcEnsureAgentRelay(1800000)"' : '') + '>' +
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
      _lcEsc((picks.length || kind === 'agent')
        ? _lcT('local.desktopDownloadAll', '查看全部下载 ↗')
        : _lcT('local.desktopDownload', '下载桌面版 ↗')) + '</a></p>';
  }
  return html;
}

// ══════════════════════════════════════════════════════
//  Switches — flip the REAL wire flags, one per capability
// ══════════════════════════════════════════════════════

function toggleBrowserFromLocalModal() {
  var sw = document.getElementById('lcBrowserSwitch');
  if (sw && sw.disabled) return;   // not connected — turning it on grants nothing
  if (typeof _applyBrowserUI === 'function') {
    _applyBrowserUI(!LocalControlShellState.browserEnabled);
  }
  if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcBrowserSwitch', LocalControlShellState.browserEnabled,
    _lcReach.browser !== false);
  _lcUpdateBadge();
}

function toggleDesktopFromLocalModal() {
  var sw = document.getElementById('lcDesktopSwitch');
  if (sw && sw.disabled) return;   // no agent — turning it on grants nothing
  if (typeof _applyDesktopUI === 'function') {
    _applyDesktopUI(!LocalControlShellState.desktopEnabled);
  }
  if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _lcSetSwitch('lcDesktopSwitch', LocalControlShellState.desktopEnabled,
    _lcReach.desktop !== false);
  _lcUpdateBadge();
}
