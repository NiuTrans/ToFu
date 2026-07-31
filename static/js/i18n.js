/* ═══════════════════════════════════════════
   i18n.js — Internationalization (Chinese / English)
   Loaded FIRST — before all other scripts.
   ═══════════════════════════════════════════ */

/**
 * Current UI language ('zh' | 'en'). Persisted in localStorage; read back as
 * a plain string (tsc can't narrow getItem()'s string return to the union).
 * @type {string}
 */
var _i18nLang = localStorage.getItem('tofu_ui_lang') || 'zh';

/* ── Server-visible language mirror (Epic-E sub-part 1, owner-approved) ──
 * localStorage stays AUTHORITATIVE — this is a write-through mirror so the
 * SERVER can pick which single-language bundle to ship. The server cannot read
 * localStorage, and the boot dictionary must be present before _applyI18n()
 * runs (index.html has 309 data-i18n bindings whose static fallback is not
 * uniformly one language), so without this cookie a per-language bundle is
 * impossible. See routes/common.py::request_ui_lang and
 * tests/test_i18n_split_blocked_on_lang_signal.py.
 *
 * Written on EVERY boot, not just on change: a user who set their language
 * before this shipped has localStorage but no cookie, and would otherwise be
 * served the default bundle forever. SameSite=Lax keeps it off cross-site
 * requests; it is a display preference, never an auth signal. */
function _syncLangCookie(lang) {
  try {
    document.cookie = 'tofu_ui_lang=' + encodeURIComponent(lang) +
                      ';path=/;max-age=31536000;SameSite=Lax';
  } catch (e) {
    /* Cookies disabled — the server falls back to the default language and the
     * client still renders from whatever pack it received, because t() keeps
     * its zh fallback (now observable via _reportMissingTranslation). */
  }
}
_syncLangCookie(_i18nLang);

/**
 * Translation dictionaries.
 * Key = translation key used in data-i18n attributes and t() calls.
 * Each key maps to { zh: '中文', en: 'English' }.
 */
var _i18n = {
  // ══════════════════════════════════════
  //  Sidebar & Navigation
  // ══════════════════════════════════════
  'sidebar.search': { zh: '搜索对话', en: 'Search conversations' },
  'sidebar.settings': { zh: '设置', en: 'Settings' },
  // Sidebar search results (conversation_list.js::_renderSearchResults)
  'sidebar.searchResults': { zh: '{n} 条结果', en: '{n} results' },
  'sidebar.searchResult': { zh: '{n} 条结果', en: '{n} result' },
  'sidebar.searching': { zh: '搜索中…', en: 'searching…' },
  'sidebar.searchNoMatches': { zh: '未找到与“{q}”匹配的结果', en: 'No matches for "{q}"' },
  'sidebar.searchRoleYou': { zh: '你', en: 'You' },
  'sidebar.searchRoleAssistant': { zh: '助手', en: 'Claude' },

  // Topbar feature launchers (relocated from the sidebar header)
  'topbar.paper': { zh: '论文', en: 'Paper' },
  'topbar.backToChat': { zh: '返回对话', en: 'Back' },
  'topbar.myday': { zh: '我的一天', en: 'My Day' },
  'topbar.trading': { zh: '交易', en: 'Trading' },
  'topbar.studio': { zh: '编排', en: 'Studio' },
  'topbar.tasks': { zh: '任务', en: 'Tasks' },
  'topbar.timer': { zh: '活动计数', en: 'Active Counter' },
  'settings.mobileClient': { zh: '手机客户端', en: 'Mobile client' },
  'settings.mobileClientTitle': { zh: '下载 Tofu 手机客户端', en: 'Download the Tofu mobile app' },
  'net.title': { zh: '网络延迟', en: 'Network latency' },
  'net.offline': { zh: '离线', en: 'Offline' },
  'net.offlineDesc': { zh: '推送连接已断开，正在重连…', en: 'Push connection dropped, reconnecting…' },
  'net.timeout': { zh: '超时', en: 'Timeout' },
  'net.timeoutDesc': { zh: '探测超时，网络可能很差', en: 'Probe timed out — network may be poor' },
  'net.state.good': { zh: '良好', en: 'good' },
  'net.state.ok': { zh: '一般', en: 'ok' },
  'net.state.poor': { zh: '较差', en: 'poor' },
  'net.reconnecting': { zh: '重连中', en: 'Reconnecting' },
  'net.reconnectingDesc': { zh: '聊天连接正在重连，回复可能暂停', en: 'Chat stream is reconnecting — replies may pause' },
  // ── Web search toggle (search-mode-toggle / main_toolbar_ui.js) ──
  'search.toggleTooltip': { zh: '联网搜索 · 点击开关', en: 'Web search — click to toggle on/off' },
  // ── Voice input (speech-to-text / voice.js) ──
  'voice.tooltip': { zh: '语音输入', en: 'Voice input' },
  'voice.recording': { zh: '正在录音 · 点击停止', en: 'Recording · click to stop' },
  'voice.transcribing': { zh: '正在转写…', en: 'Transcribing…' },
  'voice.micDenied': { zh: '麦克风权限被拒绝', en: 'Microphone access denied' },
  'voice.noMic': { zh: '此浏览器不支持录音', en: 'Recording is not supported in this browser' },
  'voice.empty': { zh: '未识别到语音', en: 'No speech detected' },
  'voice.failed': { zh: '语音转写失败', en: 'Transcription failed' },
  // ── Connection-failure surfaces (banners + toasts shown when the server/DB
  //    drops or recovers). zh primary. {cmd}/{n} are interpolated at the call
  //    site; the DB-desc {cmd} carries the styled <code> install command. ──
  'conn.dbUnavailableTitle': { zh: '数据库不可用', en: 'Database Unavailable' },
  'conn.dbUnavailableDesc': { zh: '未运行 PostgreSQL，对话与历史将无法使用。请安装 PostgreSQL（{cmd}）后重启服务器。', en: 'PostgreSQL is not running. Conversations and history will not work. Install PostgreSQL ({cmd}) and restart the server.' },
  'conn.dismiss': { zh: '关闭', en: 'Dismiss' },
  // ── Backend-offline GLOBAL monitor (core/backend_offline_monitor.js): the
  //    prominent fixed banner + tab-title prefix raised when the backend is
  //    unreachable (push socket dropped + 2 consecutive /api/health probe
  //    failures). {t}=elapsed duration, {n}=retry interval seconds. ──
  'conn.backendOfflineTitle': { zh: '后端服务器已离线', en: 'Backend server is offline' },
  'conn.backendOfflineDesc': { zh: '所有进行中的回复已暂停。每 {n} 秒自动重试，恢复后会自动重连并同步结果。', en: 'All in-flight replies are paused. Retrying every {n}s — the page reconnects and resyncs automatically when the server is back.' },
  'conn.networkOfflineTitle': { zh: '本机网络已断开', en: 'Network disconnected' },
  'conn.networkOfflineDesc': { zh: '浏览器报告网络已断开。检查网络连接；恢复后页面会自动重连。', en: 'The browser reports no network. Check your connection — the page reconnects automatically when it is back.' },
  'conn.backendOfflineElapsed': { zh: '已离线 {t}', en: 'offline for {t}' },
  'conn.backendRetryNow': { zh: '立即重试', en: 'Retry now' },
  'conn.backendSnooze': { zh: '暂时隐藏', en: 'Hide for 1 min' },
  'conn.backendRestored': { zh: '后端已恢复', en: 'Backend is back' },
  'conn.backendRestoredDesc': { zh: '正在重新连接并同步进行中的对话…', en: 'Reconnecting and resyncing in-flight conversations…' },
  'conn.backendOfflineTitlePrefix': { zh: '【后端离线】', en: '[Backend offline]' },
  'conn.networkOfflineTitlePrefix': { zh: '【网络断开】', en: '[No network]' },
  'conn.streamOfflineMsg': { zh: '服务器离线，回复可能不完整。服务器恢复后会自动重连。', en: 'Server offline — response may be incomplete. This notice will clear automatically when the server comes back.' },
  'conn.offlineToastDetail': { zh: '后端服务器无响应。已保存部分回复，连接恢复后会自动恢复。', en: 'Backend server is not responding. Your partial response has been saved. It will recover automatically when connectivity is restored.' },
  'conn.restoredTitle': { zh: '连接已恢复', en: 'Connection Restored' },
  'conn.restoredReattach': { zh: '已重连 {n} 个进行中的任务，流式已恢复。', en: 'Reconnected {n} running task(s) — streaming resumed.' },
  'conn.restoredRecovered': { zh: '已从服务器恢复 {n} 个对话，结果已更新。', en: 'Recovered {n} conversation(s) from server. Results updated.' },
  'conn.loadTimedOut': { zh: '加载超时 — 服务器可能繁忙，请重试。', en: 'Loading timed out — the server may be busy.' },
  'convWindow.loadToolActivity': { zh: '加载工具活动（{n} 次调用）', en: 'Load tool activity ({n} calls)' },
  // ── Recoverable connection-drop block (server_offline ONLY): a friendly,
  //    TRUTHFUL headline + recovery hint shown in the assistant error bubble
  //    instead of raw "Server offline" jargon. The key message: the drop is a
  //    CONNECTION verdict, a task WAS running so its result is likely safe on
  //    the server, DON'T regenerate — click Recover / reload to adopt it.
  //    NOTE: `network` (chat-start POST failed → no task ran → nothing to
  //    recover) and `premature_close`/`abnormal_stop` (upstream server↔gateway
  //    failures, retries already exhausted) are NOT recoverable and keep their
  //    own Retry-oriented hints — they do NOT get this copy or a Recover
  //    button. zh primary. ──
  'err.conn.title': { zh: '连接中断（结果可能已保存）', en: 'Connection lost (your result may be saved)' },
  'err.conn.hint': { zh: '连接似乎中断了，但服务器很可能已经完成生成。请不要重新生成或编辑——那会丢弃服务器上已完成的结果。点击下方「恢复」或刷新页面，即可取回完整回复。', en: "The connection dropped, but the server has very likely finished generating. Do NOT regenerate or edit — that would discard the completed result on the server. Click \u201cRecover\u201d below (or reload the page) to retrieve the full reply." },
  'err.conn.recover': { zh: '恢复', en: 'Recover' },
  'err.conn.recoverTip': { zh: '重新连接服务器并取回已生成的完整结果（不会重新生成）', en: 'Reconnect and fetch the completed result from the server (does not regenerate)' },
  // ── network kind: the request to START the turn never reached the server (or
  //    the connection reset before a task ran). Unlike server_offline there is
  //    NO server-side result to recover — the honest guidance is check the
  //    connection and Retry, NOT "your result may be saved". No Recover button. ──
  'err.net.hint': { zh: '无法连接到服务器——请检查你的网络或代理，然后重试。（这条请求没有开始生成，重试是安全的。）', en: "Couldn't reach the server — check your network or proxy, then Retry. (The request never started generating, so retrying is safe.)" },
  // ── Typed error-envelope kinds (err.k.*) — per-kind chip / title / hint ──
  //    Title + hint texts mirror lib/error_envelope/_constants.py _TITLES
  //    BYTE-IDENTICALLY (parity locked by tests/test_error_envelope_i18n.py).
  //    The backend ships titleKey/hintKey on the envelope and
  //    renderErrorEnvelope resolves them here in the CURRENT UI language;
  //    the legacy bilingual message/hint stays as the headless + old-bundle
  //    fallback. chip = the compact category label (was English-only
  //    ERROR_KIND_LABELS). _howToFix/_modelSuffix are shared fragments.
  'err.k._howToFix': { zh: '解决办法：', en: 'How to fix:' },
  'err.k._modelSuffix': { zh: '（模型：{model}）', en: ' (model: {model})' },
  'err.k.quota.chip': { zh: '配额耗尽', en: 'Quota exhausted' },
  'err.k.quota.title': { zh: '⚠️ API Key 余额/配额已用尽', en: 'API key quota exhausted' },
  'err.k.quota.hint': { zh: '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），手动重新启用或添加新 Key。\n• 或者稍等几分钟，让 API 限额窗口重置后再试。\n• 若问题持续，可在设置中切换到其他可用模型 / Provider。', en: '• Open "Settings → Keys / Providers" — check if any key was auto-disabled (429 / quota exhausted) and re-enable or add a new key.\n• Or wait a few minutes for the API rate-limit window to reset, then retry.\n• If the issue persists, switch to another available model / provider in Settings.' },
  'err.k.ratelimit.chip': { zh: '已限频', en: 'Rate limited' },
  'err.k.ratelimit.title': { zh: '⚠️ API 请求已达限频（429）', en: 'API rate-limited (HTTP 429)' },
  'err.k.ratelimit.hint': { zh: '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），手动重新启用或添加新 Key。\n• 或者稍等几分钟，让 API 限额窗口重置后再试。\n• 若问题持续，可在设置中切换到其他可用模型 / Provider。', en: '• Open "Settings → Keys / Providers" — check if any key was auto-disabled (429 / quota exhausted) and re-enable or add a new key.\n• Or wait a few minutes for the API rate-limit window to reset, then retry.\n• If the issue persists, switch to another available model / provider in Settings.' },
  'err.k.permission.chip': { zh: '无权限', en: 'Permission denied' },
  'err.k.permission.title': { zh: '⚠️ API Key 被拒绝（401/403，无权限或已失效）', en: 'API key rejected (401/403, invalid or lacking permission)' },
  'err.k.permission.hint': { zh: '• 打开 「设置 → Keys」 检查该 Provider 的 Key 是否填写正确、是否被停用。\n• 若该 Key 对当前模型没有访问权限，请更换为其它模型或申请开通。', en: '• Open "Settings → Keys" and verify the key for this provider is correct and enabled.\n• If the key does not have access to this model, switch models or request access.' },
  'err.k.no_slot.chip': { zh: '无可用 Key', en: 'No keys available' },
  'err.k.no_slot.title': { zh: '⚠️ 当前没有可用的 API Key', en: 'No available API key slot' },
  'err.k.no_slot.hint': { zh: '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），手动重新启用或添加新 Key。\n• 或者稍等几分钟，让 API 限额窗口重置后再试。\n• 若问题持续，可在设置中切换到其他可用模型 / Provider。', en: '• Open "Settings → Keys / Providers" — check if any key was auto-disabled (429 / quota exhausted) and re-enable or add a new key.\n• Or wait a few minutes for the API rate-limit window to reset, then retry.\n• If the issue persists, switch to another available model / provider in Settings.' },
  'err.k.dispatch_exhausted.chip': { zh: 'Key 已用尽', en: 'All keys exhausted' },
  'err.k.dispatch_exhausted.title': { zh: '⚠️ 该模型所有 Key 的重试次数都已用尽', en: 'All keys for this model have been exhausted' },
  'err.k.dispatch_exhausted.hint': { zh: '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），手动重新启用或添加新 Key。\n• 或者稍等几分钟，让 API 限额窗口重置后再试。\n• 若问题持续，可在设置中切换到其他可用模型 / Provider。', en: '• Open "Settings → Keys / Providers" — check if any key was auto-disabled (429 / quota exhausted) and re-enable or add a new key.\n• Or wait a few minutes for the API rate-limit window to reset, then retry.\n• If the issue persists, switch to another available model / provider in Settings.' },
  'err.k.timeout.chip': { zh: '已超时', en: 'Timed out' },
  'err.k.timeout.title': { zh: '⚠️ 请求超时', en: 'Request timed out' },
  'err.k.timeout.hint': { zh: '• 稍后重试。若持续超时，可在 「设置 → 模型默认」 切换到响应更快的模型。', en: '• Retry shortly. If timeouts persist, switch to a faster model in "Settings → Model defaults".' },
  'err.k.network.chip': { zh: '网络错误', en: 'Network error' },
  'err.k.network.title': { zh: '⚠️ 网络连接错误', en: 'Network connection error' },
  'err.k.network.hint': { zh: '• 检查本机网络 / 代理设置，然后重试。', en: '• Check your network / proxy settings, then retry.' },
  'err.k.endpoint_unreachable.chip': { zh: '端点不可达', en: 'Endpoint unreachable' },
  'err.k.endpoint_unreachable.title': { zh: '⚠️ 模型服务端点无法连接', en: 'Model endpoint unreachable' },
  'err.k.endpoint_unreachable.hint': { zh: '• 无法连接到模型服务端点（连接被拒绝或超时）——可能是本机代理/网络中断，也可能是自建/BYO 服务已宕机、端口未监听或防火墙不通。\n• 先检查本机网络/代理后重试；若确认服务可达仍失败，可在 「设置 → 模型默认」 切换到其他可用模型。', en: '• The model endpoint could not be reached (connection refused or timed out) — this can be a local proxy/network outage OR the self-hosted / BYO server being down, the port not listening, or a firewall blocking it.\n• Check your network / proxy and retry; if the server is confirmed reachable, switch to another available model in "Settings → Model defaults".' },
  'err.k.content_filter.chip': { zh: '内容过滤', en: 'Content filter' },
  'err.k.content_filter.title': { zh: '⚠️ 该回复被模型安全过滤器拦截', en: "Response blocked by the model's safety filter" },
  'err.k.content_filter.hint': { zh: '• 尝试换一种方式提问，或切换到其他模型。', en: '• Try rephrasing the question or switching to another model.' },
  'err.k.invalid_image.chip': { zh: '图片被拒', en: 'Image rejected' },
  'err.k.invalid_image.title': { zh: '⚠️ 图像内容被拒绝', en: 'Image rejected by the API' },
  'err.k.invalid_image.hint': { zh: '• 缩小或减少图片，再发送。', en: '• Reduce image size or count and try again.' },
  'err.k.invalid_image.hintMany': { zh: '• 过多大图。同时发送 5 张以上图片时，每张需小于 2000×2000像素。请压缩或删除部分图片。', en: '• Too many large images. When sending 5+ images, each must be under 2000×2000 pixels. Please resize or remove some images.' },
  'err.k.invalid_image.hintSize': { zh: '• 会话中某张图片超过了 API 大小限制。请使用更小的图片或删除过大的图片。', en: '• One or more images in this conversation exceed the API size limit. Please use a smaller image or remove the oversized image.' },
  'err.k.prompt_too_long.chip': { zh: '上下文超长', en: 'Prompt too long' },
  'err.k.prompt_too_long.title': { zh: '⚠️ 上下文已超过模型上限', en: "Context exceeds the model's limit" },
  'err.k.prompt_too_long.hint': { zh: '• 已尝试自动压缩仍失败，请清理对话历史或切换到上下文更大的模型。', en: '• Auto-compaction did not free enough room — trim the history or switch to a larger-context model.' },
  'err.k.stream_only.chip': { zh: '仅流式模型', en: 'Stream-only model' },
  'err.k.stream_only.title': { zh: '⚠️ 该模型仅支持流式调用', en: 'Model only supports streaming' },
  'err.k.stream_only.hint': { zh: '• 请切换到其他模型（系统已自动避开该模型）。', en: '• Switch to another model (this one is auto-excluded).' },
  'err.k.model_limit.chip': { zh: '输出超限', en: 'Model limit' },
  'err.k.model_limit.title': { zh: '⚠️ 输出长度超过模型上限', en: 'Output exceeds model max_tokens' },
  'err.k.model_limit.hint': { zh: '• 系统已记住新的上限，可重试。', en: '• The new limit has been recorded — retry.' },
  'err.k.tool_rounds_exhausted.chip': { zh: '工具轮数上限', en: 'Tool budget' },
  'err.k.tool_rounds_exhausted.title': { zh: '⚠️ 工具调用轮数已达上限', en: 'Tool call round limit reached' },
  'err.k.tool_rounds_exhausted.hint': { zh: '• 模型未在限定轮数内得出最终答复，可点击 Continue 续写。', en: '• The model did not finish within the per-task budget — click Continue to extend.' },
  'err.k.tool_timeout.chip': { zh: '工具超时', en: 'Tool timeout' },
  'err.k.tool_timeout.title': { zh: '⚠️ 工具调用连续超时', en: 'Repeated tool-execution timeouts' },
  'err.k.tool_timeout.hint': { zh: '• 工具持续超时，建议简化任务或在 「设置 → 工具」 中调高超时时间。', en: '• The tool keeps timing out — simplify the request or raise the tool-timeout in Settings.' },
  'err.k.premature_close.chip': { zh: '流被截断', en: 'Stream cut off' },
  'err.k.premature_close.title': { zh: '⚠️ 网关/代理过早关闭流', en: 'Gateway closed the stream prematurely' },
  'err.k.premature_close.hint': { zh: '• 重试已用完，回复可能不完整。可点击 Retry 重新生成。', en: '• Retries exhausted — the response may be incomplete. Click Retry to regenerate.' },
  'err.k.abnormal_stop.chip': { zh: '异常终止', en: 'Abnormal stop' },
  'err.k.abnormal_stop.title': { zh: '⚠️ API 流异常终止（缺失 finish 标记）', en: 'Stream ended without finish marker' },
  'err.k.abnormal_stop.hint': { zh: '• 回复可能不完整。可点击 Retry 重新生成。', en: '• The reply may be truncated. Click Retry to regenerate.' },
  'err.k.aborted.chip': { zh: '已停止', en: 'Stopped' },
  'err.k.aborted.title': { zh: '⏹️ 用户已中止', en: 'Stopped by user' },
  'err.k.aborted.hint': { zh: '', en: '' },
  'err.k.server_offline.chip': { zh: '服务器离线', en: 'Server offline' },
  'err.k.server_offline.title': { zh: '⚠️ 服务器离线', en: 'Server offline' },
  'err.k.server_offline.hint': { zh: '• 等待服务器恢复后页面会自动重连，并尝试拉取已生成的内容。', en: '• When the server comes back, this page will reconnect automatically and try to recover any content that was generated.' },
  'err.k.internal.chip': { zh: '内部错误', en: 'Internal error' },
  'err.k.internal.title': { zh: '⚠️ 内部错误', en: 'Internal error' },
  'err.k.internal.hint': { zh: '• 请查看服务器日志（logs/error.log）了解详情。', en: '• Check the server logs (logs/error.log) for details.' },
  'err.k.generic.chip': { zh: '错误', en: 'Error' },
  'err.k.generic.title': { zh: '⚠️ 模型调用失败', en: 'LLM call failed' },
  'err.k.generic.hint': { zh: '• 展开下方错误详情查看原始原因。\n• 若反复出现，请查看服务器日志（logs/error.log）定位根因；若确认是 Key/配额问题，再前往 「设置 → Keys / Providers」 处理。', en: '• Expand the error detail below for the underlying cause.\n• If it recurs, check the server logs (logs/error.log) for the root cause; only if it proves to be a key/quota problem, go to "Settings → Keys / Providers".' },
  'err.k.bad_request.chip': { zh: '请求无效', en: 'Bad request' },
  'err.k.bad_request.title': { zh: '⚠️ 请求被上游 API 拒绝（HTTP 400）', en: 'Request rejected by the API (HTTP 400)' },
  'err.k.bad_request.hint': { zh: '• 这不是 Key / 配额 / 429 问题——上游判定请求内容无效。展开下方错误详情查看具体原因。\n• 若反复出现且原因不明，请查看服务器日志（logs/error.log）。', en: '• This is NOT a key / quota / 429 problem — the API rejected the request payload itself. Expand the error detail below for the exact reason.\n• If it recurs with no clear cause, check the server logs (logs/error.log).' },
  'err.k.content_refused.chip': { zh: '质量校验未过', en: 'Quality check failed' },
  'err.k.content_refused.title': { zh: '⚠️ 翻译质量校验未通过', en: 'Translation rejected by quality check' },
  'err.k.content_refused.hint': { zh: '• 模型多次输出错误语言 / 空结果 / 失控文本，系统拒绝采用并已自动重试。稍后再试通常会命中正常模型。\n• 若反复出现，可在 「设置 → 模型默认」 为翻译换一个更稳定的模型。', en: '• The models repeatedly produced wrong-language / empty / runaway output; it was rejected and retried automatically. Retrying shortly usually lands a healthy model.\n• If it recurs, switch the translation model in "Settings → Model defaults".' },
  'err.k.upstream_error.chip': { zh: '上游故障', en: 'Upstream error' },
  'err.k.upstream_error.title': { zh: '⚠️ 上游模型服务暂时不可用', en: 'Upstream model service temporarily unavailable' },
  'err.k.upstream_error.hint': { zh: '• 模型厂商或网关侧故障（不是本机 Key 问题），稍后重试通常可自行恢复。\n• 若持续数分钟仍失败，可在 「设置 → 模型默认」 临时切换到其他可用模型。', en: '• The model vendor or gateway is failing (not a problem with your API keys) — retrying shortly usually recovers.\n• If it keeps failing for several minutes, temporarily switch to another available model in "Settings → Model defaults".' },
  'err.k.worker_lost.chip': { zh: '进程丢失', en: 'Worker lost' },
  'err.k.worker_lost.title': { zh: '⚠️ 任务工作进程丢失（长时间无进展）', en: 'Task worker lost (no progress)' },
  'err.k.worker_lost.hint': { zh: '• 后台工作进程长时间没有产出任何进展，已被判定死亡——重新发起任务是安全的，已生成的部分内容可能丢失。\n• 若反复出现，请查看服务器日志（logs/error.log）确认进程是否被异常终止。', en: '• The background worker produced no progress for too long and was declared dead — retrying the task is safe; partial output may have been lost.\n• If it recurs, check the server logs (logs/error.log) to see whether the process was killed.' },
  'err.k.tool_not_available.chip': { zh: '工具不可用', en: 'Tool not available' },
  'err.k.tool_not_available.title': { zh: '⚠️ 模型调用了本轮不存在的工具，任务未完成', en: 'Model called a tool that is not available this turn' },
  'err.k.tool_not_available.hint': { zh: '• 这不是 Key / 配额问题——模型反复调用了一个**本轮工具集里没有**的工具，该调用从未执行，任务因此停在半途。展开下方详情可看到具体工具名。\n• 常见原因：该能力在本会话未开启（如代码执行 / 浏览器 / 项目工具）。在工具栏打开对应开关后重试，或直接告诉模型改用已有的工具。', en: '• This is NOT a key / quota problem — the model repeatedly called a tool that is **not in this turn\'s toolset**, so it never executed and the task stopped part-way. Expand the detail below for the exact tool name.\n• Usually the capability is switched OFF for this conversation (e.g. code execution / browser / project tools). Enable it in the toolbar and retry, or tell the model to use a tool it actually has.' },
  'err.k.budget_exceeded.chip': { zh: '预算已达上限', en: 'Budget exceeded' },
  'err.k.budget_exceeded.title': { zh: '⚠️ 任务费用已达预算上限，已主动停止', en: 'Task stopped at the cost budget cap' },
  'err.k.budget_exceeded.hint': { zh: '• 本次任务的花费达到会话设置的预算上限（maxBudgetUsd）而主动停止，已生成的内容已保留。\n• 如需继续：提高该会话的预算上限后点击 Continue，或换用更低成本的模型。', en: '• The task stopped itself when its spend reached the conversation\'s budget cap (maxBudgetUsd); generated content is preserved.\n• To continue: raise the cap for this conversation and click Continue, or switch to a cheaper model.' },
  'conn.bootReconnect': { zh: '离线，显示缓存的对话，正在重连…', en: 'Offline — showing cached conversations, reconnecting…' },
  // ── In-stream liveness HUD (header timer + in-bubble line) shown when a
  //    stream stalls / the server stops responding. {n}=silent seconds,
  //    {what}=current activity label. zh primary. ──
  'conn.forceFinish': { zh: '强制结束', en: 'Force Finish' },
  'conn.reconnectingShort': { zh: '正在重连…', en: 'Reconnecting…' },
  'conn.reconnecting': { zh: '连接不稳定，正在重连并与服务器同步…', en: 'Connection unstable — reconnecting and syncing with the server…' },
  'conn.hudNotResponding': { zh: '服务器无响应', en: 'server not responding' },
  'conn.hudNotRespondingFull': { zh: '服务器无响应（静默 {n}s）— 健康检查失败', en: 'Server not responding ({n}s silent) — health check failed' },
  'conn.hudStillWorking': { zh: '仍在处理', en: 'still working' },
  'conn.hudStillWorkingFull': { zh: '服务器正常 — 仍在处理（{what}，{n}s 无流式输出）', en: 'Server alive — still working ({what}, {n}s no stream output)' },
  'conn.hudProcessing': { zh: '处理中', en: 'processing' },
  'conn.hudNoUpdate': { zh: '{n}s 无更新', en: '{n}s no update' },
  'conn.hudNoUpdateSevere': { zh: '{n}s 无更新，正在检查服务器是否仍在响应…', en: 'No update for {n}s — checking whether the server is still responding…' },
  'conn.hudNoUpdateWarn': { zh: '{n}s 无更新，正在确认服务器是否存活…', en: 'No update for {n}s — verifying the server is still alive…' },
  // Stream-phase activity labels (surface in the HUD suffix + "still working").
  'conn.phaseTools': { zh: '正在执行工具', en: 'running tools' },
  'conn.phaseThinking': { zh: '思考中', en: 'thinking' },
  'conn.phaseReasoning': { zh: '推理中', en: 'reasoning' },
  'conn.phaseCompacting': { zh: '正在压缩上下文', en: 'compacting context' },
  'conn.phaseRetrying': { zh: '重试中', en: 'retrying' },
  'conn.phaseWorking': { zh: '处理中', en: 'working' },
  'conn.phaseAutopilot': { zh: '自动接管正在生成下一条回复', en: 'autopilot is generating the next reply' },
  'timer.badgeTitle': { zh: '活动计数 — 后台定时监视器', en: 'Active Counter — background timer watchers' },
  'timer.panelTitle': { zh: '活动计数', en: 'Active Counter' },
  'timer.empty': { zh: '暂无定时器。AI 可在任务中通过 timer_create 创建。', en: 'No timers. The AI can create one with timer_create during a task.' },
  'timer.jumpHint': { zh: '点击跳转到该定时器所在对话', en: 'Click to open this timer\'s conversation' },
  'timer.convMissing': { zh: '该定时器对应的对话不在本地列表中', en: 'This timer\'s conversation is not in your local list' },
  'timer.logTitle': { zh: '轮询日志', en: 'Poll Log' },
  'timer.logEmpty': { zh: '暂无轮询日志记录。', en: 'No poll log entries yet.' },
  'timer.logError': { zh: '获取轮询日志失败', en: 'Failed to load poll log' },
  'timer.noReason': { zh: '无原因', en: 'no reason' },

  // Sidebar conversation date-group headers
  'sidebar.dateToday': { zh: '今天', en: 'Today' },
  'sidebar.dateYesterday': { zh: '昨天', en: 'Yesterday' },
  'sidebar.datePrev7': { zh: '过去 7 天', en: 'Previous 7 Days' },
  'sidebar.datePrev30': { zh: '过去 30 天', en: 'Previous 30 Days' },
  'sidebar.dateOlder': { zh: '更早', en: 'Older' },

  'sidebar.newChat': { zh: '新对话', en: 'New Chat' },
  'sidebar.uncategorized': { zh: '未分类', en: 'Uncategorized' },
  'sidebar.newFolder': { zh: '新建文件夹', en: 'New Folder' },
  'sidebar.projects': { zh: '项目', en: 'Projects' },
  'sidebar.collapseRail': { zh: '收起项目栏', en: 'Collapse projects' },
  'sidebar.expandRail': { zh: '展开项目栏', en: 'Expand projects' },
  'sidebar.allCategorized': { zh: '所有对话都已归类', en: 'All conversations are categorized' },
  'sidebar.loadMoreEarlier': { zh: '还有 {n} 条更早的对话未加载 · 点击加载', en: '{n} earlier conversations not loaded · Load more' },
  'sidebar.folderEmpty': { zh: '文件夹是空的', en: 'Folder is empty' },
  'sidebar.newChatAppear': { zh: '新对话会出现在这里，或从文件夹中移出对话', en: 'New chats will appear here, or move conversations out of folders' },
  'sidebar.clickNewChat': { zh: '点击 New Chat 创建对话，或拖拽对话到此标签', en: 'Click New Chat to create a conversation, or drag one here' },
  'sidebar.feishuConv': { zh: '飞书对话', en: 'Feishu conversation' },
  'sidebar.summaryBadge': { zh: '已生成摘要 · 点击查看', en: 'Summary available · click to view' },
  'sidebar.summaryTitle': { zh: '对话摘要', en: 'Conversation summary' },
  'sidebar.awaitingInput': { zh: '等待你的输入', en: 'Awaiting your input' },
  'sidebar.translating': { zh: '翻译中…', en: 'Translating…' },
  'sidebar.connecting': { zh: '连接中…', en: 'Connecting…' },
  /* Continue (resume an interrupted turn) — POST failure. Surfaced as a toast
   * because the message document is NOT mutated on this path: the interrupted
   * turn keeps finishReason='interrupted' and its Continue button, so the
   * action is retryable. Stamping msg.error instead would poison an otherwise
   * continuable turn with a transient network fault. */
  'continue.failed': { zh: '续接失败：{err} — 可重试', en: 'Continue failed: {err} — you can retry' },
  'sidebar.queued': { zh: '排队中', en: 'Queued' },
  'sidebar.translatingTag': { zh: '翻译中', en: 'Translating' },
  'sidebar.memoryPrefetch': { zh: '筛选记忆中…', en: 'Filtering memories…' },
  'sidebar.memoryPrefetchTag': { zh: '筛选记忆', en: 'Filtering' },
  'memPrefetch.surfacing': { zh: '正在检索相关记忆…', en: 'Surfacing relevant memories…' },
  'memPrefetch.totalN': { zh: '共 {n} 条', en: '{n} total' },
  'memPrefetch.filtering': { zh: '用轻量模型筛选 {n} 条候选…', en: 'Filtering {n} candidates with cheap model…' },
  'memPrefetch.none': { zh: '本回合没有相关的历史记忆', en: 'No prior memory relevant to this turn' },
  'memPrefetch.prefetched': { zh: '已检索 {n} 条记忆', en: 'Prefetched {n} memory' },
  'memPrefetch.prefetchedN': { zh: '已检索 {n} 条记忆', en: 'Prefetched {n} memories' },
  'memPrefetch.candidatesN': { zh: '{n} 条候选', en: '{n} candidates' },
  'memPrefetch.filterLabel': { zh: '筛选', en: 'filter' },
  'memPrefetch.totalLabel': { zh: '总计', en: 'total' },
  'memPrefetch.fallback': { zh: '⚠ 回退到 BM25 前 3 条', en: '⚠ fallback to BM25 top-3' },
  'memPrefetch.skipped': { zh: '已跳过记忆检索', en: 'Memory prefetch skipped' },
  'memPrefetch.failed': { zh: '记忆检索失败', en: 'Memory prefetch failed' },
  'memPrefetch.generic': { zh: '记忆检索…', en: 'Memory prefetch…' },
  'memPrefetch.tag': { zh: '记忆', en: 'memory' },
  'memPrefetch.tagN': { zh: '{n} 条记忆', en: '{n} memory' },
  'memPrefetch.tagNs': { zh: '{n} 条记忆', en: '{n} memories' },
  'memPrefetch.tagNone': { zh: '无相关记忆', en: 'no memory' },
  'memPrefetch.tagSkipped': { zh: '跳过记忆', en: 'memory skipped' },
  'memPrefetch.tagFailed': { zh: '记忆失败', en: 'memory failed' },
  'prefs.applied': { zh: '已在本回合提供你的偏好', en: 'Your preferences were in context' },
  'prefs.appliedN': { zh: '本回合提供了 {n} 条偏好', en: '{n} preferences in context this turn' },
  'prefs.fromProfile': { zh: '来自个人偏好档案', en: 'from your profile' },
  'prefs.tag': { zh: '偏好', en: 'preferences' },
  'prefs.tagN': { zh: '{n} 条偏好', en: '{n} preference' },
  'prefs.tagNs': { zh: '{n} 条偏好', en: '{n} preferences' },
  'prefs.tagNone': { zh: '偏好', en: 'preferences' },
  'relatedConvs.tag': { zh: '相关对话', en: 'related' },
  'relatedConvs.tagN': { zh: '{n} 个相关对话', en: '{n} related chat' },
  'relatedConvs.tagNs': { zh: '{n} 个相关对话', en: '{n} related chats' },
  'relatedConvs.sub': { zh: '模型已知晓本项目的这些对话（仅作背景感知）', en: 'siblings of this project the model was made aware of' },
  'login.awaiting': { zh: '等待手机审批', en: 'Waiting for mobile approval' },
  'login.awaitingSub': { zh: '请在大象 / 办公 App 中点击「同意」——推送可能需要几秒钟到达', en: 'Tap Approve on your mobile office app — push may take a few seconds to arrive' },
  'login.approved': { zh: '登录已通过', en: 'Login approved' },
  'login.denied': { zh: '登录被拒绝', en: 'Login denied' },
  'login.timeout': { zh: '登录超时', en: 'Login timed out' },
  'login.finished': { zh: '登录完成', en: 'Login finished' },
  'workspaceRoot.added': { zh: '已添加工作区根目录：{roots}', en: 'Added workspace root: {roots}' },
  'workspaceRoot.hint': { zh: '助手写入了所有已知根目录之外的路径，已自动登记为新的工作区根目录。若不该访问此处，请撤回改动并调整项目范围。', en: 'The assistant wrote to a path outside all known roots, so it was auto-registered as a new workspace root. If it should not have write access there, revert the change and adjust the project scope.' },
  // ── Background toast: external (IDE) edits captured into file-history ──
  'toast.fromConv': { zh: '来自', en: 'from' },
  'toast.untitledConv': { zh: '未命名对话', en: 'Untitled chat' },
  'externalEdit.title': { zh: '已捕获 {n} 个外部文件改动', en: 'Captured {n} external file change{s}' },
  'externalEdit.detail': { zh: '{preview} · 快照 {sha}', en: '{preview} · snapshot {sha}' },
  'externalEdit.moreN': { zh: '等 +{n} 个', en: '+{n} more' },
  'externalEdit.hint': { zh: '你在 Tofu 之外（如 IDE）改动了被跟踪的文件，已自动存入文件历史，可随时撤销。点击可跳转到触发的对话查看。', en: 'You changed tracked files outside Tofu (e.g. in your IDE) — they were auto-saved to file history and can be reverted anytime. Click to open the conversation that triggered this.' },
  'prefs.learned': { zh: '记下了：你偏好', en: 'Noted: you prefer' },
  'prefs.added': { zh: '已记住', en: 'Remembered' },
  'prefs.editInSettings': { zh: '可在设置中修改', en: 'editable in Settings' },
  'prefs.learnedReinforced': { zh: '已更新你的偏好档案', en: 'Updated your preference profile' },
  'prefs.learnedTagN': { zh: '记住 {n} 条偏好', en: 'remembered {n} preference' },
  'prefs.learnedTagNs': { zh: '记住 {n} 条偏好', en: 'remembered {n} preferences' },
  'prefs.learnedHeadline': { zh: '已更新你的偏好档案', en: 'Updated your preference profile' },
  'prefs.pendingHint': { zh: '待你确认后写入', en: 'Awaiting your confirmation' },
  'prefs.confirm': { zh: '确认', en: 'Confirm' },
  'prefs.dismiss': { zh: '忽略', en: 'Dismiss' },
  'prefs.undo': { zh: '撤销', en: 'Undo' },
  'settings.tabPreferences': { zh: '记忆与偏好', en: 'Memory & Preferences' },
  'memPrefs.memTitle': { zh: '积累的记忆', en: 'Accumulated memories' },
  'memPrefs.memIntro': { zh: '助手在对话中自动沉淀的经验与教训（项目约定、踩过的坑、工作流）。会按相关性在对话中检索注入。点击「管理全部」可新建、导入或拖入技能包。', en: 'Experience and lessons the assistant accumulates during conversations (project conventions, pitfalls, workflows). Retrieved by relevance and injected into conversations. Click "Manage all" to create, import, or drag in skill packages.' },
  'memPrefs.manageAll': { zh: '管理全部', en: 'Manage all' },
  'prefsPanel.title': { zh: '个人偏好档案', en: 'Personal Preference Profile' },
  'prefsPanel.intro': { zh: '这是助手长期记住的关于你、以及「你希望它如何工作」的信息（语言、语气、编码习惯、禁忌，以及你的角色、技术栈等）。它会在每次对话中自动注入，跨项目生效。助手会在对话中自动学习并更新这些条目，你可以随时在此修改或删除。', en: 'Durable facts the assistant remembers about you and how you like it to work (language, tone, coding conventions, do\'s/don\'ts, plus your role, tech stack, etc.). Injected into every conversation, applies across all projects. The assistant learns and updates these as you talk; edit or remove any of them here.' },
  'prefsPanel.secPreferences': { zh: '工作偏好', en: 'Working preferences' },
  'prefsPanel.secAbout': { zh: '关于你', en: 'About you' },
  'prefsPanel.secOther': { zh: '其他', en: 'Other' },
  'prefsPanel.addItem': { zh: '添加', en: 'Add' },
  'prefsPanel.remove': { zh: '删除', en: 'Remove' },
  'prefsPanel.itemPlaceholder': { zh: '例如：总是用中文回答', en: 'e.g. Always reply in Chinese' },
  'prefsPanel.sectionEmpty': { zh: '暂无条目', en: 'No entries yet' },
  'prefsPanel.reload': { zh: '重新加载', en: 'Reload' },
  'prefsPanel.save': { zh: '保存', en: 'Save' },
  'prefsPanel.loading': { zh: '加载中…', en: 'Loading…' },
  'prefsPanel.loadFailed': { zh: '加载失败', en: 'Load failed' },
  'prefsPanel.saving': { zh: '保存中…', en: 'Saving…' },
  'prefsPanel.saved': { zh: '已保存', en: 'Saved' },
  'prefsPanel.saveFailed': { zh: '保存失败', en: 'Save failed' },
  'prefsPanel.overCap': { zh: '超出长度上限，下次整理时会自动压缩', en: 'Over the size cap — will be auto-distilled on the next consolidation pass' },
  'sidebar.answering': { zh: '回答中', en: 'Answering' },
  'sidebar.errorTag': { zh: '出错', en: 'Error' },
  'sidebar.errorState': { zh: '上次生成以错误结束', en: 'Last generation ended with an error' },
  'sidebar.incompleteTag': { zh: '未完成', en: 'Incomplete' },
  'sidebar.incompleteState': { zh: '上次生成被中断，未正常完成', en: 'Last generation was interrupted and did not finish' },
  'sidebar.stateUnconfirmed': { zh: '实时连接中断，此状态可能不是最新的', en: 'Live connection is down — this state may be out of date' },
  'sidebar.copyConvId': { zh: '复制会话ID', en: 'Copy conversation ID' },
  'sidebar.refConv': { zh: '引用此对话', en: 'Reference this conversation' },
  'sidebar.moveToFolder': { zh: '移入文件夹', en: 'Move to folder' },
  'sidebar.duplicate': { zh: '复制为新对话', en: 'Duplicate conversation' },
  'sidebar.deleteConv': { zh: '删除对话', en: 'Delete conversation' },
  'sidebar.renameConv': { zh: '重命名对话', en: 'Rename conversation' },
  'sidebar.renameConvTitle': { zh: '重命名对话', en: 'Rename Conversation' },
  'sidebar.renameConvPh': { zh: '输入对话标题', en: 'Enter conversation title' },
  'sidebar.convDeleted': { zh: '对话已删除', en: 'Conversation deleted' },
  'sidebar.undoDelete': { zh: '撤销', en: 'Undo' },
  'sidebar.convRestored': { zh: '对话已恢复', en: 'Conversation restored' },
  'sidebar.deleteFailed': { zh: '无法删除：未能加载对话内容，请重新连接后重试', en: 'Couldn\u2019t delete \u2014 failed to load the conversation; reconnect and try again' },
  'sidebar.deleteNoUndoTitle': { zh: '永久删除对话？', en: 'Delete conversation permanently?' },
  'sidebar.deleteNoUndoBody': { zh: '未能加载该对话的完整内容（可能网络较慢），删除后将无法撤销恢复。确定要永久删除吗？', en: 'Couldn\u2019t load this conversation\u2019s full content (the connection may be slow), so deletion cannot be undone. Delete it permanently anyway?' },
  'sidebar.deleteAnyway': { zh: '仍然删除', en: 'Delete anyway' },

  // ══════════════════════════════════════
  //  Toolbar & Input
  // ══════════════════════════════════════
  'toolbar.enhance': { zh: '增强', en: 'Enhance' },
  // ── Two-tier capability dial (Chat / Studio) ──
  'toolbar.modeChatTooltip': { zh: 'Chat — 全能助手（默认），代码执行/记忆/搜索全开', en: 'Chat — the everyday all-rounder (default): code, memory, search' },
  'toolbar.modeStudioTooltip': { zh: 'Studio — 绑定项目仓库，解锁项目工具', en: 'Studio — attach a project/repo to unlock project tools' },
  'toolbar.extrasTooltip': { zh: '更多能力', en: 'More capabilities' },
  'toolbar.execSection': { zh: '执行策略', en: 'Execution strategy' },
  'mobile.capabilityMode': { zh: '能力模式', en: 'Capability mode' },
  'mobile.modeChatDesc': { zh: '全能助手，默认', en: 'All-rounder, default' },
  'mobile.modeStudioDesc': { zh: '绑定项目，最强', en: 'Attach a project' },
  'mobile.compactUsage': { zh: '已用 {pct}%', en: '{pct}% used' },
  'mobile.compactDesc': { zh: '压缩此对话以释放上下文空间', en: 'Compact this conversation to free context' },
  'mobile.context': { zh: '上下文', en: 'Context' },
  'mobile.compactHistoryDesc': { zh: '浏览此对话的压缩快照', en: 'Browse compaction snapshots for this conversation' },
  'compactCard.rounds': { zh: '已折叠 {n} 个工具轮', en: 'folded {n} tool rounds' },
  'toolbar.aiEnhance': { zh: 'AI 增强', en: 'AI Enhance' },
  'toolbar.tools': { zh: '工具', en: 'Tools' },
  'toolbar.externalTools': { zh: '外部工具', en: 'External Tools' },
  'toolbar.mode': { zh: '模式', en: 'Mode' },
  'toolbar.execMode': { zh: '执行模式', en: 'Execution Mode' },
  'toolbar.codeExec': { zh: '代码执行', en: 'Code Execution' },
  'toolbar.codeExecDesc': { zh: '允许 AI 运行代码', en: 'Allow AI to run code' },
  'toolbar.memory': { zh: '记忆经验', en: 'Memory' },
  'toolbar.memoryDesc': { zh: '注入积累的经验', en: 'Inject accumulated experience' },
  'toolbar.autoTranslate': { zh: '自动翻译', en: 'Auto Translate' },
  'toolbar.autoTranslateDesc': { zh: '中英互译 · ⌘⇧K 跳过选中', en: 'CN↔EN translate · ⌘⇧K to skip selected' },
  'toolbar.translateBadge': { zh: '译', en: 'T' },
  'toolbar.browserBridge': { zh: '浏览器桥接', en: 'Browser Bridge' },
  'toolbar.browserBridgeDesc': { zh: '控制浏览器标签页', en: 'Control browser tabs' },
  'toolbar.desktopControl': { zh: '桌面控制', en: 'Desktop Control' },
  'toolbar.desktopControlDesc': { zh: '操作本地应用与文件', en: 'Operate local apps and files' },
  'toolbar.scheduledTasks': { zh: '定时任务', en: 'Scheduled Tasks' },
  'toolbar.scheduledTasksDesc': { zh: '计划任务与 Cron', en: 'Task scheduling & Cron' },
  'toolbar.attachFiles': { zh: '附加文件（图片 / PDF / 文档）', en: 'Attach files (images, PDF, docs)' },
  'toolbar.creativeMode': { zh: '创作模式', en: 'Creative Mode' },
  'toolbar.creativeModeDesc': { zh: '切换到图像创作画布', en: 'Switch to the image canvas' },
  'toolbar.aiDrawing': { zh: 'AI 绘图', en: 'AI Drawing' },
  'toolbar.aiDrawingDesc': { zh: '对话中生成图片', en: 'Generate images in conversation' },
  'toolbar.humanAICollab': { zh: '人机协作', en: 'Human-AI Collab' },
  'toolbar.humanAICollabDesc': { zh: 'AI 可向你提问寻求指导', en: 'AI can ask you for guidance' },
  'toolbar.swarmAgents': { zh: '蜂群代理', en: 'Swarm Agents' },
  'toolbar.swarmAgentsDesc': {
    zh: '并行子代理分解任务 · 独立工具，无需开启 Project',
    en: 'Parallel sub-agents · works without Project mode',
  },
  'toolbar.autonomousMode': { zh: '自主模式', en: 'Autonomous Mode' },
  'toolbar.autonomousModeDesc': { zh: '自主执行+自我审查循环', en: 'Autonomous execution + self-review loop' },
  'autopilot.composing': { zh: 'Autopilot 正在生成下一条用户回复…', en: 'Autopilot is composing the next reply…' },
  'autopilot.warming': { zh: 'Autopilot 启动中…', en: 'Autopilot starting…' },
  'autopilot.sentToAgent': { zh: '作为下一条消息发送给智能体', en: 'Sent to the agent as the next message' },
  'autopilot.privateNotSent': { zh: '私有过程 · 不发送给智能体', en: 'Private · not sent to the agent' },
  'autopilot.runFold': { zh: '自动驾驶运行', en: 'Autopilot run' },
  /* Run-tail notices — a run that ended WITHOUT answering. Each names the
   * actual cause: a silent stop is what made the original failure invisible. */
  'autopilot.endedYielded': {
    zh: 'Autopilot 已让位 —— 你发了新消息，它到此停止',
    en: 'Autopilot stood down — you sent a message, so it stopped here' },
  'autopilot.endedAborted': {
    zh: 'Autopilot 已停止 —— 本轮在运行中被取消',
    en: 'Autopilot stopped — the turn was cancelled while it was working' },
  'autopilot.endedSuperseded': {
    zh: 'Autopilot 已让位 —— 新的一轮接管了这个对话',
    en: 'Autopilot stood down — a newer turn took over this conversation' },
  'autopilot.endedBudget': {
    zh: 'Autopilot 提前停止 —— 已用尽轮次预算（需人工复核）',
    en: 'Autopilot stopped early — it hit its turn budget (needs review)' },
  'autopilot.endedNoProgress': {
    zh: 'Autopilot 提前停止 —— 不再有实质进展（需人工复核）',
    en: 'Autopilot stopped early — it stopped making progress (needs review)' },
  'autopilot.endedStuck': {
    zh: 'Autopilot 提前停止 —— 出现重复循环（需人工复核）',
    en: 'Autopilot stopped early — it was repeating itself (needs review)' },
  'autopilot.unsentReply': {
    zh: '这条回复已写出，但从未发进对话',
    en: 'This reply was written but never sent to the conversation' },
  'autopilot.runFoldHint': { zh: '轮交互 · 点击展开', en: 'turns — click to expand' },
  'autopilot.label': { zh: 'Autopilot', en: 'Autopilot' },
  'stream.role.worker': { zh: '执行者', en: 'Worker' },
  'stream.role.planner': { zh: '规划者', en: 'Planner' },
  'stream.role.critic': { zh: '评审', en: 'Critic' },
  'stream.phase.preparing': { zh: '准备中…', en: 'Preparing…' },
  'branch.collapse': { zh: '收起', en: 'Collapse' },
  'branch.delete': { zh: '删除', en: 'Delete' },
  'branch.deleteBranch': { zh: '删除此分支', en: 'Delete this branch' },
  'branch.deleteConfirm': { zh: '确定删除此分支吗？此操作不可撤销。', en: 'Delete this branch? This cannot be undone.' },
  'branch.selectionCtx': { zh: '基于选中内容：{text}', en: 'From selection: {text}' },
  'branch.emptyHint': { zh: '分支还没有内容，在下方输入开始这一段对话', en: 'No messages in this branch yet — start it below' },
  'branch.userTurns': { zh: '{n} 轮用户发言', en: '{n} user turns' },
  'branch.stop': { zh: '停止', en: 'Stop' },
  'branch.stopGen': { zh: '停止生成', en: 'Stop generating' },
  'branch.collapseBranch': { zh: '收起分支面板', en: 'Collapse branch panel' },
  'branch.collapseCta': { zh: '收起', en: 'Collapse' },
  'branch.inputHint': { zh: '消息只进入此分支，不影响主线', en: 'Messages go to this branch only — the main line is untouched' },
  'branch.inputPlaceholder': { zh: '向分支「{title}」发消息…', en: 'Message branch "{title}"…' },
  'branch.modeBanner': { zh: '分支模式：{title}', en: 'Branch mode: {title}' },
  'branch.exit': { zh: '退出', en: 'Exit' },
  'branch.namePrompt': { zh: '给这个分支起个名字', en: 'Name this branch' },
  'branch.createFailed': { zh: '创建分支失败，请重试', en: 'Failed to create the branch — please try again' },
  // ── Image upload chip: shown on the pending-image thumbnail while it is
  //    still compressing/uploading (upload.js::renderImagePreviews). Missing
  //    this key made t() return the literal "upload.processing" on the chip.
  'upload.processing': { zh: '处理中…', en: 'Processing…' },
  'tool.noContent': { zh: '无返回内容。', en: 'No content returned.' },
  // ── Sub-agent (swarm) update card ──
  'swarmCard.received': { zh: '收到', en: 'Received' },
  'swarmCard.updateOne': { zh: '条子智能体更新', en: 'sub-agent update' },
  'swarmCard.updateMany': { zh: '条子智能体更新', en: 'sub-agent updates' },
  'swarmCard.remaining': { zh: '{r} 运行中 · {p} 等待中', en: '{r} running · {p} pending' },
  'swarmCard.noPayload': { zh: '无可用内容。', en: 'No payload available.' },
  // ── Peer-message injection card ──
  'peerCard.noPayload': { zh: '无可用消息。', en: 'No message available.' },
  // ── Timer Watcher block (tool_rounds.js _renderTimerWatcherBlock) ──
  //    Placeholders ({id}/{n}/{s}/{status}/{model}/{skip}/{err}/{m}) are
  //    substituted by the caller's .replace() chains, so keep them verbatim.
  'timerBlock.watcherTitle': { zh: '定时器监视', en: 'Timer watcher' },
  'timerBlock.waitingFirstPoll': { zh: '等待首次检查…', en: 'waiting for first check…' },
  'timerBlock.nextCheckIn': { zh: '约 {n}s 后进行下次检查…', en: 'Next check in ~{n}s…' },
  'timerBlock.nextCheckNow': { zh: '即将进行下次检查…', en: 'Next check due now…' },
  'timerBlock.headTriggered': { zh: '经过 {n} 次检查后触发', en: 'Triggered after {n} poll{s}' },
  'timerBlock.headOrphaned': { zh: '任务已中断（{n} 次检查，定时器仍在后台运行）', en: 'Task interrupted ({n} poll{s}, timer still active in background)' },
  'timerBlock.headSkipSuffix': { zh: '，跳过 {n} 次', en: ', {n} skipped' },
  'timerBlock.headErrSuffix': { zh: '，上次检查出错', en: ', last check errored' },
  'timerBlock.headWatching': { zh: '监视中…（{n} 次检查{skip}{err}）', en: 'Watching… ({n} poll{s}{skip}{err})' },
  'timerBlock.headDone': { zh: '{status}（{n} 次检查）', en: '{status} ({n} poll{s})' },
  'timerBlock.idChipTitle': { zh: '定时器 id — 点击复制', en: 'Timer id — click to copy' },
  'timerBlock.idCopied': { zh: '已复制！', en: 'Copied!' },
  'timerBlock.kindCodeTip': { zh: '由 shell 命令判定 —— 不调用模型', en: 'Decided by a shell command — no model is called' },
  'timerBlock.kindCode': { zh: '命令式', en: 'command-based' },
  'timerBlock.kindHybridTip': { zh: '模型判定；同时运行一个 shell 命令，一旦其判定持续一致即接管', en: 'Model decides; a shell command runs alongside and takes over once it consistently agrees' },
  'timerBlock.kindHybrid': { zh: '混合', en: 'hybrid' },
  'timerBlock.cadenceMax': { zh: '每 {n}s 检查一次 · 最多 {m} 次', en: 'Checks every {n}s · up to {m} times' },
  'timerBlock.cadence': { zh: '每 {n}s 检查一次', en: 'Checks every {n}s' },
  'timerBlock.showMore': { zh: '展开', en: 'show more' },
  'timerBlock.showLess': { zh: '收起', en: 'show less' },
  'timerBlock.verifying': { zh: '核查', en: 'Verifying' },
  'timerBlock.decidedBy': { zh: '判定方', en: 'Decided by' },
  'timerBlock.deciderCode': { zh: 'shell 命令退出码 —— 无模型', en: 'Shell command exit code — no model' },
  'timerBlock.verifier': { zh: '核查模型', en: 'Verifier' },
  'timerBlock.verifierLLM': { zh: '轻量 LLM · 每次轮询单独解析（见下方各次检查）', en: 'Cheap LLM · resolved per poll (see each check below)' },
  'timerBlock.predicate': { zh: '判定命令', en: 'Predicate' },
  'timerBlock.command': { zh: '命令', en: 'Command' },
  'timerBlock.cadenceLabel': { zh: '节奏', en: 'Cadence' },
  'timerBlock.verifiedByTitle': { zh: '由 {model} 核查', en: 'Verified by {model}' },
  'timerBlock.pollIdTitle': { zh: '轮询 id —— 可在 app.log 中搜索', en: 'Poll id — search app.log for this' },
  'timerBlock.rawOutput': { zh: '原始 LLM 输出（无法解析的判定）：', en: 'Raw LLM output (unparseable decision):' },
  'timerBlock.toolsCalled': { zh: '本次轮询调用的工具：', en: 'Tools called this poll:' },
  'timerBlock.checkOutput': { zh: '检查输出（证据）：', en: 'Check output (evidence):' },
  'timerBlock.toolCallsTitle': { zh: '本次轮询 {n} 次工具调用', en: '{n} tool call(s) this poll' },
  'timerBlock.hiddenChecks': { zh: '{n} 次更早的检查已隐藏', en: '{n} earlier check{s} hidden' },
  'timerBlock.skipped': { zh: '跳过 {n} 次轮询 —— check_command 输出未变化', en: '{n} poll{s} skipped — check_command output unchanged' },
  'timerBlock.predicateReady': { zh: '条件已满足（命令退出码 {code}）', en: 'Condition met (command exit {code})' },
  'timerBlock.predicateWait': { zh: '尚未满足（命令退出码 {code}）', en: 'Not met yet (command exit {code})' },
  'timerBlock.predicateAmbiguous': { zh: '命令结果无法判定 —— 继续等待', en: 'Command result inconclusive — still waiting' },
  'tool.hallucinated': { zh: '非真实工具', en: 'not a real tool' },
  'tool.hallucinatedTip': { zh: '模型调用了本轮不存在的工具，已被拒绝、未执行。', en: "The model called a tool that doesn't exist this turn — it was rejected and never run." },
  'tool.didYouMean': { zh: '是否想用', en: 'did you mean' },
  // ── Write-gate refusal card (shared-worktree guards: read-before-edit +
  //    write-freshness). Shown when a write tool call was INTERCEPTED — nothing
  //    executed; the assistant re-reads and re-issues on its own. ──
  'tool.gateStaleBadge': { zh: '已被外部修改', en: 'changed on disk' },
  'tool.gateReadFirstBadge': { zh: '需先读取文件', en: 'must read first' },
  'tool.gatePartialStaleBadge': { zh: '部分拦截·外部修改', en: 'partial · changed' },
  'tool.gatePartialReadFirstBadge': { zh: '部分拦截·需先读取', en: 'partial · unread' },
  'tool.gateContentRefBadge': { zh: '内容引用失败', en: 'content ref failed' },
  'tool.gateTargetGeneric': { zh: '目标文件', en: 'The target file' },
  'tool.gateStaleTitle': { zh: '写入被拦截：文件已被外部修改', en: 'Write blocked — file changed on disk' },
  'tool.gateStaleText': { zh: '{paths} 在本次会话上次读取/写入之后被其他会话或进程修改。为防止静默覆盖他人的改动，本次写入没有执行 —— 助手会重新读取该文件并重发编辑，无需人工处理。', en: '{paths} was modified by another conversation or process after this conversation last read/wrote it. To avoid silently overwriting their change, this write was NOT executed — the assistant will re-read the file and re-issue the edit; no action needed from you.' },
  'tool.gateReadFirstTitle': { zh: '编辑被拦截：本会话尚未读取该文件', en: 'Edit blocked — file not read in this conversation yet' },
  'tool.gateReadFirstText': { zh: '按「先读后写」保护规则，编辑 {paths} 前必须先在本次会话中用 read_files 读取它，防止凭记忆或猜测打补丁。本次编辑没有执行 —— 助手会先读取再重发。', en: 'The read-before-edit guard requires reading {paths} with read_files in this conversation before patching it, so patches are never built from guessed or remembered content. This edit was NOT executed — the assistant will read the file first and re-issue.' },
  'tool.gatePartialStaleTitle': { zh: '{skipped} 处编辑被拦截：目标文件已被外部修改', en: '{skipped} edit(s) blocked — target file(s) changed on disk' },
  'tool.gatePartialStaleText': { zh: '{paths} 在上次读取/写入之后被其他会话或进程修改，相关 {skipped} 处编辑没有执行；其余 {proceeded} 处已照常执行。被拦截的编辑会在重新读取后重发。', en: '{paths} changed on disk after this conversation last read/wrote it, so {skipped} edit(s) targeting it were NOT executed; the other {proceeded} edit(s) ran normally. The blocked edits will be re-issued after a fresh read.' },
  'tool.gatePartialReadFirstTitle': { zh: '{skipped} 处编辑被拦截：需先读取文件', en: '{skipped} edit(s) blocked — must read first' },
  'tool.gatePartialReadFirstText': { zh: '{paths} 在本次会话中尚未读取过，相关 {skipped} 处编辑没有执行；其余 {proceeded} 处已照常执行。助手会先读取再重发被拦截的编辑。', en: '{paths} has not been read in this conversation, so {skipped} edit(s) targeting it were NOT executed; the other {proceeded} edit(s) ran normally. The assistant will read the file and re-issue the blocked edits.' },
  'tool.gateContentRefTitle': { zh: '写入未执行：内容引用解析失败', en: 'Write not executed — content reference failed' },
  'tool.gateContentRefText': { zh: 'write_file 的 content_ref 指向的前轮工具结果不存在或没有内容。助手会改为显式提供 content 重新写入。', en: 'The content_ref used by write_file points to a previous tool result that does not exist or has no content. The assistant will retry with explicit content instead.' },
  'autopilot.armedTitle': { zh: '已接管', en: 'Autopilot armed' },
  'autopilot.armedBody': { zh: '当前回复结束后，虚拟用户将自动接管对话', en: 'The virtual user will take over once the current reply finishes' },
  'autopilot.pendingTakeover': { zh: 'Autopilot 待接管（当前回复结束后）', en: 'Autopilot will take over (after the current reply)' },
  'autopilot.cancelTakeover': { zh: '取消自动接管', en: 'Cancel autopilot takeover' },
  'autopilot.armedShort': { zh: 'Autopilot 已就绪', en: 'Autopilot armed' },
  'toolbar.autopilot': { zh: '自动驾驶', en: 'Autopilot' },
  'toolbar.autopilotDesc': { zh: '虚拟用户自动回复，直到任务完成', en: 'Virtual user auto-replies until task is done' },
  'toolbar.flow': { zh: '编排流程', en: 'Flow' },
  'toolbar.flowNone': { zh: '不使用', en: 'None' },
  'toolbar.flowNoneDesc': { zh: '不启用编排流程', en: 'No orchestration flow' },
  'toolbar.flowCustom': { zh: '自定义流程', en: 'Custom flow' },
  'toolbar.flowCustomDesc': { zh: '来自编排工作室的流程', en: 'A flow from the Orchestration Studio' },

  // ══════════════════════════════════════
  //  Orchestration Studio — inspector + group
  // ══════════════════════════════════════
  'orch.palette.control': { zh: '控制', en: 'Control' },
  'orch.palette.agents': { zh: '智能体', en: 'Agents' },
  'orch.palette.group': { zh: '分组', en: 'Group' },
  'orch.palette.foot': { zh: '拖到画布 → 连接端口 → 在检查器中调参。', en: 'Drag onto the canvas → wire ports → tune in the inspector.' },
  'orch.palette.tapHint': { zh: '点按任一节点即可添加到画布中央。', en: 'Tap any node to drop it onto the canvas centre.' },
  'orch.kind.agent': { zh: '智能体', en: 'Agent' },
  'orch.kind.control': { zh: '控制', en: 'Control' },
  'orch.kind.group': { zh: '分组', en: 'Group' },
  'orch.insp.empty': { zh: '选择一个节点以编辑其设置。', en: 'Select a node to edit its settings.' },
  'orch.sec.task': { zh: '任务', en: 'Task' },
  'orch.sec.execution': { zh: '执行', en: 'Execution' },
  'orch.sec.io': { zh: '数据 I/O', en: 'Data I/O' },
  'orch.sec.persona': { zh: '角色设定（只读）', en: 'Persona (read-only)' },
  'orch.sec.flow': { zh: '数据流', en: 'Data flow' },
  'orch.persona.note': { zh: '🎭 这是该角色固定的「人设」——它的系统提示词由后端设计（lib/swarm/registry.AGENT_ROLES），决定了这个角色做什么、怎么做。此处<b>仅供查看，不可编辑</b>：提示词是角色设计的一部分，不是逐流程可改的字段。要调整它的行为，请在「执行」里选择模型档位与上下文，并通过连线为它提供数据。', en: '🎭 This is the role\'s fixed persona — its system prompt is designed by the backend (lib/swarm/registry.AGENT_ROLES) and decides what this character does and how. It is shown <b>for reference only and cannot be edited</b>: the prompt is part of the role\'s design, not a per-flow field. To change its behaviour, set the model tier and context in <b>Execution</b> and feed it data via wired inputs.' },
  'orch.persona.prompt': { zh: '系统提示词设计', en: 'System prompt design' },
  'orch.persona.none': { zh: '该角色没有内置人设——运行时按通用智能体处理。', en: 'No built-in persona for this role — it runs as a generic agent.' },
  'orch.sec.lastRun': { zh: '最近运行', en: 'Last run' },
  'orch.run.note': { zh: '🚀 这是该节点在最近一次运行中实际看到与产出的内容——状态、它执行时使用的已解析指令，以及它的输出。这就是流程的可追溯轨迹。', en: '🚀 What this node actually saw and produced on the most recent run — its status, the resolved brief it ran with, and its output. This is the flow\'s traceability trail.' },
  'orch.run.status': { zh: '状态', en: 'Status' },
  'orch.run.statusRunning': { zh: '运行中…', en: 'Running…' },
  'orch.run.statusDone': { zh: '完成', en: 'Done' },
  'orch.run.statusError': { zh: '失败', en: 'Failed' },
  'orch.run.actions': { zh: '状态变更操作', en: 'State-changing actions' },
  'orch.run.output': { zh: '输出', en: 'Output' },
  'orch.run.streaming': { zh: '正在生成…', en: 'Streaming…' },
  // ── Studio run drawer (orchestration.js) — the two run modes ──
  'orch.run.title': { zh: '运行流程', en: 'Run Flow' },
  'orch.run.hint': { zh: '<b>测试运行</b>在此处执行一次，随后即丢弃。<b>作为任务运行</b>会保存一个可重开的持久任务，你能在「任务」中查看。', en: '<b>Test run</b> executes once here and is discarded. <b>Run as Task</b> saves a durable, reopenable job you can watch in Task Mode.' },
  'orch.run.previewPlan': { zh: '预览计划', en: 'Preview plan' },
  'orch.run.testRun': { zh: '测试运行', en: 'Test run' },
  'orch.run.asTask': { zh: '作为任务运行', en: 'Run as Task' },
  'orch.run.stop': { zh: '停止', en: 'Stop' },
  // ── Task Mode (task-mode.js) — the operating room ──
  'tm.top.name': { zh: '任务模式', en: 'Task Mode' },
  'tm.top.sub': { zh: '查看并重新打开你的流程运行', en: 'watch & reopen your flow runs' },
  'tm.btn.studio': { zh: '编排', en: 'Studio' },
  'tm.btn.refresh': { zh: '刷新', en: 'Refresh' },
  'tm.rail.runs': { zh: '运行', en: 'Runs' },
  'tm.select': { zh: '选择一个运行以查看其时间线。', en: 'Select a run to view its timeline.' },
  'tm.stream.timeline': { zh: '时间线', en: 'Timeline' },
  'tm.inspector': { zh: '检查器', en: 'Inspector' },
  'tm.insp.empty': { zh: '当前节点与人工确认会显示在这里。', en: 'Active node & human gates appear here.' },
  'tm.insp.emptyHint': { zh: '点击任意节点查看其运行轨迹。', en: 'Click any node to inspect its run trace.' },
  'tm.loading': { zh: '正在加载运行…', en: 'Loading runs…' },
  'tm.err.title': { zh: '无法加载运行', en: 'Couldn\'t load runs' },
  'tm.err.sub': { zh: '服务器未响应。请检查网络后重试。', en: 'The server didn\'t respond. Check your connection and retry.' },
  'tm.btn.retry': { zh: '重试', en: 'Retry' },
  'tm.empty.title': { zh: '还没有任务运行', en: 'No task runs yet' },
  'tm.empty.sub': { zh: '运行是从流程启动的长时任务。先在编排台中设计一个流程，再用<b>作为任务运行</b>在这里启动一个。', en: 'Runs are long-lived jobs launched from a flow. Design a flow in the Studio, then use <b>Run as Task</b> to start one here.' },
  'tm.btn.openStudio': { zh: '打开编排台', en: 'Open Studio' },
  'tm.btn.editStudio': { zh: '在编排台中编辑', en: 'Edit in Studio' },
  'tm.btn.delete': { zh: '删除', en: 'Delete' },
  'tm.btn.abort': { zh: '中止', en: 'Abort' },
  'tm.final.result': { zh: '结果', en: 'Result' },
  'tm.dur.running': { zh: '运行中', en: 'running' },
  'tm.line.stepRunning': { zh: '<b>{name}</b> 运行中…', en: '<b>{name}</b> running…' },
  'tm.line.reconnecting': { zh: '连接中断——正在重连…', en: 'connection lost — reconnecting…' },
  'tm.line.offline': { zh: '仍然离线——点击某个运行以重试', en: 'still offline — click a run to retry' },
  'tm.line.reconnected': { zh: '已重连', en: 'reconnected' },
  'tm.studioUnavailable': { zh: '编排台不可用', en: 'Studio unavailable' },
  'tm.abort.confirm': { zh: '要中止此运行吗？流程将停在当前位置。', en: 'Abort this run? The flow will stop where it is.' },
  'tm.abort.confirmTitle': { zh: '中止运行', en: 'Abort run' },
  'tm.delete.confirm': { zh: '要永久删除此运行吗？其时间线与结果将无法恢复。', en: 'Delete this run permanently? Its timeline and result cannot be recovered.' },
  'tm.delete.confirmTitle': { zh: '删除运行', en: 'Delete run' },
  'tm.toast.abort': { zh: '已请求中止', en: 'Abort requested' },
  'tm.status.pending': { zh: '等待中', en: 'Pending' },
  'tm.status.running': { zh: '运行中', en: 'Running' },
  'tm.status.paused': { zh: '待人工处理', en: 'Paused' },
  'tm.status.done': { zh: '完成', en: 'Done' },
  'tm.status.error': { zh: '失败', en: 'Failed' },
  'tm.status.aborted': { zh: '已中止', en: 'Aborted' },
  'tm.runNotFound': { zh: '未找到该运行。', en: 'Run not found.' },
  // ── Human gate card (the interactive approve/reject/input surface) ──
  'tm.gate.tag': { zh: '人工确认', en: 'Human gate' },
  'tm.gate.who': { zh: '人工', en: 'Human' },
  'tm.gate.approvePrompt': { zh: '批准以继续？', en: 'Approve to continue?' },
  'tm.gate.inputPrompt': { zh: '请输入？', en: 'Your input?' },
  'tm.gate.approve': { zh: '批准', en: 'Approve' },
  'tm.gate.reject': { zh: '拒绝', en: 'Reject' },
  'tm.gate.send': { zh: '发送', en: 'Send' },
  'tm.gate.inputPlaceholder': { zh: '输入你的回答…', en: 'Type your answer…' },
  'tm.gate.approved': { zh: '已批准', en: 'Approved' },
  'tm.gate.rejected': { zh: '已拒绝', en: 'Rejected' },
  'tm.gate.enterResponse': { zh: '请输入回答', en: 'Enter a response' },
  // ── Inspector trace-detail pane ──
  'tm.insp.runTrace': { zh: '运行轨迹', en: 'Run trace' },
  'tm.insp.node': { zh: '节点', en: 'Node' },
  'tm.insp.activeNode': { zh: '当前节点', en: 'Active node' },
  'tm.trace.error': { zh: '错误', en: 'Error' },
  'tm.trace.brief': { zh: '已解析指令（提示词）', en: 'Resolved brief (prompt)' },
  'tm.trace.input': { zh: '输入', en: 'Input' },
  'tm.trace.output': { zh: '输出', en: 'Output' },
  'tm.trace.truncated': { zh: '（已截断）', en: '(truncated)' },
  'tm.trace.emits': { zh: '产出', en: 'emits' },
  'tm.trace.stateChanging': { zh: '次状态变更', en: 'state-changing' },
  'tm.trace.iter': { zh: '迭代', en: 'iter' },
  'tm.trace.isolation': { zh: '隔离环境', en: 'isolation' },
  // ── DAG node ribbons + subtitles ──
  'tm.ribbon.input': { zh: '输入', en: 'INPUT' },
  'tm.ribbon.result': { zh: '结果', en: 'RESULT' },
  'tm.sub.subflow': { zh: '子流程', en: 'subflow' },
  'tm.sub.fanout': { zh: '扇出', en: 'fan-out' },
  'tm.sub.routes': { zh: '分支', en: 'routes' },
  'tm.sub.max': { zh: '最多', en: 'max' },
  'tm.sub.approvalGate': { zh: '审批门', en: 'approval gate' },
  'tm.sub.collectInput': { zh: '收集输入', en: 'collect input' },
  'tm.sub.notify': { zh: '通知', en: 'notify' },
  'tm.sub.gate': { zh: '人工门', en: 'gate' },
  'tm.sub.startInput': { zh: '输入', en: 'input' },
  'tm.sub.stopResult': { zh: '结果', en: 'result' },
  // ── Live timeline event lines ──
  'tm.ev.flowNodes': { zh: '{n} 个节点', en: '{n} nodes' },
  'tm.ev.flowFallback': { zh: '流程', en: 'flow' },
  'tm.ev.loopIter': { zh: '循环迭代 {i}/{max}', en: 'loop iteration {i}/{max}' },
  'tm.ev.zeroGuard': { zh: '零交付物保护', en: 'zero-deliverable guard' },
  'tm.ev.replan': { zh: '重新规划 #{n}', en: 're-plan #{n}' },
  'tm.ev.stuck': { zh: '卡住——正在跳出循环', en: 'stuck — breaking the loop' },
  'tm.ev.fanout': { zh: '扇出 → {n} 个分支', en: 'fan-out → {n} branches' },
  'tm.ev.route': { zh: '路由 → {name}', en: 'route → {name}' },
  'tm.ev.deliverable': { zh: '交付物：', en: 'deliverable: ' },
  'tm.ev.gateAwaiting': { zh: ' — 门等待响应', en: ' — gate awaiting response' },
  'tm.ev.unnamed': { zh: '（未命名）', en: '(unnamed)' },
  'tm.ev.none': { zh: '（无）', en: '(none)' },
  'tm.toast.deleteFailed': { zh: '删除失败', en: 'Delete failed' },
  'tm.tip.close': { zh: '关闭', en: 'Close' },
  'tm.tip.inspectNode': { zh: '点击查看该节点的运行轨迹', en: "Click to inspect this node's run trace" },
  'orch.flow.note': { zh: '🔀 这个流程节点本身不携带带类型的数据端口；这里按已连线的边汇总「输入来自谁、输出去往谁」，以及该节点对数据做了什么——让数据流向一目了然。', en: '🔀 This flow node carries no typed data ports; this summarises — from the wired edges — what feeds IN and where output goes OUT, plus what the node does to the data, so the dataflow is legible at a glance.' },
  'orch.flow.in': { zh: '输入来自', en: 'In from' },
  'orch.flow.out': { zh: '输出去往', en: 'Out to' },
  'orch.flow.none': { zh: '（未连线）', en: '(not wired)' },
  'orch.flow.fromUser': { zh: '用户请求（流程入口）', en: 'The user request (flow entry)' },
  'orch.flow.seedSet': { zh: '已设置的初始输入', en: 'The configured initial input' },
  'orch.flow.toChat': { zh: '作为流程结果返回对话', en: 'Returned to chat as the flow result' },
  'orch.flow.carry.start': { zh: '▶ 把初始上下文原样传给下游第一个智能体。', en: '▶ Passes the initial context through to the first downstream agent unchanged.' },
  'orch.flow.carry.loop': { zh: '🔁 把被包裹智能体的输出回灌为下一轮的输入，直到满足停止条件。', en: '🔁 Feeds the wrapped agent\'s output back as the next iteration\'s input until the stop condition holds.' },
  'orch.flow.carry.parallel': { zh: '⋔ 把输入扇出到多个下游分支并行执行（每项一个）。', en: '⋔ Fans the input out to multiple downstream branches in parallel (one per item).' },
  'orch.flow.carry.barrier': { zh: '⊟ 等待所有并行分支完成，把它们的输出汇聚为一份再继续。', en: '⊟ Waits for all parallel branches, then merges their outputs into one before continuing.' },
  'orch.flow.carry.branch': { zh: '⑂ 由分类器选择一条路径，只把输入送往被选中的下游分支。', en: '⑂ A classifier picks one path; the input goes only to the chosen downstream branch.' },
  'orch.flow.carry.human': { zh: '🧑 暂停流程：审批可中止，输入会把回答追加到下游上下文，通知则原样透传。', en: '🧑 Pauses the flow: approve can halt it, input appends the answer to the downstream context, notify passes data through unchanged.' },
  'orch.flow.carry.stop': { zh: '■ 终点：把上游最后的收敛输出作为流程结果返回。', en: '■ Terminal: returns the last upstream converged output as the flow result.' },
  'orch.sec.identity': { zh: '身份', en: 'Identity' },
  'orch.sec.settings': { zh: '设置', en: 'Settings' },
  'orch.note.exec': { zh: '⚙ <b>档位</b>选模型强度；<b>上下文</b>决定该智能体是跨循环记忆（共享）还是无状态扇出（全新）；<b>发言身份</b>决定这一轮落在对话的哪一侧（Critic/Virtual User → User，Worker → Assistant）。', en: '⚙ <b>Tier</b> picks model strength; <b>Context</b> sets whether the agent remembers across loops (shared) or is a stateless fan-out (fresh); <b>Speaks as</b> sets which side of the chat the turn lands on (Critic/Virtual User → User, Worker → Assistant).' },
  'orch.insp.stats': { zh: '{n} 个节点 · {m} 条连接', en: '{n} nodes · {m} links' },
  'orch.fld.label': { zh: '标签', en: 'Label' },
  'orch.fld.objective': { zh: '目标', en: 'Objective' },
  'orch.fld.objectivePh': { zh: '这个智能体要完成什么？像向刚加入的同事交代任务一样描述它。', en: 'What should this agent accomplish? Brief it like a colleague who just walked in.' },
  'orch.note.objective': { zh: '🔌 <b>目标</b>是该智能体在后端的任务简报 —— 它会原样成为子智能体的 <code>SubTaskSpec.objective</code>。留空 → 引擎回退到节点标签，再回退到通用的“执行此步骤”。请写得具体：这段文字是决定该角色行为的最大杠杆。', en: '🔌 This <b>Objective</b> is the agent\'s task brief on the backend — it becomes the sub-agent\'s <code>SubTaskSpec.objective</code> verbatim. Empty → the engine falls back to the node Label, then to a generic “Execute this step.” Be specific: this text is the single biggest lever on what this role actually does.' },
  'orch.fld.tier': { zh: '模型档位', en: 'Model tier' },
  'orch.tier.light': { zh: '轻量 · 快速', en: 'Light · fast' },
  'orch.tier.standard': { zh: '标准 · 同主模型', en: 'Standard · parent' },
  'orch.tier.heavy': { zh: '重型 · 最强', en: 'Heavy · strongest' },
  'orch.fld.context': { zh: '上下文', en: 'Context' },
  'orch.iso.fresh': { zh: '全新 —— 一次性、隔离', en: 'Fresh — one-shot, isolated' },
  'orch.iso.shared': { zh: '共享 —— 跨循环累积', en: 'Shared — accumulates across loops' },
  'orch.note.context': { zh: '💡 <b>共享</b>上下文让 Worker 跨循环迭代学习（类似自主模式）。<b>全新</b>则是无状态的扇出子智能体。', en: '💡 <b>Shared</b> context makes a Worker that learns across loop iterations (like endpoint mode). <b>Fresh</b> is a stateless fan-out sub-agent.' },
  'orch.fld.emits': { zh: '发言身份', en: 'Speaks as' },
  'orch.emits.auto': { zh: '自动 · 按角色（{role}）', en: 'Auto · by role ({role})' },
  'orch.emits.assistant': { zh: 'Assistant —— AI 一方', en: 'Assistant — the AI side' },
  'orch.emits.user': { zh: 'User —— 人类 / 评审一方', en: 'User — the human / reviewer side' },
  'orch.note.emits': { zh: '🗣️ <b>发言身份</b>决定这一轮落在对话的哪一侧。Critic 或 Virtual User 以 <b>User</b> 身份发言；Worker 以 <b>Assistant</b> 身份发言。这正是让自定义流程同时覆盖自主模式（critic→user）和自动驾驶（virtual user→user）的关键。', en: '🗣️ <b>Speaks as</b> sets which side of the chat this turn lands on. A Critic or Virtual User speaks as <b>User</b>; a Worker speaks as <b>Assistant</b>. This is what lets a custom flow cover both autonomous mode (critic→user) and autopilot (virtual user→user).' },
  'orch.fld.maxIter': { zh: '最大迭代次数', en: 'Max iterations' },
  'orch.fld.stopWhen': { zh: '停止条件', en: 'Stop when' },
  'orch.stop.verdict': { zh: '校验者返回 STOP（已接线）', en: 'Verifier returns STOP (wired)' },
  'orch.stop.noNew': { zh: '某轮没有新发现（计划中）', en: 'A round finds nothing new (planned)' },
  'orch.stop.maxOnly': { zh: '仅迭代次数上限', en: 'Only the iteration cap' },
  'orch.fld.verifier': { zh: '校验者角色', en: 'Verifier role' },
  'orch.verifier.critic': { zh: 'Critic', en: 'Critic' },
  'orch.verifier.reviewer': { zh: 'Reviewer', en: 'Reviewer' },
  'orch.verifier.none': { zh: '无校验者', en: 'No verifier' },
  'orch.note.loop': { zh: '🔁 在此循环中包裹一个 Worker（+ 可选校验者）。校验者是与生产者<i>不同</i>的智能体 —— 这就是结构化的对抗式校验。', en: '🔁 Wrap a Worker (+ optional Verifier) inside this loop. The verifier is a <i>different</i> agent than the producer — that\'s structural adversarial verification.' },
  'orch.fld.maxConcurrent': { zh: '最大并发数', en: 'Max concurrent' },
  'orch.fld.perItem': { zh: '每项一个智能体', en: 'One agent per item' },
  'orch.fld.classifier': { zh: '分类器角色', en: 'Classifier role' },
  'orch.classifier.router': { zh: 'Router', en: 'Router' },
  'orch.classifier.analyst': { zh: 'Analyst', en: 'Analyst' },
  'orch.classifier.general': { zh: 'General', en: 'General' },
  'orch.fld.branchCount': { zh: '分支数', en: 'Branch count' },
  'orch.fld.filePath': { zh: '文件路径', en: 'File path' },
  'orch.fld.filePathPh': { zh: '例如 reports/findings.md', en: 'e.g. reports/findings.md' },
  'orch.fld.artifactKind': { zh: '类型', en: 'Kind' },
  'orch.afmt.file': { zh: '文件', en: 'File' },
  'orch.afmt.report': { zh: '报告', en: 'Report' },
  'orch.afmt.dataset': { zh: '数据集', en: 'Dataset' },
  'orch.afmt.code': { zh: '代码', en: 'Code' },
  'orch.afmt.image': { zh: '图片', en: 'Image' },
  'orch.fld.description': { zh: '描述', en: 'Description' },
  'orch.fld.artifactDescPh': { zh: '这个交付物必须包含什么 —— 生产方与消费方智能体之间的契约。', en: 'What this deliverable must contain — the contract between the producing and consuming agents.' },
  'orch.note.artifact': { zh: '📦 <b>交付物</b>声明一个预期的中间产出。引擎会记录它并在运行日志中呈现，便于你跟踪每个阶段应当产出什么。', en: '📦 A <b>Deliverable</b> declares an expected intermediate output. The engine records it and surfaces it in the run log so you can track what each stage is supposed to produce.' },
  'orch.fld.humanMode': { zh: '模式', en: 'Mode' },
  'orch.hmode.approve': { zh: 'Approve —— 暂停以决定通过 / 否决', en: 'Approve — pause for go / no-go' },
  'orch.hmode.input': { zh: 'Input —— 收集一个回答', en: 'Input — collect an answer' },
  'orch.hmode.notify': { zh: 'Notify —— 发送消息，不阻塞', en: 'Notify — message, don\'t block' },
  'orch.fld.prompt': { zh: '提示语', en: 'Prompt' },
  'orch.fld.promptPh': { zh: '在此关卡向用户询问 / 告知什么。', en: 'What to ask / tell the user at this gate.' },
  'orch.fld.approveTimeout': { zh: '审批超时（秒）', en: 'Approve timeout (sec)' },
  'orch.note.human': { zh: '🧑 <b>Human</b> 关卡会暂停流程：<b>Approve</b> 在否决时中止运行，<b>Input</b> 把回答追加到下游智能体的上下文，<b>Notify</b> 只呈现一条消息。复用与对话相同的审批 / 询问人类机制。', en: '🧑 A <b>Human</b> gate pauses the flow: <b>Approve</b> halts the run on reject, <b>Input</b> appends the answer to the context for downstream agents, <b>Notify</b> just surfaces a message. Reuses the same approval / ask-human plumbing as chat.' },
  'orch.fld.startInput': { zh: '初始输入 —— 流程的起点', en: 'Initial input — where the flow begins' },
  'orch.fld.startInputPh': { zh: '进入流程的请求。例如“调研排名前 3 的向量数据库并写一份对比”。', en: 'The request that enters the flow. e.g. \'Research the top 3 vector DBs and write a comparison.\'' },
  'orch.note.start': { zh: '▶ <b>Start</b> 是唯一入口：这段文字就是第一个智能体收到的 <code>initial_context</code>。你可以在此设置以让流程自包含，或留空并在 ▶ 运行面板输入请求 —— 运行面板的输入会覆盖此处的种子。', en: '▶ <b>Start</b> is the single entry point: this text is the <code>initial_context</code> the first agent receives. You can set it here so the flow is self-contained, or leave it blank and type a request in the ▶ Run panel — a Run-panel input overrides this seed.' },
  'orch.note.stop': { zh: '■ <b>Stop</b> 是唯一出口：最后一个智能体的收敛输出，将作为流程结果返回到对话。', en: '■ <b>Stop</b> is the single exit: the converged output of the last agent before it is returned to chat as the flow result.' },
  'orch.conn.in': { zh: '→ 入：', en: '→ in:' },
  'orch.conn.out': { zh: '出：', en: 'out:' },
  'orch.btn.deleteNode': { zh: '删除节点', en: 'Delete node' },
  'orch.edge.clickTip': { zh: '点击选中连线（选中后按 Delete 删除）', en: 'Click to select (then Delete to remove)' },
  'orch.edge.title': { zh: '连线', en: 'Connection' },
  'orch.edge.reverse': { zh: '⇄ 反转方向', en: '⇄ Reverse direction' },
  'orch.edge.delete': { zh: '删除连线', en: 'Delete connection' },
  'orch.edge.bindNote': { zh: '把目标的输入端口绑定到来源的某个输出（让连线携带具体数据）：', en: 'Bind the target\'s input ports to a source output (so the line carries concrete data):' },
  'orch.edge.bindTo': { zh: '输入「{port}」← ', en: 'input "{port}" ← ' },
  'orch.edge.bindNone': { zh: '（未绑定）', en: '(unbound)' },
  'orch.toast.stopNoOut': { zh: 'Stop 没有输出。', en: 'Stop has no output.' },
  'orch.toast.startNoIn': { zh: 'Start 没有输入。', en: 'Start has no input.' },
  'orch.toast.dupEdge': { zh: '已存在反向连线。', en: 'Reverse edge already exists.' },
  'orch.io.title': { zh: '数据 I/O（端口契约）', en: 'Data I/O (port contract)' },
  'orch.io.note': { zh: '为节点声明带类型的输入/输出端口。声明后，本节点只读取被显式连接的上游输出（Dify 式数据流），而非整段累积上下文。纯自然语言节点可只保留隐式 <code>text</code> 输出；工具密集型 Worker 可加一个 <code>artifact</code> 输出来暴露变更清单。', en: 'Declare typed input/output ports. Once declared, this node reads ONLY its wired upstream outputs (Dify-style dataflow), not the whole accumulating context. A pure natural-language node can keep just the implicit <code>text</code> output; a tool-heavy worker adds an <code>artifact</code> output to expose its change manifest.' },
  'orch.io.outputs': { zh: '输出', en: 'Outputs' },
  'orch.io.inputs': { zh: '输入', en: 'Inputs' },
  'orch.io.implicitOut': { zh: '隐式：单个 text 输出', en: 'Implicit: a single text output' },
  'orch.io.addOutput': { zh: '添加输出', en: 'Add output' },
  'orch.io.addInput': { zh: '添加输入', en: 'Add input' },
  'orch.io.removePort': { zh: '移除端口', en: 'Remove port' },
  'orch.io.fromStart': { zh: 'start（流程初始输入）', en: 'start (flow initial input)' },
  'orch.io.fromStale': { zh: '⚠ {node}（未连线，已失效）', en: '⚠ {node} (not wired — stale)' },
  'orch.io.fromLabel': { zh: '来自', en: 'from' },
  'orch.io.inputsHint': { zh: '每个输入端口绑定到一个上游节点的输出——只能选已连线到本节点的上游。', en: 'Each input binds to an upstream node\'s output — only nodes wired ahead of this one are selectable.' },
  'orch.io.noUpstream': { zh: '本节点还没有上游连线。先从上游节点的 ● 端口拖一条线连到本节点，再来绑定输入。', en: 'No upstream wired yet. Drag an edge from an upstream node\'s ● port into this node first, then bind the input.' },
  'orch.io.presetHint': { zh: '一键把本节点设为「工具密集型 Worker」：声明两个输出——summary（人读的文字总结）和 changes（机器可读的变更清单）。', en: 'One click sets this node up as a tool-heavy worker: declares two outputs — summary (human-readable text) and changes (a machine-readable change manifest).' },
  'orch.io.toolHeavyPreset': { zh: '设为工具密集型 Worker（summary + changes）', en: 'Set up as tool-heavy worker (summary + changes)' },
  'orch.group.chip': { zh: '分组', en: 'Group' },
  'orch.group.chipTip': { zh: '一个黑盒子流程：内部自成体系，对外像一个角色。双击进入编辑。', en: 'A black-box sub-flow: self-contained inside, acts as one role outside. Double-click to enter.' },
  'orch.group.defaultLabel': { zh: '分组', en: 'Group' },
  'orch.group.singleAllowedNote': { zh: '', en: '' },
  'orch.fld.groupFace': { zh: '对外角色（外观）', en: 'Face role (appearance)' },
  'orch.fld.groupScope': { zh: '作用域', en: 'Scope' },
  'orch.scope.isolated': { zh: '隔离 —— 黑盒，独立上下文', en: 'Isolated — black box, own context' },
  'orch.scope.inline': { zh: '内联 —— 展开到父流程', en: 'Inline — flattened into parent' },
  'orch.note.group': { zh: '🧩 <b>分组</b>是一个黑盒子：它在自己的嵌套引擎中运行，只把上游上下文作为种子读入，只把收敛后的交付物传回父流程 —— 对外它就像一个角色。<b>隔离</b>维持这层边界；<b>内联</b>则把内部节点展开进父流程（无边界）。双击节点进入分组画布。', en: '🧩 A <b>Group</b> is a black box: it runs in its own nested engine, reads only the upstream context as a seed, and returns only its converged deliverable to the parent — to the outside it acts as one role. <b>Isolated</b> keeps that boundary; <b>Inline</b> flattens its inner nodes into the parent (no boundary). Double-click the node to enter the group canvas.' },
  'orch.group.open': { zh: '↳ 打开分组画布', en: '↳ Open group canvas' },
  'orch.group.summary': { zh: '内部：{n} 个节点 · {m} 条连接', en: 'Inside: {n} nodes · {m} links' },
  'orch.crumb.root': { zh: '根流程', en: 'Root' },

  // Per-role structured params (consumed from /role-schema; labels are keys)
  'orch.field.task': { zh: '任务', en: 'Task' },
  'orch.field.taskWorker': { zh: '任务', en: 'Task' },
  'orch.field.reviewCriteria': { zh: '审查标准', en: 'Review criteria' },
  'orch.field.researchQuestions': { zh: '调研问题', en: 'Research questions' },
  'orch.field.planningBrief': { zh: '规划简报', en: 'Planning brief' },
  'orch.field.persona': { zh: '人设 / 立场', en: 'Persona / stance' },
  'orch.field.mustCheck': { zh: '必须检查', en: 'Must check' },
  'orch.field.mustDo': { zh: '必须做', en: 'Must do' },
  'orch.field.mustNotDo': { zh: '禁止做', en: 'Must not do' },
  'orch.field.sources': { zh: '信息来源', en: 'Sources' },
  'orch.field.deliverables': { zh: '交付物', en: 'Deliverables' },
  'orch.field.expectedOutcome': { zh: '预期结果', en: 'Expected outcome' },
  'orch.field.verdictFormat': { zh: '判定格式', en: 'Verdict format' },
  'orch.field.adversarial': { zh: '对抗式校验', en: 'Adversarial verification' },
  'orch.field.doneSignal': { zh: '完成信号', en: 'Done signal' },
  'orch.field.acceptance': { zh: '验收标准', en: 'Acceptance criteria' },
  'orch.field.taskCoder': { zh: '编码任务', en: 'Coding task' },
  'orch.field.scopePaths': { zh: '涉及文件 / 路径', en: 'Files / paths' },
  'orch.field.constraints': { zh: '约束', en: 'Constraints' },
  'orch.field.verifyCmd': { zh: '验证命令', en: 'Verify command' },
  'orch.field.analysisQuestion': { zh: '分析问题', en: 'Analysis question' },
  'orch.field.dataSources': { zh: '数据源', en: 'Data sources' },
  'orch.field.metrics': { zh: '指标', en: 'Metrics' },
  'orch.field.writeTask': { zh: '写作任务', en: 'Writing task' },
  'orch.field.audience': { zh: '读者对象', en: 'Audience' },
  'orch.field.tone': { zh: '语气', en: 'Tone' },
  'orch.field.mustCover': { zh: '必须涵盖', en: 'Must cover' },
  'orch.field.browseTask': { zh: '浏览任务', en: 'Browser task' },
  'orch.field.startUrl': { zh: '起始 URL', en: 'Start URL' },
  'orch.field.steps': { zh: '操作步骤', en: 'Steps' },
  'orch.field.extract': { zh: '提取内容', en: 'Extract' },
  'orch.field.synthTask': { zh: '综合任务', en: 'Synthesis task' },
  'orch.field.inputsDesc': { zh: '输入说明', en: 'Inputs' },
  'orch.field.conflictPolicy': { zh: '冲突处理', en: 'Conflict policy' },
  'orch.field.outputShape': { zh: '输出形态', en: 'Output shape' },
  'orch.field.routeBasis': { zh: '分流依据', en: 'Routing basis' },
  'orch.field.categories': { zh: '类别', en: 'Categories' },
  'orch.field.defaultRoute': { zh: '默认分支', en: 'Default route' },
  'orch.opt.unset': { zh: '（未设置）', en: '(unset)' },
  'orch.opt.stopContinue': { zh: 'STOP / CONTINUE', en: 'STOP / CONTINUE' },
  'orch.opt.passFail': { zh: 'PASS / FAIL', en: 'PASS / FAIL' },
  'orch.opt.toneNeutral': { zh: '中性', en: 'Neutral' },
  'orch.opt.toneFormal': { zh: '正式', en: 'Formal' },
  'orch.opt.toneCasual': { zh: '轻松', en: 'Casual' },
  'orch.opt.toneTechnical': { zh: '技术', en: 'Technical' },
  'orch.opt.tonePersuasive': { zh: '说服', en: 'Persuasive' },
  'orch.opt.reconcile': { zh: '调和差异', en: 'Reconcile' },
  'orch.opt.majority': { zh: '多数为准', en: 'Majority' },
  'orch.opt.flag': { zh: '标记冲突', en: 'Flag conflicts' },
  'orch.ph.task': { zh: '这个智能体要完成什么？像向刚加入的同事交代任务一样描述它。', en: 'What should this agent accomplish? Brief it like a colleague who just walked in.' },
  'orch.ph.taskWorker': { zh: '要执行的具体任务。第一步工具调用必须是产生改变的动作，而非只做分析。', en: 'The concrete task to execute. The first tool call must change state, not just analyze.' },
  'orch.ph.reviewCriteria': { zh: '这个审查者要对照什么标准检查？例如清单、正确性、无回归。', en: 'What must this reviewer check against? e.g. the checklist, correctness, no regressions.' },
  'orch.ph.researchQuestions': { zh: '要查清楚什么，从哪里查。', en: 'What to find out, and from where.' },
  'orch.ph.planningBrief': { zh: '计划应当覆盖什么？', en: 'What should the plan cover?' },
  'orch.ph.persona': { zh: '这个虚拟用户扮演谁、抱持什么立场？', en: 'Who does this virtual user play, and with what stance?' },
  'orch.ph.mustCheck': { zh: '每行一项检查点。', en: 'One check per line.' },
  'orch.ph.mustDo': { zh: '每行一项硬性要求。', en: 'One requirement per line.' },
  'orch.ph.mustNotDo': { zh: '每行一项禁区。', en: 'One prohibition per line.' },
  'orch.ph.sources': { zh: '每行一个来源（URL / 仓库 / 文档）。', en: 'One source per line (URL / repo / doc).' },
  'orch.ph.deliverables': { zh: '每行一个预期交付物。', en: 'One expected deliverable per line.' },
  'orch.ph.expectedOutcome': { zh: '完成时应当呈现的样子。', en: 'What it should look like when done.' },
  'orch.ph.doneSignal': { zh: '何种回复表示任务已完成（例如 [VU: TASK_DONE]）。', en: 'What reply signals the task is done (e.g. [VU: TASK_DONE]).' },
  'orch.ph.acceptance': { zh: '每行一条验收标准（满足即可 STOP）。', en: 'One acceptance criterion per line (met ⇒ STOP).' },
  'orch.ph.taskCoder': { zh: '要实现/修复什么？第一步应当是改动代码而非只读。', en: 'What to implement / fix? The first step should change code, not just read.' },
  'orch.ph.scopePaths': { zh: '每行一个文件或目录，限定改动范围。', en: 'One file or dir per line to scope the change.' },
  'orch.ph.constraints': { zh: '每行一条约束（如：不改公共 API、保持向后兼容）。', en: 'One constraint per line (e.g. no public API change, keep back-compat).' },
  'orch.ph.verifyCmd': { zh: '验证改动的命令，如 pytest tests/foo.py。', en: 'Command to verify the change, e.g. pytest tests/foo.py.' },
  'orch.ph.analysisQuestion': { zh: '要从数据里回答什么问题？', en: 'What question should the data answer?' },
  'orch.ph.dataSources': { zh: '每行一个数据文件 / 路径 / 表。', en: 'One data file / path / table per line.' },
  'orch.ph.metrics': { zh: '每行一个要计算的指标。', en: 'One metric to compute per line.' },
  'orch.ph.writeTask': { zh: '要写什么文档？', en: 'What document to write?' },
  'orch.ph.audience': { zh: '写给谁看？例如终端用户、维护者。', en: 'Who is it for? e.g. end users, maintainers.' },
  'orch.ph.mustCover': { zh: '每行一个必须涵盖的要点。', en: 'One point that must be covered per line.' },
  'orch.ph.browseTask': { zh: '要在浏览器里完成什么交互？', en: 'What interaction to perform in the browser?' },
  'orch.ph.startUrl': { zh: '从哪个页面开始（可留空用已打开的标签页）。', en: 'Page to start from (blank = use an open tab).' },
  'orch.ph.steps': { zh: '每行一个操作步骤（点击 / 填写 / 滚动…）。', en: 'One action per line (click / fill / scroll…).' },
  'orch.ph.extract': { zh: '需要从页面提取并返回什么？', en: 'What to extract from the page and return?' },
  'orch.ph.synthTask': { zh: '要把哪些上游结果合并成什么？', en: 'Which upstream outputs to merge, and into what?' },
  'orch.ph.inputsDesc': { zh: '描述要合并的输入（来自哪些节点）。', en: 'Describe the inputs to merge (from which nodes).' },
  'orch.ph.outputShape': { zh: '期望的产出结构（如：要点列表、对比表）。', en: 'Desired output structure (e.g. bullet list, comparison table).' },
  'orch.ph.routeBasis': { zh: '依据什么把每个条目分流？', en: 'On what basis is each item routed?' },
  'orch.ph.categories': { zh: '每行一个类别 / 分支标签。', en: 'One category / branch label per line.' },
  'orch.ph.defaultRoute': { zh: '无法分类时走哪个分支。', en: 'Which branch when nothing matches.' },
  'toolbar.moreOptions': { zh: '更多选项', en: 'More options' },
  'toolbar.exitCreativeMode': { zh: '退出创作模式 (Esc)', en: 'Exit creative mode (Esc)' },
  'toolbar.generate': { zh: '生成', en: 'Generate' },
  'toolbar.loadingModels': { zh: '正在加载模型…', en: 'Loading models…' },

  // ══════════════════════════════════════
  //  Image Generation
  // ══════════════════════════════════════
  'ig.square': { zh: '1:1 正方形', en: '1:1 Square' },
  'ig.landscape': { zh: '16:9 横屏宽幅', en: '16:9 Landscape' },
  'ig.portrait': { zh: '9:16 竖屏', en: '9:16 Portrait' },
  'ig.classic': { zh: '4:3 经典', en: '4:3 Classic' },
  'ig.tallPortrait': { zh: '3:4 竖版', en: '3:4 Tall portrait' },
  'ig.standardRes': { zh: '1024px · 标准分辨率', en: '1024px · Standard resolution' },
  'ig.hdRes': { zh: '2048px · 高清分辨率', en: '2048px · HD resolution' },
  'ig.single': { zh: '单抽', en: 'Single' },
  'ig.double': { zh: '2连', en: '×2' },
  'ig.quad': { zh: '4连', en: '×4' },
  'ig.singleDesc': { zh: '生成单张图片', en: 'Generate a single image' },
  'ig.doubleDesc': { zh: '同时生成 2 张，多个选择', en: 'Generate 2 images at once' },
  'ig.quadDesc': { zh: '同时生成 4 张，大量出图！', en: 'Generate 4 images at once!' },
  'ig.generateBtn': { zh: '生成 (Enter)', en: 'Generate (Enter)' },
  'ig.placeholder': { zh: '描述你想生成的图片 / 粘贴图片后描述修改内容…', en: 'Describe the image to generate / paste image to edit…' },
  'ig.hint': { zh: 'Enter 生成 · Esc 退出 · 粘贴/拖拽图片可编辑 · 支持中英文', en: 'Enter to generate · Esc to exit · Paste/drag image to edit' },

  // ══════════════════════════════════════
  //  Debug Panel
  // ══════════════════════════════════════
  // ── Tool-schema latch ("apply on next conversation") ──
  'chat.scrollToBottom': { zh: '滚动到最新', en: 'Scroll to latest' },
  'toolset.pending': { zh: '工具改动将在新会话生效（保持缓存命中）', en: 'Tool changes will apply in a new conversation (keeps cache hits)' },
  'toolset.pendingDiff': { zh: '以下工具改动将在新会话生效（保持缓存命中）：', en: 'These tool changes will apply in a new conversation (keeps cache hits):' },
  'toolset.moreChips': { zh: '…还有 %n 项', en: '…and %n more' },
  'toolset.applyNow': { zh: '立即应用', en: 'Apply now' },
  'toolset.restore': { zh: '恢复工具', en: 'Restore tools' },
  'toolset.applyNowDesc': { zh: '立即应用工具改动（会重建一次提示缓存）', en: 'Apply tool changes now (rebuilds the prompt cache once)' },
  'toolset.dismissDesc': { zh: '撤销改动，把工具开关恢复到不破坏缓存的状态', en: 'Revert the change — restore toggles to the cache-safe tool set' },
  'toolset.applied': { zh: '工具改动已应用，下一轮重建缓存', en: 'Tool changes applied — cache rebuilds next round' },
  'toolset.applyFailed': { zh: '应用失败', en: 'Apply failed' },
  'debug.approxTitle': { zh: '重建的近似态（非某一具体轮次）', en: 'Reconstructed approximation (not a specific turn)' },
  'debug.approxMemDate': { zh: '记忆 <relevant_memories> 与日期按"新一轮首轮"重建，非历史某轮的真实取值。', en: 'Memory <relevant_memories> and date are reconstructed as a hypothetical first-round, not a specific historical turn.' },
  'debug.approxTransport': { zh: '传输层变换（图片解析/降采样、provider 报文重组）未在此展开。', en: 'Transport-layer transforms (image resolve/downscale, provider body reshape) are not expanded here.' },
  'debug.copyAll': { zh: '复制全部', en: 'Copy all' },
  'debug.preview': { zh: '预览', en: 'Preview' },
  'debug.previewCompare': { zh: '对比预览', en: 'Compare preview' },
  'debug.clean': { zh: '清理', en: 'Clean' },
  'debug.cleanApply': { zh: '应用规则清理', en: 'Apply rule cleaning' },
  'debug.aiCompress': { zh: 'AI压缩', en: 'AI Compress' },
  'debug.aiCompressDesc': { zh: '用 AI 智能压缩，去除冗余保留关键信息', en: 'AI smart compression, remove redundancy, keep key info' },
  'debug.keepOriginal': { zh: '保持原文', en: 'Keep original' },
  'debug.brainCharter': { zh: '章程', en: 'charter' },
  'debug.brainBoard': { zh: '任务板', en: 'board' },
  'debug.brainBadgeTitle': { zh: '本条 system 消息注入了项目大脑上下文（模型实际可见）', en: 'This system message carries injected Project Brain context (visible to the model)' },
  'debug.brainSummaryTitle': { zh: '本次任务向模型注入的项目大脑上下文块', en: 'Project Brain context blocks injected into the model this task' },

  // ══════════════════════════════════════
  //  Request Inspector (P2 drawer)
  // ══════════════════════════════════════
  'ri.title': { zh: '请求检视器', en: 'Request Inspector' },
  'ri.requests': { zh: '请求', en: 'requests' },
  'ri.states': { zh: '状态镜像', en: 'State mirrors' },
  'ri.stateNote': { zh: '非 LLM 请求', en: 'not LLM requests' },
  'ri.empty': { zh: '该会话暂无任务记录', en: 'No tasks recorded for this conversation' },
  'ri.loading': { zh: '加载中…', en: 'Loading…' },
  'ri.expired': { zh: '事件日志已过期（>6h）或不存在', en: 'Event log expired (>6h) or missing' },
  'ri.coveragePartial': { zh: '此任务为 endpoint 驱动：Planner/Critic 调用未纳入检视', en: 'Endpoint-driven task: Planner/Critic calls are not captured' },
  'ri.live': { zh: '在飞', en: 'live' },
  'ri.openTip': { zh: '在请求检视器中定位产生此回复的 LLM 请求', en: 'Locate the LLM request(s) that produced this reply in the Request Inspector' },
  'ri.prefixFold': { zh: '与 {base} 相同的前 {k} 条已折叠 — 点击展开', en: 'Prefix of {k} message(s) identical to {base} collapsed — click to expand' },
  'ri.turnPlanning': { zh: '规划', en: 'Planner' },
  'ri.turnWorking': { zh: '执行', en: 'Worker' },
  'ri.turnReviewing': { zh: '评审', en: 'Critic' },
  'ri.coverageAmbiguous': { zh: '旧 endpoint 任务：轮次未标相位（planner/worker/critic 同号不可区分）', en: 'Legacy endpoint task: rounds carry no phase tag (planner/worker/critic rounds share numbers)' },
  'ri.turnSwarmAgent': { zh: '子代理', en: 'sub-agent' },
  'ri.toolAnchorTip': { zh: '调试这次工具调用（第 {round} 轮）：产生它的请求 + 执行后的消息状态', en: 'Debug this tool call (round {round}): the request that produced it, and the message state after it ran' },
  'ri.tabRequest': { zh: '请求', en: 'Request' },
  'ri.tabState': { zh: '结果状态', en: 'Result state' },
  'ri.stateKindTip': { zh: '工具执行后的消息状态（已包含产生这次调用的请求内容）', en: 'Message state after the tools ran (includes the request that produced this call)' },
  'ri.requestKindTip': { zh: '这一轮没有留下执行后状态（如子代理轮次），显示产生这次调用的请求', en: 'This round left no post-tool state (e.g. a sub-agent round) — showing the request that produced the call' },
  'ri.stateRowTip': { zh: '点击跳到对应工具调用旁，就地查看该状态镜像', en: 'Jump to the tool call and view this state mirror inline' },
  'ri.stateEmpty': { zh: '该状态镜像已过期（>6h）或不存在', en: 'State mirror expired (>6h) or missing' },
  'ri.stateClose': { zh: '关闭状态检视', en: 'Close state inspector' },

  // ══════════════════════════════════════
  //  Settings — Tabs
  // ══════════════════════════════════════
  'settings.title': { zh: '设置', en: 'Settings' },
  'settings.close': { zh: '关闭', en: 'Close' },
  'settings.tabGeneral': { zh: '通用', en: 'General' },
  'settings.tabProviders': { zh: '服务商', en: 'Providers' },
  'settings.tabDisplay': { zh: '显示', en: 'Display' },
  'settings.tabSearch': { zh: '搜索', en: 'Search' },
  'settings.tabNetwork': { zh: '网络', en: 'Network' },
  'settings.tabDevices': { zh: '设备', en: 'Devices' },
  'devices.agentsTitle': { zh: '在线代理', en: 'Agents' },
  'devices.remoteGroup': { zh: '远程设备', en: 'Remote devices' },
  'devices.agentsDesc': { zh: '运行了 desktop agent 的机器会出现在这里;项目面板可把远程共享根挂为工作树。', en: 'Machines running the desktop agent appear here; the project panel can mount a remote share root as a worktree.' },
  'devices.tokensTitle': { zh: 'Bridge 令牌', en: 'Bridge tokens' },
  'devices.tokensDesc': { zh: 'agent 用令牌连接本服务器(请求头 X-Bridge-Secret)。令牌只颁发给你自己,命令只投递给你的设备。', en: 'Agents connect with a token (X-Bridge-Secret header). Tokens are minted for you only; commands are delivered to your devices only.' },
  'devices.mintNamePh': { zh: '设备备注(如 my-mac)', en: 'Device label (e.g. my-mac)' },
  'devices.mint': { zh: '颁发新令牌', en: 'Mint token' },
  'devices.mintedHint': { zh: '令牌只显示这一次,请立即复制保存:', en: 'The token is shown only once — copy it now:' },
  'devices.copy': { zh: '复制', en: 'Copy' },
  'devices.copied': { zh: '已复制', en: 'Copied' },
  'devices.empty': { zh: '暂无设备连接。在本机运行 desktop agent 后会出现在这里。', en: 'No devices yet. Run the desktop agent on a machine and it will appear here.' },
  'devices.noTokens': { zh: '还没有 bridge 令牌。', en: 'No bridge tokens yet.' },
  'devices.loadFailed': { zh: '没能读取设备列表 —— 服务器未应答。这不代表你没有设备。', en: 'Could not load your devices — the server did not answer. This does NOT mean you have none.' },
  'devices.colDevice': { zh: '设备', en: 'Device' },
  'devices.colPlatform': { zh: '平台', en: 'Platform' },
  'devices.colRoots': { zh: '共享根', en: 'Share roots' },
  'devices.colStatus': { zh: '状态', en: 'Status' },
  'devices.online': { zh: '在线', en: 'online' },
  'devices.offline': { zh: '离线', en: 'offline' },
  'devices.revoke': { zh: '吊销', en: 'Revoke' },
  'devices.revoked': { zh: '已吊销', en: 'Revoked' },
  'devices.revokeFailed': { zh: '吊销失败', en: 'Revoke failed' },
  'devices.mintFailed': { zh: '颁发失败', en: 'Mint failed' },
  'settings.tabFeishu': { zh: '飞书', en: 'Feishu' },
  'settings.tabOAuth': { zh: '订阅登录', en: 'OAuth Login' },
  'settings.tabMCP': { zh: 'MCP', en: 'MCP' },
  'settings.tabSkills': { zh: 'Skills', en: 'Skills' },
  'settings.tabAdvanced': { zh: '高级', en: 'Advanced' },
  'skills.title': { zh: 'Skills', en: 'Skills' },
  'skills.tabCatalog': { zh: '市场', en: 'Catalog' },
  'skills.tabInstalled': { zh: '已安装', en: 'Installed' },
  'skills.newMemory': { zh: '新建记忆', en: 'New memory' },
  'skills.newMemoryTitle': { zh: '手动创建一条记忆', en: 'Manually create a memory' },
  'skills.searchPh': { zh: '搜索 Skills…', en: 'Search skills…' },
  'skills.intro': { zh: 'Skills 是 Claude / OpenClaw / AgentSkills 标准的可重用知识包（SKILL.md + references/scripts/）。可拖入 .zip 或一键安装下方推荐。', en: 'Skills are reusable knowledge packs (SKILL.md + references/scripts/) following the Claude / OpenClaw / AgentSkills standard. Drag-and-drop a .zip or one-click install from below.' },
  'skills.dropZone': { zh: '拖入 .zip 安装本地技能包，或点击选择文件', en: 'Drop a .zip here, or click to choose a file' },
  'skills.installBtn': { zh: '安装', en: 'Install' },
  'skills.installedBtn': { zh: '已安装', en: 'Installed' },
  'skills.uninstallBtn': { zh: '卸载', en: 'Uninstall' },
  'skills.viewFiles': { zh: '查看文件', en: 'View files' },
  'skills.openHomepage': { zh: '主页', en: 'Homepage' },
  // JS-rendered (skills.js): catalog/installed cards, pagination, actions, toasts
  'skills.loadFailed': { zh: '加载失败: {err}', en: 'Load failed: {err}' },
  'skills.scopeAll': { zh: '全部', en: 'All' },
  'skills.countInstalled': { zh: '已安装 {n} 个', en: '{n} installed' },
  'skills.countCatalog': { zh: '市场 {n} 个', en: '{n} in catalog' },
  'skills.pageInfo': { zh: '显示 {from}–{to} / {total}', en: 'Showing {from}–{to} of {total}' },
  'skills.noMatch': { zh: '没有匹配的 Skill。', en: 'No matching skills.' },
  'skills.official': { zh: '官方', en: 'Official' },
  'skills.by': { zh: '作者：{author}', en: 'by {author}' },
  'skills.reqBins': { zh: '需要 {bins}', en: 'Requires {bins}' },
  'skills.reqEnv': { zh: '需要环境变量 {env}', en: 'Requires env vars {env}' },
  'skills.repo': { zh: '仓库', en: 'Repo' },
  'skills.installedTag': { zh: '✓ 已安装', en: '✓ Installed' },
  'skills.emptyInstalled': { zh: '还没有安装任何技能包。可在「市场」标签页一键安装，或拖入 .zip 文件。', en: 'No skill packs installed yet. Install one from the Catalog tab, or drop a .zip file here.' },
  'skills.statusOn': { zh: '启用', en: 'ON' },
  'skills.statusOff': { zh: '停用', en: 'OFF' },
  'skills.scopeIdLine': { zh: 'scope: {scope} · id: {id}', en: 'scope: {scope} · id: {id}' },
  'skills.enable': { zh: '启用', en: 'Enable' },
  'skills.disable': { zh: '禁用', en: 'Disable' },
  // Actions / toasts
  'skills.installing': { zh: '安装中…', en: 'Installing…' },
  'skills.downloadingInstalling': { zh: '正在下载并安装 {id} …', en: 'Downloading and installing {id}…' },
  'skills.installFailed': { zh: '安装失败: {err}', en: 'Install failed: {err}' },
  'skills.installedToast': { zh: '已安装 "{name}"', en: 'Installed "{name}"' },
  'skills.installHintSuffix': { zh: ' · 发现安装脚本 {files}（出于安全未自动执行）', en: ' · found install scripts {files} (not auto-run for safety)' },
  'skills.installHintSuffixUpload': { zh: ' · 安装脚本: {files}', en: ' · install scripts: {files}' },
  'skills.installError': { zh: '安装异常: {err}', en: 'Install error: {err}' },
  'skills.uninstallConfirm': { zh: '确定要卸载技能包 "{id}" 吗？整个目录会被删除。', en: 'Uninstall skill pack "{id}"? The entire directory will be deleted.' },
  'skills.uninstallFailed': { zh: '卸载失败: {err}', en: 'Uninstall failed: {err}' },
  'skills.uninstalledToast': { zh: '已卸载 {id}', en: 'Uninstalled {id}' },
  'skills.uninstallError': { zh: '卸载异常: {err}', en: 'Uninstall error: {err}' },
  'skills.toggleFailed': { zh: '切换失败: {err}', en: 'Toggle failed: {err}' },
  'skills.noResponse': { zh: '无响应', en: 'no response' },
  // File browser modal
  'skills.filesLoading': { zh: '加载中…', en: 'Loading…' },
  'skills.filesLoadFailed': { zh: '加载失败', en: 'Load failed' },
  'skills.filesCount': { zh: '{n} 个文件 · {root}', en: '{n} files · {root}' },
  'skills.filesError': { zh: '异常: {err}', en: 'Error: {err}' },
  // Drag-and-drop
  'skills.notZip': { zh: '拖入的不是 .zip 技能包', en: 'The dropped file is not a .zip skill pack' },
  'skills.installingFile': { zh: '正在安装 {name} …', en: 'Installing {name}…' },

  // ══════════════════════════════════════
  //  Memory modal (memory.js)
  // ══════════════════════════════════════
  'memory.disable': { zh: '停用 Memory', en: 'Disable Memory' },
  'memory.enable': { zh: '启用 Memory', en: 'Enable Memory' },
  'memory.loading': { zh: '加载中...', en: 'Loading…' },
  'memory.loadFailed': { zh: '加载失败', en: 'Load failed' },
  'memory.retry': { zh: '重试', en: 'Retry' },
  'memory.noMatch': { zh: '没有匹配「{q}」的记忆', en: 'No memories matching “{q}”' },
  'memory.matchCount': { zh: '共 {n} 条记忆', en: '{n} memories total' },
  'memory.emptyTitle': { zh: '还没有积累任何记忆', en: 'No memories accumulated yet' },
  'memory.emptyHint': { zh: 'AI 在对话中发现有用模式时会自动保存记忆<br>你也可以点击下方「+ 新建」手动添加', en: 'The AI saves a memory automatically when it spots a useful pattern.<br>You can also click “+ New” below to add one manually.' },
  'memory.renderFailed': { zh: '渲染失败: {name}', en: 'Render failed: {name}' },
  'memory.scopeGlobal': { zh: '全局', en: 'Global' },
  'memory.scopeProject': { zh: '项目', en: 'Project' },
  'memory.toggleOnTip': { zh: '已启用 — 点击禁用', en: 'Enabled — click to disable' },
  'memory.toggleOffTip': { zh: '已禁用 — 点击启用', en: 'Disabled — click to enable' },
  'memory.deleteTip': { zh: '删除此记忆', en: 'Delete this memory' },
  'memory.statTotal': { zh: '总计', en: 'Total' },
  'memory.statEnabled': { zh: '启用', en: 'Enabled' },
  'memory.statProject': { zh: '项目', en: 'Project' },
  'memory.statGlobal': { zh: '全局', en: 'Global' },
  'memory.deleteConfirm': { zh: '确定要删除这条 Memory 吗？', en: 'Delete this memory?' },
  'memory.nameBodyRequired': { zh: '名称和内容为必填项', en: 'Name and content are required' },
  'memory.createFailed': { zh: '创建失败', en: 'Create failed' },
  'memory.errorPrefix': { zh: '错误: {err}', en: 'Error: {err}' },
  'memory.notZip': { zh: '拖入的文件不是 .zip 技能包', en: 'The dropped file is not a .zip skill pack' },
  'memory.installingFile': { zh: '正在安装 {name} …', en: 'Installing {name}…' },
  'memory.installFailed': { zh: '安装失败: {err}', en: 'Install failed: {err}' },
  'memory.noResponse': { zh: '无响应', en: 'no response' },
  'memory.installedPackage': { zh: '已安装技能包 "{name}" (scope={scope})', en: 'Installed skill pack "{name}" (scope={scope})' },
  'memory.replacedOld': { zh: ' · 已覆盖旧版', en: ' · replaced the old version' },
  'memory.installHintSuffix': { zh: ' · 发现安装脚本 {files}（出于安全未自动执行）', en: ' · found install scripts {files} (not auto-run for safety)' },
  'memory.installError': { zh: '安装异常: {err}', en: 'Install error: {err}' },
  'settings.cancel': { zh: '取消', en: 'Cancel' },
  'settings.save': { zh: '保存', en: 'Save' },

  // ══════════════════════════════════════
  //  Settings — General Tab
  // ══════════════════════════════════════
  'settings.language': { zh: '界面语言', en: 'UI Language' },
  'settings.languageDesc': { zh: '切换界面显示语言（中文/英文）', en: 'Switch the UI display language (Chinese/English)' },
  'settings.langZh': { zh: '中文', en: '中文 (Chinese)' },
  'settings.langEn': { zh: 'English', en: 'English' },
  'settings.theme': { zh: '主题', en: 'Theme' },
  'settings.themeDark': { zh: '暗色', en: 'Dark' },
  'settings.themeLight': { zh: '亮色', en: 'Light' },
  'settings.themeTofu': { zh: '豆腐', en: 'Tofu' },
  'settings.sectionNeedsRestart': { zh: '此功能需重启服务后生效。', en: 'This feature becomes available after restarting the server.' },
  'settings.modelParams': { zh: '模型参数', en: 'Model Parameters' },
  'settings.temperature': { zh: '温度 (Temperature)', en: 'Temperature' },
  'settings.maxTokens': { zh: '最大 Token 数', en: 'Max Tokens' },
  'settings.imageMaxWidth': { zh: '图片最大宽度', en: 'Image Max Width' },
  'settings.imageMaxWidthPh': { zh: '0=跟随服务器策略', en: '0 = follow server policy' },
  'settings.defaultThinkingDepth': { zh: '默认思维深度 (Thinking Depth)', en: 'Default Thinking Depth' },
  'settings.thinkingOff': { zh: 'Off — 关闭', en: 'Off' },
  'settings.thinkingMedium': { zh: 'Medium — 中等', en: 'Medium' },
  'settings.thinkingHigh': { zh: 'High — 深度', en: 'High' },
  'settings.thinkingXHigh': { zh: 'xHigh — 超深度 (Claude 4.7+)', en: 'xHigh (Claude 4.7+)' },
  'settings.thinkingMax': { zh: 'Max — 最大', en: 'Max' },
  'settings.thinkingUltra': { zh: 'Ultra — 至臻 (GPT-5.6+)', en: 'Ultra (GPT-5.6+)' },
  'settings.defaultThinkingDesc': { zh: '新对话的默认思维深度级别', en: 'Default thinking depth for new conversations' },
  'settings.systemPrompt': { zh: '系统提示词', en: 'System Prompt' },
  'settings.systemPromptPh': { zh: '输入自定义系统提示词...', en: 'Enter custom system prompt...' },
  'settings.systemPromptMode': { zh: '注入方式', en: 'Injection Mode' },
  'settings.systemPromptModeAppend': { zh: '追加到内置提示词之上', en: 'Append on top of built-in prompt' },
  'settings.systemPromptModeReplace': { zh: '完全替换内置提示词', en: 'Fully replace built-in prompt' },
  'settings.systemPromptModeDesc': { zh: '追加：你的内容叠加在内置提示词之上；替换：完全使用你的内容作为基础提示词（CLAUDE.md / 记忆 / 多智能体 / 日期仍会注入）', en: 'Append: your text is added on top of the built-in prompt. Replace: your text becomes the sole base prompt (CLAUDE.md / memory / swarm / date are still injected).' },
  'settings.systemPromptEdit': { zh: '编辑系统提示词…', en: 'Edit system prompt…' },
  'settings.systemPromptEmpty': { zh: '（未设置自定义系统提示词）', en: '(no custom system prompt set)' },
  'settings.systemPromptSet': { zh: '已设置自定义提示词', en: 'Custom prompt set' },
  'settings.systemPromptEditorHint': { zh: '在此编辑自定义系统提示词。', en: 'Edit your custom system prompt here.' },
  'settings.systemPromptEditorHintAppend': { zh: '追加模式：在此填写要叠加在内置提示词之上的附加指令。（「载入内置默认」仅在替换模式可用，以免重复整段内置提示词）', en: 'Append mode: enter additional instructions to layer on top of the built-in prompt. ("Load built-in default" is only available in Replace mode to avoid duplicating the entire built-in prompt.)' },
  'settings.systemPromptEditorHintReplace': { zh: '替换模式：此内容将完全替换内置基础提示词。点击「载入内置默认」可填入当前内置提示词作为微调起点。', en: 'Replace mode: this content fully replaces the built-in base prompt. Click "Load built-in default" to pre-fill the current built-in prompt as a fine-tuning starting point.' },
  'settings.systemPromptLoadDefault': { zh: '载入内置默认', en: 'Load built-in default' },
  'settings.systemPromptLoadDefaultDisabled': { zh: '仅在替换模式可用 —— 追加模式下你的内容会叠加在内置提示词之上。', en: 'Only available in Replace mode — in Append mode your text is added on top of the built-in prompt.' },
  'settings.systemPromptApply': { zh: '应用', en: 'Apply' },
  'settings.systemPromptLoading': { zh: '正在载入内置提示词…', en: 'Loading built-in prompt…' },
  'settings.systemPromptLoaded': { zh: '内置提示词已载入', en: 'Built-in prompt loaded' },
  'settings.systemPromptLoadFailed': { zh: '载入内置提示词失败', en: 'Failed to load built-in prompt' },
  'settings.systemPromptOverwriteConfirm': { zh: '用内置默认提示词替换当前编辑内容？', en: 'Replace the current editor content with the built-in default?' },
  'settings.systemPromptBlocksTitle': { zh: '内置提示词分块', en: 'Built-in prompt blocks' },
  'settings.systemPromptBlocksDesc': { zh: '逐块开关：关闭某块即可把它从内置系统提示词中移除。动态块（环境 / 日期）内容在运行时生成，仅可开关。', en: 'Toggle each block: switch one off to drop it from the built-in system prompt. Dynamic blocks (environment / date) are generated at runtime and can only be toggled.' },
  'settings.systemPromptBlocksHint': { zh: '逐块开关内置系统提示词，并可追加自定义指令。CLAUDE.md / 记忆 / 多智能体 / 日期仍会注入。', en: 'Toggle the built-in system-prompt blocks and optionally append custom instructions. CLAUDE.md / memory / swarm / date are still injected.' },
  'settings.systemPromptCustomLabel': { zh: '附加指令（追加在内置提示词之后）', en: 'Additional instructions (appended after the built-in prompt)' },
  'settings.systemPromptBlocksOff': { zh: '块已关闭', en: 'blocks off' },
  'settings.systemPromptDynamic': { zh: '动态', en: 'dynamic' },
  'settings.systemPromptProjectBlock': { zh: '项目', en: 'project' },
  'settings.systemPromptProjectBlockTip': { zh: '此块内容在「代码 / 项目模式」下会改写，与聊天模式不同。', en: 'This block is rewritten in code/project mode and differs from chat mode.' },
  'settings.systemPromptResetBlocks': { zh: '全部启用', en: 'Enable all' },
  'settings.systemPromptPreviewCode': { zh: '显示代码 / 项目块', en: 'show code/project blocks' },
  'settings.systemPromptPreviewProject': { zh: '预览：代码 / 项目模式', en: 'preview: code/project mode' },
  'settings.systemPromptPreviewChat': { zh: '预览：聊天模式', en: 'preview: chat mode' },
  'settings.featureModules': { zh: '功能模块', en: 'Feature Modules' },
  'settings.tradingModule': { zh: '交易 / 基金模块', en: 'Trading / Fund Module' },
  'settings.tradingModuleDesc': { zh: '交易顾问、基金筛选、自动驾驶、资讯爬虫。关闭后立即生效：顶栏入口隐藏、接口停止服务，后台资讯爬虫与自动驾驶也会停止（不再产生模型调用费用）。', en: 'Trading advisor, fund screening, autopilot and news crawler. Takes effect immediately: the topbar entry is hidden, the API stops serving, and the background crawler and autopilot stop too — so they no longer run up model-call costs.' },
  'settings.pptxTranslateModule': { zh: 'PPT 翻译模块', en: 'PPTX Translation Module' },
  'settings.pptxTranslateModuleDesc': { zh: '上传 PPTX 文件进行全文翻译，保留原始格式', en: 'Upload PPTX files for full translation with formatting preserved' },
  'settings.aboutUpdate': { zh: '关于与更新', en: 'About & Updates' },
  'settings.updateTitle': { zh: '软件更新', en: 'Software Update' },
  'settings.updateDesc': { zh: '从官方仓库拉取最新版本。更新完成后需要重启服务。', en: 'Pull the latest release from the official repository. A restart is required once the update completes.' },
  'settings.updateCheck': { zh: '检查更新', en: 'Check for updates' },
  'settings.updateCurrent': { zh: '当前版本 v{version}', en: 'Current version v{version}' },
  'settings.debugMode': { zh: '调试模式', en: 'Debug Mode' },
  'settings.debugModeDesc': { zh: '显示 trace_id、复制会话 ID 按钮等开发调试信息', en: 'Show trace_id, copy conv ID buttons, and other debug info' },
  'settings.optimizerModule': { zh: '每日优化器', en: 'Daily Optimizer' },
  'settings.optimizerModuleDesc': { zh: '每晚 03:30 分析当日日志并自动提出改进建议（如屏蔽垃圾搜索域名）。关闭后不再运行分析，顶栏 OPTIMIZER 徽章隐藏。已应用的改动会保留直到手动撤销。', en: 'Every night at 03:30 local, analyses the day\'s logs and auto-proposes improvements (e.g. blocking spammy search domains). When off, analysis stops and the top-bar OPTIMIZER badge is hidden. Already-applied changes persist until manually reverted.' },
  'settings.keepToolHistory': { zh: '保留工具调用历史', en: 'Keep Tool Call History' },
  'settings.keepToolHistoryDesc': { zh: '多轮对话时保留完整的工具调用记录（搜索内容、网页抓取结果等），模型能看到之前搜过什么，避免重复调用。关闭可节省 token 但模型会丢失工具上下文', en: 'Preserve full tool call records (search results, fetched pages, etc.) across conversation turns. Model can see what was searched before, avoiding redundant calls. Disable to save tokens but model loses tool context' },
  'settings.autoGenerateTitle': { zh: '自动生成对话标题', en: 'Auto-Generate Conversation Titles' },
  'settings.autoGenerateTitleDesc': { zh: '首轮回复结束后用 AI 自动为对话生成标题。关闭后需要手动重命名对话（默认关闭）。', en: 'After the first reply, let AI generate a title for the conversation. When off, you rename conversations manually (off by default).' },
  'settings.inputSendMode': { zh: '输入框发送方式', en: 'Input Send Behavior' },
  'settings.inputSendModeDesc': { zh: '选择触发"发送消息"的按键。Shift+Enter 始终插入换行。', en: 'Choose which key sends the message. Shift+Enter always inserts a newline.' },
  'settings.inputSendModeEnter': { zh: 'Enter 发送 · Ctrl+Enter 换行', en: 'Enter send · Ctrl+Enter newline' },
  'settings.inputSendModeCtrlEnter': { zh: 'Ctrl+Enter 发送 · Enter 换行', en: 'Ctrl+Enter send · Enter newline' },
  'input.hintEnter': { zh: 'Enter 发送 · Ctrl+Enter / Shift+Enter 换行 · {clip} 或拖拽上传', en: 'Enter send · Ctrl+Enter / Shift+Enter newline · {clip} or drop files' },
  'input.hintCtrlEnter': { zh: 'Ctrl+Enter 发送 · Enter / Shift+Enter 换行 · {clip} 或拖拽上传', en: 'Ctrl+Enter send · Enter / Shift+Enter newline · {clip} or drop files' },

  // ══════════════════════════════════════
  //  Settings — Providers Tab
  // ══════════════════════════════════════
  'settings.providersTitle': { zh: 'API 服务商 & 模型', en: 'API Providers & Models' },
  'settings.autoSetup': { zh: '自动配置', en: 'Auto Setup' },
  'settings.fromTemplate': { zh: '从模板添加', en: 'From Template' },
  'settings.syncTemplate': { zh: '同步模板', en: 'Sync Template' },
  'settings.localProvider': { zh: '本地部署模型', en: 'Local Deployment' },
  'settings.localPresetTitle': { zh: '选择本地部署类型', en: 'Choose a local deployment type' },
  'settings.localPresetDesc': { zh: '预设只预填默认端口与占位地址；探测、熔断、健康检查四类完全相同。', en: 'Presets only prefill the default port and placeholder URL — probing, failover and health checks are identical for all.' },
  'settings.localPresetVllmDesc': { zh: '默认端口 8000，每个实例服务一个模型', en: 'Default port 8000, one model per instance' },
  'settings.localPresetSglangDesc': { zh: '默认端口 30000，每个实例服务一个模型', en: 'Default port 30000, one model per instance' },
  'settings.localPresetOllamaDesc': { zh: '默认端口 11434，单实例可服务多个模型', en: 'Default port 11434, one instance can serve many models' },
  'settings.localPresetCustomName': { zh: '自定义', en: 'Custom' },
  'settings.localPresetCustomDesc': { zh: '其它 OpenAI 兼容服务，完全手动配置', en: 'Any other OpenAI-compatible server, fully manual' },
  'settings.localEngineProviderName': { zh: '{name} 本地部署', en: '{name} (local)' },
  'settings.epServedModelsTitle': { zh: '该端点服务的模型', en: 'Models served by this endpoint' },
  'settings.customProvider': { zh: '+ 自定义服务商', en: '+ Custom Provider' },
  'settings.providersDesc': { zh: '使用「自动配置」只需填写 API 地址和密钥，系统自动发现模型、检测余额接口和定价。也可从模板添加或手动创建。', en: 'Use "Auto Setup" — just enter API URL and key, the system auto-discovers models, balance endpoint, and pricing. You can also add from templates or create manually.' },
  'settings.loadingConfig': { zh: '正在加载配置…', en: 'Loading config…' },

  // ══════════════════════════════════════
  //  Self-update (topbar button)
  // ══════════════════════════════════════
  'update.title': { zh: '软件更新', en: 'Software Update' },
  'update.subtitle': { zh: '让 Tofu 保持最新版本', en: 'Keep Tofu up to date' },
  'update.btnLabel': { zh: '更新', en: 'Update' },
  'update.btnNew': { zh: '有新版', en: 'New' },
  'update.checkTitle': { zh: '检查更新', en: 'Check for updates' },
  'update.availableTitle': { zh: '有可用更新：v%s', en: 'Update available: v%s' },
  'update.checking': { zh: '正在检查更新…', en: 'Checking for updates…' },
  'update.checkFailed': { zh: '无法检查更新，请稍后再试。', en: 'Could not check for updates. Please try again later.' },
  // Concrete, cause-specific failure copy — never a vague "try again later".
  'update.checkFailTitle': { zh: '无法检查更新', en: "Couldn't check for updates" },
  'update.errBackend': { zh: '无法连接到 Tofu 后端服务，服务器可能已停止或正在重启。', en: 'Could not reach the Tofu backend — the server may be stopped or restarting.' },
  'update.errBackendHttp': { zh: '后端返回了错误（HTTP %s），更新检查无法完成。', en: 'The backend returned an error (HTTP %s), so the update check could not complete.' },
  'update.errTimeout': { zh: '检查更新超时：后端在限定时间内没有响应。', en: 'The update check timed out — the backend did not respond in time.' },
  'update.errNetwork': { zh: '无法连接到 GitHub 来获取最新版本（网络或连接问题）。', en: 'Could not reach GitHub to fetch the latest version (network/connection problem).' },
  'update.errRateLimited': { zh: 'GitHub 暂时限制了更新检查的请求频率，请几分钟后再试。', en: 'GitHub rate-limited the update check. Please try again in a few minutes.' },
  'update.errHttp': { zh: 'GitHub 返回了异常响应（%s），更新检查无法完成。', en: 'GitHub returned an unexpected response (%s).' },
  'update.errParse': { zh: 'GitHub 返回了无法解析的响应，更新检查无法完成。', en: 'GitHub returned an unreadable response.' },
  'update.errNoTags': { zh: '更新源仓库还没有发布任何版本。', en: 'The update repository has no released versions yet.' },
  'update.errUnknown': { zh: '更新检查因未知原因失败。', en: 'The update check failed for an unknown reason.' },
  'update.errReasonLabel': { zh: '原因', en: 'Reason' },
  'update.retry': { zh: '重试', en: 'Retry' },
  'update.current': { zh: '当前版本', en: 'Current' },
  'update.latest': { zh: '最新版本', en: 'Latest' },
  'update.upToDate': { zh: '已是最新版本。', en: 'You are up to date.' },
  'update.ready': { zh: '有新版本可用。点击下方按钮拉取更新（不会影响你的个人设置）。', en: 'A new version is available. Click below to pull it (your personal settings are untouched).' },
  'update.applyBtn': { zh: '拉取更新', en: 'Pull update' },
  'update.applying': { zh: '正在拉取…', en: 'Pulling…' },
  'update.applyFailed': { zh: '更新失败。', en: 'Update failed.' },
  'update.pulled': { zh: '已更新到 %s。需要重启服务器以生效。', en: 'Updated to %s. A restart is required to apply it.' },
  'update.depsInstalled': { zh: '已安装新的依赖包。', en: 'New dependencies installed.' },
  'update.depsFailed': { zh: '已更新到 %s，但安装新依赖失败。请手动运行 pip install -r requirements.txt 后再重启。', en: 'Updated to %s, but installing new dependencies failed. Run "pip install -r requirements.txt" manually, then restart.' },
  'update.restartBtn': { zh: '立即重启', en: 'Restart now' },
  'update.restartNow': { zh: '重启服务器', en: 'Restart server' },
  'update.restartNowHint': { zh: '无需手动停止再启动，直接原地重启。', en: 'Restarts in place — no manual stop-then-start needed.' },
  'update.shutdownNow': { zh: '关闭服务器', en: 'Shut down' },
  'update.shutdownConfirm': { zh: '确定要关闭服务器吗？服务将停止且不会自动重启，正在进行的任务会被标记为手动停止（不会自动恢复）。', en: 'Shut the server down? It will stop and NOT restart on its own; in-progress turns are marked as a manual stop (not auto-recovered).' },
  'update.shuttingDown': { zh: '正在关闭服务器…', en: 'Shutting the server down…' },
  'update.shutdownHint': { zh: '服务已停止，需要手动重新启动。', en: 'The server has stopped — start it again manually when needed.' },
  'update.restartConfirm': { zh: '确定要重启服务器吗？正在进行的任务会被中断。', en: 'Restart the server now? Any in-progress tasks will be interrupted.' },
  'update.restartForceConfirm': { zh: '另有 %s 个会话仍有任务在运行，重启会中断它们。确定要继续吗？', en: '%s other conversation(s) still have running tasks that a restart will interrupt. Continue anyway?' },
  'update.restarting': { zh: '正在重启…', en: 'Restarting…' },
  'update.restartHint': { zh: '重启期间页面会短暂不可用，完成后会自动刷新。', en: 'The page will be briefly unavailable and will auto-refresh once back.' },
  'update.restartWait': { zh: '正在等待服务器恢复…', en: 'Waiting for the server to come back…' },
  'update.restartTimeout': { zh: '服务器重启耗时较长，请手动刷新页面。', en: 'Server is taking a while — please refresh manually.' },
  'update.restartTitle': { zh: '正在重启服务器', en: 'Restarting server' },
  'update.restartSub': { zh: '原地重启，完成后会自动刷新页面。', en: 'Restarting in place — the page will auto-refresh when ready.' },
  'update.phase.shutdown': { zh: '正在优雅关闭旧进程…', en: 'Gracefully shutting down…' },
  'update.phase.reload': { zh: '重新加载核心模块…', en: 'Reloading core modules…' },
  'update.phase.db': { zh: '初始化数据库…', en: 'Initializing database…' },
  'update.phase.imports': { zh: '校验关键依赖…', en: 'Validating critical imports…' },
  'update.phase.services': { zh: '启动后台服务…', en: 'Starting background services…' },
  'update.phase.bind': { zh: '绑定端口，等待服务器响应…', en: 'Binding port, waiting for the server…' },
  'update.phase.online': { zh: '已恢复在线', en: 'Back online' },
  'update.restartElapsed': { zh: '已用时 %ss', en: 'elapsed %ss' },
  'update.codeUnchangedTitle': { zh: '服务器已重启，但代码未改变', en: 'Server restarted, but code is unchanged' },
  'update.codeUnchangedHint': { zh: '新进程加载的源码与重启前完全一致 —— 你的本地改动可能没有生效（是否改了别的目录，或改动被还原？）。', en: 'The new process loaded byte-identical source — your local changes may not have taken effect (edited a different checkout, or the changes were reverted?).' },
  'update.dirty': { zh: '检测到对受版本控制的源码文件的本地改动，已阻止自动更新（不会自动暂存或覆盖）。请先提交或还原以下文件：', en: 'Local changes to tracked source files were detected — the update is blocked (we never auto-stash or overwrite). Commit or revert these first:' },
  'update.noGit': { zh: '当前不是 git 检出目录，将通过下载发布包覆盖更新。', en: 'Not a git checkout — updating by downloading and overlaying the release archive.' },
  'update.tarballNote': { zh: '当前非 git 目录，将下载官方发布包并覆盖更新（你的设置、数据与记忆不受影响；被替换的文件会备份到 .update_backup/）。', en: 'Not a git checkout — the official release archive will be downloaded and overlaid (your settings, data and memories are untouched; replaced files are backed up to .update_backup/).' },
  'update.step.fetch': { zh: '拉取远端', en: 'Fetch from remote' },
  'update.step.fetchDl': { zh: '下载发布包', en: 'Download release' },
  'update.step.pullOverlay': { zh: '覆盖更新文件', en: 'Apply files' },
  'update.step.pull': { zh: '合并更新', en: 'Pull changes' },
  'update.step.deps': { zh: '安装依赖', en: 'Install dependencies' },
  'update.step.depsSkip': { zh: '无需更新依赖', en: 'No dependency changes' },
  'update.applyStarting': { zh: '正在准备更新…', en: 'Preparing update…' },
  'update.applyStartFailed': { zh: '无法启动更新，请稍后再试。', en: 'Could not start the update. Please try again.' },
  'update.applyTimeout': { zh: '更新耗时异常，请检查服务器日志。', en: 'The update is taking unusually long — check the server log.' },
  'update.logLabel': { zh: '完整日志', en: 'Full log' },
  'update.copyLog': { zh: '复制日志', en: 'Copy log' },
  'update.logCopied': { zh: '已复制', en: 'Copied' },
  'update.pendingApprovals': { zh: '待审批的服务器操作（由后台任务发起，需真人确认）', en: 'Pending server actions (initiated by background tasks — human decision required)' },
  'update.approveExecuteRestart': { zh: '批准并重启', en: 'Approve & restart' },
  'update.approveExecuteShutdown': { zh: '批准并关机', en: 'Approve & shut down' },
  'update.deny': { zh: '拒绝', en: 'Deny' },
  'update.approvalDenied': { zh: '已拒绝该请求。', en: 'Request denied.' },
  'update.restartCooldown': { zh: '服务器刚重启过，处于冷却期（剩余 %s 秒），请稍后再试。', en: 'The server was just restarted — cooldown active (%ss left). Try again later.' },
  'settings.loadingFailed': { zh: '加载服务器配置失败。请检查服务器是否正在运行。', en: 'Failed to load server config. Please check if the server is running.' },
  'settings.noProviders': { zh: '还没有配置服务商。点击"+ 自定义服务商"开始添加。', en: 'No providers configured. Click "+ Custom Provider" to start.' },
  'settings.keys': { zh: '个密钥', en: 'keys' },
  'settings.models': { zh: '个模型', en: 'models' },
  'settings.disabled': { zh: '已禁用', en: 'Disabled' },
  'settings.displayName': { zh: '显示名称', en: 'Display Name' },
  'settings.baseUrl': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.apiKeys': { zh: 'API 密钥', en: 'API Keys' },
  'settings.apiKeysHint': { zh: '安全存储；点击 👁 切换可见性', en: 'Securely stored; click 👁 to toggle visibility' },
  // ── Per-key today's stats (settings/key_stats.js) ──
  'settings.keyStatAutoDisablePolicy': { zh: '{day}: 上游报告额度耗尽 (402) 或成功率 < {pct}% 时自动停用；429 限流只计数、永不停用；次日自动重置。', en: '{day}: auto-disabled on quota exhaustion (HTTP 402) or success rate < {pct}%; 429s are counted but never disable; auto-resets the next day.' },
  'settings.keyStatOverrideOff': { zh: '手动关闭', en: 'Manually off' },
  'settings.keyStatOverrideOn': { zh: '手动开启', en: 'Manually on' },
  'settings.keyStatLastResort': { zh: '保留为最后备选', en: 'Kept as last resort' },
  'settings.keyStatLastResortTip': { zh: '本应自动停用，但这是该服务商今天唯一可用的密钥 — 保留为最后备选', en: 'Would be auto-disabled, but this is the only usable key for this provider today — kept as last resort' },
  'settings.keyStatExhausted': { zh: '自动停用 (额度耗尽)', en: 'Auto-disabled (quota exhausted)' },
  'settings.keyStatExhaustedTip': { zh: '上游报告额度耗尽/欠费 (402)，今日停用；充值后点「重置」恢复', en: 'Provider reported quota/credit exhaustion (HTTP 402); disabled for today — top up and press Reset' },
  'settings.keyStatModelExhausted': { zh: '{models} 熔断', en: '{models} stopped' },
  'settings.keyStatModelExhaustedTip': { zh: '这些模型今日被上游报告配额耗尽 — 按模型停用，不影响此 Key 的其他模型：\n{reasons}', en: 'These models reported out of quota today — stopped per-model, other models on this key are unaffected:\n{reasons}' },
  'settings.keyStatOverrideVsExhausted': { zh: '手动开启·但有熔断', en: 'Manual ON, stop active' },
  'settings.keyStatOverrideVsExhaustedTip': { zh: '你手动开启了此 Key，但上游今日报告了配额耗尽。手动设置优先 — 被熔断模型的请求仍会被派发并可能失败；充值后点「重置」回到自动判定。', en: 'You manually enabled this key, but the provider reported a quota/billing stop today. Your manual setting wins — requests to the stopped model(s) will still be dispatched and may fail; after topping up, hit Reset to return to automatic.' },
  'settings.keyStatAutoOff': { zh: '自动停用', en: 'Auto-disabled' },
  'settings.keyStat429Streak': { zh: '连续 429 × {n}', en: 'Consecutive 429 × {n}' },
  'settings.keyStat429StreakTip': { zh: '连续收到 429 限流；只计数不停用，上游恢复后自动回到轮换', en: 'Consecutive 429s; counted only — the key rejoins rotation once the upstream recovers' },
  'settings.keyStatLastError': { zh: '最近错误', en: 'Last error' },
  'settings.keyStatRateTip': { zh: '今日成功率 = 成功 {succ} / 调用 {total}（429 限流不计入）', en: "Today's success rate = success {succ} / calls {total} (429 rate-limits excluded)" },
  'settings.keyStatNoCallsTip': { zh: '今日尚无调用', en: 'No calls today' },
  'settings.keyStatCount': { zh: '调用 {n}', en: 'Calls {n}' },
  'settings.keyStatCountTip': { zh: '今日总调用次数（不含 429 限流）', en: "Today's total calls (excluding 429 rate-limits)" },
  'settings.keyStatFail': { zh: '失败 {n}', en: 'Failed {n}' },
  'settings.keyStatFailTip': { zh: '真正的调用失败次数（网络/5xx/解析错误等，不含 429）', en: 'Genuine call failures (network/5xx/parse errors, etc.; excluding 429)' },
  'settings.keyStat429': { zh: '限流 {n}', en: 'Limited {n}' },
  'settings.keyStat429Tip': { zh: '今日收到的 429 限流次数（不计入成功率，也不会自动停用）', en: '429 rate-limits today (excluded from success rate; never auto-disables)' },
  'settings.keyStatToggleTip': { zh: '今日启用/禁用此密钥（明日自动重置）', en: 'Enable/disable this key for today (auto-resets tomorrow)' },
  'settings.keyStatClearOverrideTip': { zh: '清除手动设置，恢复自动判定', en: 'Clear the manual override and revert to automatic judgment' },
  'settings.keyStatReset': { zh: '重置', en: 'Reset' },
  'settings.addApiKey': { zh: '+ 添加密钥', en: '+ Add Key' },
  'settings.addApiKeyTitle': { zh: '新增一个 API 密钥', en: 'Add a new API key' },
  'settings.deleteApiKeyTitle': { zh: '删除该密钥', en: 'Delete this key' },
  'settings.showHideKeyTitle': { zh: '切换密钥可见性', en: 'Toggle key visibility' },
  'settings.noApiKeys': { zh: '暂无 API 密钥。点击右上角 + 添加。', en: 'No API keys yet. Click + above to add one.' },
  'settings.noApiKeysLocal': { zh: '暂无 API 密钥（多数本地部署不需要鉴权）。', en: 'No API keys (most local deployments don\'t need auth).' },
  'settings.balanceUrl': { zh: '余额查询地址', en: 'Balance Query URL' },
  'settings.balanceUrlHint': { zh: '可选 — OpenAI 兼容的账单接口', en: 'Optional — OpenAI-compatible billing endpoint' },
  'settings.checkBalance': { zh: '查询 ▸', en: 'Check ▸' },
  'settings.modelsPath': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.modelsPathHint': { zh: '可选 — 默认在 Base URL 后追加 /models', en: 'Optional — defaults to Base URL + /models' },
  'settings.customHeaders': { zh: '自定义请求头', en: 'Custom Headers' },
  'settings.customHeadersHint': { zh: '可选 — 每行一对，附加到本服务商的所有请求', en: 'Optional — one pair per row, appended to every request to this provider' },
  'settings.addHeader': { zh: '+ 添加请求头', en: '+ Add Header' },
  'settings.addHeaderTitle': { zh: '新增一行请求头', en: 'Add a new header row' },
  'settings.deleteHeaderTitle': { zh: '删除该请求头', en: 'Delete this header' },
  'settings.headerNamePlaceholder': { zh: 'Header 名称', en: 'Header-Name' },
  'settings.headerValuePlaceholder': { zh: 'Header 值', en: 'value' },
  'settings.noHeaders': { zh: '暂无自定义请求头。点击右上角 + 添加。', en: 'No custom headers yet. Click + above to add one.' },
  'settings.thinkingFormat': { zh: '思维参数格式', en: 'Thinking Parameter Format' },
  'settings.thinkingFormatAuto': { zh: '自动检测（按模型名称）', en: 'Auto-detect (by model name)' },
  'settings.thinkingFormatEnable': { zh: 'enable_thinking（LongCat/Qwen 风格）', en: 'enable_thinking (LongCat/Qwen style)' },
  'settings.thinkingFormatType': { zh: 'thinking.type（Doubao/Claude 风格）', en: 'thinking.type (Doubao/Claude style)' },
  'settings.thinkingFormatReasoningEffort': { zh: 'reasoning_effort（Gemini 3.x 风格）', en: 'reasoning_effort (Gemini 3.x style)' },
  'settings.thinkingFormatNone': { zh: '不发送思维参数', en: 'Do not send thinking parameter' },
  'settings.enabled': { zh: '启用', en: 'Enabled' },
  'settings.deleteProvider': { zh: '删除服务商', en: 'Delete Provider' },
  'settings.oauthManagedBadge': { zh: '订阅登录', en: 'Subscription' },
  'settings.oauthManagedTitle': { zh: '由订阅登录（OAuth）自动创建，无需 API Key', en: 'Auto-created from a subscription (OAuth) login — no API key needed' },
  'settings.oauthManagedNoteTitle': { zh: '这是 {name} 订阅登录自动生成的服务商', en: 'This provider was auto-created from your {name} subscription login' },
  'settings.oauthManagedNoteDesc': { zh: '它使用你登录的 {name} 订阅额度，令牌每次请求实时获取，无需填写 API Key。要移除它，请点下方「退出登录」——只删这张卡片不会清除登录凭证，下次刷新令牌时它会重新出现。', en: 'It uses your logged-in {name} subscription — the token is fetched live per request, so no API key is needed. To remove it, use “Log out” below: deleting only this card leaves the login token on disk, so it reappears on the next token refresh.' },
  'settings.oauthLogoutRemove': { zh: '退出登录', en: 'Log out' },
  'settings.modelList': { zh: '模型列表', en: 'Model List' },
  'settings.autoDiscover': { zh: '自动发现', en: 'Auto Discover' },
  'settings.addModel': { zh: '+ 添加模型', en: '+ Add Model' },
  'settings.noModels': { zh: '还没有配置模型。点击"自动发现"自动检测可用模型，或点击"+ 添加模型"手动添加。', en: 'No models configured. Click "Auto Discover" to detect available models, or click "+ Add Model" to add manually.' },
  'settings.autoDiscoverHint': { zh: '从 /v1/models 接口自动发现模型', en: 'Auto-discover models from /v1/models endpoint' },
  'settings.aliases': { zh: '别名：', en: 'Aliases:' },
  'settings.requestIds': { zh: '请求名：', en: 'Request IDs:' },
  'settings.addAlias': { zh: '+ 别名', en: '+ Alias' },
  'settings.apply': { zh: '应用', en: 'Apply' },
  // ── Access Matrix (per-key × per-model capability grid) ──
  'settings.matrixViewMatrix': { zh: '访问矩阵', en: 'Access Matrix' },
  'settings.matrixViewCards': { zh: '卡片视图', en: 'Card View' },
  'settings.matrixToggleHint': { zh: '按「密钥 × 模型」逐格管理访问、别名、限速与能力', en: 'Manage access, aliases, RPM and capabilities per (key × model) cell' },
  'settings.matrixModelCol': { zh: '模型 ＼ 密钥', en: 'Model ＼ Key' },
  'settings.matrixNoKeys': { zh: '该服务商还没有 API 密钥。', en: 'This provider has no API keys yet.' },
  'settings.matrixBlankKey': { zh: '（无密钥）', en: '(no key)' },
  'settings.matrixRenameKey': { zh: '为此密钥命名（如 高配额 / 测试）', en: 'Name this key (e.g. high-quota / test)' },
  'settings.matrixLegendOn': { zh: '已开放', en: 'Granted' },
  'settings.matrixLegendOff': { zh: '已禁用', en: 'Disabled' },
  'settings.matrixLegendOverride': { zh: '有覆盖', en: 'Overridden' },
  'settings.matrixGlobalToggle': { zh: '全局启用 / 禁用该模型（影响所有密钥）', en: 'Globally enable/disable this model (all keys)' },
  'settings.matrixClickEnable': { zh: '点击：对该密钥开放此模型', en: 'Click: grant this key access to this model' },
  'settings.matrixClickDisable': { zh: '点击：对该密钥禁用此模型', en: 'Click: deny this key access to this model' },
  'settings.matrixEditCell': { zh: '为此（密钥 × 模型）设置专属别名 / 限速 / 能力', en: 'Set per-cell aliases / RPM / capabilities' },
  'settings.matrixCellEnabled': { zh: '该密钥可访问此模型', en: 'This key may access this model' },
  'settings.matrixOverrideRpm': { zh: '覆盖限速 (RPM)', en: 'Override RPM' },
  'settings.matrixOverrideAlias': { zh: '覆盖别名', en: 'Override aliases' },
  'settings.matrixOverrideCaps': { zh: '覆盖能力', en: 'Override capabilities' },
  'settings.matrixInheritHint': { zh: '默认继承：', en: 'Inherits:' },
  'settings.matrixNoAlias': { zh: '无别名', en: 'no aliases' },
  // ── Access Matrix: probe & recommend ──
  'settings.matrixProbe': { zh: '探测推荐', en: 'Probe & Recommend' },
  'settings.matrixProbeHint': { zh: '逐格发送最小请求，检测每个密钥可访问哪些模型/别名：聊天模型发 1-token 补全，图像模型真实生成一张小图（按 1 次生成计费，仅测 1 次），语音模型发一段空白音频，嵌入模型发一个单词。在后台运行并持久化——关闭设置也不会中断，重开后自动恢复', en: 'Send a tiny request per cell to detect which models/aliases each key can reach: chat models get a 1-token completion, image models generate one small real image (billed as one generation, single attempt), speech models get a blank audio clip, embedding models one word. Runs in the background and is persisted — closing Settings won\'t interrupt it; it resumes on reopen' },
  'settings.matrixRetest': { zh: '重新探测', en: 'Retest' },
  'settings.matrixProbing': { zh: '正在后台探测…', en: 'Probing in background…' },
  'settings.matrixProbeFailed': { zh: '探测失败', en: 'Probe failed' },
  'settings.matrixNothingToProbe': { zh: '没有可探测的密钥或模型', en: 'No keys or models to probe' },
  'settings.matrixOkCount': { zh: '可用', en: 'reachable' },
  'settings.matrixFlaggedCount': { zh: '建议禁用', en: 'flagged' },
  'settings.matrixApplyRec': { zh: '应用推荐', en: 'Apply Recommendations' },
  'settings.matrixApplyHint': { zh: '禁用所有「建议禁用」的格子（每个别名独立处理：失效别名单独禁用，根模型与其它别名保持可用）', en: 'Disable every flagged cell (each alias is independent: a dead alias is disabled on its own; the root and other aliases stay reachable)' },
  'settings.matrixAliasTag': { zh: '别名', en: 'alias' },
  'settings.matrixEditorSub': { zh: '限速与能力作用于整行模型；别名的开关请直接点击别名行的格子。', en: 'RPM & capabilities apply to the whole model entry; toggle an alias on/off by clicking its own row\'s cells.' },
  'settings.matrixAliasOne': { zh: '别名', en: 'alias' },
  'settings.matrixAliasMany': { zh: '别名', en: 'aliases' },
  'settings.matrixAliasCountHint': { zh: '该模型的别名各自路由到不同的上游模型，可逐个开关', en: 'Each alias routes to a different upstream model and can be toggled independently' },
  'settings.matrixAttempts': { zh: '探测次数', en: 'Attempts' },
  'settings.matrixAttemptsHint': { zh: '每个格子探测多次以过滤偶发的假 429；任一次成功即视为可用', en: 'Probe each cell several times to filter out false 429s; a single success counts as reachable' },
  'settings.matrixApplied': { zh: '已应用：禁用了 {n} 个格子', en: 'Applied: disabled {n} cell(s)' },
  'settings.matrixNothingApplied': { zh: '没有需要禁用的格子', en: 'No cells needed disabling' },
  'settings.matrixClearProbe': { zh: '清除探测结果', en: 'Clear results' },
  'settings.matrixProbeColHint': { zh: '探测此列——检测该密钥下的全部模型（结果合并进现有探测，不影响其它列）', en: 'Probe this column — every model under this key (results merge into the existing snapshot; other columns untouched)' },
  'settings.matrixProbeRowHint': { zh: '探测此行——检测该模型在所有密钥下的可达性（结果合并进现有探测，不影响其它行）', en: 'Probe this row — this model across all keys (results merge into the existing snapshot; other rows untouched)' },
  'settings.matrixProbeCellHint': { zh: '重新探测此格', en: 'Re-probe this cell' },
  'settings.probeOk': { zh: '可用', en: 'Reachable' },
  'settings.probeRateLimited': { zh: '限流 (429)', en: 'Rate-limited (429)' },
  'settings.probeUnauthorized': { zh: '无权限', en: 'Unauthorized' },
  'settings.probeNotFound': { zh: '模型不存在', en: 'Model not found' },
  'settings.probeUnavailable': { zh: '不可用 / 超时', en: 'Unavailable / timeout' },
  'settings.probeSkipped': { zh: '不适用（非聊天模型）', en: 'N/A (non-chat model)' },
  'settings.matrixSkippedCount': { zh: '不适用', en: 'N/A' },
  'settings.probeError': { zh: '错误', en: 'Error' },
  'settings.edit': { zh: '编辑', en: 'Edit' },
  'settings.delete': { zh: '删除', en: 'Delete' },
  'settings.free': { zh: '免费', en: 'Free' },
  'settings.noPricing': { zh: '暂无价格数据', en: 'No pricing data' },
  'settings.input': { zh: '输入', en: 'Input' },
  'settings.output': { zh: '输出', en: 'Output' },
  'settings.perMillionTokens': { zh: '每百万 Token', en: 'per million tokens' },
  'settings.balance': { zh: '余额', en: 'Balance' },
  'settings.balanceClickRefresh': { zh: '余额（点击刷新）', en: 'Balance (click to refresh)' },
  'settings.used': { zh: '已用', en: 'Used' },
  'settings.remaining': { zh: '剩余', en: 'Remaining' },
  'settings.quota': { zh: '额度', en: 'Quota' },
  'settings.checking': { zh: '查询中…', en: 'Checking…' },
  'settings.apiKeysHintLocal': { zh: '可选 — 多数本地部署不需要鉴权', en: 'Optional — most local deployments don\'t need auth' },
  'settings.checkBalanceTitle': { zh: '查询余额', en: 'Check balance' },
  'settings.thinkingFormatHint': { zh: '默认自动检测 — 仅当端点使用非标准格式时需配置', en: 'Auto-detected by default — configure only if the endpoint uses a non-standard format' },
  'settings.syncTemplateTitle': { zh: '从内置模板同步新增模型', en: 'Sync new models from the built-in template' },
  'settings.probeAllEndpoints': { zh: '探测全部端点', en: 'Probe All Endpoints' },
  'settings.probeAllEndpointsTitle': { zh: '并行探测所有端点并合并模型列表', en: 'Probe all endpoints in parallel and merge their model lists' },
  'settings.modelEnabledTitle': { zh: '该模型已启用 — 点击禁用', en: 'Model enabled — click to disable' },
  'settings.modelDisabledTitle': { zh: '该模型已禁用 — 点击启用', en: 'Model disabled — click to enable' },
  'settings.editTitle': { zh: '编辑', en: 'Edit' },
  'settings.deleteTitle': { zh: '删除', en: 'Delete' },
  'settings.endpointsSuffix': { zh: '个端点', en: 'endpoints' },
  'settings.moreEndpointsSuffix': { zh: '个端点', en: 'more' },
  // Local endpoint manager (inside provider card)
  'settings.endpointUrlList': { zh: '端点 URL 列表', en: 'Endpoint URL List' },
  'settings.addEndpoint': { zh: '+ 添加端点', en: '+ Add Endpoint' },
  'settings.addEndpointTitle': { zh: '新增一行端点', en: 'Add a new endpoint row' },
  'settings.bulkEdit': { zh: '批量编辑', en: 'Bulk Edit' },
  'settings.bulkEditTitle': { zh: '一次性粘贴或编辑全部 URL', en: 'Paste or edit all URLs at once' },
  'settings.probeAll': { zh: '探测全部', en: 'Probe All' },
  'settings.probeAllTitle': { zh: '并行探测全部端点并合并模型列表', en: 'Probe all endpoints in parallel and merge their model lists' },
  'settings.clearAll': { zh: '清空', en: 'Clear' },
  'settings.clearAllTitle': { zh: '清空全部端点', en: 'Clear all endpoints' },
  'settings.localEndpointsHint': { zh: '每个端点的模型独立路由：请求只会发给它实际服务的模型；多台机服务同名模型时自动负载均衡，单机宕机自动绕开。私网/公网 IP 都会自动加入代理白名单。', en: 'Each endpoint is routed only the models it actually serves; when several boxes serve the same model the scheduler load-balances across them and routes around any that go down. Private and public IPs are added to the proxy bypass list automatically.' },
  'settings.testEndpointTitle': { zh: '立即刷新该端点的实时指标', en: 'Refresh live metrics for this endpoint now' },
  'settings.deleteEndpointTitle': { zh: '删除该端点', en: 'Delete this endpoint' },
  // Inline "thinking" toggle on model cards
  'settings.thinking': { zh: '思考', en: 'thinking' },
  'settings.enableThinkingHint': {
    zh: '点击启用思考能力（开启后聊天时会显示思维深度选择器）',
    en: 'Click to enable thinking capability (chat input will show the thinking-depth picker)'
  },
  'settings.disableThinkingHint': {
    zh: '点击关闭思考能力',
    en: 'Click to disable thinking capability'
  },
  // Live per-endpoint metrics (auto-polled, recorded from real traffic)
  'settings.epHealthy':       { zh: '运行正常', en: 'Healthy' },
  'settings.epRecentFailure': { zh: '近期失败', en: 'Recent failures' },
  'settings.epNoTraffic':     { zh: '尚无流量 · 等待第一次请求', en: 'No traffic yet — waiting for first request' },
  'settings.epLastSeen':      { zh: '最近', en: 'last' },
  'settings.epLatency':       { zh: '延迟', en: 'Latency' },
  'settings.epThroughput':    { zh: '吞吐', en: 'Throughput' },
  'settings.epSuccessRate':   { zh: '成功率', en: 'Success' },
  'settings.epInflight':      { zh: '在途', en: 'inflight' },
  'settings.epRequests':      { zh: '次请求', en: 'requests' },
  'settings.epAllFailed': { zh: '全部端点异常', en: 'All endpoints failing' },
  'settings.epAllOk': { zh: '全部正常', en: 'All OK' },
  'settings.epPartialOk': { zh: '{ok}/{total} 个端点正常', en: '{ok}/{total} endpoints OK' },
  'settings.epNotProbed': { zh: '尚未探测', en: 'Not probed yet' },
  'settings.epProbing': { zh: '探测中…', en: 'Probing…' },
  'settings.epOk': { zh: '正常', en: 'OK' },
  'settings.epError': { zh: '异常', en: 'Error' },
  'settings.epProbeFailed': { zh: '探测失败', en: 'Probe failed' },
  // ── Local endpoints: provider default name, bulk-edit modal, discovery status ──
  'settings.epDefaultProviderName': { zh: '本地部署模型', en: 'Local Deployment' },
  'settings.epClearAllConfirm': { zh: '确定要清空全部 {n} 个端点 URL 吗？此操作不会立即保存到服务器。', en: 'Clear all {n} endpoint URL(s)? This will not save to the server immediately.' },
  'settings.epBulkEditTitle': { zh: '批量编辑端点 URL', en: 'Bulk Edit Endpoint URLs' },
  'settings.epBulkEditDesc': { zh: '每行一个 URL。粘贴 <code>/chat/completions</code> 链接也会自动转换为 base URL。', en: 'One URL per line. Pasting a <code>/chat/completions</code> link is auto-converted to a base URL.' },
  'settings.epBulkCancel': { zh: '取消', en: 'Cancel' },
  'settings.epBulkApply': { zh: '应用', en: 'Apply' },
  'settings.epNoUrlToProbe': { zh: '请先在“端点 URL 列表”中添加至少一个 URL。', en: 'Add at least one URL in the "Endpoint URL List" first.' },
  'settings.epProbingN': { zh: '正在并行探测 {n} 个端点…', en: 'Probing {n} endpoint(s) in parallel…' },
  'settings.epModelsCount': { zh: '{n} 个模型', en: '{n} models' },
  'settings.epProbeDone': { zh: '探测完成：{ok}/{total} 个端点存活，模型并集 {models} 个（详见各端点状态指示）。', en: 'Probe complete: {ok}/{total} endpoint(s) alive, {models} model(s) in union (see per-endpoint status).' },
  'settings.epNetworkError': { zh: '网络错误: {error}', en: 'Network error: {error}' },
  // ── Auto Setup modal (settings/auto_setup.js) ──
  'settings.asTitle': { zh: '自动配置服务商', en: 'Auto-Configure Provider' },
  'settings.asDesc': { zh: '只需填写 API 地址和密钥，系统将自动发现模型、检测余额接口、识别服务商品牌并获取定价信息。', en: 'Just enter the API URL and key — the system auto-discovers models, detects the balance endpoint, identifies the provider brand, and fetches pricing.' },
  'settings.asUrlLabel': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.asUrlHint': { zh: '填写 OpenAI 兼容的 API 地址，通常以 /v1 结尾', en: 'Enter an OpenAI-compatible API URL, usually ending in /v1' },
  'settings.asKeyLabel': { zh: 'API 密钥', en: 'API Key' },
  'settings.asModelsPathLabel': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.asModelsPathHint': { zh: '（可选 — 默认 /models）', en: '(optional — defaults to /models)' },
  'settings.asProbeBtn': { zh: '开始探测', en: 'Start Probe' },
  'settings.asProbing': { zh: '正在探测…', en: 'Probing…' },
  'settings.asProbeNetFail': { zh: '探测失败 (网络/超时)', en: 'Probe failed (network/timeout)' },
  'settings.asTextModels': { zh: '{n} 个文本', en: '{n} text' },
  'settings.asThinkingModels': { zh: '{n} 个推理', en: '{n} thinking' },
  'settings.asVisionModels': { zh: '{n} 个视觉', en: '{n} vision' },
  'settings.asCheapModels': { zh: '{n} 个低价', en: '{n} cheap' },
  'settings.asIgModels': { zh: '{n} 个图片生成', en: '{n} image-gen' },
  'settings.asEmbeddingModels': { zh: '{n} 个嵌入', en: '{n} embedding' },
  'settings.asModelsJoin': { zh: '，', en: ', ' },
  'settings.asModelsCount': { zh: '{n} 个模型', en: '{n} models' },
  'settings.asDiscovered': { zh: '✅ 发现 {n} 个模型（{summary}）{balance}{thinking}', en: '✅ Discovered {n} model(s) ({summary}){balance}{thinking}' },
  'settings.asBalanceDetected': { zh: '，已检测到余额接口', en: ', balance endpoint detected' },
  'settings.asThinkingFormat': { zh: '，建议思维格式: {fmt}', en: ', suggested thinking format: {fmt}' },
  'settings.asNetworkError': { zh: '网络错误: {error}', en: 'Network error: {error}' },
  // ── Balance check (settings/balance.js) ──
  'settings.balNoUrl': { zh: '未配置余额查询地址', en: 'No balance query URL configured' },
  'settings.balNoKey': { zh: '未配置 API 密钥', en: 'No API key configured' },
  'settings.balLoading': { zh: '查询中…', en: 'Querying…' },
  'settings.balUnknownError': { zh: '未知错误', en: 'Unknown error' },
  'settings.balNetworkError': { zh: '网络错误: {error}', en: 'Network error: {error}' },
  'settings.balUsed': { zh: '已用', en: 'Used' },
  'settings.balRemaining': { zh: '剩余', en: 'Remaining' },
  'settings.balLimit': { zh: '额度', en: 'Limit' },
  'settings.balBalance': { zh: '余额', en: 'Balance' },
  'settings.balGranted': { zh: '赠送', en: 'Granted' },
  'settings.balInsufficient': { zh: '余额不足', en: 'Insufficient balance' },
  // ── Model edit form (settings/model_edit.js) ──
  'settings.meDeleteConfirm': { zh: '确定删除模型“{name}”吗？', en: 'Delete model "{name}"?' },
  'settings.meUnnamed': { zh: '未命名', en: 'unnamed' },
  'settings.meModelId': { zh: '模型 ID', en: 'Model ID' },
  'settings.meRpm': { zh: '每分钟请求数 (RPM)', en: 'Requests per minute (RPM)' },
  'settings.meCost': { zh: '路由成本', en: 'Routing cost' },
  'settings.meCostHint': { zh: '（综合 $/1K，用于调度优先级）', en: '(blended $/1K, used for routing priority)' },
  'settings.meCapabilities': { zh: '模型能力', en: 'Model capabilities' },
  'settings.meAliases': { zh: '别名', en: 'Aliases' },
  'settings.meAliasesHint': { zh: '（逗号分隔的替代模型 ID）', en: '(comma-separated alternate model IDs)' },
  'settings.meRequestIds': { zh: '请求名池', en: 'Request IDs' },
  'settings.meRequestIdsHint': { zh: '（逗号分隔；实际发给网关的模型 ID，可多个用于轮换）', en: '(comma-separated; the model IDs actually sent to the gateway — several rotate)' },
  'settings.meRequestIdsLastWarn': { zh: '请求名池不能为空——清空后该模型不会再被路由到。如需停用请直接关闭或删除该模型。', en: 'The request-ID pool cannot be empty — the model would stop being routed entirely. Disable or delete the model instead.' },
  'settings.meThinkingDefault': { zh: '默认开启思考模式', en: 'Enable thinking mode by default' },
  'settings.meAliasPrompt': { zh: '输入别名（同一模型的替代 ID）:', en: 'Enter an alias (alternate ID for the same model):' },
  'settings.meModelIdPlaceholder': { zh: '如 gpt-4o, claude-opus-4', en: 'e.g. gpt-4o, claude-opus-4' },
  'settings.meAliasesPlaceholder': { zh: '如 model-v2-b, vertex/model-v2', en: 'e.g. model-v2-b, vertex/model-v2' },
  'settings.meRequestIdsPlaceholder': { zh: '如 yuju-claude-opus-5-evaDaily, aws.claude-opus-5', en: 'e.g. yuju-claude-opus-5-evaDaily, aws.claude-opus-5' },
  'settings.meRequestIdPrompt': { zh: '新增请求名（实际发给网关的模型 ID）', en: 'Add a request ID (the model ID actually sent to the gateway)' },
  'settings.meInputPrice': { zh: '输入价格', en: 'Input price' },
  'settings.meOutputPrice': { zh: '输出价格', en: 'Output price' },
  'settings.mePriceHint': { zh: '（$/1M tokens，留空 = 不覆盖内置价目）', en: '($/1M tokens, blank = keep built-in pricing)' },
  'settings.meCostDerived': { zh: '（已由输入/输出价自动折算）', en: '(auto-derived from input/output prices)' },
  'settings.mePriceCustomTag': { zh: '自定义', en: 'custom' },
  'settings.mePriceInvalidWarn': { zh: '输入/输出价格需同时填写，且为非负数字——本次未改动已保存的价格。', en: 'Input and output prices must both be filled with non-negative numbers — your saved pricing was left unchanged.' },
  // ── Model card health strip (per-model runtime state, auto-polled) ──
  'settings.mhCooldown': { zh: '冷却 {s}s', en: 'cooldown {s}s' },
  'settings.mhReasonRateLimit': { zh: '限流', en: 'rate-limited' },
  'settings.mhReasonUpstream': { zh: '上游故障', en: 'upstream outage' },
  'settings.mhReasonError': { zh: '连续错误', en: 'consecutive errors' },
  'settings.mhReasonQuota': { zh: '配额耗尽', en: 'quota exhausted' },
  'settings.mhSuccessRate': { zh: '成功率', en: 'success' },
  'settings.mhNoTraffic': { zh: '暂无流量', en: 'no traffic yet' },
  'settings.mhInflight': { zh: '{n} 在途', en: '{n} inflight' },
  'settings.mhConsecErrors': { zh: '连续 {n} 错', en: '{n} consec errors' },
  'settings.mhContention': { zh: '争抢 {n}', en: 'contention {n}' },
  'settings.mhReasonContention': { zh: '项目级争抢', en: 'shared contention' },
  'settings.mhContentionTip': { zh: '项目级共享额度被其他租户打满导致的 429 次数 — 外部争抢，不计入成功率', en: '429s caused by other tenants saturating the shared project quota — external contention, excluded from the success rate' },
  'settings.mhRequestsTip': { zh: '本进程累计 {n} 次请求', en: '{n} requests this process' },
  // ── Visibility / model defaults (settings/visibility_defaults.js) ──
  'settings.vdNoIgModels': { zh: '未找到图片生成模型。请在服务商中添加具有 <code>image_gen</code> 能力的模型。', en: 'No image-gen models found. Add a model with the <code>image_gen</code> capability under a provider.' },
  'settings.vdNoChatModels': { zh: '未配置聊天模型。请先添加服务商。', en: 'No chat models configured. Add a provider first.' },
  'settings.vdFallbackEmpty': { zh: '（禁用自动回退）', en: '(disable auto-fallback)' },
  'settings.vdDefaultEmpty': { zh: '（使用环境变量）', en: '(use environment variable)' },
  'settings.vdUnregistered': { zh: '{model} (未注册)', en: '{model} (unregistered)' },
  // ── OAuth subscription login (settings/oauth.js) ──
  'settings.oauthTokenExchangeFailed': { zh: 'Token 交换失败: {msg}', en: 'Token exchange failed: {msg}' },
  'settings.oauthTokenExchangeNoCmd': { zh: 'Token 交换失败: {reason}（且无法生成手动命令，请重新点击登录）', en: 'Token exchange failed: {reason} (and no manual command could be generated — please click Login again)' },
  'settings.oauthCurlHelp': { zh: '服务器网络无法连接 Anthropic（已被区域封锁）。请在<strong>本机终端（已开代理）</strong>运行下面的命令，再把返回的 JSON（含 <code>access_token</code>）粘贴到上方输入框后点「提交」：', en: 'The server cannot reach Anthropic (geo-blocked). Run the command below in <strong>your local terminal (with proxy on)</strong>, then paste the returned JSON (containing <code>access_token</code>) into the box above and click Submit:' },
  'settings.oauthCopyCmd': { zh: '复制命令', en: 'Copy command' },
  'settings.oauthPasteJsonPlaceholder': { zh: '粘贴 curl 返回的 JSON（含 access_token）', en: 'Paste the JSON returned by curl (containing access_token)' },
  'settings.oauthLoggedIn': { zh: '已登录', en: 'Logged in' },
  'settings.oauthGettingToken': { zh: '正在获取 Token…', en: 'Getting token…' },
  'settings.oauthWaitingAuth': { zh: '等待授权…', en: 'Waiting for authorization…' },
  'settings.oauthCancelRetry': { zh: '取消 / 重试', en: 'Cancel / Retry' },
  'settings.oauthError': { zh: '错误', en: 'Error' },
  'settings.oauthNotLoggedIn': { zh: '未登录', en: 'Not logged in' },
  'settings.oauthLoginChatGPT': { zh: '登录 ChatGPT', en: 'Log in to ChatGPT' },
  'settings.oauthLoginClaude': { zh: '登录 Claude', en: 'Log in to Claude' },
  'settings.oauthPreparing': { zh: '正在准备…', en: 'Preparing…' },
  'settings.oauthLoginFailed': { zh: 'OAuth 登录失败: {error}', en: 'OAuth login failed: {error}' },
  'settings.oauthReopenPopup': { zh: '重新打开弹窗', en: 'Reopen popup' },
  'settings.oauthLoginReqFailed': { zh: 'OAuth 登录请求失败: {error}', en: 'OAuth login request failed: {error}' },
  'settings.oauthLogoutConfirm': { zh: '确定要退出 {provider} 订阅登录吗？', en: 'Log out of the {provider} subscription?' },
  'settings.oauthLogoutFailed': { zh: '退出失败: {error}', en: 'Logout failed: {error}' },
  'settings.oauthPasteCodePrompt': { zh: '请粘贴授权码或回调 URL', en: 'Please paste the authorization code or callback URL' },
  'settings.oauthSaveFailed': { zh: '保存失败: {error}', en: 'Save failed: {error}' },
  'settings.oauthNoAccessToken': { zh: '粘贴的 JSON 中未找到 access_token', en: 'No access_token found in the pasted JSON' },
  'settings.oauthNoCodeInUrl': { zh: '回调 URL 中未找到授权码', en: 'No authorization code found in the callback URL' },
  'settings.oauthAutoConfigured': { zh: '✅ {name} 登录成功！已自动注册为模型服务商，可直接在模型选择器中使用其订阅额度。', en: '✅ {name} login successful! Auto-registered as a model provider — use its subscription quota directly from the model picker.' },
  'settings.relJustNow': { zh: '刚刚', en: 'just now' },
  'settings.relSecAgo': { zh: '秒前', en: 's ago' },
  'settings.relMinAgo': { zh: '分钟前', en: 'min ago' },
  'settings.relHourAgo': { zh: '小时前', en: 'h ago' },
  'settings.relDayAgo': { zh: '天前', en: 'd ago' },

  // ══════════════════════════════════════
  //  Settings — Display / Preset Tab
  // ══════════════════════════════════════
  'settings.imageGen': { zh: '图片生成', en: 'Image Generation' },
  'settings.showAll': { zh: '全部显示', en: 'Show All' },
  'settings.hideAll': { zh: '全部隐藏', en: 'Hide All' },
  'settings.igDesc': { zh: '选择在图片生成选择器中显示哪些模型。隐藏的模型仍然可用，只是不会出现在选择器中。', en: 'Choose which models appear in the image generation picker. Hidden models are still usable, just not shown in the picker.' },
  'settings.modelDropdown': { zh: '模型下拉列表', en: 'Model Dropdown' },
  'settings.probeAllModels': { zh: '测试全部', en: 'Test All' },
  'settings.probeAllModelsTitle': { zh: '对每个模型发送一条最小请求验证可用性(1 token/次)', en: 'Send a minimal 1-token request to each model to verify reachability' },
  'settings.mhNeverProbed': { zh: '未测试', en: 'not tested' },
  'settings.mhStaleTip': { zh: '结果已过期(超过 24 小时),建议重测', en: 'Result is stale (over 24h) — retest recommended' },
  'settings.mhProbedAt': { zh: '上次测试 {t}', en: 'last tested {t}' },
  'settings.mhProbing': { zh: '测试中…', en: 'probing…' },
  'settings.modelDropdownDesc': { zh: '选择在模型切换下拉列表中显示哪些模型。隐藏的模型仍然可用，只是不会出现在下拉列表中。', en: 'Choose which models appear in the model switcher dropdown. Hidden models are still usable.' },
  'settings.modelDefaults': { zh: '模型默认', en: 'Model Defaults' },
  'settings.modelDefaultsDesc': { zh: '配置自动回退模型和预设的默认模型。当主模型请求失败时，系统将自动切换到回退模型继续生成。预设默认模型用于快捷切换时的模型选择。留空表示禁用或使用系统默认。', en: 'Configure fallback model and default models. When the primary model fails, the system switches to the fallback model. Leave empty to disable or use system defaults.' },
  'settings.fallbackModel': { zh: '回退模型', en: 'Fallback Model' },
  'settings.fallbackModelHint': { zh: '主模型失败时自动切换', en: 'Auto-switch when primary model fails' },
  'settings.disableFallback': { zh: '（禁用自动回退）', en: '(Disable auto-fallback)' },
  'settings.defaultModel': { zh: '默认模型', en: 'Default Model' },
  'settings.useEnvVar': { zh: '（使用环境变量）', en: '(Use environment variable)' },

  // ══════════════════════════════════════
  //  Settings — Search Tab
  // ══════════════════════════════════════
  'settings.searchFetch': { zh: '搜索与抓取', en: 'Search & Fetch' },
  'settings.llmContentFilter': { zh: 'LLM 内容过滤', en: 'LLM Content Filter' },
  'settings.llmContentFilterDesc': { zh: '抓取网页后用模型过滤无关内容（导航栏、广告等）。关闭可显著提升抓取速度并节省 token，但搜索质量会下降', en: 'Filter irrelevant content (nav, ads) after fetching pages. Turning off improves speed and saves tokens, but reduces search quality.' },
  'settings.searchFetchParams': { zh: '搜索与抓取参数', en: 'Search & Fetch Parameters' },
  'settings.fetchTopN': { zh: '抓取前 N 条', en: 'Fetch Top N' },
  'settings.fetchTopNHint': { zh: '搜索后自动抓取排名靠前的网页', en: 'Auto-fetch top-ranked pages after search' },
  'settings.fetchTimeout': { zh: '抓取超时', en: 'Fetch Timeout' },
  'settings.fetchTimeoutHint': { zh: '秒', en: 'seconds' },
  'settings.maxCharsSearch': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsSearchHint': { zh: '搜索结果页面', en: 'Search result pages' },
  'settings.maxCharsDirect': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsDirectHint': { zh: '直接抓取 URL', en: 'Direct URL fetch' },
  'settings.maxCharsPdf': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsPdfHint': { zh: 'PDF 文件，0=不限制', en: 'PDF files, 0=unlimited' },
  'settings.maxBytes': { zh: '最大下载大小', en: 'Max Download Size' },
  'settings.maxBytesHint': { zh: '字节，默认 20MB', en: 'bytes, default 20MB' },
  'settings.blockedDomains': { zh: '屏蔽域名', en: 'Blocked Domains' },
  'settings.blockedDomainsDesc': { zh: '抓取器不会访问的域名，每行一个。', en: 'Domains the fetcher will not visit, one per line.' },

  // Authenticated fetch sources (login-walled: Xiaohongshu, …)
  'settings.authSources': { zh: '需要登录的来源', en: 'Login-Required Sources' },
  'settings.authSourcesDesc': { zh: '小红书等需要登录的站点。在你自己的浏览器中登录后粘贴 Cookie 即可连接；之后搜索与抓取链接将使用该会话读取内容。Cookie 仅保存在本地服务器，不会上传。', en: 'Sites that require a login (e.g. Xiaohongshu). Connect by logging in via your OWN browser and pasting the cookie; search and link fetching then use that session. Cookies are stored only on your local server and never uploaded.' },
  'settings.authSourcesEmpty': { zh: '暂无可登录的来源。', en: 'No login-required sources yet.' },
  'settings.authSourcesLoadFail': { zh: '加载失败', en: 'Failed to load' },

  // Internal-host allowlist (SSRF exemption — reachability, NOT credentials)
  'settings.privateHosts': { zh: '内网主机放行', en: 'Internal Host Allowlist' },
  'settings.privateHostsDesc': { zh: '默认不允许抓取解析到内网地址的网址（防止 SSRF）。在此填写你确实需要访问的内网主机名，例如 your-llm-gateway.example.com。填父域名可覆盖其子域。此处只开放“能否访问”，不提供登录凭证；需要登录的站点请在上方“需要登录的来源”连接。', en: 'By default the fetcher refuses URLs whose host resolves to an internal address (SSRF protection). List the internal hostnames you do mean to reach, e.g. your-llm-gateway.example.com. A parent domain covers its subdomains. This grants REACHABILITY only, never credentials — for sites needing a login, connect them under "Login-Required Sources" above.' },
  'settings.privateHostsEmpty': { zh: '尚未放行任何内网主机。', en: 'No internal hosts allowlisted yet.' },
  'settings.privateHostsLoadFail': { zh: '加载失败', en: 'Failed to load' },
  'settings.privHostAllowed': { zh: '已放行', en: 'Allowed' },
  'settings.privHostPaused': { zh: '已停用', en: 'Disabled' },
  'settings.privHostEnable': { zh: '启用', en: 'Enable' },
  'settings.privHostDisable': { zh: '停用', en: 'Disable' },
  'settings.privHostRemove': { zh: '移除', en: 'Remove' },
  'settings.privHostAdd': { zh: '添加', en: 'Add' },
  'settings.privHostPlaceholder': { zh: 'your-llm-gateway.example.com', en: 'your-llm-gateway.example.com' },
  'settings.privHostNeedHost': { zh: '请输入主机名。', en: 'Enter a hostname.' },
  'settings.privHostSaveFail': { zh: '保存失败', en: 'Failed to save' },
  'common.remove': { zh: '移除', en: 'Remove' },
  'common.saving': { zh: '保存中…', en: 'Saving…' },
  'settings.authSrcConnected': { zh: '已连接', en: 'Connected' },
  'settings.authSrcDisabled': { zh: '已连接（已停用）', en: 'Connected (disabled)' },
  'settings.authSrcNotConnected': { zh: '未连接', en: 'Not connected' },
  'settings.authSrcToggle': { zh: '启用 / 停用', en: 'Enable / disable' },
  'settings.authSrcConnect': { zh: '连接', en: 'Connect' },
  'settings.authSrcReconnect': { zh: '重新连接', en: 'Reconnect' },
  'settings.authSrcDisconnectBtn': { zh: '断开', en: 'Disconnect' },
  'settings.authSrcStep1': { zh: '在你自己的浏览器中打开该站点并登录', en: 'Open the site in YOUR browser and log in' },
  'settings.authSrcStep1Generic': { zh: '在你自己的浏览器中登录该站点', en: 'Log in to the site in your own browser' },
  'settings.authSrcOpenLogin': { zh: '打开登录页 ↗', en: 'Open login page ↗' },
  'settings.authSrcStep2Fields': { zh: '打开开发者工具 (F12) → Application → Cookies，找到下面每个 Cookie，逐个复制它的 Value', en: 'Open DevTools (F12) → Application → Cookies, find each cookie below and copy its Value' },
  'settings.authSrcStep3Fields': { zh: '分别粘贴到对应输入框并保存（只填值，不要带名字或分号）', en: 'Paste each one into its own box and save (value only — no name, no semicolons)' },
  'settings.authSrcRequired': { zh: '必填', en: 'Required' },
  'settings.authSrcRecommended': { zh: '建议填写', en: 'Recommended' },
  'settings.authSrcOptional': { zh: '可选', en: 'Optional' },
  'settings.authSrcFieldPh': { zh: '粘贴 {name} 的值', en: 'Paste the {name} value' },
  'settings.authSrcFieldMissing': { zh: '请填写必填 Cookie：', en: 'Please fill in the required cookie(s): ' },
  'settings.authSrcProxyPh': { zh: '可选代理，例如 http://host:port', en: 'Optional proxy, e.g. http://host:port' },
  'settings.authSrcSaveConnect': { zh: '保存并连接', en: 'Save & connect' },
  'settings.authSrcSaved': { zh: '已连接', en: 'Connected' },
  'settings.authSrcSaveFail': { zh: '保存失败: ', en: 'Save failed: ' },
  'settings.authSrcDisconnectConfirm': { zh: '断开并清除该来源的 Cookie？', en: 'Disconnect and clear this source\'s cookies?' },

  // ══════════════════════════════════════
  //  Settings — Translation Tab
  // ══════════════════════════════════════
  // ── Speech recognition (voice input / STT) ──
  'settings.tabSpeech': { zh: '语音识别', en: 'Speech' },
  'settings.sttService': { zh: '语音识别（语音输入）', en: 'Speech Recognition (Voice Input)' },
  'settings.sttServiceDesc': { zh: '配置语音转文字（STT）服务后，聊天输入框会出现麦克风按钮，可直接说话转成文字。<br>未配置时麦克风按钮自动隐藏。', en: 'Configure a speech-to-text (STT) service and a microphone button appears in the composer — speak to transcribe into text.<br>When unconfigured the mic button is hidden.' },
  'settings.sttEnable': { zh: '启用语音输入', en: 'Enable Voice Input' },
  'settings.sttEnableDesc': { zh: '关闭后立即停用语音识别（麦克风按钮隐藏），已填写的凭证会保留', en: 'Disabling turns voice input off immediately (mic button hidden); saved credentials are kept' },
  'settings.sttProvider': { zh: '识别服务商', en: 'Recognition Provider' },
  'settings.sttProviderOpenai': { zh: 'OpenAI (gpt-4o-transcribe)', en: 'OpenAI (gpt-4o-transcribe)' },
  'settings.sttProviderGroq': { zh: 'Groq (Whisper)', en: 'Groq (Whisper)' },
  'settings.sttProviderOmni': { zh: 'Omni 对话模型 (audio_chat)', en: 'Omni chat model (audio_chat)' },
  'settings.sttProviderCustom': { zh: '自定义 Whisper 端点', en: 'Custom Whisper endpoint' },
  'settings.sttOpenaiDesc': { zh: '官方 /audio/transcriptions 端点 · 高准确率 · 需公网直连 OpenAI', en: 'Official /audio/transcriptions endpoint · high accuracy · needs direct OpenAI access' },
  'settings.sttGroqDesc': { zh: 'LPU 上的 Whisper · 极快 · 极低价 · 兼容 /audio/transcriptions', en: 'Whisper on LPUs · very fast · very cheap · /audio/transcriptions compatible' },
  'settings.sttOmniName': { zh: 'Omni 对话模型', en: 'Omni chat model' },
  'settings.sttOmniDesc': { zh: '全模态对话模型内联识别（/chat/completions）· 适合无独立 Whisper 端点的网关（如美团 AIGC gemini-3-flash-preview / LongCat-Flash-Omni）', en: 'Omni chat model, audio sent inline (/chat/completions) · for gateways with no standalone Whisper endpoint (e.g. YourProvider AIGC gemini-3-flash-preview / LongCat-Flash-Omni)' },
  'settings.sttCustomName': { zh: '自定义端点', en: 'Custom endpoint' },
  'settings.sttCustomDesc': { zh: '任何兼容 OpenAI /audio/transcriptions 的端点（自建 faster-whisper、vLLM 等）', en: 'Any endpoint compatible with OpenAI /audio/transcriptions (self-hosted faster-whisper, vLLM, …)' },
  'settings.sttModelLabel': { zh: '模型', en: 'Model' },
  'settings.sttBaseUrlLabel': { zh: 'API 地址', en: 'API URL' },
  'settings.sttApplyKey': { zh: '获取 API Key', en: 'Get API Key' },
  'settings.sttOptional': { zh: '（可选）', en: '(optional)' },
  'settings.sttRequired': { zh: '（必填）', en: '(required)' },
  'settings.sttStatusOn': { zh: '✓ 语音输入已就绪', en: '✓ Voice input is ready' },
  'settings.sttStatusOff': { zh: '语音输入未就绪 — 保存有效凭证后麦克风按钮才会出现', en: 'Voice input not ready — the mic button appears once valid credentials are saved' },
  'settings.tabTranslate': { zh: '翻译', en: 'Translation' },
  'settings.mtService': { zh: '机器翻译服务', en: 'Machine Translation Service' },
  'settings.mtServiceDesc': { zh: '配置专用机器翻译 API，比 LLM 翻译更快、更便宜。<br>未配置或关闭时，翻译将自动使用 LLM cheap 模型。', en: 'Configure a dedicated machine translation API — faster and cheaper than LLM translation.<br>When not configured or disabled, translation falls back to LLM cheap model.' },
  'settings.mtEnable': { zh: '启用机器翻译', en: 'Enable Machine Translation' },
  'settings.mtEnableDesc': { zh: '开启后，翻译将优先使用机器翻译 API，失败时自动回退到 LLM', en: 'When enabled, translation uses the MT API first, falling back to LLM on failure' },
  'settings.mtProvider': { zh: '翻译服务商', en: 'Translation Provider' },
  'settings.mtProviderNiutrans': { zh: '小牛翻译 NiuTrans', en: 'NiuTrans' },
  'settings.mtProviderCustom': { zh: '自定义 Custom', en: 'Custom' },
  'settings.mtNiutransName': { zh: '小牛翻译', en: 'NiuTrans' },
  'settings.mtNiutransDesc': { zh: '支持 400+ 语种互译 · 中英日韩高质量翻译 · 东北大学 NLP 实验室', en: '400+ language pairs · High-quality CJK translation · NEU NLP Lab' },
  'settings.mtApplyKey': { zh: '申请 API Key', en: 'Get API Key' },
  'settings.mtApiKeyPh': { zh: '在小牛翻译控制台 → API 管理中获取', en: 'Get from NiuTrans console → API Management' },
  'settings.mtAppIdLabel': { zh: 'App ID', en: 'App ID' },
  'settings.mtAppIdHint': { zh: '可选，v2 签名鉴权', en: 'Optional, v2 signed auth' },
  'settings.mtAppIdPh': { zh: '留空使用简单 API Key 鉴权 (v1)', en: 'Leave empty for simple API Key auth (v1)' },
  'settings.mtApiUrlLabel': { zh: 'API 地址', en: 'API URL' },
  'settings.mtApiUrlHint': { zh: '可选', en: 'Optional' },
  'settings.mtApiUrlPh': { zh: '留空使用默认地址', en: 'Leave empty for default URL' },
  'settings.mtTestBtn': { zh: '测试连接', en: 'Test Connection' },
  'settings.mtTesting': { zh: '测试中…', en: 'Testing…' },
  'settings.mtTestOk': { zh: '连接成功：', en: 'Connected: ' },
  'settings.mtTestFail': { zh: '未知错误', en: 'Unknown error' },
  'settings.mtTestReqFail': { zh: '请求失败: ', en: 'Request failed: ' },
  // ── Network + Feishu + Advanced tabs (settings/other_tabs.js) ──
  'settings.proxyEnvBanner': { zh: '系统环境变量: {vars}', en: 'System env vars: {vars}' },
  'settings.proxyEnvHint': { zh: '环境变量基线 (PROXY_BYPASS_DOMAINS): {val}', en: 'Env baseline (PROXY_BYPASS_DOMAINS): {val}' },
  'settings.feishuConnected': { zh: '已连接', en: 'Connected' },
  'settings.feishuConnectedDesc': { zh: 'WebSocket 已连接 · 应用：{app}', en: 'WebSocket connected · App: {app}' },
  'settings.feishuEnabledNotConnected': { zh: '已启用但未连接', en: 'Enabled but not connected' },
  'settings.feishuCredsNotConnected': { zh: '凭证已设置但未连接', en: 'Credentials set but not connected' },
  'settings.feishuDisabled': { zh: '未启用', en: 'Not enabled' },
  'settings.feishuDisabledDesc': { zh: '请设置 App ID 和 App Secret 以启用', en: 'Set App ID and App Secret to enable' },
  'settings.feishuSecretSaved': { zh: '••••••••（已保存 — 留空则保持不变）', en: '••••••••(saved — leave blank to keep unchanged)' },
  'settings.feishuSecretPlaceholder': { zh: '输入应用密钥', en: 'Enter app secret' },
  'settings.cacheUnavailable': { zh: 'IndexedDB 不可用', en: 'IndexedDB unavailable' },
  'settings.cacheCached': { zh: '已缓存 {n} 个对话', en: '{n} conversation(s) cached' },
  'settings.cacheClearing': { zh: '清除中…', en: 'Clearing…' },
  'settings.cacheClearBtn': { zh: '清除缓存', en: 'Clear cache' },
  'settings.cacheCleared': { zh: '缓存已清除 — 下次点击对话时将重新从服务器加载', en: 'Cache cleared — conversations reload from server on next open' },
  'settings.mtCustomName': { zh: '自定义服务商', en: 'Custom Provider' },
  'settings.mtCustomDesc': { zh: '接入其他兼容 NiuTrans API 格式的翻译服务', en: 'Connect to other translation services compatible with NiuTrans API format' },
  'settings.mtCustomApiKeyPh': { zh: '翻译服务 API Key', en: 'Translation service API Key' },
  'settings.mtCustomAppIdPh': { zh: '如需签名鉴权则填写', en: 'Fill in if signed auth is required' },
  'settings.mtCustomApiUrlHint': { zh: '必填', en: 'Required' },

  // ══════════════════════════════════════
  //  Settings — Network Tab
  // ══════════════════════════════════════
  'settings.httpProxy': { zh: 'HTTP 代理', en: 'HTTP Proxy' },
  'settings.httpProxyDesc': { zh: '配置用于所有出站请求（LLM API、网页搜索、页面抓取）的 HTTP/HTTPS 代理。留空则使用系统环境变量（http_proxy / https_proxy）。修改立即生效，无需重启。', en: 'Configure HTTP/HTTPS proxy for all outbound requests (LLM API, web search, page fetch). Leave empty to use system env vars (http_proxy / https_proxy). Changes take effect immediately.' },
  'settings.httpsProxy': { zh: 'HTTPS 代理', en: 'HTTPS Proxy' },
  'settings.proxyBypassTitle': { zh: '不代理域名', en: 'Proxy Bypass Domains' },
  'settings.proxyBypassDesc': { zh: '在此添加不需要走代理的域名后缀或主机名（每行一个，后缀匹配）。匹配的请求会完全绕过 HTTP 代理。', en: 'Add domain suffixes or hostnames that should bypass the proxy (one per line, suffix matching). Matching requests bypass the HTTP proxy entirely.' },
  'settings.proxyBypassTip': { zh: '💡 提示：内网地址和 LLM API 域名都应加在这里。企业/VPN 代理会静默断开长连接（SSE 流），导致 BrokenPipeError，添加对应域名即可解决。也可通过环境变量 PROXY_BYPASS_DOMAINS（逗号分隔）配置，两处合并生效。', en: '💡 Tip: Internal addresses and LLM API domains should be added here. Corporate/VPN proxies may silently close long connections (SSE streams), causing BrokenPipeError — adding the domain here fixes it. Can also be set via PROXY_BYPASS_DOMAINS env var (comma-separated).' },
  'settings.bypassDomains': { zh: '绕过域名', en: 'Bypass Domains' },
  'settings.bypassDomainsHint': { zh: '每行一个，后缀匹配 — 例如 .your-corp.com', en: 'One per line, suffix matching — e.g. .your-corp.com' },

  // ══════════════════════════════════════
  //  Settings — Feishu Tab
  // ══════════════════════════════════════
  'settings.feishuBot': { zh: '飞书 (Lark) 机器人', en: 'Feishu (Lark) Bot' },
  'settings.connectionStatus': { zh: '连接状态', en: 'Connection Status' },
  'settings.loadingStatus': { zh: '加载状态中…', en: 'Loading status…' },
  'settings.credentials': { zh: '凭证', en: 'Credentials' },
  'settings.defaultProjectPath': { zh: '默认项目路径', en: 'Default Project Path' },
  'settings.workspaceRoot': { zh: '工作空间根目录', en: 'Workspace Root' },
  'settings.workspace': { zh: '工作空间', en: 'Workspace' },
  // ── Project Co-Pilot modal — Recent card ──
  'pm.recent': { zh: '最近', en: 'Recent' },
  'pm.recentSearchPlaceholder': { zh: '搜索最近…', en: 'Search recent…' },
  'pm.recentClearAll': { zh: '清空全部', en: 'Clear all' },
  'pm.recentClearSearch': { zh: '清除搜索', en: 'Clear search' },
  'pm.recentNoMatch': { zh: '无匹配项目', en: 'No matching projects' },
  'pm.recentEmpty': { zh: '暂无最近项目', en: 'No recent projects' },
  'pm.dragReorder': { zh: '拖动排序 — 最上方为主根目录', en: 'Drag to reorder — the top folder is the root' },
  'settings.accessControl': { zh: '访问控制', en: 'Access Control' },
  'settings.allowedUsers': { zh: '允许的用户', en: 'Allowed Users' },
  'settings.allowedUsersHint': { zh: '飞书 open_id，每行一个 — 留空表示允许所有人', en: 'Feishu open_id, one per line — leave empty to allow everyone' },
  'settings.credModRestart': { zh: '凭证修改需要重启服务器才能生效', en: 'Credential changes require server restart' },

  // ══════════════════════════════════════
  //  Settings — OAuth Tab
  // ══════════════════════════════════════
  'settings.oauthTitle': { zh: '订阅登录', en: 'OAuth Login' },
  'settings.oauthDesc': { zh: '使用 ChatGPT Plus / Claude Pro 订阅账号登录，无需 API Key，直接使用订阅额度。', en: 'Login with ChatGPT Plus / Claude Pro subscription — no API Key needed, use your subscription quota directly.' },
  'settings.oauthChinaWarn': { zh: '⚠️ 中国用户需要全程代理（Clash/VPN），授权弹窗和服务器换 token 都需要能访问外网。建议在本地浏览器无痕窗口中完成授权。', en: '⚠️ Users in China need a proxy (Clash/VPN) throughout. Both the auth popup and server token exchange require internet access. Use an incognito window.' },
  'settings.notLoggedIn': { zh: '未登录', en: 'Not logged in' },
  'settings.loginClaude': { zh: '登录 Claude', en: 'Login Claude' },
  'settings.loginChatGPT': { zh: '登录 ChatGPT', en: 'Login ChatGPT' },
  'settings.logout': { zh: '退出', en: 'Logout' },
  'settings.claudeDesc': { zh: '登录 Claude 订阅，使用 Sonnet / Opus 等模型，无需 API Key。', en: 'Login with Claude subscription to use Sonnet / Opus models without API Key.' },
  'settings.codexDesc': { zh: '登录 ChatGPT 订阅，使用 Codex 模型，请求自动转换为 Responses API 格式。', en: 'Login with ChatGPT subscription to use Codex models, auto-converted to Responses API format.' },
  'settings.popupBlocked': { zh: '如弹窗无法打开，请复制链接到开了代理的浏览器无痕窗口中打开：', en: 'If the popup is blocked, copy the link and open it in an incognito window with proxy enabled:' },
  'settings.copyLink': { zh: '复制链接', en: 'Copy Link' },
  'settings.copied': { zh: '✓ 已复制', en: '✓ Copied' },
  'settings.authCodeHint': { zh: '授权成功后页面显示授权码，复制 code#state 粘贴到下方：', en: 'After authorization, copy the code#state from the page and paste below:' },
  'settings.callbackHint': { zh: '授权成功后，复制浏览器地址栏中的回调 URL 粘贴到下方：', en: 'After authorization, copy the callback URL from the browser address bar below:' },
  'settings.submit': { zh: '提交', en: 'Submit' },
  'settings.oauthInstructions': { zh: '使用说明', en: 'Instructions' },

  // ══════════════════════════════════════
  //  Settings — MCP Tab
  // ══════════════════════════════════════
  'settings.searchApps': { zh: '搜索 Apps…', en: 'Search Apps…' },
  'settings.loading': { zh: '正在加载…', en: 'Loading…' },
  'settings.installed': { zh: '已安装', en: 'Installed' },
  'settings.connectAll': { zh: '全部连接', en: 'Connect All' },
  'settings.manualAdd': { zh: '⚙ 手动添加自定义服务器', en: '⚙ Manually add custom server' },
  'settings.name': { zh: '名称', en: 'Name' },
  'settings.transport': { zh: '传输协议', en: 'Transport' },
  'settings.transportStdio': { zh: 'stdio (本地命令)', en: 'stdio (local command)' },
  'settings.transportSSE': { zh: 'SSE (远程 URL)', en: 'SSE (remote URL)' },
  'settings.command': { zh: '命令', en: 'Command' },
  'settings.args': { zh: '参数', en: 'Arguments' },
  'settings.argsHint': { zh: '每行一个', en: 'One per line' },
  'settings.envVars': { zh: '环境变量', en: 'Environment Variables' },
  'settings.envVarsHint': { zh: '每行 KEY=VALUE', en: 'One KEY=VALUE per line' },
  'settings.description': { zh: '描述', en: 'Description' },
  'settings.optional': { zh: '可选', en: 'Optional' },
  'settings.saveAndConnect': { zh: '保存并连接', en: 'Save & Connect' },
  'settings.installAndConnect': { zh: '安装并连接', en: 'Install & Connect' },

  // ══════════════════════════════════════
  //  Settings — Advanced Tab
  // ══════════════════════════════════════
  'settings.importExport': { zh: '导入 / 导出', en: 'Import / Export' },
  'settings.importExportDesc': { zh: '将所有服务器端配置导出为 JSON，或从文件导入。', en: 'Export all server config as JSON, or import from file.' },
  'settings.exportJson': { zh: '导出 JSON', en: 'Export JSON' },
  'settings.importJson': { zh: '导入 JSON', en: 'Import JSON' },
  'settings.pricingOverride': { zh: '价格覆盖', en: 'Pricing Override' },
  'settings.pricingOverrideDesc': { zh: '模型定价（美元 / 每百万 Token）。编辑以下 JSON 进行自定义。', en: 'Model pricing (USD / per million tokens). Edit the JSON below to customize.' },
  'settings.localCache': { zh: '本地缓存', en: 'Local Cache' },
  'settings.localCacheDesc': { zh: '对话缓存在 IndexedDB 中以实现即时加载。服务器始终是数据的唯一来源。', en: 'Conversations are cached in IndexedDB for instant loading. The server is always the source of truth.' },
  'settings.clearCache': { zh: '清除缓存', en: 'Clear Cache' },
  'settings.serverInfo': { zh: '服务器信息', en: 'Server Info' },
  'settings.status': { zh: '状态', en: 'Status' },

  // ══════════════════════════════════════
  //  Settings — Auto Setup Modal
  // ══════════════════════════════════════
  'settings.autoSetupTitle': { zh: '自动配置服务商', en: 'Auto Setup Provider' },
  'settings.autoSetupDesc': { zh: '只需填写 API 地址和密钥，系统将自动发现模型、检测余额接口、识别服务商品牌并获取定价信息。', en: 'Just enter the API URL and key — the system will auto-discover models, detect balance endpoint, identify provider brand, and fetch pricing info.' },
  'settings.autoSetupUrl': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.autoSetupUrlHint': { zh: '填写 OpenAI 兼容的 API 地址，通常以 /v1 结尾', en: 'Enter an OpenAI-compatible API URL, usually ending with /v1' },
  'settings.autoSetupKey': { zh: 'API 密钥', en: 'API Key' },
  'settings.autoSetupModelsPath': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.autoSetupModelsPathHint': { zh: '可选 — 默认 /models', en: 'Optional — defaults to /models' },
  'settings.startProbe': { zh: '开始探测', en: 'Start Probe' },
  'settings.probing': { zh: '正在探测…', en: 'Probing…' },
  'settings.discoveringModels': { zh: '正在发现模型… 这可能需要几秒钟', en: 'Discovering models… This may take a few seconds' },
  'settings.fillUrl': { zh: '请填写 API 地址', en: 'Please enter API URL' },
  'settings.fillKey': { zh: '请填写 API 密钥', en: 'Please enter API Key' },
  'settings.probeFailed': { zh: '探测失败', en: 'Probe failed' },
  'settings.networkError': { zh: '网络错误', en: 'Network error' },
  'settings.textModels': { zh: '个文本', en: 'text' },
  'settings.thinkingModels': { zh: '个推理', en: 'thinking' },
  'settings.visionModels': { zh: '个视觉', en: 'vision' },
  'settings.cheapModels': { zh: '个低价', en: 'cheap' },
  'settings.igModels': { zh: '个图片生成', en: 'image gen' },
  'settings.embeddingModels': { zh: '个嵌入', en: 'embedding' },
  'settings.discovered': { zh: '发现', en: 'Discovered' },
  'settings.balanceDetected': { zh: '已检测到余额接口', en: 'balance endpoint detected' },
  'settings.thinkingFormatSuggested': { zh: '建议思维格式', en: 'suggested thinking format' },
  'settings.configSaved': { zh: '服务器配置已保存，设置已实时生效。', en: 'Server config saved. Settings applied in real-time.' },
  'settings.saved': { zh: '已保存', en: 'Saved' },
  'settings.serverConfigFailed': { zh: '无法加载服务器配置', en: 'Cannot load server config' },

  // ── Provider template actions (settings/template_actions.js) ──
  'settings.tplDeleteConfirm': { zh: '确定删除服务商“{name}”及其 {n} 个模型吗？', en: 'Delete provider "{name}" and its {n} model(s)?' },
  'settings.tplNewProvider': { zh: '新服务商', en: 'New Provider' },
  'settings.tplCatOfficial': { zh: '官方 API', en: 'Official API' },
  'settings.tplCatOfficialDesc': { zh: '直连模型厂商', en: 'Direct to model vendors' },
  'settings.tplCatRelay': { zh: '中转 API', en: 'Relay API' },
  'settings.tplCatRelayDesc': { zh: '聚合多家模型', en: 'Aggregated multi-vendor' },
  'settings.tplCatOther': { zh: '其他', en: 'Other' },
  'settings.tplModelsCount': { zh: '{n} 个模型', en: '{n} models' },
  'settings.tplAlreadyAdded': { zh: '{name}（相同 API 地址）似乎已添加。要再添加一个吗？', en: '{name} (same API URL) appears to be already added. Add another?' },
  'settings.tplKeyPlaceholder': { zh: '在此粘贴你的 {name} API 密钥', en: 'Paste your {name} API key here' },
  'settings.tplNoMatch': { zh: '找不到匹配的内置模板。', en: 'No matching built-in template found.' },
  'settings.tplSyncDone': { zh: '模板同步完成：', en: 'Template sync complete: ' },
  'settings.tplSyncAdded': { zh: '新增 {n} 个模型', en: 'added {n} model(s)' },
  'settings.tplSyncUpdated': { zh: '更新 {n} 个模型', en: 'updated {n} model(s)' },
  'settings.tplSyncAliases': { zh: '补全 {n} 个别名', en: 'filled in {n} alias(es)' },
  'settings.tplSyncFaces': { zh: '补全 {n} 个协议面', en: 'added {n} wire face(s)' },
  'settings.faceRefusedTitle': { zh: '{n} 个模型未注册（协议面缺失）', en: '{n} model(s) not registered (missing wire face)' },
  'settings.faceRefusedHint': { zh: '这些模型需要 Anthropic 原生协议面才能保留思考签名。点击「从模板同步」即可补全。', en: 'These models need the Anthropic-native wire face to preserve thinking signatures. Click "Sync from template" to add it.' },
  'settings.faceChipRefused': { zh: '未注册', en: 'not registered' },
  'settings.faceChipTitle': { zh: '协议面：{face}\n地址：{url}', en: 'Wire face: {face}\nEndpoint: {url}' },
  'settings.faceChipPinnedTag': { zh: '已钉选', en: 'pinned' },
  'settings.faceChipPinnedTitle': { zh: '已手动钉选，覆盖了按模型家族的自动选择', en: 'Manually pinned — overrides the automatic family rule' },
  'settings.meFace': { zh: '协议面（wire face）', en: 'Wire face' },
  'settings.meFaceHint': { zh: '决定这个模型走哪条协议线', en: 'which protocol this model dispatches over' },
  'settings.meFaceAuto': { zh: '自动（按模型家族，推荐）', en: 'Automatic (by model family — recommended)' },
  'settings.meFacePinWarn': { zh: '⚠️ 手动钉选会覆盖自动规则。把 Claude 模型钉到非 Anthropic 面会丢失思考块签名，导致多轮对话被上游拒绝。', en: '⚠️ A manual pin overrides the automatic rule. Pinning a Claude model to a non-Anthropic face drops thinking-block signatures, which upstream rejects on later turns.' },
  'settings.wireFaces': { zh: '备用协议面', en: 'Alternate wire faces' },
  'settings.wireFacesHint': { zh: '同一套密钥、另一条协议线；留空即单面网关', en: 'same keys, another protocol endpoint; empty = single-face gateway' },
  'settings.addFace': { zh: '+ 协议面', en: '+ Face' },
  'settings.addFaceTitle': { zh: '新增一条备用协议面', en: 'Add an alternate wire face' },
  'settings.noFaces': { zh: '未声明备用协议面（单面网关）', en: 'No alternate faces declared (single-face gateway)' },
  'settings.faceNamePlaceholder': { zh: '面名，如 anthropic', en: 'face name, e.g. anthropic' },
  'settings.deleteFaceTitle': { zh: '删除这条协议面', en: 'Delete this wire face' },
  'settings.faceDeleteConfirm': { zh: '删除协议面「{face}」？\n\n有 {n} 个模型正通过它路由：\n{models}\n\n删除后这些模型将无法注册（在双面网关上 Claude 会被拒绝而不是静默降级）。', en: 'Delete wire face "{face}"?\n\n{n} model(s) currently route through it:\n{models}\n\nThey will no longer be registered (on a dual-face gateway Claude is refused rather than silently downgraded).' },
  'settings.tplSyncJoin': { zh: '，', en: ', ' },
  'settings.tplSyncNoChange': { zh: '所有模型已是最新，无需更新。', en: 'All models are up to date, no update needed.' },
  'settings.tplSyncUserAliases': { zh: '\n\n⚠ 以下别名是你手动添加的（模板中没有），请确认它们在网关上确实存在；不存在的会一直返回 HTTP 400：\n  • {list}', en: '\n\n⚠ The following aliases were added by you (not in the template). Confirm they exist on the gateway; missing ones will keep returning HTTP 400:\n  • {list}' },
  'settings.tplSyncSaveHint': { zh: '\n\n记得点击「保存」按钮来保存更改。', en: '\n\nRemember to click "Save" to persist the changes.' },
  'settings.tplFillUrlFirst': { zh: '请先设置 API 地址 (Base URL) 再进行模型发现。', en: 'Please set the API URL (Base URL) before discovering models.' },
  'settings.tplFillKeyFirst': { zh: '请先添加至少一个 API 密钥再进行模型发现。', en: 'Please add at least one API key before discovering models.' },
  'settings.tplDiscovering': { zh: '发现中…', en: 'Discovering…' },
  'settings.tplDiscoverNetFail': { zh: '发现失败 (网络/超时)', en: 'Discovery failed (network/timeout)' },
  'settings.tplDiscoverFail': { zh: '发现失败: {error}', en: 'Discovery failed: {error}' },
  'settings.tplUnknownError': { zh: '未知错误', en: 'Unknown error' },
  'settings.tplNoModelsFound': { zh: '在 {url} 未找到模型', en: 'No models found at {url}' },
  'settings.tplDiscoverResult': { zh: '✅ 发现 {total} 个模型（{cheap} 个标记为低价）。\n新增 {added} 个模型{rest}', en: '✅ Discovered {total} model(s) ({cheap} marked cheap).\nAdded {added} model(s){rest}' },
  'settings.tplDiscoverRest': { zh: '，{n} 个已存在。', en: ', {n} already existed.' },
  'settings.tplDiscoverRestEnd': { zh: '。', en: '.' },
  'settings.tplDiscoverError': { zh: '发现出错: {error}', en: 'Discovery error: {error}' },
  'settings.tplUpdateConfirm': { zh: '是否将当前模型列表（{n} 个）写入源码模板？\n\n这样新部署时就自带最新模型，无需再次发现。', en: 'Write the current model list ({n}) into the source template?\n\nSo new deployments ship with the latest models without re-discovery.' },
  'settings.tplUpdateDone': { zh: '✅ 模板已更新：{n} 个模型写入 {files}。', en: '✅ Template updated: {n} model(s) written to {files}.' },
  'settings.tplUpdateFail': { zh: '模板更新失败: {error}', en: 'Template update failed: {error}' },
  'settings.tplUpdateError': { zh: '模板更新出错: {error}', en: 'Template update error: {error}' },

  // ══════════════════════════════════════
  //  Browser Bridge Modal
  // ══════════════════════════════════════
  'browser.close': { zh: '关闭', en: 'Close' },

  // ══════════════════════════════════════
  //  Local Control (merged browser bridge + desktop agent)
  //  ONE surface for "let Tofu act on my machine". The *Setup keys below are
  //  mutually exclusive: exactly one is rendered, chosen by detected state.
  // ══════════════════════════════════════
  'local.title': { zh: '本机控制', en: 'Local Control' },
  'local.desc': { zh: '让 AI 操作你的浏览器与电脑', en: 'Let AI drive your browser and computer' },
  'local.modalDesc': { zh: '让 AI 读取并操作你自己的设备。两项能力互相独立，可分别开启。', en: 'Let AI read and act on your own device. The two capabilities are independent — enable either on its own.' },
  'local.browserName': { zh: '浏览器标签页', en: 'Browser tabs' },
  'local.desktopName': { zh: '这台电脑', en: 'This computer' },
  'local.browserAbout': { zh: '读取你已打开的标签页内容，并代你点击、填表单、切换页面。', en: 'Reads the tabs you already have open, and can click, fill forms and navigate for you.' },
  'local.desktopAbout': { zh: '浏览与读写本机文件、截屏、打开应用、运行命令（写入与执行需单独授权）。', en: 'Browses and reads local files, takes screenshots, opens apps and runs commands (writing and running need separate permission).' },
  'local.switchBlocked': { zh: '连接成功后才能开启 —— 现在打开，AI 也拿不到任何工具。', en: 'Available once connected — switching it on now would give the AI no tools.' },
  'local.switchStale': { zh: '已开启，但当前未连接 —— AI 现在拿不到这项能力的任何工具。可随时关闭。', en: 'On, but nothing is connected — the AI gets no tools from this right now. You can switch it off at any time.' },
  'local.badgeStale': { zh: '已开启，但当前未连接 —— AI 实际拿不到这些工具。', en: 'On, but not connected — the AI is not actually getting these tools.' },
  'local.connected': { zh: '已连接', en: 'Connected' },
  'local.connectedN': { zh: '已连接 {n} 个', en: '{n} connected' },
  'local.notInstalled': { zh: '尚未安装', en: 'Not installed' },
  'local.notRunning': { zh: '未运行', en: 'Not running' },
  'local.unreachable': { zh: '无法连接服务器', en: 'Cannot reach server' },
  'local.browserOpenPageBtn': { zh: '帮我打开扩展管理页（自动复制路径）', en: 'Open the extensions page for me (path auto-copied)' },
  'local.browserLoadUnpacked': { zh: '剩下的三步 {browser} 不允许网页代劳：① 打开右上角「开发者模式」→ ② 点「加载已解压的扩展程序」→ ③ 粘贴路径（已自动复制）选择这个文件夹：', en: 'Three clicks remain that {browser} forbids any web page from doing for you: ① turn on Developer mode → ② click "Load unpacked" → ③ paste the path (already copied) and pick this folder:' },
  'local.browserPageOpened': { zh: '已在你的浏览器打开扩展管理页，路径已复制 —— 剩下三步只能你来点。', en: 'The extensions page is open in your browser and the path is copied — the remaining three clicks are yours.' },
  'local.browserPageOpenFailed': { zh: '没能替你打开 —— 请自己打开浏览器的扩展管理页，路径已复制。', en: 'Could not open it for you — open your browser\'s extensions page yourself; the path is copied.' },
  'local.browserDownload': { zh: '下载扩展并解压，然后在 Chrome / Edge 里打开扩展管理页 → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择解压出的文件夹。', en: 'Download and unzip the extension, then open the extensions page in Chrome / Edge → turn on Developer mode → "Load unpacked" → pick the unzipped folder.' },
  'local.desktopTray': { zh: '右键点击系统托盘里的 Tofu 图标 → 勾选「Enable Computer Control」。', en: 'Right-click the Tofu icon in your system tray → tick "Enable Computer Control".' },
  'local.desktopSource': { zh: '当前 Tofu 以源码方式运行。安装桌面版后即可在系统托盘一键开启「Enable Computer Control」。', en: 'Tofu is running from source. Install the desktop app to get the tray\'s one-click "Enable Computer Control".' },
  'local.desktopFloor': { zh: '安装桌面版后，即可在系统托盘一键开启「Enable Computer Control」，让 AI 操作这台电脑。', en: 'Install the desktop app, then switch on "Enable Computer Control" from the system tray to let the AI drive this computer.' },
  'local.desktopRemote': { zh: 'Tofu 运行在远程服务器上。在你自己的电脑安装桌面版，再用下面这行把它连过来：', en: 'Tofu runs on a remote server. Install the desktop app on your own machine, then connect it with the line below:' },
  'local.desktopDownload': { zh: '下载桌面版 ↗', en: 'Download the desktop app ↗' },
  'local.desktopDownloadFor': { zh: '下载桌面版', en: 'Download' },
  'local.desktopDownloadAll': { zh: '查看全部下载 ↗', en: 'See all downloads ↗' },
  'local.desktopArchAmbiguous': { zh: '浏览器没告诉我们这台 Mac 的芯片型号（Apple Silicon 也会自称 Intel）。Apple 芯片（M1/M2/M3…）选 arm64，Intel 芯片选 x86_64；在「关于本机」里可以看到。', en: 'Your browser did not reveal this Mac’s chip (Apple Silicon reports itself as Intel too). Pick arm64 for Apple chips (M1/M2/M3…) and x86_64 for Intel — see “About This Mac”.' },
  'local.mintToken': { zh: '生成连接命令', en: 'Generate connect line' },
  'local.permNote': { zh: '写文件、运行命令、控制鼠标键盘默认全部关闭；需要时在托盘菜单的「Permissions」里单独授予。', en: 'Writing files, running commands and mouse/keyboard control are all OFF by default — grant them individually in the tray\'s "Permissions" menu when needed.' },

  // ══════════════════════════════════════
  //  Memory Modal
  // ══════════════════════════════════════
  'memory.title': { zh: '记忆积累 · AI 自动学习并应用的知识库', en: 'Memory · AI auto-learns and applies knowledge' },
  'memory.all': { zh: '全部', en: 'All' },
  'memory.project': { zh: '项目', en: 'Project' },
  'memory.global': { zh: '全局', en: 'Global' },
  'memory.searchPh': { zh: '搜索记忆…', en: 'Search memories…' },
  'memory.createNew': { zh: '创建新记忆', en: 'Create New Memory' },
  'memory.namePh': { zh: '记忆名称 (短横线命名, e.g. react-hooks-convention)', en: 'Memory name (kebab-case, e.g. react-hooks-convention)' },
  'memory.descPh': { zh: '简短描述 — 什么时候该使用这条记忆？', en: 'Brief description — when should this memory be used?' },
  'memory.bodyPh': { zh: '记忆内容（支持 Markdown）…', en: 'Memory content (Markdown supported)…' },
  'memory.projectScope': { zh: '项目级', en: 'Project scope' },
  'memory.globalScope': { zh: '全局', en: 'Global' },
  'memory.tagsPh': { zh: '例如：react, hooks, 前端', en: 'e.g. react, hooks, frontend' },
  'memory.tagsLabel': { zh: '标签', en: 'Tags' },
  'memory.tagsHint': { zh: '逗号分隔的关键词，用于搜索过滤和让 AI 更精准地召回这条记忆（可选）', en: 'Comma-separated keywords for search filtering and to help the AI recall this memory more accurately (optional)' },
  'memory.create': { zh: '创建', en: 'Create' },
  'memory.new': { zh: '新建', en: 'New' },
  'memory.installSkill': { zh: '安装技能包', en: 'Install skill' },
  'memory.openSkillsStore': { zh: '技能市场', en: 'Skills Store' },
  'memory.dropTitle': { zh: '松开鼠标以安装技能包', en: 'Drop to install skill package' },
  'memory.dropHint': { zh: '支持 Claude Skills / OpenClaw / AgentSkills 格式的 .zip', en: 'Accepts Claude Skills / OpenClaw / AgentSkills .zip bundles' },
  'memory.enableMemory': { zh: '启用 Memory', en: 'Enable Memory' },

  // ══════════════════════════════════════
  //  Mobile Sheet
  // ══════════════════════════════════════
  'mobile.options': { zh: '选项', en: 'Options' },
  'mobile.thinkingDepth': { zh: '思考深度', en: 'Thinking Depth' },
  'mobile.off': { zh: '关闭', en: 'Off' },
  'mobile.medium': { zh: '中', en: 'Med' },
  'mobile.high': { zh: '高', en: 'High' },
  'mobile.xhigh': { zh: '超高', en: 'xHigh' },
  'mobile.max': { zh: '最大', en: 'Max' },
  'mobile.ultra': { zh: '至臻', en: 'Ultra' },
  'mobile.aiEnhance': { zh: 'AI 增强', en: 'AI Enhance' },
  'mobile.memoryInject': { zh: '记忆注入', en: 'Memory Inject' },
  'mobile.memoryInjectDesc': { zh: '注入累积经验', en: 'Inject accumulated experience' },
  'mobile.autoTranslate': { zh: '自动翻译', en: 'Auto Translate' },
  'mobile.autoTranslateDesc': { zh: '中英互译', en: 'CN↔EN translate' },
  'mobile.tools': { zh: '工具', en: 'Tools' },
  'mobile.projectAssistant': { zh: '项目助手', en: 'Project Assistant' },
  'mobile.projectAssistantDesc': { zh: '打开项目面板', en: 'Open project panel' },
  'mobile.aiCanAsk': { zh: 'AI 可向你提问', en: 'AI can ask you' },
  'mobile.backend': { zh: '后端', en: 'Backend' },
  'mobile.allFeatures': { zh: '全部功能', en: 'All features' },
  'mobile.checkingStatus': { zh: '检查中...', en: 'Checking...' },
  'mobile.modes': { zh: '模式', en: 'Modes' },
  'mobile.parallelAgents': {
    zh: '并行子代理 · 无需 Project',
    en: 'Parallel agents · no Project needed',
  },
  'mobile.autoExecLoop': { zh: '自主执行循环', en: 'Auto-exec loop' },
  'mobile.paperReader': { zh: '论文阅读', en: 'Paper Reader' },
  'mobile.paperReaderDesc': { zh: 'PDF阅读 + 问答 + 报告 + Babel PDF', en: 'PDF reading + Q&A + reports + Babel PDF' },
  'mobile.settingsDesc': { zh: '模型、提供方与偏好', en: 'Models, providers & preferences' },
  'mobile.mydayDesc': { zh: '每日活动报告', en: 'Daily activity report' },
  'mobile.studioDesc': { zh: '设计多代理工作流', en: 'Design multi-agent workflows' },
  'mobile.tasksDesc': { zh: '查看与重开编排任务', en: 'Watch & reopen orchestration runs' },
  'mobile.flowDesc': { zh: '选择编排流程', en: 'Pick an orchestration flow' },
  'mobile.monitoring': { zh: '监控', en: 'Monitoring' },
  'mobile.timerDesc': { zh: '后台定时观察器', en: 'Background timer watchers' },
  'mobile.optimizerDesc': { zh: '自主改进提案', en: 'Autonomous improvement proposals' },

  // ══════════════════════════════════════
  //  Feature lazy-load
  // ══════════════════════════════════════
  'feature.loadFailed': { zh: '功能模块加载失败，请检查网络后重试', en: 'Feature failed to load — check your connection and retry' },

  // ══════════════════════════════════════
  //  Paper Reader
  // ══════════════════════════════════════
  'paper.title': { zh: '论文阅读', en: 'Paper Reader' },
  'paper.imageZoomHint': { zh: '放大图片', en: 'enlarge image' },
  'paper.readerSettings': { zh: '阅读舒适度', en: 'Reading comfort' },
  'paper.readerFontSmaller': { zh: '缩小正文', en: 'Smaller text' },
  'paper.readerFontLarger': { zh: '放大正文', en: 'Larger text' },
  'paper.readerWidth': { zh: '阅读宽度', en: 'Reading width' },
  'paper.readerWidthNarrow': { zh: '窄', en: 'Narrow' },
  'paper.readerWidthComfortable': { zh: '适中', en: 'Comfortable' },
  'paper.readerWidthWide': { zh: '宽', en: 'Wide' },
  'paper.noPaperOpen': { zh: '未打开论文', en: 'No paper open' },
  'paper.pages': { zh: '{count} 页', en: '{count} pages' },
  'paper.myPapers': { zh: '我的论文', en: 'My Papers' },
  'paper.addPaper': { zh: '添加论文', en: 'Add paper' },
  'paper.newFolder': { zh: '新建文件夹', en: 'New folder' },
  'paper.folderNamePrompt': { zh: '文件夹名称', en: 'Folder name' },
  'paper.folderRenamePrompt': { zh: '重命名文件夹', en: 'Rename folder' },
  'paper.folderRename': { zh: '重命名', en: 'Rename' },
  'paper.folderOpen': { zh: '打开文件夹', en: 'Open folder' },
  'paper.folderBackAll': { zh: '← 全部论文', en: '← All papers' },
  'paper.folderEmpty': { zh: '此文件夹暂无论文', en: 'No papers in this folder yet' },
  'paper.folderNone': { zh: '无文件夹', en: 'No folder' },
  'paper.folderMoveTo': { zh: '移动到文件夹', en: 'Move to folder' },
  'paper.folderDeleteConfirm': { zh: '删除文件夹“{name}”？其中的论文将被移出，不会被删除。', en: 'Delete folder "{name}"? Papers inside are moved out, not deleted.' },
  'paper.loadingLibrary': { zh: '正在加载书架…', en: 'Loading bookshelf…' },
  'paper.noPapersYet': { zh: '暂无论文', en: 'No papers yet' },
  'paper.noPapersHint': { zh: '上传 PDF 或从 arXiv 获取', en: 'Upload a PDF or fetch from arXiv' },
  'paper.landingDesc': { zh: '上传 PDF 或粘贴 arXiv 链接以开始', en: 'Upload a PDF or paste an arXiv URL to get started' },
  'paper.uploadPdf': { zh: '上传 PDF', en: 'Upload PDF' },
  'paper.arxivPlaceholder': { zh: '搜索标题，或粘贴 arXiv 链接 / 编号', en: 'Search by title, or paste an arXiv URL / ID' },
  'paper.fetch': { zh: '获取', en: 'Fetch' },
  'paper.search': { zh: '搜索', en: 'Search' },
  'paper.searching': { zh: '正在搜索 arXiv…', en: 'Searching arXiv…' },
  'paper.searchNoResults': { zh: '未找到匹配的论文，请换个关键词试试。', en: 'No matching papers found. Try different keywords.' },
  'paper.searchFailed': { zh: 'arXiv 搜索失败，请稍后重试。', en: 'arXiv search failed. Please try again.' },
  'paper.searchResultsTitle': { zh: 'arXiv 候选论文', en: 'arXiv candidates' },
  'paper.searchResultsHint': { zh: '点击任意论文即可加载阅读', en: 'Click any paper to load it' },
  'paper.searchBack': { zh: '返回', en: 'Back' },
  // Describe-to-recommend (fuzzy description → grounded arXiv papers)
  'paper.describeOr': { zh: '或', en: 'or' },
  'paper.describeLabel': { zh: '记不清标题？描述一下这篇论文', en: 'Forgot the title? Describe the paper' },
  'paper.describePlaceholder': { zh: '例如：今年 NeurIPS 有几篇关于扩散语言模型的论文拿了最佳/杰出论文，但我不记得标题了……', en: 'e.g. a few diffusion language model papers won Best/Outstanding at NeurIPS this year, but I forget the titles…' },
  'paper.describeBtn': { zh: '推荐论文', en: 'Recommend papers' },
  // Auto-research (direction → harvest/survey/ideate). Shares the landing
  // describe box: recommend FINDS papers, research RUNS the whole pipeline.
  'paper.research.startBtn': { zh: '自动科研', en: 'Auto-research' },
  'paper.research.subtitle': { zh: '爬取文献 → 综述与空白地图 → 新颖性打分', en: 'Harvest → survey + gap map → novelty-scored ideas' },
  'paper.research.running': { zh: '正在研究…', en: 'Researching…' },
  'paper.research.finished': { zh: '研究完成', en: 'Research complete' },
  'paper.research.abort': { zh: '停止', en: 'Stop' },
  'paper.research.harvest': { zh: '爬取入库', en: 'Harvest' },
  'paper.research.survey': { zh: '综述与空白地图', en: 'Survey' },
  'paper.research.ideate': { zh: '创新点与新颖性闸', en: 'Ideate' },
  'paper.research.openFolder': { zh: '打开文献文件夹', en: 'Open paper folder' },
  // The finished-panel artifact surface. The pipeline pays many LLM calls for
  // these; before this they were fetched and then dropped by the renderer,
  // which showed only three integers.
  'paper.research.acceptedTitle': { zh: '通过闸的创新点', en: 'Ideas that cleared the gate' },
  'paper.research.untitled': { zh: '(无标题)', en: '(untitled)' },
  'paper.research.mechanism': { zh: '机理', en: 'Mechanism' },
  'paper.research.novelty': { zh: '新颖性主张', en: 'Novelty claim' },
  'paper.research.prediction': { zh: '可证伪预测', en: 'Falsifiable prediction' },
  'paper.research.whyNotAB': { zh: '为何不是缝合', en: 'Why not A+B' },
  'paper.research.noIdeas': { zh: '本次没有创新点通过闸 —— 宁缺毋滥,这是诚实的结果,不是故障。', en: 'No idea cleared the gate this run — an honest zero, not a failure.' },
  // Collapsed by default: a 0-accepted / 6-rejected run must be legible at a
  // glance without a wall of failures dominating the panel.
  'paper.research.rejectedSummary': { zh: '{n} 个被淘汰(最高 {best} / 阈值 {threshold})', en: '{n} rejected (best {best} / threshold {threshold})' },
  'paper.research.rejectReason': { zh: '淘汰理由', en: 'Reason' },
  'paper.research.rejectStage': { zh: '淘汰于', en: 'Gate' },
  'paper.research.surveyTitle': { zh: '综述全文', en: 'Full survey' },
  'paper.research.gapsTitle': { zh: '空白地图', en: 'Open-gap map' },
  // Past-research index. The persisted rows are keyed by a ONE-WAY hash of the
  // direction, so without this list a user who forgot their exact wording
  // could never reach their own artifacts again.
  'paper.research.recentTitle': { zh: '最近的研究', en: 'Recent research' },
  'paper.research.recentHint': { zh: '点击任意方向即可重新打开它的产物', en: 'Click any direction to reopen its artifacts' },
  'paper.research.recentCounts': { zh: '{accepted} 个创新点 / {rejected} 个淘汰', en: '{accepted} ideas / {rejected} rejected' },
  'paper.research.restoring': { zh: '正在读取已保存的研究…', en: 'Loading saved research…' },
  'paper.research.restoreFailed': { zh: '没有找到该方向的已保存研究', en: 'No saved research found for this direction' },
  // A degraded job keeps status='done' by design, so this banner is the ONLY
  // honest signal that the pipeline misfired (e.g. the structural gate
  // rejected every idea before the expensive gates ever ran).
  'paper.research.degraded': { zh: '产物可用，但流水线有问题', en: 'Delivered, but the pipeline was degraded' },
  'paper.recommending': { zh: '正在理解你的描述并核对 arXiv…', en: 'Interpreting your description & verifying against arXiv…' },
  'paper.recommendInterpreting': { zh: '正在理解你的描述…', en: 'Interpreting your description…' },
  'paper.recommendResearching': { zh: '正在检索最新文献（第 {n} 次搜索）…', en: 'Researching current literature (search {n})…' },
  'paper.recommendGrounding': { zh: '正在 arXiv 核实（{n}/{total}）…', en: 'Verifying against arXiv ({n}/{total})…' },
  'paper.recommendTitle': { zh: '为你推荐的论文', en: 'Recommended papers' },
  'paper.recommendHint': { zh: '每篇都已在 arXiv 核实，点击即可加载', en: 'Each is verified on arXiv — click to load' },
  'paper.recommendNoResults': { zh: '未能在 arXiv 上核实到匹配的论文，换个描述再试试。', en: 'Could not verify a matching paper on arXiv. Try describing it differently.' },
  'paper.recommendFailed': { zh: '推荐失败，请稍后重试。', en: 'Recommendation failed. Please try again.' },
  'paper.correctionTitle': { zh: '需要澄清一下', en: 'A quick correction' },
  'paper.correctionActual': { zh: '实际获奖的论文', en: 'The paper that actually won' },
  'paper.delete': { zh: '删除', en: 'Delete' },
  'paper.recommended': { zh: '推荐', en: 'Rec' },
  'paper.tabQA': { zh: '问答', en: 'Q&A' },
  'paper.tabReport': { zh: '报告', en: 'Report' },
  'paper.tabReview': { zh: '审稿', en: 'Review' },
  'paper.tabBabel': { zh: 'Babel PDF', en: 'Babel PDF' },
  'paper.viewPdf': { zh: '原文', en: 'PDF' },
  'paper.viewReader': { zh: '阅读', en: 'Reader' },
  'paper.qaEmptyTitle': { zh: '就本论文提问', en: 'Ask questions about this paper' },
  'paper.qaEmptyHint': { zh: '在 PDF 或报告中选中文字以提问，或在下方输入问题', en: 'Select text in the PDF or report to ask about it, or type a question below' },
  'paper.qaInputPlaceholder': { zh: '就本论文提一个问题…', en: 'Ask a question about this paper…' },
  'paper.qaError': { zh: '出错', en: 'Error' },
  'paper.qaExpired': { zh: '问答任务已过期，请重试。', en: 'Q&A task expired. Please ask again.' },
  'paper.fitWidth': { zh: '适应宽度', en: 'Fit to width' },
  'paper.zoomOut': { zh: '缩小', en: 'Zoom out' },
  'paper.zoomIn': { zh: '放大', en: 'Zoom in' },
  'paper.send': { zh: '发送（回车）', en: 'Send (Enter)' },
  // Report tab
  'paper.reportLangTitle': { zh: '报告语言（按论文记忆）', en: 'Report language (remembered per paper)' },
  'paper.reportSelectModelTitle': { zh: '选择生成报告的模型', en: 'Select model for report generation' },
  'paper.reportSelectModel': { zh: '选择模型', en: 'Select model' },
  'paper.reportRegenerate': { zh: '重新生成', en: 'Regenerate' },
  'paper.reportStop': { zh: '停止', en: 'Stop' },
  'paper.reportStopping': { zh: '正在停止…', en: 'Stopping…' },
  'paper.reportStopped': { zh: '已停止生成', en: 'Generation stopped' },
  'paper.reportStoppedHint': { zh: '点击「重新生成」可重新开始', en: 'Click Regenerate to start over' },
  'paper.reportCopy': { zh: '复制', en: 'Copy' },
  'paper.reportCopied': { zh: '已复制', en: 'Copied' },
  'paper.reportExport': { zh: '导出', en: 'Export' },
  'paper.reportExportMd': { zh: 'Markdown (.md)', en: 'Markdown (.md)' },
  'paper.reportExportHtml': { zh: '独立 HTML', en: 'Standalone HTML' },
  'paper.reportExportPdf': { zh: '打印 / 存为 PDF', en: 'Print / Save as PDF' },
  'paper.reportEmptyTitle': { zh: '准备就绪，点击下方按钮生成分析报告', en: 'Ready — click below to generate the analysis report' },
  'paper.reportEmptyHint': { zh: '生成前可先在上方调整模型与语言；模型会联网搜索补充背景与相关工作', en: 'Adjust the model & language above first if needed; the model will search the web for additional context and related work' },
  'paper.reportGenerate': { zh: '生成报告', en: 'Generate report' },
  'paper.loadingReport': { zh: '正在加载报告…', en: 'Loading report…' },
  'paper.reviewGenerate': { zh: '生成审稿', en: 'Generate review' },
  // Review tab (peer-review mode)
  // Rebuttal follow-up (author response → reviewer reply + score decision)
  'paper.rebuttalSectionTitle': { zh: '作者 Rebuttal → 后续回复', en: 'Author rebuttal → follow-up reply' },
  'paper.reviewSegReview': { zh: '审稿报告', en: 'Review' },
  'paper.reviewSegRebuttal': { zh: '作者回复', en: 'Rebuttal' },
  'paper.reviewSegHasReply': { zh: '已有后续回复', en: 'Follow-up reply available' },
  'paper.reviewSegAria': { zh: '审稿子视图', en: 'Review sub-view' },
  'paper.rebuttalHint': { zh: '把作者的 rebuttal 粘贴到下方。审稿人会逐点回复，并判断 OA / Confidence 是否应调整（多数 rebuttal 不应改分）。', en: "Paste the authors' rebuttal below. The reviewer replies point by point and decides whether the OA / Confidence score should change (most rebuttals should not)." },
  'paper.rebuttalPlaceholder': { zh: '在此粘贴作者的 rebuttal…', en: 'Paste the author rebuttal here…' },
  'paper.rebuttalGenerate': { zh: '生成后续回复', en: 'Generate follow-up' },
  'paper.rebuttalNeedText': { zh: '请先粘贴作者的 rebuttal', en: 'Paste the author rebuttal first' },
  'paper.rebuttalNeedReview': { zh: '请先生成审稿意见', en: 'Generate the review first' },
  'paper.rebuttalScoreChanged': { zh: '评分已调整', en: 'Score adjusted' },
  'paper.rebuttalScoreUnchanged': { zh: '评分不变', en: 'Score unchanged' },
  'paper.rebuttalOverall': { zh: '总评分 (OA)', en: 'Overall (OA)' },
  'paper.rebuttalConfidence': { zh: '置信度', en: 'Confidence' },
  // OpenReview auto-fill (killer feature)
  'paper.autofillBtn': { zh: '自动填 OpenReview', en: 'Auto-fill OpenReview' },
  'paper.autofillTitle': { zh: '在当前打开的 OpenReview 页自动填入审稿（绝不自动提交，填好后请你自己点提交）', en: 'Auto-fill the review on the open OpenReview tab (never submits — you click Submit yourself)' },
  'paper.autofillNoPaper': { zh: '请先打开一篇论文', en: 'Open a paper first' },
  'paper.autofillWorking': { zh: '正在填写 OpenReview 表单…', en: 'Filling the OpenReview form…' },
  'paper.autofillDone': { zh: '已填入 {n} 个字段。未提交——请核对表单后自行点击 Submit。', en: 'Filled {n} field(s). NOT submitted — review the form and click Submit yourself.' },
  'paper.autofillFailed': { zh: '自动填写未能完成', en: 'Auto-fill could not complete' },
  // Review tab (peer-review mode)
  'paper.reviewSelectVenue': { zh: '选择会议', en: 'Select venue' },
  'paper.reviewSelectVenueTitle': { zh: '选择目标会议（评审表单与评分量表）', en: 'Select target conference (review form & rating scale)' },
  'paper.reviewEmptyTitle': { zh: '先在上方选择目标会议，再点击下方按钮生成审稿意见', en: 'Pick a target conference above, then click below to generate a peer review' },
  'paper.reviewEmptyHint': { zh: '模型会按该会议真实评审表打分，并联网核验代码/数据是否真的公开', en: "The model reviews against the venue's real scorecard and verifies code/data claims via web search" },
  // Review is generated in English (submission language); zh UI can read a translation.
  'paper.reviewTranslate': { zh: '翻译', en: 'Translate' },
  'paper.reviewTranslating': { zh: '翻译中…', en: 'Translating…' },
  'paper.reviewShowOriginal': { zh: '显示原文', en: 'Show original' },
  'paper.reviewTranslateTitle': { zh: '审稿始终以英文生成、缓存、导出（投稿语言）；选「中」可切换中文阅读视图，选「EN」恢复原文', en: 'Review is always generated, cached & exported in English (submission language); pick 中 to read in Chinese, EN to restore the original' },
  'paper.reviewTranslateFailed': { zh: '翻译失败', en: 'Translation failed' },
  'paper.reportNoText': { zh: '暂无论文文本，请先加载 PDF。', en: 'No paper text available. Load a PDF first.' },
  'paper.retry': { zh: '重试', en: 'Retry' },
  // Video Abstract tab (paper video: report → narrated MG video; P3)
  'paper.tabVideo': { zh: '视频', en: 'Video' },
  'paper.videoHint': { zh: '把这篇论文变成一条配音 MG 动画短视频——分镜、图表与动态文字。', en: 'A short narrated motion-graphic video of this paper — beats, charts and kinetic type.' },
  'paper.videoHeroTitle': { zh: '把这篇论文看给你', en: 'Watch this paper' },
  'paper.videoNeedReport': { zh: '视频摘要由分析报告改编——请先生成报告。', en: 'The video abstract is adapted from the analysis report — generate the report first.' },
  'paper.videoGoReport': { zh: '去生成报告', en: 'Go generate the report' },
  'paper.videoGenerate': { zh: '生成视频', en: 'Generate video' },
  'paper.videoNarration': { zh: '配音', en: 'Narration' },
  'paper.videoBurnIn': { zh: '烧录字幕', en: 'Burn-in subtitles' },
  'paper.videoQualityDraft': { zh: '草稿（快）', en: 'Draft (fast)' },
  'paper.videoQualityStandard': { zh: '标准', en: 'Standard' },
  'paper.videoQualityHigh': { zh: '高画质', en: 'High' },
  'paper.videoStarting': { zh: '正在启动…', en: 'Starting…' },
  'paper.videoFailed': { zh: '视频生成失败', en: 'Video generation failed' },
  'paper.videoLookupFailed': { zh: '视频状态查询失败——请查看服务器日志。', en: 'Video status lookup failed — check the server log.' },
  'paper.videoRetry': { zh: '重试', en: 'Retry' },
  'paper.videoDownload': { zh: '下载视频', en: 'Download video' },
  'paper.videoDownloadSrt': { zh: '下载字幕', en: 'Download SRT' },
  'paper.videoSilent': { zh: '无声', en: 'silent' },
  'paper.videoNoTts': { zh: '未配置 TTS 语音槽位——本次将生成无声视频。', en: 'No TTS voice slot is configured — this run generates a silent video.' },
  'paper.videoScenesTitle': { zh: '分镜——预览或单独重渲', en: 'Scenes — preview or re-render one' },
  'paper.videoRegen': { zh: '重渲', en: 'Re-render' },
  'paper.videoRegening': { zh: '重渲中…', en: 'Re-rendering…' },
  'paper.videoPhaseParse': { zh: '解析字幕', en: 'Parsing subtitles' },
  'paper.videoPhaseStoryboard': { zh: '分镜中', en: 'Storyboarding' },
  'paper.videoPhaseNarrate': { zh: '配音中', en: 'Voicing scenes' },
  'paper.videoPhaseCompose': { zh: '合成镜头', en: 'Composing scenes' },
  'paper.videoPhaseRender': { zh: '渲染镜头', en: 'Rendering scenes' },
  'paper.videoPhaseConcat': { zh: '拼接镜头', en: 'Joining scenes' },
  'paper.videoPhaseMux': { zh: '混流中', en: 'Mixing audio' },
  'paper.videoPhaseBurnIn': { zh: '烧录字幕', en: 'Burning subtitles' },
  'paper.videoPhaseRegen': { zh: '重渲镜头', en: 'Re-rendering scene' },

  // Podcast tab (paper podcast: report → spoken script → TTS audio)
  'paper.tabPodcast': { zh: '播客', en: 'Podcast' },
  'paper.podcastHint': { zh: '把这篇论文变成一档单人有声精读课——适合通勤路上或睡前听。', en: 'A solo spoken deep-read of this paper — for the commute or before sleep.' },
  'paper.podcastModeShort': { zh: '短版 · 约 5 分钟', en: 'Short · ~5 min' },
  'paper.podcastModeFull': { zh: '完整 · 约 15 分钟', en: 'Full · ~15 min' },
  'paper.podcastVoice': { zh: '音色（可选）', en: 'voice (optional)' },
  'paper.podcastGenerate': { zh: '生成播客', en: 'Generate podcast' },
  'paper.podcastRegenerate': { zh: '重新生成', en: 'Regenerate' },
  'paper.podcastAbort': { zh: '中止', en: 'Abort' },
  'paper.podcastScriptPhase': { zh: '正在撰写口播稿…', en: 'Writing the spoken script…' },
  'paper.podcastAudioPhase': { zh: '正在合成语音', en: 'Synthesizing audio' },
  'paper.podcastPhaseSource': { zh: '素材', en: 'Material' },
  'paper.podcastPhaseScript': { zh: '剧本', en: 'Script' },
  'paper.podcastPhaseAudio': { zh: '配音', en: 'Voice-over' },
  'paper.podcastStepDraft': { zh: '初稿完成', en: 'draft done' },
  'paper.podcastStepValidate': { zh: '质检中', en: 'checking quality' },
  'paper.podcastStepRevise': { zh: '修订中', en: 'revising' },
  'paper.podcastStepCritic': { zh: '审听编辑中', en: 'editor review' },
  'paper.podcastStreamSegments': { zh: '小节', en: 'segment' },
  'paper.podcastStreamChars': { zh: '字', en: 'chars' },
  'paper.podcastLost': { zh: '任务丢失或连接已断开——生成任务的状态已无法查询。', en: 'Task lost or connection dropped — the generation task can no longer be reached.' },
  'paper.podcastInterrupted': { zh: '上次生成被服务器重启打断。', en: 'The last generation was cut short by a server restart.' },
  'paper.podcastRecheck': { zh: '重新查询状态', en: 'Re-check status' },
  'paper.mediaElapsed': { zh: '已用', en: 'elapsed' },
  'paper.mediaLastActive': { zh: '最后活动', en: 'last activity' },
  'paper.mediaStillRunning': { zh: '仍在运行(这一步通常要几分钟)', en: 'still running (this step can take minutes)' },
  'paper.mediaEtaPrefix': { zh: '约', en: '≈' },
  'paper.podcastNeedReport': { zh: '播客由分析报告改编——请先生成报告。', en: 'The podcast is adapted from the analysis report — generate the report first.' },
  'paper.podcastGoReport': { zh: '去生成报告', en: 'Go generate the report' },
  'paper.podcastHeroTitle': { zh: '把这篇论文讲给你听', en: 'Listen to this paper' },
  'paper.podcastStepReport': { zh: '1. 生成报告', en: '1. Generate the report' },
  'paper.podcastStepPodcast': { zh: '2. 改编播客', en: '2. Adapt into a podcast' },
  'paper.podcastLookupFailed': { zh: '播客状态查询失败——请查看服务器日志。', en: 'Podcast status lookup failed — check the server log.' },
  'paper.podcastRetry': { zh: '重试', en: 'Retry' },
  'paper.podcastNoTts': { zh: '未配置 TTS 语音合成槽位——本次仅生成剧本与逐字稿（无音频）。', en: 'No TTS voice slot is configured — this run generates the script + transcript only (no audio).' },
  'paper.podcastLowConfidence': { zh: '质检未完全通过，部分内容可能不够精确。', en: 'QA gates did not fully pass — some content may be imprecise.' },
  'paper.podcastDownloadAudio': { zh: '下载音频', en: 'Download audio' },
  'paper.podcastExportScript': { zh: '导出剧本 (md)', en: 'Export script (md)' },
  'paper.podcastSleepTimer': { zh: '睡眠定时', en: 'Sleep timer' },
  'paper.podcastSleepOff': { zh: '关闭', en: 'Off' },
  'paper.podcastSleepMin': { zh: '分钟', en: 'min' },
  'paper.podcastFailed': { zh: '播客生成失败', en: 'Podcast generation failed' },
  // ── Media studio console (podcast + video tabs, 2026-07 redesign) ──
  'paper.podcastStudioTitle': { zh: '播客演播室', en: 'Podcast studio' },
  'paper.videoStudioTitle': { zh: '视频制片台', en: 'Video studio' },
  'paper.podcastMakingTitle': { zh: '播客制作中', en: 'Producing your podcast' },
  'paper.videoMakingTitle': { zh: '视频制作中', en: 'Producing your video' },
  'paper.mediaOptDuration': { zh: '时长', en: 'Duration' },
  'paper.mediaOptLang': { zh: '语言', en: 'Language' },
  'paper.mediaOptVoice': { zh: '音色', en: 'Voice' },
  'paper.mediaOptModel': { zh: '模型', en: 'Model' },
  'paper.mediaModelTitle': { zh: '生成所用的模型', en: 'Model used for generation' },
  'paper.mediaOptQuality': { zh: '画质', en: 'Quality' },
  'paper.mediaOptExtras': { zh: '选项', en: 'Options' },
  'paper.mediaOptional': { zh: '可选', en: 'optional' },
  'paper.podcastModeShortName': { zh: '短版速听', en: 'Quick brief' },
  'paper.podcastModeShortSub': { zh: '约 5 分钟 · 通勤路上', en: '~5 min · for the commute' },
  'paper.podcastModeFullName': { zh: '完整深读', en: 'Full deep-read' },
  'paper.podcastModeFullSub': { zh: '约 15 分钟 · 睡前细听', en: '~15 min · before sleep' },
  'paper.videoQualityDraftSub': { zh: '快速预览', en: 'fast preview' },
  'paper.videoQualityStandardSub': { zh: '推荐', en: 'recommended' },
  'paper.videoQualityHighSub': { zh: '更慢 · 更精细', en: 'slower, finer' },
  'paper.videoNarrationSub': { zh: 'TTS 语音旁白', en: 'TTS voice-over' },
  'paper.videoBurnInSub': { zh: '字幕嵌入画面', en: 'subtitles baked into the frame' },
  // Composition tier — a SEPARATE axis from the draft/standard/high RENDER
  // preset above (that one is bitrate/scale and says nothing about layout).
  'paper.videoVisual': { zh: '画面构图', en: 'Composition' },
  'paper.videoVisualAuthored': { zh: '精心设计（推荐）', en: 'Designed (recommended)' },
  'paper.videoVisualAuthoredSub': { zh: '每个镜头单独排版', en: 'bespoke layout per scene' },
  'paper.videoVisualTemplate': { zh: '纯文字卡', en: 'Plain cards' },
  'paper.videoVisualTemplateSub': { zh: '最快 · 每张一行字', en: 'fastest, one line per card' },
  // Product-quality axis: the film played, but is not what was asked for.
  'paper.videoDegraded': { zh: '这部片子生成完成了，但没有达到所要求的画质。',
                           en: 'This film played, but was not produced at the quality requested.' },
  'paper.podcastTranscriptTitle': { zh: '逐字稿', en: 'Transcript' },
  // Reading-time estimate + progress bar (Report tab)
  'paper.readTimeTotal': { zh: '阅读时长约 {min}', en: '{min} read' },
  'paper.readTimeLeft': { zh: '剩余约 {min}', en: '{min} left' },
  'paper.readTimeDone': { zh: '已读完', en: 'Finished' },
  'paper.readTimeMin': { zh: '{n} 分钟', en: '{n} min' },
  'paper.readTimeLessMin': { zh: '不到 1 分钟', en: 'under 1 min' },
  'paper.readTimeHour': { zh: '{h} 小时 {m} 分', en: '{h} h {m} min' },
  'paper.readTimeAdapted': { zh: '已按你的阅读速度校准（约 {wpm} 词/分）', en: 'Calibrated to your reading speed (~{wpm} wpm)' },
  'paper.readTimeDefault': { zh: '按平均阅读速度估算', en: 'Estimated at average reading speed' },
  // Babel PDF tab
  'paper.babelSubtitle': { zh: '学术论文翻译', en: 'Academic paper translation' },
  'paper.babelOriginal': { zh: '原文', en: 'Original' },
  'paper.babelEmptyTitle': { zh: '选择目标语言以翻译论文', en: 'Select a target language to translate the paper' },
  'paper.babelEmptyHint': { zh: '翻译会通过 LLM 逐段进行', en: 'Translation runs section by section via LLM' },
  'paper.babelNoPaper': { zh: '尚未加载论文，请先上传 PDF。', en: 'No paper loaded. Upload a PDF first.' },
  'paper.babelTranslatingTo': { zh: '正在翻译为{lang}…', en: 'Translating to {lang}…' },
  'paper.babelTranslatedCount': { zh: '已翻译 {done}/{total} 段', en: 'Translated {done}/{total} sections' },
  'paper.babelComplete': { zh: '翻译完成', en: 'Translation complete' },
  'paper.babelCompleteCached': { zh: '翻译完成（缓存）', en: 'Translation complete (cached)' },
  'paper.babelFailed': { zh: '翻译失败', en: 'Translation failed' },
  // ── Report cards: TOC, citation-audit, terminology-audit, finish-tag (paper/report.js) ──
  'paper.reportTocLabel': { zh: '目录', en: 'Contents' },
  'paper.citeAuditTitle': { zh: '引用完整性警告 — {n} 条可疑引用', en: 'Citation integrity — {n} suspicious reference(s)' },
  'paper.citeAuditSub': { zh: '已核验 {total} 条引用标识符：{verified} 条确认存在、{suspicious} 条可疑、{unverifiable} 条无法核验（无法核验 ≠ 虚构）。', en: 'Checked {total} cited identifiers: {verified} verified, {suspicious} suspicious, {unverifiable} unverifiable (unverifiable ≠ fabricated).' },
  'paper.citeDidNotResolve': { zh: '未能解析', en: 'did not resolve' },
  'paper.citeCheckedSource': { zh: '查证来源', en: 'checked source' },
  'paper.citeClaimed': { zh: '声称：', en: 'claimed: ' },
  'paper.citeResolvesTo': { zh: '实际解析为：', en: 'resolves to: ' },
  'paper.termAuditTitle': { zh: '术语完整性 — {n} 处未定义', en: 'Terminology completeness — {n} undefined term(s)' },
  'paper.termAuditSub': { zh: '术语表共 {glossaryCount} 条：{missing} 个术语在正文中出现却未收录、{dangling} 处术语表定义引用了未定义的术语。以下条目缺少可供理解的解释。', en: 'Glossary has {glossaryCount} entries: {missing} term(s) used in the body but never defined, {dangling} glossary definition(s) leaning on an undefined term. The items below lack an explanation the reader can rely on.' },
  'paper.termAppearsIn': { zh: '出现于：', en: 'appears in: ' },
  'paper.termUsedNotInGlossary': { zh: '正文使用但术语表未收录', en: 'used but not in the glossary' },
  'paper.termDanglingReason': { zh: '术语表中「{term}」的定义引用了未定义的「{referencedTerm}」', en: 'the glossary definition of “{term}” references undefined “{referencedTerm}”' },
  'paper.finishModelTitle': { zh: '生成本报告的模型', en: 'Model that generated this report' },
  'paper.finishCostTitle': { zh: '本次生成的预估费用', en: 'Estimated cost of this generation' },
  'paper.finishTokensTitle': { zh: '输入 / 输出 tokens', en: 'input / output tokens' },
  'paper.finishGeneratedBy': { zh: '由以下模型生成', en: 'Generated by' },

  // ══════════════════════════════════════
  //  MyDay
  // ══════════════════════════════════════
  'myday.title': { zh: '我的一天', en: 'My Day' },
  'myday.refresh': { zh: '生成/刷新报告', en: 'Generate/refresh report' },
  'myday.done': { zh: '完成', en: 'Done' },
  'myday.open': { zh: '未完成', en: 'Open' },
  // Date helpers
  'myday.today': { zh: '今天', en: 'Today' },
  'myday.yesterday': { zh: '昨天', en: 'Yesterday' },
  'myday.weekdays': { zh: '日,一,二,三,四,五,六', en: 'Sun,Mon,Tue,Wed,Thu,Fri,Sat' },
  'myday.weekdayPrefix': { zh: '周', en: '' },
  'myday.monthDay': { zh: '{m}月{d}日', en: '{m}/{d}' },
  'myday.yearMonth': { zh: '{y}年{m}月', en: '{y}/{m}' },
  'myday.dateFull': { zh: '{y}年{m}月{d}日', en: '{m}/{d}/{y}' },
  // Calendar day headers
  'myday.calWeek': { zh: '日,一,二,三,四,五,六', en: 'S,M,T,W,T,F,S' },
  // Status labels
  'myday.statusDone': { zh: '✓ 完成', en: '✓ Done' },
  'myday.statusInProgress': { zh: '进行中', en: 'In Progress' },
  'myday.statusBlocked': { zh: '受阻', en: 'Blocked' },
  'myday.statusIncomplete': { zh: '进行中', en: 'In Progress' },
  'myday.toggleStatus': { zh: '切换状态', en: 'Toggle status' },
  // Section labels
  'myday.todayTodos': { zh: '今日待办', en: "Today's TODOs" },
  'myday.unfinishedSection': { zh: '未完成', en: 'Unfinished' },
  'myday.activeSection': { zh: '进行中', en: 'In Progress' },
  'myday.doneSection': { zh: '已完成', en: 'Completed' },
  'myday.tomorrowPlan': { zh: '明日计划', en: "Tomorrow's Plan" },
  'myday.nextDayPlan': { zh: '次日计划', en: 'Next Day Plan' },
  'myday.todoItems': { zh: '待办事项', en: 'TODOs' },
  // Stream info
  'myday.convCount': { zh: '{n} 个对话', en: '{n} conversations' },
  'myday.independentConvs': { zh: '{n} 个独立对话', en: '{n} independent conversations' },
  // Waiting / empty
  'myday.reportNotGenerated': { zh: '报告尚未生成', en: 'Report not generated yet' },
  'myday.checkingConvs': { zh: '正在查询对话数量…', en: 'Checking conversation count…' },
  'myday.hasConvsHint': { zh: '有 {n} 个对话，点击上方刷新按钮或下方按钮生成报告', en: '{n} conversations found — click refresh or the button below to generate' },
  'myday.noConvsHint': { zh: '还没有对话记录，开始聊天后可以生成报告', en: 'No conversations yet — start chatting to generate a report' },
  'myday.generateBtn': { zh: '生成报告', en: 'Generate Report' },
  'myday.generateDaily': { zh: '生成日报', en: 'Generate Report' },
  'myday.quietDay': { zh: '这天很安静', en: 'A quiet day' },
  'myday.noConvsFound': { zh: '没有找到对话记录', en: 'No conversations found' },
  // Progress stages
  'myday.generating': { zh: '正在生成报告', en: 'Generating report' },
  'myday.stageStarting': { zh: '正在启动…', en: 'Starting…' },
  'myday.stageExtracting': { zh: '扫描对话', en: 'Scanning conversations' },
  'myday.stageAnalyzing': { zh: 'AI 分析', en: 'AI Analysis' },
  'myday.stageSaving': { zh: '保存报告', en: 'Saving report' },
  'myday.stageScanMsg': { zh: '扫描对话 {c}/{t}', en: 'Scanning {c}/{t}' },
  'myday.stageAnalyzeMsg': { zh: 'LLM 分析 {n} 个对话…', en: 'Analyzing {n} conversations…' },
  'myday.stageSaveMsg': { zh: '保存报告…', en: 'Saving report…' },
  'myday.genHint': { zh: '你可以切换到其他日期，生成不会中断', en: 'You can switch dates — generation continues in background' },
  'myday.genFailed': { zh: '生成失败', en: 'Generation failed' },
  'myday.genFailRetry': { zh: '启动生成失败，请重试', en: 'Failed to start generation, please retry' },
  'myday.analyzing': { zh: '分析中…', en: 'Analyzing…' },
  // Stats
  'myday.convStat': { zh: '{n} 对话', en: '{n} convs' },
  'myday.streamStat': { zh: '{n} 工作流', en: '{n} streams' },
  // Badges
  'myday.badgeYesterday': { zh: '昨日', en: 'Yesterday' },
  'myday.badgeCarried': { zh: '延续', en: 'Carried' },
  // TODO actions
  'myday.addPlaceholder': { zh: '添加待办…', en: 'Add a task…' },
  'myday.markDone': { zh: '标记完成', en: 'Mark done' },
  'myday.markUndone': { zh: '标记未完成', en: 'Mark undone' },
  'myday.startConv': { zh: '开始对话', en: 'Start conversation' },
  'myday.deleteTodo': { zh: '删除', en: 'Delete' },
  // Inherited prompt
  'myday.hasConvsToday': { zh: '今日已有 {n} 个对话', en: '{n} conversations today' },
  // Close button
  'myday.close': { zh: '关闭', en: 'Close' },
  // Misc streams
  'myday.miscQA': { zh: '零碎问答', en: 'Misc Q&A' },
  // Reminder toast
  'myday.reminderTitle': { zh: '查看今日日报', en: 'Check your daily report' },
  'myday.reminderBody': { zh: '今天有 {n} 个对话，来看看你的工作总结吧', en: "You had {n} conversations today — review your summary" },
  'myday.reminderBodyGeneric': { zh: '来看看今天的工作总结吧', en: 'Review your daily work summary' },

  // ══════════════════════════════════════
  //  Conversation Actions
  // ══════════════════════════════════════
  'conv.copyFailed': { zh: '复制失败', en: 'Copy failed' },
  'conv.cannotLoadOriginal': { zh: '无法加载原始对话内容', en: 'Cannot load original conversation content' },
  'conv.copying': { zh: '对话复制中…', en: 'Copying conversation…' },
  'conv.copied': { zh: '对话已复制 ✓', en: 'Conversation copied ✓' },
  'conv.copy': { zh: '副本', en: 'Copy' },
  'conv.messages': { zh: '条对话', en: 'messages' },
  'conv.quote': { zh: '引用', en: 'Quote' },
  'conv.quoteConv': { zh: '引用对话', en: 'Quote conversation' },
  'conv.branch': { zh: '分支', en: 'Branch' },
  'conv.reply': { zh: '引用', en: 'Quote' },

  // ══════════════════════════════════════
  //  Folders
  // ══════════════════════════════════════
  'folder.moveToFolder': { zh: '移入文件夹', en: 'Move to folder' },
  'folder.removeFromFolder': { zh: '移出文件夹', en: 'Remove from folder' },
  'folder.newFolder': { zh: '新建文件夹', en: 'New Folder' },
  'folder.movedToFolder': { zh: '已移入文件夹', en: 'Moved to folder' },
  'folder.removedFromFolder': { zh: '已移出文件夹', en: 'Removed from folder' },
  'folder.createTitle': { zh: '新建文件夹', en: 'New Folder' },
  'folder.namePh': { zh: '文件夹名称', en: 'Folder name' },
  'folder.cancel': { zh: '取消', en: 'Cancel' },
  'folder.create': { zh: '创建', en: 'Create' },
  'folder.creating': { zh: '创建中…', en: 'Creating…' },
  'folder.createFailed': { zh: '创建失败', en: 'Create failed' },
  'folder.cannotCreate': { zh: '无法创建文件夹', en: 'Cannot create folder' },
  'folder.created': { zh: '文件夹已创建', en: 'Folder created' },
  'folderDrop.saved': { zh: '已保存 {n} 个文件', en: 'Saved {n} file(s)' },
  'folderDrop.savedInto': { zh: '已保存到「{dir}」', en: 'Saved into “{dir}”' },
  'folderDrop.renamedNote': { zh: '{n} 个自动重命名以避免覆盖', en: '{n} auto-renamed to avoid overwrite' },
  'folderDrop.failed': { zh: '{n} 个文件保存失败', en: '{n} file(s) failed to save' },
  'folderDrop.notInWorkspace': { zh: '该目录不在工作区内', en: 'Folder not in workspace' },
  'folderDrop.addRootConfirm': { zh: '「{dir}」尚未加入工作区。要将其添加为项目文件夹并保存到这里吗？', en: '“{dir}” isn’t in your workspace yet. Add it as a project folder and save here?' },
  'folderDrop.addAndSave': { zh: '添加并保存', en: 'Add & save' },
  'folder.renameTitle': { zh: '重命名文件夹', en: 'Rename Folder' },
  'folder.ok': { zh: '确定', en: 'OK' },
  'folder.deleteTitle': { zh: '删除文件夹', en: 'Delete Folder' },
  'folder.deleteConfirm': { zh: '确定删除文件夹', en: 'Delete folder' },
  'folder.deleteHint': { zh: '文件夹内的对话不会被删除，只是变为未分类。', en: 'Conversations in the folder will not be deleted, just become uncategorized.' },
  'folder.deleted': { zh: '文件夹已删除', en: 'Folder deleted' },
  'folder.rename': { zh: '重命名', en: 'Rename' },
  'folder.deleteAction': { zh: '删除文件夹', en: 'Delete folder' },
  'folder.createInHint': { zh: '在此目录中新建文件夹：\n{dir}', en: 'Create a new folder in:\n{dir}' },
  'folder.deleteFailed': { zh: '删除失败', en: 'Delete failed' },
  'folder.deleteDirConfirm': { zh: '确定删除文件夹“{name}”吗？', en: 'Delete the folder "{name}"?' },
  'folder.deleteDirHint': { zh: '文件夹将被移入 .tofu_trash 回收站，可从文件系统恢复。', en: 'The folder is moved to a .tofu_trash bin and can be recovered from the filesystem.' },

  // ══════════════════════════════════════
  //  Translation
  // ══════════════════════════════════════
  'translate.failed': { zh: '翻译失败，点击重试', en: 'Translation failed, click to retry' },
  'translate.translatingToCN': { zh: '正在翻译为中文…', en: 'Translating to Chinese…' },
  'translate.original': { zh: '原文', en: 'Original' },
  'translate.translated': { zh: '译文', en: 'Translation' },
  // Retry/status sub-messages shown below the spinner when the backend reports
  // a transient issue (rate-limit, empty output, etc.) while retrying.
  'translate.retry.started': { zh: '正在调用翻译模型，请稍候…', en: 'Translating, please wait…' },
  'translate.retry.in_progress': { zh: '翻译仍在进行中…', en: 'Still translating…' },
  'translate.retry.rate_limited': { zh: '所有密钥被限流，正在重试…', en: 'All keys rate-limited, retrying…' },
  'translate.retry.dispatch_error': { zh: '接口错误，正在重试…', en: 'Provider error, retrying…' },
  'translate.retry.dispatch_failed_final': { zh: '接口错误重试已耗尽', en: 'Provider errors exhausted' },
  'translate.retry.empty_output': { zh: '返回为空，正在换模型重试…', en: 'Empty response, retrying with another model…' },
  'translate.retry.empty_final': { zh: '多次返回为空', en: 'Empty response after retries' },
  'translate.retry.truncated': { zh: '输出被截断，正在重试…', en: 'Output truncated, retrying…' },
  'translate.retry.truncated_final': { zh: '多次截断后返回部分结果', en: 'Output truncated after retries' },
  'translate.retry.wrong_language': { zh: '输出语言不对，正在换模型重试…', en: 'Output in wrong language, retrying with another model…' },
  'translate.retry.wrong_language_final': { zh: '多次重试后输出语言仍不对', en: 'Output in wrong language after retries' },
  'translate.retry.mt_fallback': { zh: '机器翻译失败，已切换到大模型', en: 'MT provider failed, using LLM' },
  'translate.retry.timed_out': { zh: '翻译超时，已直接发送原文', en: 'Translation timed out, sent original text' },
  // Send-path auto-translate failed: the ORIGINAL (untranslated) text was sent
  // to the model. Shown as a quiet click-to-retry notice under the user bubble.
  'translate.sendFailed.timed_out': { zh: '自动翻译超时，已按原文发送', en: 'Auto-translate timed out — sent original text' },
  'translate.sendFailed.failed': { zh: '自动翻译失败，已按原文发送', en: 'Auto-translate failed — sent original text' },
  'translate.sendFailed.retry': { zh: '重新翻译', en: 'Retranslate' },

  // ══════════════════════════════════════
  //  Time / Relative
  // ══════════════════════════════════════
  'time.secondsAgo': { zh: 's前', en: 's ago' },
  'time.minutesAgo': { zh: 'm前', en: 'm ago' },
  'time.hoursAgo': { zh: 'h前', en: 'h ago' },
  'time.daysAgo': { zh: 'd前', en: 'd ago' },
  'time.justNow': { zh: '刚刚', en: 'Just now' },
  'time.minutesAgoFull': { zh: '分钟前', en: 'min ago' },
  'time.hoursAgoFull': { zh: '小时前', en: 'hr ago' },
  'time.daysAgoFull': { zh: '天前', en: 'd ago' },

  // ══════════════════════════════════════
  //  Message Actions / Status
  // ══════════════════════════════════════
  'msg.contentFiltered': { zh: '内容违反安全政策，已被模型安全系统拦截', en: 'Content violated safety policy and was blocked' },
  'msg.prematureClose': { zh: 'API网关超时，模型深度思考中被中断。内容可能不完整。', en: 'API gateway timeout, model was interrupted during deep thinking. Content may be incomplete.' },
  'msg.gatewayInterrupt': { zh: '网关中断', en: 'Gateway interrupt' },
  'msg.abnormalStop': { zh: 'API流异常终止（连接被代理/网关中断，缺失finish标记）。回复内容可能不完整。', en: 'API stream terminated abnormally (connection interrupted by proxy/gateway, missing finish marker). Response may be incomplete.' },
  'msg.abnormalInterrupt': { zh: '异常中断', en: 'Abnormal interrupt' },
  'msg.thinking': { zh: '思', en: 'think' },
  'msg.rounds': { zh: '轮', en: 'rnd' },
  'msg.tooManyModels': { zh: '模型太多？在 <b>设置 → 模型</b> 中隐藏不需要的模型', en: 'Too many models? Hide unwanted ones in <b>Settings → Display</b>' },

  // ══════════════════════════════════════
  //  Tool activity panel (ptool-*)
  // ══════════════════════════════════════
  // {s} carries the English plural suffix ('' | 's'); zh ignores it.
  'toolPanel.working': { zh: '处理中…（{n}）', en: 'Working… ({n})' },
  'toolPanel.cmdRunning': { zh: '运行中', en: 'Running' },
  'toolPanel.toolsUsed': { zh: '使用了 {n} 个工具', en: '{n} tool{s} used' },
  'toolPanel.turnsSuffix': { zh: ' · {n} 轮', en: ' · {n} turn{s}' },
  'toolPanel.roundTag': { zh: '第{n}轮', en: 'Round {n}' },
  'toolPanel.parallelCalls': { zh: '{n} 个并行调用', en: '{n} parallel calls' },
  'toolPanel.hidden': { zh: '隐藏了 {n} 个更早的工具调用 — 点击展开', en: '{n} earlier tool calls hidden — click to expand' },

  // inspect_image transform chip (ui/tool_rounds.js — _localizeInspectOps)
  'inspect.opsTitle': { zh: '已应用的图像变换', en: 'Applied transform' },
  'inspect.fullFrame': { zh: '完整画面', en: 'full frame' },
  'inspect.cropped': { zh: '已裁剪', en: 'cropped' },
  'inspect.gridOverlay': { zh: '网格叠加', en: 'grid overlay' },
  'inspect.rotated': { zh: '旋转 {deg}', en: 'rotated {deg}' },
  'inspect.zoom': { zh: '放大 {factor}', en: 'zoom {factor}' },
  'inspect.fitTo': { zh: '缩放至 {size}', en: 'fit to {size}' },
  'inspect.opsSep': { zh: '、', en: ', ' },

  // ══════════════════════════════════════
  //  Finish-info / cost-breakdown popover (ui/finish_info.js)
  // ══════════════════════════════════════
  // Cache-break flag labels (key === backend cacheBreak key)
  'finishInfo.cb.system_prompt': { zh: 'System prompt 改变', en: 'System prompt changed' },
  'finishInfo.cb.tools': { zh: '工具定义改变', en: 'Tool definitions changed' },
  'finishInfo.cb.model': { zh: '模型切换', en: 'Model switched' },
  'finishInfo.cb.message_count': { zh: '上下文压缩', en: 'Context compacted' },
  'finishInfo.cb.server_side': { zh: '服务端缓存失效', en: 'Server-side cache invalidated' },
  'finishInfo.cb.no_cache_reuse': { zh: '上下文重新计费（缓存未复用）', en: 'Context re-billed (cache not reused)' },
  'finishInfo.cb.prefix_mutation': { zh: '缓存前缀被改写', en: 'Cached prefix rewritten' },
  'finishInfo.cbWithVal': { zh: '{label}（{val}）', en: '{label} ({val})' },
  // List separators (CJK punctuation in zh, ASCII in en)
  'finishInfo.listSep': { zh: '，', en: ', ' },
  'finishInfo.listSepSemi': { zh: '；', en: '; ' },
  'finishInfo.listSepDot': { zh: '、', en: ', ' },
  // Per-round breakdown table
  'finishInfo.apiRoundsTitle': { zh: '{n} 轮 API 调用', en: '{n} API rounds' },
  'finishInfo.fallbackSuffix': { zh: ' 回退', en: ' fallback' },
  'finishInfo.metaSum': { zh: '（计 {v}）', en: ' (total {v})' },
  // write-breakdown term chips
  'finishInfo.wbPrevOutput': { zh: '上一轮回复 {v}', en: 'prev reply {v}' },
  'finishInfo.wbToolResults': { zh: '上一轮工具结果 {v}', en: 'last round\u2019s tool results {v}' },
  // Batch-correspondence hint: makes the offset-by-one explicit on the main
  // row so a reader can cross-check the cost round against the tool panel
  // without inferring the offset. Round N's write carries batch N-1's results.
  'finishInfo.wbBatchRef': { zh: '（工具批次{n}流入）', en: ' (from tool batch {n})' },
  'finishInfo.wbContextWrite': { zh: '首次缓存上下文 {v}', en: 'first-cache context {v}' },
  'finishInfo.wbRecacheBody': { zh: '重新缓存正文 {v}', en: 're-cache body {v}' },
  'finishInfo.wbEnvelope': { zh: '消息开销 {v}', en: 'msg overhead {v}' },
  // write-breakdown tooltip (concatenated fragments; each leads with \n· )
  'finishInfo.wbTipHead': { zh: '本轮 write = {v} tok，是“自上一轮以来新写入缓存的上下文”，不是模型本轮生成的内容。它由以下几部分构成（后端按真实用量计算，相加恰好等于 write）：', en: 'This round\u2019s write = {v} tok \u2014 the context newly written to cache since the previous round, NOT what the model generated this round. It breaks down as (computed by the backend from real usage; the parts sum exactly to write):' },
  'finishInfo.wbTipPrevOutput': { zh: '\n· 上一轮回复 {v} —— 上一轮模型生成的文本/推理 + 工具调用参数', en: '\n\u00b7 prev reply {v} \u2014 text/reasoning the model generated last round + tool-call arguments' },
  'finishInfo.wbTipToolResults': { zh: '\n· 工具结果 {v} —— 上一轮各工具返回结果', en: '\n\u00b7 tool results {v} \u2014 results returned by last round\u2019s tools' },
  'finishInfo.wbTipToolResultsDetail': { zh: '（明细：{detail}；按本地分词统计，与服务端口径略有出入）', en: ' (detail: {detail}; counted with the local tokenizer, slightly differs from the server)' },
  'finishInfo.wbTipContextWrite': { zh: '\n· 首次缓存上下文 {v} —— 系统提示、工具定义与历史消息首次写入缓存（通常是首轮的前缀预热；本轮 cache 读取未下降，预期下一轮可低价复用，若未命中会在该轮标注为“重新缓存正文”）', en: '\n\u00b7 first-cache context {v} \u2014 system prompt, tool definitions and history written to cache for the first time (usually the first round\u2019s prefix warm-up; this round\u2019s cache read did not drop, so the next round should reuse it cheaply \u2014 a miss would be flagged there as \u201cre-cache body\u201d)' },
  'finishInfo.wbTipRecacheBody': { zh: '\n· 重新缓存正文 {v} —— 此前已缓存的会话正文未被读回、被重新计费（真实浪费）', en: '\n\u00b7 re-cache body {v} \u2014 previously-cached conversation body was not read back and got re-billed (real waste)' },
  'finishInfo.wbTipReadDrop': { zh: '；本轮 cache 读取较上一轮下降 {v} tok', en: '; this round\u2019s cache read dropped {v} tok vs the previous round' },
  'finishInfo.wbTipSeeBreak': { zh: '，详见下方“缓存失效”', en: ', see \u201ccache miss\u201d below' },
  'finishInfo.wbTipEnvelope': { zh: '\n· 消息开销 {v} —— 每条消息的 JSON/role 信封开销（仅占少量，已封顶）', en: '\n\u00b7 msg overhead {v} \u2014 per-message JSON/role envelope overhead (small, capped)' },
  'finishInfo.wbTipCapped': { zh: '\n（注：各分项按本地分词统计，与服务端 write 口径略有差异，已按 write 总量校准，为近似值。）', en: '\n(Note: each part is counted with the local tokenizer and differs slightly from the server\u2019s write; calibrated to the write total, approximate.)' },
  'finishInfo.wbSumApprox': { zh: '↳ write {v} 的来源（约）：{terms}', en: '\u21b3 write {v} sources (approx): {terms}' },
  'finishInfo.wbSum': { zh: '↳ write {v} 的来源：{terms}', en: '\u21b3 write {v} sources: {terms}' },
  // Legacy inflow fallback (rounds persisted before writeBreakdown shipped)
  'finishInfo.inflowTip': { zh: '本轮 write 里含上一轮 {n} 个工具的返回结果（首次写入缓存）。每项 token 数与工具面板徽章一致，按本地分词统计，与服务端 write 口径略有出入。', en: 'This round\u2019s write includes results from last round\u2019s {n} tools (written to cache for the first time). Each token count matches the tool-panel badge, counted with the local tokenizer, slightly differing from the server\u2019s write.' },
  'finishInfo.inflowLabel': { zh: '↳ 含上一轮工具结果：{detail}', en: '\u21b3 incl. last round\u2019s tool results: {detail}' },
  // Activity line
  'finishInfo.actTip': { zh: '本轮模型调用了 {n} 个工具；它们的调用参数与返回结果会在下一轮写入缓存', en: 'The model called {n} tools this round; their arguments and results are written to cache next round' },
  'finishInfo.actLabel': { zh: '↳ 调用 {n} 个工具：{tools}', en: '\u21b3 called {n} tools: {tools}' },
  'finishInfo.finalTip': { zh: '本轮没有调用工具，是模型生成的最终回答。这是 API 轮数比工具批次多 1 的原因。', en: 'No tools this round \u2014 this is the model\u2019s final answer. It\u2019s why the API round count is 1 more than the tool-batch count.' },
  'finishInfo.finalLabel': { zh: '↳ 最终回答（无工具）', en: '\u21b3 Final answer (no tools)' },
  // write-source note
  'finishInfo.writeNoteTipTools': { zh: '本轮 write {v} ≈ 上一轮输出 + {n} 个工具的返回结果（首次写入缓存），并非本轮模型新生成', en: 'This round\u2019s write {v} \u2248 last round\u2019s output + results from {n} tools (written to cache for the first time), not newly generated this round' },
  'finishInfo.writeNoteTipPlain': { zh: '本轮 write {v} 主要是上一轮输出 + 返回内容首次写入缓存，并非本轮模型新生成', en: 'This round\u2019s write {v} is mostly last round\u2019s output + returned content written to cache for the first time, not newly generated this round' },
  'finishInfo.writeNoteLabel': { zh: 'ⓘ write 来源：上一轮产出 + 工具结果', en: '\u24d8 write source: last round\u2019s output + tool results' },
  // Re-cache WASTE line — shown when writeBreakdown.recacheBody > 0 but the
  // banner-level detector stayed silent (sub-threshold / cross-turn round-1
  // read drop). Explains the money-losing "重新缓存正文" term that would
  // otherwise sit bare in the equation with no reason. Distinct from the
  // banner cacheBreak line (which has its own fault-state verdict).
  'finishInfo.wbWasteLabel': { zh: 'ⓘ 其中 {v} 是已缓存正文被重新计费（本轮 cache 读取较上一轮/上一回合下降 {drop}，非本轮新增）', en: '\u24d8 {v} of this is already-cached body re-billed (cache read dropped {drop} vs the previous round/turn, not new this round)' },
  'finishInfo.wbWasteTip': { zh: '本轮 write 里有 {v} tok 属于「重新缓存正文」——之前已经付费缓存过的会话正文这一轮没有被读回，被重新计费（真实浪费）。cache 读取较上次下降了 {drop} tok。可能是上一回合的缓存前缀在上游未被复用，也可能是客户端跨轮重序列化了已缓存字节。', en: 'This round\u2019s write contains {v} tok of "re-cache body" \u2014 conversation body we already paid to cache was not read back and got re-billed (real waste). Cache read dropped {drop} tok vs last time. This may be the previous turn\u2019s cached prefix not being reused upstream, or the client re-serializing already-cached bytes across turns.' },
  // Inline cache-break line
  'finishInfo.cacheBreakLabel': { zh: '缓存失效：{reason}', en: 'Cache miss: {reason}' },
  // Fault-state badge (wire-fingerprint verdict, 2026-07) + named culprit
  'finishInfo.cbState.eviction': { zh: '上游未命中（非本轮客户端改动）', en: 'Upstream miss (not this round’s client change)' },
  'finishInfo.cbState.upstream': { zh: '疑似上游未复用（本轮字节未变 · 未在真实流量验证）', en: 'Likely upstream non-reuse (bytes unchanged this round · unverified on live traffic)' },
  'finishInfo.cbState.proven': { zh: '服务端（已实证）', en: 'Server-side (proven)' },
  'finishInfo.cbState.unproven': { zh: '疑似服务端（未证实）', en: 'Likely server (unproven)' },
  'finishInfo.cbState.culprit': { zh: '本地改动（可定位）', en: 'Our edit (traceable)' },
  'finishInfo.cbState.namespace': { zh: '换缓存命名空间（key/beta/endpoint 变了）', en: 'Cache-namespace switch (key/beta/endpoint)' },
  'finishInfo.cbState.boundary': { zh: '回合边界未复用（新回合第 1 轮）', en: 'Turn-boundary miss (new turn, round 1)' },
  'finishInfo.cbCulpritLabel': { zh: '↳ 破坏缓存的消息：{culprits}', en: '\u21b3 message(s) that broke cache: {culprits}' },
  // Legend (contains <b> HTML — rendered as innerHTML, do NOT escape)
  'finishInfo.legendXY': { zh: '<b>X → Y</b>：本轮输入 X / 模型生成 Y', en: '<b>X \u2192 Y</b>: input X this round / model generated Y' },
  'finishInfo.legendCache': { zh: '<b class="cp-hit">cache</b>：复用上文缓存（便宜）', en: '<b class="cp-hit">cache</b>: reused context cache (cheap)' },
  'finishInfo.legendWrite': { zh: '<b class="cp-write">write</b>：本轮新写入缓存的上下文（贵，预期下一轮可复用——未命中则计为重新缓存正文）；来源＝上一轮回复 ＋ 工具结果 ＋ 首次缓存上下文 ＋ 消息开销，并非模型本轮新生成', en: '<b class="cp-write">write</b>: context newly written to cache this round (expensive, expected to be reused next round \u2014 a miss counts as re-cache body); source = prev reply + tool results + first-cache context + msg overhead, not newly generated this round' },
  // Aggregate cost rows
  'finishInfo.thinkingInOutput': { zh: '(含在 output 中)', en: '(included in output)' },
  'finishInfo.cacheSavings': { zh: 'Cache 节省', en: 'Cache saved' },
  // Task ID
  'finishInfo.taskIdTip': { zh: '点击复制任务 ID（用于定位日志 / 提供给排查）\nTask ID: {id}', en: 'Click to copy the task ID (for locating logs / sharing for diagnosis)\nTask ID: {id}' },
  // Collapsed cache-miss marker tooltip
  'finishInfo.cacheBreakSummary': { zh: '缓存失效（{n} 轮）：{reasons}', en: 'Cache miss ({n} rounds): {reasons}' },
  'finishInfo.cacheBreakSummaryPlain': { zh: '缓存失效（{n} 轮）', en: 'Cache miss ({n} rounds)' },
  // Finish-reason tags
  'finishInfo.reasonError': { zh: '错误', en: 'Error' },
  'finishInfo.reasonStopped': { zh: '已停止', en: 'Stopped' },
  'finishInfo.reasonInterrupted': { zh: '已中断', en: 'Interrupted' },
  'finishInfo.reasonInterruptedTip': { zh: '生成过程中服务器崩溃。内容已从最近的检查点恢复——可能不完整。', en: 'Server crashed during generation. Content recovered from last checkpoint \u2014 may be incomplete.' },
  'finishInfo.reasonInterruptedKilled': { zh: '已中断', en: 'Interrupted' },
  'finishInfo.reasonInterruptedKilledTip': { zh: '上次运行被非正常终止（进程被杀 / 内存溢出 / 崩溃），生成中断。内容已从最近的检查点恢复——可能不完整。', en: 'The previous run was killed uncleanly (process kill / OOM / crash), interrupting generation. Content recovered from last checkpoint \u2014 may be incomplete.' },
  'finishInfo.reasonInterruptedRestart': { zh: '因重启中断', en: 'Interrupted by restart' },
  'finishInfo.reasonInterruptedRestartTip': { zh: '服务器受控重启，生成被中断。内容已从最近的检查点恢复——可能不完整。', en: 'Generation was interrupted by a controlled server restart. Content recovered from last checkpoint \u2014 may be incomplete.' },
  'finishInfo.reasonInterruptedUnknown': { zh: '已中断', en: 'Interrupted' },
  'finishInfo.reasonInterruptedUnknownTip': { zh: '生成在服务器重启期间中断。内容已从最近的检查点恢复——可能不完整。', en: 'Generation was interrupted during a server restart. Content recovered from last checkpoint \u2014 may be incomplete.' },
  'finishInfo.reasonIncomplete': { zh: '提前停止 · 待复核', en: 'Stopped early · needs review' },
  'finishInfo.reasonIncompleteTip': { zh: '自动驾驶/端点循环触发了安全上限（最大轮次、重规划、原地打转或预算耗尽）而停止——目标尚未验证完成，请人工复核。', en: 'The autonomous loop (autopilot / endpoint) hit a safety cap (max iterations, replans, stuck, or budget) and stopped \u2014 the objective is NOT verified complete. Human review needed.' },
  'finishInfo.reasonServerOffline': { zh: '服务器离线', en: 'Server Offline' },
  'finishInfo.reasonServerOfflineTip': { zh: '生成过程中服务器离线（例如 VSCode 断开、网络中断）。已保存部分回复。', en: 'Server went offline during generation (e.g. VSCode disconnect, network drop). Partial response saved.' },
  'finishInfo.reconnect': { zh: '重新连接', en: 'Reconnect' },
  'finishInfo.reconnectTip': { zh: '检查服务器是否已生成完成结果', en: 'Check server for completed result' },
  'finishInfo.reasonTruncated': { zh: '已截断', en: 'Truncated' },
  'finishInfo.reasonTool': { zh: '工具', en: 'Tool' },
  'finishInfo.reasonFiltered': { zh: '已过滤', en: 'Filtered' },
  'finishInfo.reasonToolLimit': { zh: '工具上限', en: 'Tool limit' },
  // Fallback tag
  'finishInfo.fallbackTag': { zh: '回退', en: 'Fallback' },
  'finishInfo.fallbackReason': { zh: '\n失败原因 / Reason: {reason}', en: '\nReason: {reason}' },
  'finishInfo.fallbackTip': { zh: '原模型 {from} 失败，已回退到 {to}{reason}', en: 'Original model {from} failed, fell back to {to}{reason}' },
  // Trace-copy tooltip (debug)
  'finishInfo.traceCopyTip': { zh: '点击复制 trace_id:\n{ids}', en: 'Click to copy trace_id:\n{ids}' },

  // ══════════════════════════════════════
  //  File-changes bar (ui/finish_info.js _renderFileChangesHtml)
  // ══════════════════════════════════════
  'fileChanges.filesChanged': { zh: '{n} 个文件已修改', en: '{n} file{s} changed' },
  'fileChanges.inProgress': { zh: '{n} 个进行中', en: '{n} in progress' },
  'fileChanges.failed': { zh: '{n} 个失败', en: '{n} failed' },
  'fileChanges.summarySep': { zh: '，', en: ', ' },
  'fileChanges.undo': { zh: '撤销', en: 'Undo' },
  'fileChanges.undoAll': { zh: '全部撤销', en: 'Undo All' },
  'fileChanges.undoTip': { zh: '撤销本轮修改', en: 'Undo this round\u2019s changes' },
  'fileChanges.undoAllTip': { zh: '撤销所有对话中的所有修改', en: 'Undo all changes across the whole conversation' },
  'fileChanges.redo': { zh: '重做', en: 'Redo' },
  'fileChanges.redoTip': { zh: '重新应用本轮修改', en: 'Re-apply this round\u2019s changes' },
  'fileChanges.undone': { zh: '{n} 个文件已撤销', en: '{n} file{s} undone' },
  // Per-file action verbs (key === backend action; unknown actions render verbatim)
  'fileChanges.action.created': { zh: '已创建', en: 'created' },
  'fileChanges.action.modified': { zh: '已修改', en: 'modified' },
  'fileChanges.action.patched': { zh: '已修补', en: 'patched' },
  'fileChanges.action.deleted': { zh: '已删除', en: 'deleted' },
  'fileChanges.action.written': { zh: '已写入', en: 'written' },

  // ══════════════════════════════════════
  //  Streaming phase indicator (.stream-phase-*)
  // ══════════════════════════════════════
  'stream.phase.reasoning': { zh: '推理中', en: 'Reasoning' },
  'stream.phase.deepThinking': { zh: '深度思考', en: 'Deep thinking' },
  'stream.phase.chars': { zh: '{n} 字符', en: '{n} chars' },
  'stream.phase.waitingModel': { zh: '已发送给模型，等待开始回复…', en: 'Sent to the model, waiting for it to start replying…' },
  'stream.phase.retrying': { zh: '正在重试…', en: 'Retrying…' },
  // Model-fallback EARLY in-bubble banner (see llm_fallback model_fallback
  // SSE event + streaming_ui.js data-zone="fallback"). NOT a toast.
  'stream.fallback.banner': { zh: '主模型请求失败，已自动切换', en: 'Primary model failed — auto-switched' },
  'stream.fallback.bannerTip': { zh: '原模型 {from} 失败，已回退到 {to}\n原因：{reason}', en: 'Original model {from} failed, fell back to {to}\nReason: {reason}' },
  'stream.phase.waiting': { zh: '等待中…', en: 'Waiting…' },
  // Backend-emitted phase.detail localizations. Each corresponds to a
  // `detailKey` shipped alongside a legacy English `detail` fallback so
  // headless clients that don't localize still render sensible text.
  'stream.phase.generatingResponse': {
    zh: '正在生成回复…',
    en: 'Generating response…',
  },
  'stream.phase.analyzingRound': {
    zh: '正在分析结果并规划下一步…（第 {round} 轮）',
    en: 'Analyzing results and planning next step… (round {round})',
  },
  'stream.phase.waitingForModel': {
    zh: '已发送给 {model}，等待开始回复…',
    en: 'Sent to {model}, waiting for it to start replying…',
  },
  // First-byte waiting heartbeat (backend on_waiting → retrying PHASE):
  // a wedged/slow upstream shows a LIVE label instead of the static
  // waiting_model spinner for the whole wait. {elapsed} is seconds since
  // request send; the Reason variant carries the slot's known cause.
  'stream.phase.waitingFirstByte': {
    zh: '已等待 {elapsed}s：{model} 尚未返回首个字节…',
    en: 'Waiting {elapsed}s — {model} has not sent its first byte yet…',
  },
  'stream.phase.waitingFirstByteReason': {
    zh: '已等待 {elapsed}s：{model} 尚未返回首个字节（{reason}）',
    en: 'Waiting {elapsed}s — no first byte from {model} yet ({reason})',
  },
  // Mid-stream stall heartbeat: the model already sent text and then went
  // quiet. There is no read timeout, so this wait is unbounded by design —
  // the beat is what proves the turn is still alive (and what keeps the
  // stuck-task reaper from force-failing it). Press Stop to end it.
  'stream.phase.stalledMidStream': {
    zh: '已停顿 {elapsed}s：{model} 回复到一半没继续（仍在等，可随时点停止）…',
    en: 'Paused {elapsed}s — {model} stopped mid-reply (still waiting; press Stop to end it)…',
  },
  'stream.phase.stalledMidStreamReason': {
    zh: '已停顿 {elapsed}s：{model} 回复到一半没继续（{reason}）',
    en: 'Paused {elapsed}s — {model} stopped mid-reply ({reason})',
  },
  // Intent-stall nudge (backend _analyse.py → intent_stall_nudge PHASE):
  // the previous tool call did not run and the model answered with prose
  // only, so the loop re-drives it ONCE. Transient status, not an error.
  'stream.phase.intentStallNudge': {
    zh: '↻ 上一个工具未执行成功，正在提示模型继续…',
    en: '↻ Previous tool didn\'t run — nudging the model to continue…',
  },
  'stream.phase.compactingWindow': {
    zh: '正在压缩早期上下文以适配窗口…',
    en: 'Compressing earlier context to fit the window…',
  },
  'stream.phase.reactiveCompact': {
    zh: '⚡ 上下文超长，已自动压缩（reactive compact {attempt}/{max}）…',
    en: '⚡ Prompt too long — auto-compacted (reactive compact {attempt}/{max})…',
  },
  'stream.phase.retryRateLimited': {
    zh: '⏳ 模型 {model} 限流中，正在排队重试（第 {attempt} 次）…',
    en: '⏳ Model {model} is rate-limited — queued retry (attempt {attempt})…',
  },
  'stream.phase.retryReason': {
    zh: '重试中…{reason}（{model}，第 {attempt} 次）',
    en: 'Retrying… {reason} ({model}, attempt {attempt})',
  },
  'stream.phase.retryGeneric': {
    zh: '正在重试 {model}…（第 {attempt} 次）',
    en: 'Retrying {model}… (attempt {attempt})',
  },
  // Typed retry CAUSES (`reasonKey`): the dispatcher passes short English log
  // tokens as `reason`; the backend maps the known ones to these stable keys
  // so the HUD localizes the cause instead of leaking raw jargon.
  'stream.retryReason.endpointUnreachable': {
    zh: '连不上模型服务器（网关/网络波动，正在自动切换通道）',
    en: 'Model endpoint unreachable (gateway/network issue — switching channel)',
  },
  'stream.retryReason.requestTimedOut': {
    zh: '请求超时',
    en: 'Request timed out',
  },
  'stream.retryReason.waitingForModel': {
    zh: '等待模型（限流排队中）',
    en: 'Waiting for model (rate-limited)',
  },
  'stream.retryReason.keyBalanceExhausted': {
    zh: '密钥余额/配额已用尽',
    en: 'Key balance exhausted',
  },
  'stream.retryReason.rateLimited': {
    zh: '请求被限流 (429)',
    en: 'Rate limited (429)',
  },
  'stream.retryReason.upstreamError': {
    zh: '上游服务错误，正在换线重试',
    en: 'Upstream error — rotating channel',
  },
  'stream.retryReason.waitingBackoff': {
    zh: '等待模型（错误退避中，非限流）',
    en: 'Waiting for model (error backoff, not rate-limit)',
  },
  'stream.retryReason.waitingSharedProject': {
    zh: '等待模型（项目级共享额度争抢中，非本 key 问题）',
    en: 'Waiting for model (shared project contention, not this key)',
  },
  'stream.thinking.active': { zh: '思考中...', en: 'Thinking...' },
  'stream.thinking.done': { zh: '思考过程', en: 'Thinking Process' },
  'stream.roundMessages': { zh: 'Round {round} · {n}条', en: 'Round {round} · {n} msgs' },
  // ── Human-guidance submit + code-undo confirms (project.js) ──
  'project.hgTranslating': { zh: '翻译中…', en: 'Translating…' },
  'project.hgSubmit': { zh: '提交', en: 'Submit' },
  'project.hgTranslateFailed': { zh: '翻译失败，已发送原文', en: 'Translation failed — sent original' },
  'project.hgTranslatingQuestion': { zh: '正在翻译问题…', en: 'Translating question…' },
  'project.hgTextareaPlaceholder': { zh: '输入你的回答（支持中文，会自动翻译）…', en: 'Type your answer (Chinese supported — auto-translated)…' },
  'project.hgPanelTitle': { zh: 'AI 需要你的指导', en: 'The AI needs your guidance' },
  'project.hgWaitingReply': { zh: '等待回复', en: 'Awaiting reply' },
  'project.hgUnanswered': { zh: '未回答', en: 'Unanswered' },
  'project.hgAnswered': { zh: '✓ 已回答', en: '✓ Answered' },
  'project.hgWaitingContinue': { zh: '等待 AI 继续…', en: 'Waiting for the AI to continue…' },
  // ── Scannable QR recovered from terminal output (tool_rounds.js
  //    _renderQrStrip). Shown above the code while a scan-to-login command
  //    is still blocking for the scan. ──
  'project.qrScan': { zh: '可扫描的二维码', en: 'Scannable QR code' },
  'project.qrScanMulti': { zh: '个可扫描的二维码', en: 'scannable QR codes' },
  // ── Message action bar (chat_render.js .message-actions) ──
  'msgAction.copy': { zh: '复制', en: 'Copy' },
  'msgAction.copyTitle': { zh: '复制', en: 'Copy' },
  'msgAction.edit': { zh: '编辑', en: 'Edit' },
  'msgAction.editTitle': { zh: '编辑', en: 'Edit' },
  'msgAction.regen': { zh: '重新生成', en: 'Regen' },
  'msgAction.regenTitle': { zh: '从这条消息重新生成回复', en: 'Regenerate response from this message' },
  'msgAction.continue': { zh: '继续', en: 'Continue' },
  'msgAction.continueTitle': { zh: '从中断处继续生成', en: 'Continue generating from where it left off' },
  'msgAction.continueLosslessTitle': { zh: '无损续写：接着上次的文本继续，不重写已生成内容', en: 'Lossless resume: continues the SAME text, keeping everything already generated' },
  'msgAction.continueFromRoundTitle': { zh: '从上次工具调用检查点继续（丢弃其后的草稿文本）', en: 'Resume from the last tool-call checkpoint (discards the draft text after it)' },
  'msgAction.regenerateTitle': { zh: '此回复无法续接，将重新生成', en: 'This response cannot be resumed — it will be regenerated' },
  'msgAction.translate': { zh: '翻译', en: 'Translate' },
  'msgAction.original': { zh: '原文', en: 'Original' },
  'msgAction.translateTitle': { zh: '翻译', en: 'Translate' },
  'msgAction.showOriginalTitle': { zh: '显示原文', en: 'Show Original' },
  'msgAction.export': { zh: '导出', en: 'Export' },
  'msgAction.exportTitle': { zh: '导出为手机屏幕图片', en: 'Export as phone-screen images' },
  'msgAction.inspect': { zh: '检视', en: 'Inspect' },
  'msgAction.deleteTurnTitle': { zh: '删除此轮对话', en: 'Delete this turn' },
  'msgAction.deleteMsgTitle': { zh: '删除此消息', en: 'Delete this message' },
  'branch.add': { zh: '分支', en: 'Branch' },
  // ── Inline message editor (edit_message.js) ──
  'editMsg.cancel': { zh: '取消', en: 'Cancel' },
  'editMsg.save': { zh: '保存', en: 'Save' },
  'editMsg.resend': { zh: '保存并重发', en: 'Save & Resend' },
  'editMsg.hintHuman': { zh: '保存：保留后续消息 · 保存并重发：截断并重新生成 · 可拖拽/粘贴文件作为附件', en: 'Save: keep subsequent · Save & Resend: truncate and regenerate · Drop/paste files to attach' },
  'editMsg.hintInPlace': { zh: '就地编辑这条消息', en: 'Edit this message in place' },
  // ── Welcome-screen capability chips (core/icons.js _WELCOME_PILLS) ──
  'welcome.extendedThinking': { zh: '深度思考', en: 'Extended Thinking' },
  'welcome.search': { zh: '搜索', en: 'Search' },
  'welcome.urlFetch': { zh: 'URL 抓取', en: 'URL Fetch' },
  'welcome.imageInput': { zh: '图片输入', en: 'Image Input' },
  'welcome.coPilot': { zh: '副驾驶', en: 'Co-Pilot' },
  'welcome.browser': { zh: '浏览器', en: 'Browser' },
  'project.undoTurnConfirm': { zh: '确定要撤销本轮对话的 {count} 处代码修改吗？\n此操作将恢复这些文件到修改前的状态。', en: 'Undo the {count} code change(s) from this turn?\nThis restores those files to their pre-edit state.' },
  'project.undoAllConfirm': { zh: '确定要撤销所有代码修改吗？\n\n此操作将恢复所有被修改的文件到原始状态，包括所有对话中的修改。', en: 'Undo ALL code changes?\n\nThis restores every modified file to its original state, including changes from all conversations.' },
  // ── Chat view: conv-ref badge, image-gen batch banner, compaction marker, bilingual labels (ui/chat_render.js) ──
  'chat.convRefTitle': { zh: '引用对话: {title}', en: 'Referenced conversation: {title}' },
  'chat.convRefMeta': { zh: '引用对话', en: 'Conversation reference' },
  'chat.igBatchAll': { zh: '全模型 {n}连抽', en: 'All models ×{n}' },
  'chat.igBatchN': { zh: '{n}连抽', en: '×{n}' },
  'chat.igBatchSuccess': { zh: '{ok}/{total} 成功', en: '{ok}/{total} succeeded' },
  'chat.compactReactive': { zh: '紧急压缩', en: 'Emergency compaction' },
  'chat.compactManual': { zh: '手动压缩', en: 'Manual compaction' },
  'chat.compactAuto': { zh: '自动压缩', en: 'Auto compaction' },
  'chat.compactArchivedTokens': { zh: '{k}k tokens 已归档', en: '{k}k tokens archived' },
  'chat.compactArchived': { zh: '已归档', en: 'Archived' },
  'chat.compactViewSnapshot': { zh: '点击查看压缩前的完整上下文', en: 'Click to view the full pre-compaction context' },
  'chat.compactViewHistory': { zh: '查看历史', en: 'View history' },
  'chat.bilingualOriginal': { zh: '原文', en: 'Original' },
  'chat.bilingualTranslated': { zh: '译文', en: 'Translation' },
  // ── Image-gen generate button (main.js) ──
  'main.igGenerate': { zh: '生成', en: 'Generate' },
  'main.igBatchGo': { zh: '{n}连抽!', en: '×{n}!' },
  // ── Copy-conversation flow toasts (main/main_conv_lifecycle.js) ──
  'convLifecycle.copyFailed': { zh: '复制失败', en: 'Copy failed' },
  'convLifecycle.copyFailedBody': { zh: '无法加载原始对话内容', en: 'Could not load the source conversation content' },
  'convLifecycle.copying': { zh: '对话复制中…', en: 'Copying conversation…' },
  'convLifecycle.copyingBody': { zh: '正在复制 "{title}"', en: 'Copying "{title}"' },
  'convLifecycle.copySuffix': { zh: ' (副本)', en: ' (copy)' },
  'convLifecycle.copied': { zh: '对话已复制 ✓', en: 'Conversation copied ✓' },
  'convLifecycle.copiedBody': { zh: '"{title}" → 独立副本已创建', en: '"{title}" → an independent copy was created' },

  // Peer-message provenance banner (chat_render.js) — a KIND_PEER_MSG turn
  // injected by a sibling conversation via project_message / project_intervene.
  'peer.messageBanner': { zh: '来自同项目其他对话的消息', en: 'Message from a peer conversation' },
  'peer.operatorBanner': { zh: '来自项目操作者的消息', en: 'Message from the project operator' },
  // Header role label (chat_render.js) for the same turns — replaces "You" so
  // the avatar+label identity is not byte-identical to human input.
  'peer.senderLabel': { zh: '其他对话', en: 'Peer' },
  'peer.operatorLabel': { zh: '操作者', en: 'Operator' },
  // Header role labels for auto-initiated (non-human) turns — replaces "You"
  // / "Agent" so every automatically-started turn is visually attributable.
  // Keyed by the _initiator vocabulary (lib/conversations/turn_initiation.py).
  'initiator.proactive': { zh: '定时代理', en: 'Proactive Agent' },
  'initiator.timer': { zh: '定时器', en: 'Timer' },
  'initiator.brain': { zh: '项目大脑', en: 'Project Brain' },
  'initiator.swarm': { zh: '自动续跑', en: 'Auto-continued' },
  // In-timeline chip (tool_rounds.js _renderStallNudgeRow) marking an
  // intent-stall nudge: the loop re-drove a model that said what it would do
  // and then stopped. System-authored, so the copy must read as a SYSTEM
  // action — never as something the user said.
  'stall.injectRowLabel': { zh: '已提示模型继续', en: 'Nudged the model to continue' },
  'stall.reasonWithTool': {
    zh: '`{tool}` 未执行成功，而下一轮只有文字、没有工具调用——模型说了要做，然后停下了。',
    en: '`{tool}` did not run, and the next round was text only — the model said what it would do, then stopped.',
  },
  'stall.reasonGeneric': {
    zh: '上一个工具调用未执行成功，而下一轮只有文字。',
    en: 'The previous tool call did not run, and the next round was text only.',
  },
  'stall.bound': {
    zh: '每回合最多一次——若模型再次停下，就允许它结束。',
    en: 'At most once per turn — if the model stalls again it is allowed to stop.',
  },
  'stall.promptLabel': { zh: '发给模型的内容', en: 'Sent to the model' },
  // In-timeline chip (tool_rounds.js _renderPeerInjectRow) for a peer message
  // delivered at a round boundary of a LIVE turn (Pillar #6 fast-path lane) —
  // distinct from the queue-lane .peer-msg-banner user bubble.
  'peer.injectRowLabel': { zh: '收到', en: 'Received' },
  'peer.injectRowOne': { zh: '条对话消息', en: 'peer message' },
  'peer.injectRowMany': { zh: '条对话消息', en: 'peer messages' },
  'peer.injectRowBadge': { zh: '已注入上下文', en: 'injected → context' },

  // Post-send inject-mode chooser (main_send_pipeline.js _promptInjectMode) —
  // shown ONLY when a turn is already generating for this conversation. Two
  // lanes: steer into the running reply vs queue as a fresh turn.
  'inject.promptTitle': { zh: '正在生成中 — 这条消息怎么发送？', en: 'A reply is generating — how should this message be sent?' },
  'inject.labelSteer': { zh: '插入当前回复', en: 'Steer into this reply' },
  'inject.subSteer': { zh: '在下一个工具调用边界注入,让模型立刻看到', en: 'Injected at the next tool-call boundary so the model sees it right away' },
  'inject.labelQueue': { zh: '排到下一轮', en: 'Queue as the next turn' },
  'inject.subQueue': { zh: '当前回复结束后作为新一轮自动发送', en: 'Sent automatically as a fresh turn after this reply ends' },
  'steer.injected': { zh: '已插入当前回复,将在下一个工具调用边界注入', en: 'Steered into the reply — injected at the next tool-call boundary' },
  // In-timeline chip (tool_rounds.js _renderUserSteerInjectRow) marking a human
  // steer message injected mid-turn.
  'steer.injectRowLabel': { zh: '你中途插入了', en: 'You steered mid-turn' },
  'steer.injectRowOne': { zh: '条消息', en: 'steer message' },
  'steer.injectRowMany': { zh: '条消息', en: 'steer messages' },
  'steer.noPayload': { zh: '无消息内容', en: 'No message available.' },
  // Visible send-feedback toast when a message is queued behind a running turn
  // (main_send_pipeline.js). Steer reuses steer.injected.
  'queue.queuedToast': { zh: '已排队 (#{n})，将在当前回复结束后自动发送', en: 'Queued (#{n}) — sent automatically after this reply ends' },

  // Swarm agent phase pills (streaming_swarm_panel.js)
  'swarm.phase.thinking': { zh: '思考中…', en: 'Thinking…' },
  'swarm.phase.tool_use': { zh: '调用工具中', en: 'Using tools' },
  'swarm.phase.writing': { zh: '撰写中…', en: 'Writing…' },
  'swarm.phase.searching': { zh: '搜索中…', en: 'Searching…' },
  'swarm.phase.coding': { zh: '编码中…', en: 'Coding…' },
  'swarm.phase.analyzing': { zh: '分析中…', en: 'Analyzing…' },
  'swarm.phase.complete': { zh: '完成', en: 'Complete' },
  'swarm.phase.failed': { zh: '失败', en: 'Failed' },
  'swarm.phase.error': { zh: '错误', en: 'Error' },
  'swarm.phase.queued': { zh: '排队中', en: 'Queued' },
  'swarm.phase.running': { zh: '工作中…', en: 'Working…' },
  'swarm.phase.retrying': { zh: '重试中…', en: 'Retrying…' },
  'swarm.phase.noResult': { zh: '无结果', en: 'No result' },
  'swarm.autoContinue': { zh: '子智能体完成后自动继续', en: 'Continued automatically after sub-agents finished' },

  // ══════════════════════════════════════
  //  Queue
  // ══════════════════════════════════════
  'queue.messagesQueued': { zh: '条消息排队中', en: 'messages queued' },
  'queue.clearAll': { zh: '全部清空', en: 'Clear all' },
  'queue.fromConv': { zh: '来自', en: 'from' },
  'queue.fromOperator': { zh: '来自操作员', en: 'from operator' },
  'queue.images': { zh: '张图片', en: 'images' },
  'queue.attachment': { zh: '附件', en: 'Attachment' },
  'queue.cancelMsg': { zh: '取消此消息', en: 'Cancel this message' },
  'queue.imagesCount': { zh: '{n} 张图片', en: '{n} images' },
  'queue.syncedToServer': { zh: '已同步到服务器', en: 'Synced to server' },
  // Queue bar collapse toggle + brain-dispatch attribution
  // (main/main_send_pipeline.js renderPendingQueueUI).
  'queue.collapse': { zh: '收起队列', en: 'Collapse queue' },
  'queue.expand': { zh: '展开队列', en: 'Expand queue' },
  'queue.fromWorkflow': { zh: '项目大脑派发', en: 'Brain dispatch' },
  // ── Log-noise pre-send confirm dialog (main/main_send_pipeline.js) ──
  'send.logNoiseConfirm': { zh: '检测到日志噪音，可节省 {chars} 字符（{pct}%）\n\n清理项: {ops}\n\n点击「确定」清理后发送，「取消」保持原文发送。', en: 'Log noise detected — can save {chars} characters ({pct}%)\n\nCleanup: {ops}\n\nClick "OK" to clean then send, "Cancel" to send as-is.' },
  'send.logNoiseClean': { zh: '清理并发送', en: 'Clean & send' },
  'send.logNoiseKeep': { zh: '保持原文', en: 'Keep original' },
  // ── Log-noise banner + preview modal (log-clean.js) ──
  'logClean.banner': { zh: '检测到日志噪音，可节省 <strong>{chars}</strong> 字符（<strong>{pct}%</strong>）', en: 'Log noise detected — can save <strong>{chars}</strong> characters (<strong>{pct}%</strong>)' },
  'logClean.compressing': { zh: '压缩中…', en: 'Compressing…' },
  'logClean.retry': { zh: '失败，重试', en: 'Failed — retry' },
  'logClean.before': { zh: '清理前', en: 'Before' },
  'logClean.after': { zh: '清理后', en: 'After' },
  'logClean.metaBefore': { zh: '{chars} 字符 · {lines} 行', en: '{chars} chars · {lines} lines' },
  'logClean.metaAfter': { zh: '{chars} 字符 · 节省 {pct}%', en: '{chars} chars · saved {pct}%' },
  'logClean.previewTitle': { zh: '日志噪音清理预览', en: 'Log Noise Cleanup Preview' },
  'logClean.previewMeta': { zh: '节省 {chars} 字符 ({pct}%)', en: 'Saved {chars} chars ({pct}%)' },

  // ══════════════════════════════════════
  //  Agent backends
  // ══════════════════════════════════════
  'agent.notInstalled': { zh: '未安装', en: 'Not installed' },
  'agent.notAuthenticated': { zh: '未认证', en: 'Not authenticated' },
  'agent.ready': { zh: '就绪', en: 'Ready' },

  // ══════════════════════════════════════
  //  Log Clean
  // ══════════════════════════════════════
  'logClean.detected': { zh: '检测到日志噪音，可节省', en: 'Log noise detected, can save' },
  'logClean.chars': { zh: '字符', en: 'characters' },

  // ══════════════════════════════════════
  //  Conversation reference
  // ══════════════════════════════════════
  'convRef.title': { zh: '@ 引用对话', en: '@ Reference Conversation' },
  'convRef.searchPh': { zh: '搜索对话标题…', en: 'Search conversation titles…' },
  'convRef.noMatch': { zh: '没有匹配的对话', en: 'No matching conversations' },
  'convRef.noOther': { zh: '暂无其他对话', en: 'No other conversations' },
  'convRef.messages': { zh: '条消息', en: 'messages' },
  'convRef.cannotRef': { zh: '无法引用当前对话', en: 'Cannot reference current conversation' },
  'convRef.alreadyRef': { zh: '该对话已在引用列表中', en: 'This conversation is already referenced' },
  'convRef.referenced': { zh: '已引用', en: 'Referenced' },
  'convRef.removeRef': { zh: '移除引用', en: 'Remove reference' },
  'convRef.convRef': { zh: '对话引用', en: 'Conversation reference' },
  'convRef.messagesCount': { zh: '{n} 条消息', en: '{n} messages' },
  'convRef.referencedToast': { zh: '已引用: {title}', en: 'Referenced: {title}' },

  // ══════════════════════════════════════
  //  Batch / Multi-gen
  // ══════════════════════════════════════
  'batch.allModels': { zh: '全模型', en: 'All models' },
  'batch.multiGen': { zh: '连抽', en: 'multi-gen' },
  'batch.success': { zh: '成功', en: 'success' },

  // ══════════════════════════════════════
  //  Daily Optimizer panel / badge
  // ══════════════════════════════════════
  'optimizer.badgeTitle': { zh: '每日优化器 — 自主改进建议', en: 'Daily Optimizer — autonomous improvement proposals' },
  'optimizer.panelTitle': { zh: '每日优化器', en: 'Daily Optimizer' },
  'optimizer.loading': { zh: '加载中…', en: 'Loading…' },
  'optimizer.disabled': { zh: '每日优化器已禁用。可在 设置 → 功能模块 中启用。', en: 'Daily Optimizer is disabled. Enable it in Settings → Feature Modules.' },
  'optimizer.empty': { zh: '每日优化器还没有提出任何建议。它每晚 03:30 本地时间自动运行，分析日志并可能提出或自动应用小改进。', en: "The Daily Optimizer hasn't proposed anything yet. It wakes up nightly at 03:30 local, analyses the day's logs, and may suggest or auto-apply small improvements." },
  'optimizer.runNow': { zh: '▶ 立即运行', en: '▶ Run now' },
  'optimizer.runNowTitle': { zh: '同步运行优化器', en: 'Synchronously run the optimiser now' },
  'optimizer.running': { zh: '运行中…', en: 'Running…' },
  'optimizer.lastRefresh': { zh: '最近刷新：', en: 'Last refresh: ' },
  'optimizer.appliedToday': { zh: '今日已应用', en: 'Applied today' },
  'optimizer.pendingReview': { zh: '等待你审阅', en: 'Pending your review' },
  'optimizer.revertedToday': { zh: '今日已撤销/过期', en: 'Reverted / expired today' },
  'optimizer.olderProposals': { zh: '更早的建议', en: 'Older proposals' },
  'optimizer.approve': { zh: '✓ 批准', en: '✓ Approve' },
  'optimizer.approveTitle': { zh: '立即应用此建议', en: 'Apply this proposal now' },
  'optimizer.reject': { zh: '✗ 拒绝', en: '✗ Reject' },
  'optimizer.rejectTitle': { zh: '标记为已拒绝', en: 'Mark as rejected' },
  'optimizer.rejectPrompt': { zh: '拒绝此建议的理由（可选）：', en: 'Reason for rejecting this proposal (optional):' },
  'optimizer.revert': { zh: '↩ 撤销', en: '↩ Revert' },
  'optimizer.revertTitle': { zh: '撤销此次变更', en: 'Undo this change' },
  'optimizer.revertConfirm': { zh: '撤销此次已应用的变更？底层配置将回滚，lib.SKIP_DOMAINS（或类似）会热重载。', en: 'Revert this applied change? The underlying config will be rolled back and lib.SKIP_DOMAINS (or similar) will be hot-reloaded.' },
  'optimizer.blockSearchDomain': { zh: '屏蔽搜索域名', en: 'block search domain' },
  'optimizer.untitled': { zh: '(无标题)', en: '(untitled)' },
  // Status reasons emitted by the backend (lib/optimizer/applier.py).
  // _optStatusReasonText() in optimizer.js maps the raw English string
  // to one of these keys; unknown reasons fall through to the raw text.
  'optimizer.reason.notInWhitelist': { zh: '未在自动应用白名单中', en: 'not in auto-apply whitelist' },
  'optimizer.reason.dryRun': { zh: '试运行（未实际应用）', en: 'dry run (not actually applied)' },
  'optimizer.reason.unknownAction': { zh: '未知的操作类型：{type}', en: 'unknown action type: {type}' },
  'optimizer.reason.manualApproveBlocked': { zh: '手动批准被拦截：操作类型不在自动应用白名单中', en: 'manual approve blocked: action_type not in auto-apply whitelist' },

  // ══════════════════════════════════════
  //  Settings — Search/Fetch params (inline labels)
  // ══════════════════════════════════════
  'settings.fetchTopNFull': { zh: '抓取前 N 条 <span style="color:var(--text-tertiary);font-weight:normal">（搜索后自动抓取排名靠前的网页）</span>', en: 'Fetch Top N <span style="color:var(--text-tertiary);font-weight:normal">(auto-fetch top-ranked pages after search)</span>' },
  'settings.fetchTimeoutFull': { zh: '抓取超时 <span style="color:var(--text-tertiary);font-weight:normal">（秒）</span>', en: 'Fetch Timeout <span style="color:var(--text-tertiary);font-weight:normal">(seconds)</span>' },
  'settings.maxCharsSearchFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（搜索结果页面）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(search result pages)</span>' },
  'settings.maxCharsDirectFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（直接抓取 URL）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(direct URL fetch)</span>' },
  'settings.maxCharsPdfFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（PDF 文件，0=不限制）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(PDF files, 0=unlimited)</span>' },
  'settings.maxBytesFull': { zh: '最大下载大小 <span style="color:var(--text-tertiary);font-weight:normal">（字节，默认 20MB）</span>', en: 'Max Download Size <span style="color:var(--text-tertiary);font-weight:normal">(bytes, default 20MB)</span>' },
  'settings.maxCharsPdfPh': { zh: '0=不限制', en: '0=unlimited' },
  'settings.bypassDomainsFull': { zh: '绕过域名 <span style="color:var(--text-tertiary);font-weight:normal">（每行一个，后缀匹配 — 例如 <code>.your-corp.com</code>）</span>', en: 'Bypass Domains <span style="color:var(--text-tertiary);font-weight:normal">(one per line, suffix matching — e.g. <code>.your-corp.com</code>)</span>' },
  'settings.fallbackModelFull': { zh: '回退模型 <span style="color:var(--text-tertiary);font-weight:normal">（主模型失败时自动切换）</span>', en: 'Fallback Model <span style="color:var(--text-tertiary);font-weight:normal">(auto-switch on primary failure)</span>' },
  'settings.defaultModelFull': { zh: '默认模型 <span style="color:var(--text-tertiary);font-weight:normal">（LLM_MODEL）</span>', en: 'Default Model <span style="color:var(--text-tertiary);font-weight:normal">(LLM_MODEL)</span>' },
  'settings.allowedUsersFull': { zh: '允许的用户 <span style="color:var(--text-tertiary);font-weight:normal">（飞书 open_id，每行一个 — 留空表示允许所有人）</span>', en: 'Allowed Users <span style="color:var(--text-tertiary);font-weight:normal">(Feishu open_id, one per line — empty = allow everyone)</span>' },

  // Feishu app ID/secret placeholders
  'settings.feishuAppIdPh': { zh: 'cli_xxxx（从 open.feishu.cn 获取）', en: 'cli_xxxx (get from open.feishu.cn)' },
  'settings.feishuAppSecretPh': { zh: '留空则保持不变', en: 'Leave empty to keep unchanged' },
  'settings.feishuHowto': { zh: '在 <a href="https://open.feishu.cn/app" target="_blank" style="color:var(--accent-color)">open.feishu.cn</a> 创建飞书应用 → 启用机器人能力 → 在上方填写 <strong>App ID</strong> 和 <strong>App Secret</strong>。凭证修改需要重启服务器才能生效。', en: 'Create a Feishu app at <a href="https://open.feishu.cn/app" target="_blank" style="color:var(--accent-color)">open.feishu.cn</a> → enable bot capability → fill in <strong>App ID</strong> and <strong>App Secret</strong> above. Credential changes require a server restart.' },

  // OAuth extended strings
  'settings.oauthPageDesc': { zh: '使用 ChatGPT Plus / Claude Pro 订阅账号登录，无需 API Key，直接使用订阅额度。', en: 'Log in with ChatGPT Plus / Claude Pro — no API Key needed, use your subscription quota directly.' },
  'settings.oauthChinaNote': { zh: '⚠️ 中国用户需要全程代理（Clash/VPN），授权弹窗和服务器换 token 都需要能访问外网。建议在<strong>本地浏览器无痕窗口</strong>中完成授权。', en: '⚠️ Users in China need a proxy (Clash/VPN) throughout. Both the auth popup and server-side token exchange require internet access. Use a <strong>local incognito window</strong> to authorize.' },
  'settings.oauthPopupBlocked': { zh: '如弹窗无法打开，请复制链接到<strong>开了代理的浏览器无痕窗口</strong>中打开：', en: 'If the popup is blocked, copy the link into a <strong>proxy-enabled incognito browser window</strong>:' },
  'settings.oauthCopyLink': { zh: '复制链接', en: 'Copy Link' },
  'settings.oauthCopied': { zh: '✓ 已复制', en: '✓ Copied' },
  'settings.oauthClaudeCodeHint': { zh: '授权成功后页面显示授权码，复制 <strong>code#state</strong> 粘贴到下方：', en: 'After authorization succeeds, the page shows an auth code — copy the <strong>code#state</strong> and paste below:' },
  'settings.oauthCodexCbHint': { zh: '授权成功后，复制浏览器地址栏中的回调 URL 粘贴到下方：', en: 'After authorization, copy the callback URL from the browser address bar and paste below:' },
  'settings.oauthClaudePh': { zh: '粘贴 code#state（或完整回调 URL）', en: 'Paste code#state (or full callback URL)' },
  'settings.oauthCodexPh': { zh: '粘贴回调 URL (http://localhost:1455/auth/callback?code=...)', en: 'Paste callback URL (http://localhost:1455/auth/callback?code=...)' },
  'settings.oauthSubmit': { zh: '提交', en: 'Submit' },
  'settings.oauthClaudeDesc': { zh: '登录 Claude 订阅，使用 Sonnet / Opus 等模型，无需 API Key。', en: 'Log in with your Claude subscription to use Sonnet / Opus — no API Key required.' },
  'settings.oauthCodexDesc': { zh: '登录 ChatGPT 订阅，使用 Codex 模型，请求自动转换为 Responses API 格式。', en: 'Log in with your ChatGPT subscription to use Codex — requests auto-converted to Responses API format.' },
  'settings.oauthNote1': { zh: '点击登录 → 弹窗打开官方授权页 → 用订阅账号登录并授权', en: 'Click Login → popup opens the official auth page → log in with your subscription account and authorize' },
  'settings.oauthNote2': { zh: '授权后 Claude 显示 <code>code#state</code>、ChatGPT 自动回调，按提示操作即可', en: 'After authorization Claude shows <code>code#state</code>; ChatGPT auto-callbacks — follow the on-screen hints' },
  'settings.oauthNote3': { zh: '如弹窗被拦截，点击「复制链接」到<strong>本地浏览器无痕窗口</strong>中打开（避免登录态冲突）', en: 'If the popup is blocked, click "Copy Link" and open it in a <strong>local incognito window</strong> (avoids session conflicts)' },
  'settings.oauthNote4': { zh: 'Token 过期后自动刷新，无需重新登录', en: 'Tokens auto-refresh on expiry — no need to re-login' },

  // Network proxy: extended body/hint
  'settings.httpProxyBody': { zh: '配置用于所有出站请求（LLM API、网页搜索、页面抓取）的 HTTP/HTTPS 代理。留空则使用系统环境变量（<code>http_proxy</code> / <code>https_proxy</code>）。修改立即生效，无需重启。', en: 'Configure HTTP/HTTPS proxy for all outbound requests (LLM API, web search, page fetch). Leave empty to use system env vars (<code>http_proxy</code> / <code>https_proxy</code>). Changes take effect immediately.' },
  'settings.proxyBypassBody': { zh: '在此添加不需要走代理的域名后缀或主机名（每行一个，后缀匹配）。匹配的请求会<strong>完全绕过 HTTP 代理</strong>。', en: 'Add domain suffixes or hostnames that should bypass the proxy (one per line, suffix matching). Matching requests <strong>fully bypass the HTTP proxy</strong>.' },
  'settings.proxyBypassTipFull': { zh: '<strong>💡 提示：</strong>内网地址和 LLM API 域名都应加在这里。企业/VPN 代理会静默断开长连接（SSE 流），导致 <code>BrokenPipeError</code>，添加对应域名即可解决。也可通过环境变量 <code>PROXY_BYPASS_DOMAINS</code>（逗号分隔）配置，两处合并生效。', en: '<strong>💡 Tip:</strong> Internal addresses and LLM API domains should both be added here. Corporate/VPN proxies silently drop long-lived connections (SSE streams) causing <code>BrokenPipeError</code> — adding the domain here fixes it. You can also set <code>PROXY_BYPASS_DOMAINS</code> (comma-separated) env var; both merge.' },

  // ══════════════════════════════════════
  //  MCP manual-add form (settings)
  // ══════════════════════════════════════
  'mcp.scopeAll': { zh: '全部', en: 'All' },
  'mcp.scopeInstalled': { zh: '已安装', en: 'Installed' },
  'mcp.scopeAvailable': { zh: '未安装', en: 'Available' },
  'mcp.addCustom': { zh: '+ 添加', en: '+ Add' },
  'mcp.addCustomDesc': { zh: '填写命令或远程 URL，即可连接任意 MCP 服务器，无需改代码。', en: 'Connect any MCP server by entering a command or remote URL — no code required.' },
  'mcp.manualAddSummary': { zh: '手动添加自定义服务器', en: 'Manually add a custom server' },
  'mcp.fieldName': { zh: '名称', en: 'Name' },
  'mcp.fieldTransport': { zh: '传输协议', en: 'Transport' },
  'mcp.transportStdio': { zh: 'stdio (本地命令)', en: 'stdio (local command)' },
  'mcp.transportSse': { zh: 'SSE (远程 URL)', en: 'SSE (remote URL)' },
  'mcp.fieldCommand': { zh: '命令', en: 'Command' },
  'mcp.fieldArgs': { zh: '参数', en: 'Arguments' },
  'mcp.fieldArgsHint': { zh: '（每行一个）', en: '(one per line)' },
  'mcp.fieldEnv': { zh: '环境变量', en: 'Environment Variables' },
  'mcp.fieldEnvHint': { zh: '（每行 KEY=VALUE）', en: '(KEY=VALUE per line)' },
  'mcp.fieldDesc': { zh: '描述', en: 'Description' },
  'mcp.fieldDescHint': { zh: '（可选）', en: '(optional)' },
  'mcp.saveConnect': { zh: '保存并连接', en: 'Save & Connect' },
  'mcp.obtainKey': { zh: '去申请 Key', en: 'Get a key' },
  'mcp.suggestTitle': { zh: '从这几个开始（都能自助完成安装）', en: 'Start with these — all installable on your own' },
  'mcp.sharedCredential': { zh: '「{name}」已保存同一个凭证，留空即可复用，无需重新申请', en: 'Already saved by “{name}” — leave blank to reuse it; no need to apply again' },
  'mcp.installConnect': { zh: '安装并连接', en: 'Install & Connect' },
  'mcp.cancel': { zh: '取消', en: 'Cancel' },
  'mcp.reconnecting': { zh: '连接失败，自动重试中', en: 'Connection failed, auto-retrying' },
  'mcp.retryInSec': { zh: '{n} 秒后重试', en: 'retry in {n}s' },
  'mcp.retryInMin': { zh: '{n} 分钟后重试', en: 'retry in {n} min' },
  'mcp.retryNow': { zh: '即将重试…', en: 'retrying…' },
  'mcp.retryFailCount': { zh: '已失败 {n} 次', en: '{n} failed attempts' },
  // Catalog grid / loading / empty states
  'mcp.loading': { zh: '正在加载…', en: 'Loading…' },
  'mcp.loadFailed': { zh: '加载 Apps 失败: {err}', en: 'Failed to load Apps: {err}' },
  'mcp.toolsCount': { zh: '{n} 个工具', en: '{n} tools' },
  'mcp.installedCount': { zh: '已安装 {n} 个', en: '{n} installed' },
  'mcp.emptyInstalled': { zh: '还没有安装任何 App。可在「全部」或「未安装」中安装，或点「+ 添加」接入自定义服务器。', en: 'No Apps installed yet. Install one from “All” or “Available”, or click “+ Add” to connect a custom server.' },
  'mcp.emptyAvailable': { zh: '所有 App 都已安装。', en: 'All Apps are installed.' },
  'mcp.emptyNoMatch': { zh: '没有匹配的 App。', en: 'No matching Apps.' },
  // Card status badges + actions
  'mcp.statusOn': { zh: '已连接', en: 'ON' },
  'mcp.statusIdle': { zh: '空闲', en: 'IDLE' },
  // Credential health (subprocess alive but stored cookie/token expired)
  'mcp.credExpired': { zh: '凭据已过期', en: 'Credentials expired' },
  'mcp.credExpiredTitle': { zh: '服务已连接，但保存的登录凭据（如 Overleaf 会话 Cookie）已失效——请更新凭据后功能才能恢复。', en: 'Connected, but the saved login credentials (e.g. the Overleaf session cookie) are no longer valid — update them to restore functionality.' },
  'mcp.updateCreds': { zh: '更新凭据', en: 'Update credentials' },
  'mcp.repo': { zh: '仓库', en: 'Repo' },
  'mcp.repoTitle': { zh: '源代码仓库', en: 'Source Repository' },
  'mcp.uninstall': { zh: '卸载', en: 'Uninstall' },
  'mcp.uninstallTitle': { zh: '断开连接，但保留已填写的凭据，方便下次一键重新启用', en: 'Disconnect but keep saved credentials for easy one-click re-enable' },
  'mcp.connect': { zh: '连接', en: 'Connect' },
  'mcp.connectCustomTitle': { zh: '重新连接此自定义服务器', en: 'Reconnect this custom server' },
  'mcp.connectReinstallTitle': { zh: '编辑凭据并重新连接（已有凭据会回填为默认；留空则沿用）', en: 'Edit credentials and reconnect (existing ones prefill as defaults; leave blank to keep)' },
  'mcp.purge': { zh: '清除凭据', en: 'Clear credentials' },
  'mcp.purgeTitle': { zh: '彻底删除配置，包括已保存的凭据', en: 'Permanently delete the config, including saved credentials' },
  'mcp.install': { zh: '安装', en: 'Install' },
  'mcp.quickInstallTitle': { zh: '无需配置，点击直接安装并连接；如需修改默认值可在安装后点“连接”', en: 'No config needed — click to install and connect; to change defaults, click “Connect” after install' },
  'mcp.connectAll': { zh: '全部连接', en: 'Connect All' },
  // Error detail
  'mcp.unknownError': { zh: '未知错误', en: 'Unknown error' },
  'mcp.unknownErrorNoConn': { zh: '未知错误（无法连接到服务器）', en: 'Unknown error (could not connect to the server)' },
  'mcp.serverOutputLabel': { zh: '服务器输出:', en: 'Server output:' },
  // Quick install
  'mcp.quickInstallFailed': { zh: '一键安装失败: {err}\n\n将打开高级设置，可手动调整参数后重试。', en: 'One-click install failed: {err}\n\nOpening advanced settings so you can adjust the parameters and retry.' },
  // Install modal fields
  'mcp.savedHint': { zh: '已保存，留空则沿用；填写即覆盖', en: 'Saved — leave blank to keep; fill to override' },
  'mcp.savedBadge': { zh: '● 已保存', en: '● Saved' },
  'mcp.selectProvider': { zh: '— 选择服务商 —', en: '— Select provider —' },
  'mcp.noEnvNeeded': { zh: '无需配置，直接安装即可。', en: 'No configuration needed — just install.' },
  'mcp.advancedToggle': { zh: '▸ 高级设置（可选，{n} 项）', en: '▸ Advanced settings (optional, {n})' },
  // Poll / install progress
  'mcp.installingDeps': { zh: '正在安装依赖…（已等待 {n} 秒，请勿关闭）', en: 'Installing dependencies… (waited {n}s, please keep this open)' },
  'mcp.installTimeout': { zh: '安装超时（依赖下载耗时过长）。请稍后在“未安装”中重试。', en: 'Install timed out (dependency download took too long). Retry later under “Available”.' },
  'mcp.installing': { zh: '安装中…', en: 'Installing…' },
  'mcp.startingFirstInstall': { zh: '正在启动 {name}…（首次安装需下载依赖，可能耗时约一分钟，请勿关闭）', en: 'Starting {name}… (first install downloads dependencies, may take ~a minute, please keep this open)' },
  'mcp.installingDepsName': { zh: '正在安装 {name} 的依赖…（首次安装可能耗时约一分钟，请勿关闭）', en: 'Installing {name} dependencies… (first install may take ~a minute, please keep this open)' },
  'mcp.installedSuccess': { zh: '✓ {name} 已安装 — {n} 个工具可用', en: '✓ {name} installed — {n} tools available' },
  'mcp.installFailedNoConn': { zh: '安装失败（无法连接到服务器）', en: 'Install failed (could not connect to the server)' },
  'mcp.retry': { zh: '重试', en: 'Retry' },
  // Uninstall / purge confirms
  'mcp.uninstallConfirm': { zh: '卸载 {name}？\n\n将断开连接并禁用，但会保留已填写的凭据；下次点击“连接”可一键重新启用，无需再填一遍。\n\n如需彻底清除凭据，请先卸载，再在空闲卡片上点“清除凭据”。', en: 'Uninstall {name}?\n\nIt will be disconnected and disabled, but your saved credentials are kept; click “Connect” next time to re-enable in one click without re-entering them.\n\nTo wipe credentials entirely, uninstall first, then click “Clear credentials” on the idle card.' },
  'mcp.uninstallFailed': { zh: '卸载失败: {err}', en: 'Uninstall failed: {err}' },
  'mcp.purgeConfirm': { zh: '清除 {name} 的全部配置和已保存的凭据？\n\n此操作不可恢复，重新启用时需要再次填写所有凭据。', en: 'Clear all config and saved credentials for {name}?\n\nThis cannot be undone — you’ll need to re-enter every credential to re-enable it.' },
  'mcp.purgeFailed': { zh: '清除失败: {err}', en: 'Clear failed: {err}' },
  // Connect / reconnect
  'mcp.connecting': { zh: '连接中…', en: 'Connecting…' },
  'mcp.connectFailed': { zh: '连接失败: {err}', en: 'Connect failed: {err}' },
  // Save custom server
  'mcp.needName': { zh: '请输入服务器名称', en: 'Please enter a server name' },
  'mcp.needCommand': { zh: '请输入命令 (command)', en: 'Please enter a command' },
  'mcp.needUrl': { zh: '请输入 SSE URL', en: 'Please enter an SSE URL' },
  'mcp.saving': { zh: '保存中…', en: 'Saving…' },
  'mcp.startingName': { zh: '正在启动 {name}…', en: 'Starting {name}…' },
  'mcp.saveFailedNoConn': { zh: '保存失败（无法连接到服务器）', en: 'Save failed (could not connect to the server)' },
  'mcp.savedConnected': { zh: '✓ {name} 已保存并连接', en: '✓ {name} saved & connected' },

  // ══════════════════════════════════════
  //  Relay admin page (relay-admin.js — standalone /admin)
  // ══════════════════════════════════════
  // Shared
  'relayAdmin.loadFailed': { zh: '加载失败：{err}', en: 'Load failed: {err}' },
  // Users tab
  'relayAdmin.pwPlaceholder': { zh: '临时密码', en: 'Temporary password' },
  'relayAdmin.create': { zh: '创建', en: 'Create' },
  'relayAdmin.colEmail': { zh: '邮箱', en: 'Email' },
  'relayAdmin.colRole': { zh: '角色', en: 'Role' },
  'relayAdmin.colStatus': { zh: '状态', en: 'Status' },
  'relayAdmin.colBalance': { zh: '余额', en: 'Balance' },
  'relayAdmin.colCreated': { zh: '注册时间', en: 'Registered' },
  'relayAdmin.colLastLogin': { zh: '最近登录', en: 'Last login' },
  'relayAdmin.colActions': { zh: '操作', en: 'Actions' },
  'relayAdmin.topup': { zh: '+充值', en: '+Top up' },
  'relayAdmin.payments': { zh: '支付', en: 'Payments' },
  'relayAdmin.disable': { zh: '停用', en: 'Disable' },
  'relayAdmin.enable': { zh: '启用', en: 'Enable' },
  'relayAdmin.noUsers': { zh: '还没有用户', en: 'No users yet' },
  'relayAdmin.needEmailPw': { zh: '需要邮箱和密码', en: 'Email and password are required' },
  'relayAdmin.createFailed': { zh: '创建失败：{err}', en: 'Create failed: {err}' },
  'relayAdmin.topupAmount': { zh: '充值金额（credits，正数）：', en: 'Top-up amount (credits, positive):' },
  'relayAdmin.invalidAmount': { zh: '无效金额', en: 'Invalid amount' },
  'relayAdmin.topupNote': { zh: '备注（可选）：', en: 'Note (optional):' },
  'relayAdmin.topupNoteDefault': { zh: '管理员充值', en: 'Admin top-up' },
  'relayAdmin.topupFailed': { zh: '充值失败：{err}', en: 'Top-up failed: {err}' },
  'relayAdmin.toggleConfirm': { zh: '将该用户改为 {status}？', en: 'Change this user to {status}?' },
  'relayAdmin.updateFailed': { zh: '更新失败：{err}', en: 'Update failed: {err}' },
  // Payments drill-down
  'relayAdmin.loadingPayments': { zh: '加载支付记录…', en: 'Loading payments…' },
  'relayAdmin.paymentsOf': { zh: '{email} 的支付记录（{n}）', en: '{email}\u2019s payments ({n})' },
  'relayAdmin.colTime': { zh: '时间', en: 'Time' },
  'relayAdmin.colProvider': { zh: '提供商', en: 'Provider' },
  'relayAdmin.colAmount': { zh: '金额', en: 'Amount' },
  'relayAdmin.colCurrency': { zh: '币种', en: 'Currency' },
  'relayAdmin.colCredited': { zh: '入账', en: 'Credited' },
  'relayAdmin.colExtId': { zh: '外部 ID', en: 'External ID' },
  'relayAdmin.noPayments': { zh: '无支付记录', en: 'No payments' },
  'relayAdmin.noPaymentsYet': { zh: '还没有支付记录', en: 'No payments yet' },
  // Pricing tab
  'relayAdmin.marginLabel': { zh: '默认利润率（%）：', en: 'Default margin (%):' },
  'relayAdmin.saveMargin': { zh: '保存利润率', en: 'Save margin' },
  'relayAdmin.marginDesc': { zh: '基础价 × (1 + 利润率) = 客户最终价。这是本页唯一可调的计费旋钮。', en: 'Base price × (1 + margin) = customer’s final price. This is the only billing knob tunable on this page.' },
  'relayAdmin.ratesReadonly': { zh: '<strong>模型费率为只读。</strong>费率的唯一真实来源是 <code>lib/pricing.py</code>（单一成本引擎 <code>lib.cost.compute_cost</code> 读取它，显示与扣费同源）；此处不再可编辑，以免出现第二份会漂移的费率表。如需改价请编辑 <code>lib/pricing.py</code>。', en: '<strong>Model rates are read-only.</strong> The single source of truth for rates is <code>lib/pricing.py</code> (the one cost engine <code>lib.cost.compute_cost</code> reads it, so display and billing share a source); they are no longer editable here to avoid a second rate table that would drift. To change prices, edit <code>lib/pricing.py</code>.' },
  'relayAdmin.colModel': { zh: '模型', en: 'Model' },
  'relayAdmin.colInput': { zh: '输入(µ/Mtok)', en: 'Input (µ/Mtok)' },
  'relayAdmin.colOutput': { zh: '输出(µ/Mtok)', en: 'Output (µ/Mtok)' },
  'relayAdmin.colCacheHit': { zh: '缓存命中(µ/Mtok)', en: 'Cache hit (µ/Mtok)' },
  'relayAdmin.colCacheWrite': { zh: '缓存写入(µ/Mtok)', en: 'Cache write (µ/Mtok)' },
  'relayAdmin.defaultModelFallback': { zh: 'default_model (兜底)', en: 'default_model (fallback)' },
  'relayAdmin.invalidMargin': { zh: '利润率无效', en: 'Invalid margin' },
  'relayAdmin.marginSaved': { zh: '利润率已保存并热重载。', en: 'Margin saved and hot-reloaded.' },
  'relayAdmin.saveFailed': { zh: '保存失败：{err}', en: 'Save failed: {err}' },
  // Codes tab
  'relayAdmin.codeCountPh': { zh: '个数', en: 'Count' },
  'relayAdmin.codeAmountPh': { zh: '单张金额(credits)', en: 'Amount each (credits)' },
  'relayAdmin.codeExpiresPh': { zh: 'N 天后过期', en: 'Expires in N days' },
  'relayAdmin.codeBatchPh': { zh: '批次名(可选)', en: 'Batch name (optional)' },
  'relayAdmin.generate': { zh: '生成', en: 'Generate' },
  'relayAdmin.colCode': { zh: '代码', en: 'Code' },
  'relayAdmin.colBatch': { zh: '批次', en: 'Batch' },
  'relayAdmin.colRedeemer': { zh: '使用人', en: 'Redeemed by' },
  'relayAdmin.codeRedeemed': { zh: '已使用', en: 'Redeemed' },
  'relayAdmin.codeUnredeemed': { zh: '未使用', en: 'Unredeemed' },
  'relayAdmin.noCodes': { zh: '还没有兑换码', en: 'No redeem codes yet' },
  'relayAdmin.invalidParams': { zh: '参数无效', en: 'Invalid parameters' },
  'relayAdmin.codesGenerated': { zh: '已生成 <strong>{n}</strong> 张兑换码，每张 {amount} credits。', en: 'Generated <strong>{n}</strong> redeem codes, {amount} credits each.' },
  'relayAdmin.copyAll': { zh: '复制全部', en: 'Copy all' },
  'relayAdmin.generateFailed': { zh: '生成失败：{err}', en: 'Generate failed: {err}' },
  // Payments tab
  'relayAdmin.paymentsDesc': { zh: '显示当前管理员账号的支付记录。如需查看某个用户的支付记录，请在「用户」标签页中筛选（规划中）。', en: 'Shows the current admin account’s payments. To view a specific user’s payments, filter in the “Users” tab (planned).' },
  // Restricted-mode banner (innerHTML — contains <code>)
  'relayAdmin.bannerByoOnly': { zh: 'BYO-only：用户必须使用各自注册的模型端点（<code>/api/v1/providers</code> → <code>agents:run</code>），新发放的密钥不含 <code>chat</code> 权限，无法访问平台模型池。', en: 'BYO-only: users must use their own registered model endpoints (<code>/api/v1/providers</code> → <code>agents:run</code>); newly issued keys carry no <code>chat</code> scope and cannot reach the platform model pool.' },
  'relayAdmin.bannerNoBilling': { zh: '未计费：平台不收取费用，「定价 / 兑换码 / 支付」已隐藏。', en: 'No billing: the platform charges nothing; “Pricing / Codes / Payments” are hidden.' },
  // ── admin.html static chrome (data-i18n / data-i18n-html) ──
  'relayAdmin.loadingPlaceholder': { zh: '加载中…', en: 'Loading…' },
  'relayAdmin.page.navDashboard': { zh: '客户 Dashboard', en: 'Customer Dashboard' },
  'relayAdmin.page.navBackToTofu': { zh: '返回 Tofu', en: 'Back to Tofu' },
  'relayAdmin.page.gateTitle': { zh: '需要管理员权限', en: 'Admin access required' },
  'relayAdmin.page.gateP1': { zh: '本页仅在中转站模式（<code>auth_mode = multi-user</code>）下、且以管理员密钥登录时可用。', en: 'This page is only available in relay mode (<code>auth_mode = multi-user</code>) when logged in with an admin key.' },
  'relayAdmin.page.gateP2': { zh: '个人 / 自用部署无需此页 —— 在 <a href="/">Tofu 设置</a> 中配置你自己的服务商即可。', en: 'Personal / self-hosted deployments don’t need this page — just configure your own providers in <a href="/">Tofu Settings</a>.' },
  'relayAdmin.page.bannerPrefix': { zh: '<strong>受限中转模式</strong> —— ', en: '<strong>Restricted relay mode</strong> — ' },
  'relayAdmin.page.bannerSuffix': { zh: '可在 <code>data/config/relay.json</code> 中调整 <code>billing_enabled</code> / <code>model_relay_enabled</code>（二者互相独立）。', en: 'Adjust <code>billing_enabled</code> / <code>model_relay_enabled</code> in <code>data/config/relay.json</code> (the two are independent).' },
  'relayAdmin.page.tabUsers': { zh: '用户', en: 'Users' },
  'relayAdmin.page.tabPricing': { zh: '定价', en: 'Pricing' },
  'relayAdmin.page.tabCodes': { zh: '兑换码', en: 'Redeem codes' },
  'relayAdmin.page.tabPayments': { zh: '支付', en: 'Payments' },
  'relayAdmin.page.usersTitle': { zh: '用户管理', en: 'User management' },
  'relayAdmin.page.usersDesc': { zh: '管理使用本中转站的用户、调整余额、暂停账号、查看历史。', en: 'Manage the users of this relay — adjust balances, suspend accounts, view history.' },
  'relayAdmin.page.pricingTitle': { zh: '定价表', en: 'Pricing table' },
  'relayAdmin.page.pricingDesc': { zh: '每千 token 的微币(µ)单价。修改保存到 <code>data/config/pricing.json</code>，对未来请求立即生效。1 credit = 1,000,000 µ ≈ US $0.001。', en: 'Micro-credit (µ) price per 1K tokens. Changes save to <code>data/config/pricing.json</code> and take effect immediately for future requests. 1 credit = 1,000,000 µ ≈ US $0.001.' },
  'relayAdmin.page.codesTitle': { zh: '兑换码', en: 'Redeem codes' },
  'relayAdmin.page.codesDesc': { zh: '批量生成单次有效的兑换码，客户在 <code>/dashboard</code> 输入即可获得对应余额。', en: 'Batch-generate single-use redeem codes; customers enter them at <code>/dashboard</code> to credit their balance.' },
  'relayAdmin.page.paymentsTitle': { zh: '支付记录', en: 'Payment records' },
  'relayAdmin.page.paymentsDesc': { zh: '所有 Stripe / Alipay 完成的充值。Webhook 处理是幂等的——同一笔重复回调不会被重复入账。', en: 'All completed Stripe / Alipay top-ups. Webhook handling is idempotent — a duplicate callback for the same payment is never credited twice.' },

  // ══════════════════════════════════════
  //  Manual /compact — boundary card (chat_render.js) + trigger (context-bar.js)
  // ══════════════════════════════════════
  'compactCard.title': { zh: '上下文已压缩（主动）', en: 'Context compacted (manual)' },
  'compactCard.expand': { zh: '展开摘要', en: 'Show summary' },
  'compactCard.viewSnapshot': { zh: '查看压缩前快照', en: 'View pre-compaction snapshot' },
  'compactCard.msgs': { zh: '{before} → {after} 条消息', en: '{before} → {after} msgs' },
  'compactNow.action': { zh: '立即压缩上下文', en: 'Compact context now' },
  'compactNow.viewHistory': { zh: '查看压缩历史', en: 'View compaction history' },
  'compactNow.busy': { zh: '任务进行中，无法压缩', en: 'A task is running — cannot compact' },
  'compactNow.running': { zh: '正在压缩上下文…', en: 'Compacting context…' },
  'compactNow.streaming': { zh: '正在生成压缩摘要…', en: 'Generating summary…' },
  'compactNow.done': { zh: '已压缩：{before} → {after} tokens（-{pct}%）', en: 'Compacted: {before} → {after} tokens (-{pct}%)' },
  'compactNow.nothing': { zh: '上下文太短，无需压缩', en: 'Context is too short to compact' },
  'compactNow.failed': { zh: '压缩失败', en: 'Compaction failed' },

  // ══════════════════════════════════════
  //  Compaction viewer drawer (compaction-viewer.js)
  // ══════════════════════════════════════
  // ══════════════════════════════════════
  //  Compaction viewer drawer (compaction-viewer.js)
  // ══════════════════════════════════════
  // Trigger labels (icon prepended in JS; text keeps the English (kind) suffix)
  'compactionViewer.trigger.force': { zh: '自动压缩 (force)', en: 'Auto-compaction (force)' },
  'compactionViewer.trigger.reactive': { zh: '紧急压缩 (reactive)', en: 'Emergency compaction (reactive)' },
  'compactionViewer.trigger.manual': { zh: '手动压缩 (manual)', en: 'Manual compaction (manual)' },
  // Header chrome
  'compactionViewer.title': { zh: '压缩前的上下文快照', en: 'Pre-compaction context snapshot' },
  'compactionViewer.subtitle': { zh: '这里展示的是<strong>压缩触发瞬间</strong>发送给 LLM 的完整消息列表——包含 system prompt、工具调用、工具结果，以及已经过 L1/L2 处理（如 thinking 剥离、screenshot 替换）的中间态。它<em>不是</em>用户输入的"原始文本"——查看原始对话请使用左侧主窗口。', en: 'This shows the <strong>full message list sent to the LLM at the moment compaction fired</strong> — the system prompt, tool calls, tool results, and the post-L1/L2 intermediate state (thinking stripped, screenshots replaced). It is <em>not</em> your original input text — to see the raw conversation, use the main window on the left.' },
  'compactionViewer.tabMessages': { zh: '上下文消息', en: 'Context messages' },
  'compactionViewer.tabSummary': { zh: '压缩结果摘要', en: 'Compaction summary' },
  'compactionViewer.tabHistory': { zh: '该会话全部快照', en: 'All snapshots for this conversation' },
  'compactionViewer.loading': { zh: '加载中…', en: 'Loading…' },
  'compactionViewer.copyJson': { zh: '复制原始 JSON', en: 'Copy raw JSON' },
  'compactionViewer.download': { zh: '下载完整快照', en: 'Download full snapshot' },
  // Meta rows
  'compactionViewer.metaReason': { zh: '触发原因', en: 'Trigger reason' },
  'compactionViewer.metaType': { zh: '类型', en: 'Type' },
  'compactionViewer.metaTime': { zh: '发生时间', en: 'Time' },
  'compactionViewer.metaMsgs': { zh: '消息数', en: 'Messages' },
  'compactionViewer.metaModel': { zh: '模型', en: 'Model' },
  'compactionViewer.metaRound': { zh: '回合', en: 'Round' },
  // Empty / error states
  'compactionViewer.emptySnapshot': { zh: '（该快照为空）', en: '(This snapshot is empty)' },
  'compactionViewer.revealImage': { zh: '展开显示 · 可能较大', en: 'Reveal · may be large' },
  'compactionViewer.noSummary1': { zh: '该快照没有压缩摘要。', en: 'This snapshot has no compaction summary.' },
  'compactionViewer.noSummary2': { zh: '这通常意味着压缩在 L1 micro-compact 或 reactive image-strip 阶段就完成了，未调用 LLM 生成摘要。', en: 'This usually means compaction finished at the L1 micro-compact or reactive image-strip stage, without calling the LLM to generate a summary.' },
  'compactionViewer.noHistory': { zh: '该会话暂无压缩记录。', en: 'No compaction records for this conversation yet.' },
  'compactionViewer.loadFailed': { zh: '加载失败：{err}', en: 'Load failed: {err}' },
  'compactionViewer.noCompaction': { zh: '该会话尚未触发过上下文压缩。', en: 'This conversation has not triggered context compaction yet.' },
  'compactionViewer.historyFailed': { zh: '无法获取压缩历史：{err}', en: 'Could not fetch compaction history: {err}' },
  'compactionViewer.copied': { zh: '已复制 JSON', en: 'JSON copied' },
  'compactionViewer.copyFailed': { zh: '复制失败：{err}', en: 'Copy failed: {err}' },

  // ══════════════════════════════════════
  //  Browser bridge modal
  // ══════════════════════════════════════
  'browser.stepDownload': { zh: '下载并解压扩展', en: 'Download & unzip the extension' },
  'browser.stepDownloadBtn': { zh: '下载扩展 ZIP', en: 'Download Extension ZIP' },
  'browser.clickToCopy': { zh: '点击复制', en: 'Click to copy' },
  'browser.lnaTitle': { zh: 'Chrome 142+ 可能反复弹出「访问本地网络设备」', en: 'Chrome 142+ may repeatedly prompt for "local network access"' },
  'browser.lnaDesc': { zh: '从 Chrome 142 起，访问本地网络的网页会逐站弹出权限请求，多标签搜索时需反复点击。扩展无法代为授权，请用以下任一方式关闭提示：', en: 'Since Chrome 142, pages touching the local network prompt per-site, so multi-tab searches require repeated clicks. The extension cannot grant this itself — disable the prompt with either method below:' },
  'browser.lnaFlag': { zh: '<strong>方式一（最快）</strong>：打开 <code>chrome://flags/#local-network-access-check</code> → 设为 <strong>Disabled</strong> → 重启 Chrome。', en: '<strong>Option 1 (fastest)</strong>: open <code>chrome://flags/#local-network-access-check</code> → set it to <strong>Disabled</strong> → restart Chrome.' },
  'browser.lnaPolicy': { zh: '<strong>方式二（持久）</strong>：放置一个 Chrome 托管策略文件，内容为：', en: '<strong>Option 2 (persistent)</strong>: drop a Chrome managed-policy file with this content:' },
  'browser.lnaPathLabel': { zh: '放置位置：', en: 'Place it at:' },

  // ══════════════════════════════════════
  //  Debug / toolbar tooltips
  // ══════════════════════════════════════
  'debug.copyAllTooltip': { zh: '复制全部', en: 'Copy all' },
  'toolbar.aiEnhanceTooltip': { zh: 'AI 增强', en: 'AI Enhance' },
  'toolbar.externalToolsTooltip': { zh: '外部工具', en: 'External Tools' },
  'toolbar.execModeTooltip': { zh: '执行模式', en: 'Execution Mode' },
  'toolbar.moreOptionsTooltip': { zh: '更多选项', en: 'More options' },
  'toolbar.exitCreativeTooltip': { zh: '退出创作模式 (Esc)', en: 'Exit creative mode (Esc)' },
  'ig.stdResTooltip': { zh: '1024px · 标准分辨率', en: '1024px · Standard resolution' },
  'ig.hdResTooltip': { zh: '2048px · 高清分辨率', en: '2048px · HD resolution' },
  'ig.generateTooltip': { zh: '生成 (Enter)', en: 'Generate (Enter)' },
  'feishu.statusDotTitle': { zh: '连接状态', en: 'Connection status' },
  'settings.loadingPricing': { zh: '正在加载价格数据...', en: 'Loading pricing data…' },
  'settings.feishuBotTitleSuffix': { zh: '飞书 (Lark) 机器人', en: 'Feishu (Lark) Bot' },

  // ══════════════════════════════════════
  //  Common
  // ══════════════════════════════════════
  'common.confirm': { zh: '确定', en: 'OK' },
  'common.cancel': { zh: '取消', en: 'Cancel' },
  // cookie-capture consent banner (cookie_capture_consent.js)
  'cc.banner.title': { zh: '允许读取 {domain} 的登录态？', en: 'Allow reading {domain} session?' },
  'cc.banner.body': { zh: '抓取该页面遇到登录墙。允许后 Tofu 会在你的浏览器中打开登录页，仅读取 {domain} 这一个域名的 cookies 并保存，之后的抓取将自动携带登录态。', en: 'Fetching this page hit a login wall. If you allow it, Tofu opens the login page in your browser, reads cookies for {domain} only, and stores them so later fetches carry your session.' },
  'cc.banner.allow': { zh: '允许（记住此域名）', en: 'Allow (remember domain)' },
  'cc.banner.deny': { zh: '拒绝', en: 'Deny' },
  'cc.captured': { zh: '已保存 {domain} 的登录态，重试即可抓取', en: 'Session for {domain} saved — retry to fetch' },
  'common.close': { zh: '关闭', en: 'Close' },
  'common.save': { zh: '保存', en: 'Save' },
  'common.delete': { zh: '删除', en: 'Delete' },
  'common.edit': { zh: '编辑', en: 'Edit' },
  'common.loading': { zh: '正在加载…', en: 'Loading…' },
  'common.error': { zh: '错误', en: 'Error' },
  'common.success': { zh: '成功', en: 'Success' },
  'common.required': { zh: '必填', en: 'Required' },
  'common.officialApi': { zh: '官方 API', en: 'Official API' },
  'common.relayApi': { zh: '中转 API', en: 'Relay API' },
  // ── Themed dialog (confirm/alert/prompt) default button labels ──
  'dialog.confirm': { zh: '确定', en: 'OK' },
  'dialog.cancel': { zh: '取消', en: 'Cancel' },
  'dialog.ok': { zh: '好的', en: 'OK' },

  // ══════════════════════════════════════
  //  Cross-conversation presence ("who's working here")
  // ══════════════════════════════════════
  'presence.title': { zh: '其他对话正在协作', en: 'Others working here' },
  'presence.untitled': { zh: '(未命名对话)', en: '(untitled)' },
  'presence.justNow': { zh: '刚刚', en: 'just now' },
  'presence.secsAgo': { zh: '{n} 秒前', en: '{n}s ago' },
  'presence.minsAgo': { zh: '{n} 分钟前', en: '{n}m ago' },

  // ── Project Collaboration Bar (replaces the old presence strip) ──
  'collab.project': { zh: '项目', en: 'Project' },
  'collab.openBrain': { zh: '打开项目大脑', en: 'Open Project Brain' },
  'collab.decisionsAwaiting': { zh: '{n} 项决策待你审批', en: '{n} decisions awaiting you' },
  // The attention SSOT segment. Two wordings so the bar can distinguish "work
  // is STOPPED until you act" from "you could weigh in" — the same
  // blocking/advisory split the Needs-you tab renders.
  'collab.needsYouBlocking': { zh: '{n} 项需你处理', en: '{n} need you' },
  'collab.needsYou': { zh: '{n} 项待你确认', en: '{n} awaiting you' },
  'collab.epicsInProgress': { zh: '{n} 个任务推进中', en: '{n} epics in progress' },
  'collab.epicsOpen': { zh: '{n} 个待认领', en: '{n} open' },
  'collab.peersOnline': { zh: '{n} 个会话在线', en: '{n} conversations online' },
  'collab.peerAdvancing': { zh: '推进', en: 'advancing' },
  'collab.conflicts': { zh: '{n} 处文件冲突', en: '{n} file conflicts' },

  // ══════════════════════════════════════
  //  Project-bar pet — scene switcher + day-report bubble
  // ══════════════════════════════════════
  // Click-to-report speech bubble — the pet summarises your day (daily report).
  'pet.dayReport': { zh: '今日完成 {done}/{total}', en: '{done}/{total} done today' },
  'pet.dayBlocked': { zh: '· {n} 项受阻', en: '\u00b7 {n} blocked' },
  'pet.dayIdle': { zh: '今天还没有记录', en: 'Nothing logged yet today' },
  'pet.dayGreeting': { zh: '你好呀！', en: 'Hi there!' },
  // Scene switcher + pet tooltip. The `pet.scene.` / `pet.greet.` / `pet.feel.`
  // namespaces are read via DYNAMIC t('prefix.' + x) calls in tofu-pet.js, so
  // lib/i18n_boot_keys.T_CALL_DYNAMIC_PREFIX_RE discovers each prefix and
  // expands it to every matching key here (charter #18) — adding a scene or a
  // mood tier needs no edit to any key list.
  'pet.scene.meadow': { zh: '草地', en: 'Meadow' },
  'pet.scene.pool': { zh: '水池', en: 'Pool' },
  'pet.scene.sky': { zh: '天空', en: 'Sky' },
  'pet.scene.off': { zh: '关闭', en: 'Off' },
  'pet.sceneTooltip': { zh: '场景：{scene} · 点击切换', en: 'Scene: {scene} \u00b7 click to change' },
  // Composed via a template key, NOT string concatenation: word order differs
  // by language, and a hardcoded '—' separator would strand the zh reading.
  'pet.title': { zh: '豆腐 — {greet} · {feel}', en: 'Tofu \u2014 {greet} \u00b7 {feel}' },
  'pet.greet.deepNight': { zh: '睡得正香', en: 'fast asleep' },
  'pet.greet.earlyMorning': { zh: '刚睡醒', en: 'waking up' },
  'pet.greet.morning': { zh: '精神饱满', en: 'bright and early' },
  'pet.greet.afternoon': { zh: '悠闲发呆', en: 'hanging out' },
  'pet.greet.evening': { zh: '准备收工', en: 'winding down' },
  'pet.greet.night': { zh: '有点困了', en: 'getting sleepy' },
  'pet.feel.great': { zh: '心情很好', en: 'feeling great' },
  'pet.feel.fine': { zh: '状态还行', en: 'doing fine' },
  'pet.feel.blue': { zh: '有点低落', en: 'a bit blue' },

  // ══════════════════════════════════════
  //  Project Brain — cross-conversation Activity Feed (Pillar #1)
  // ══════════════════════════════════════
  'projectBrain.title': { zh: '项目大脑', en: 'Project Brain' },
  'projectBrain.open': { zh: '打开项目大脑', en: 'Open Project Brain' },
  'projectBrain.showMore': { zh: '展开全文', en: 'Show more' },
  'projectBrain.showLess': { zh: '收起', en: 'Show less' },
  // ── Hover preview for opaque conversation IDs ──
  'projectBrain.previewFirstQuestion': { zh: '第一个问题', en: 'First question' },
  'projectBrain.previewEmpty': { zh: '暂无消息', en: 'No messages yet' },
  'projectBrain.previewUntitled': { zh: '未命名对话', en: 'Untitled' },
  'projectBrain.previewLoading': { zh: '加载中…', en: 'Loading…' },
  'projectBrain.charter': { zh: '章程', en: 'Charter' },
  // ── Needs you (the attention tab) ──
  'projectBrain.attention': { zh: '待你处理', en: 'Needs you' },
  'projectBrain.attnBlocking': { zh: '已停摆', en: 'Stopped' },
  'projectBrain.attnAdvisory': { zh: '可选', en: 'Advisory' },
  'projectBrain.attnKindEpic': { zh: '任务停摆', en: 'Epic halted' },
  'projectBrain.attnKindProposal': { zh: '提议中的决策', en: 'Proposed decision' },
  'projectBrain.attnKindConflict': { zh: '文件冲突', en: 'File conflict' },
  'projectBrain.attnLeadBlocking': { zh: '{n} 项停摆', en: '{n} stopped' },
  'projectBrain.attnLeadAdvisory': { zh: '{n} 项可选', en: '{n} advisory' },
  'projectBrain.attnOpenTeam': { zh: '去看团队', en: 'Open Team' },
  'projectBrain.attnEmpty': { zh: '没有需要你处理的事', en: 'Nothing needs you' },
  'projectBrain.attnEmptySub': { zh: '所有工作流都在自行推进。', en: 'Every workstream is moving on its own.' },
  'projectBrain.attnEmptyWaiting': { zh: '{n} 项在等待自己的门禁 — 无需你动手', en: '{n} waiting on their own gates — no action needed' },
  'projectBrain.attnWaiting': { zh: '{n} 项在等待自己的门禁 — 无需你动手', en: '{n} waiting on their own gates — no action needed' },
  'projectBrain.board': { zh: '任务板', en: 'Board' },
  'projectBrain.activity': { zh: '动态', en: 'Activity' },
  'projectBrain.team': { zh: '团队', en: 'Team' },
  'projectBrain.peersEmpty': { zh: '暂无活跃的协作会话', en: 'No sibling conversations active' },
  'projectBrain.status': { zh: '状态与关注', en: 'Status & Watch' },
  'projectBrain.statusEmpty': { zh: '暂无状态 — 当项目有章程或任务板动态后自动综合生成。', en: 'No status yet — synthesized once the project has a charter or board activity.' },
  'projectBrain.statusLoading': { zh: '正在综合项目状态…', en: 'Synthesizing project status…' },
  'projectBrain.statusTitle': { zh: '项目当前状态', en: 'Where the project is' },
  'projectBrain.statusUpdating': { zh: '更新中…', en: 'Updating…' },
  'projectBrain.statusRefresh': { zh: '刷新状态', en: 'Refresh status' },
  'projectBrain.statusHistory': { zh: '状态历史', en: 'Status history' },
  'projectBrain.statusAskHead': { zh: '向项目提问', en: 'Ask the project' },
  'projectBrain.statusAskPlaceholder': { zh: '例如：我们是否偏离了北极星？有什么被阻塞了？', en: 'e.g. Are we drifting from the north star? What is blocked?' },
  'projectBrain.statusAsk': { zh: '提问', en: 'Ask' },
  'projectBrain.statusAsking': { zh: '思考中…', en: 'Thinking…' },
  'projectBrain.statusAskFailed': { zh: '提问失败', en: 'Could not ask' },
  'projectBrain.statusTrigEpic': { zh: '任务完成', en: 'epic completed' },
  'projectBrain.statusTrigDecision': { zh: '决策确认', en: 'decision committed' },
  'projectBrain.statusTrigBlocked': { zh: '工作受阻', en: 'work blocked' },
  'projectBrain.statusTrigOpen': { zh: '已刷新', en: 'refreshed' },
  'projectBrain.statusTrigManual': { zh: '手动', en: 'manual' },
  'projectBrain.statusEvOpen': { zh: '待认领', en: 'open' },
  'projectBrain.statusEvInflight': { zh: '进行中', en: 'in-flight' },
  'projectBrain.statusEvDone': { zh: '已完成', en: 'done' },
  'projectBrain.statusEvBlocked': { zh: '受阻', en: 'blocked' },
  'projectBrain.statusEvPending': { zh: '决策待定', en: 'decisions pending' },
  'projectBrain.statusEvPeers': { zh: '活跃会话', en: 'active peers' },
  'projectBrain.statusEvCharter': { zh: '章程 v', en: 'charter v' },
  'projectBrain.watchHead': { zh: '我关心的事', en: 'Things I care about' },
  'projectBrain.watchPlaceholder': { zh: '想让大脑持续关注的事…', en: 'Something you want the brain to keep an eye on…' },
  'projectBrain.watchAdd': { zh: '添加', en: 'Add' },
  'projectBrain.watchAdding': { zh: '添加中…', en: 'Adding…' },
  'projectBrain.watchAddFailed': { zh: '添加失败', en: 'Could not add' },
  'projectBrain.watchEmpty': { zh: '添加一个关注点、问题或目标，大脑会持续为你盯着它。', en: 'Add a concern, question, or goal and the brain will keep an eye on it.' },
  'projectBrain.watchKindConcern': { zh: '关注点', en: 'Concern' },
  'projectBrain.watchKindQuestion': { zh: '问题', en: 'Question' },
  'projectBrain.watchKindGoal': { zh: '目标', en: 'Goal' },
  'projectBrain.watchNotAddressed': { zh: '尚未回应', en: 'Not addressed yet' },
  'projectBrain.watchResolved': { zh: '已解决', en: 'resolved' },
  'projectBrain.watchPromoted': { zh: '已进章程', en: 'in charter' },
  // A GOAL is injected because it EXISTS — it never travels through the charter
  // (owner-directed 2026-07-30). So its badge states a fact, and there is no
  // promote button, no diverged state, no replacement preview and no version
  // gate: all of those existed only to reconcile two copies of one sentence.
  // The keys for that machinery (watchDiverged*, watchSetAsGoal, watchReadopt,
  // watchSetTitle, watchReplace*, watchRecompare, watchIsNorthStar) were
  // removed with it.
  'projectBrain.watchGoalLive': { zh: '每个会话都在读它', en: 'every conversation reads this' },
  'projectBrain.watchGoalLiveHint': { zh: '未解决的目标会进入本项目每个会话的上下文。标记解决即可撤出。', en: 'Open goals are included in every conversation of this project. Resolve it to withdraw it.' },
  'projectBrain.watchCancel': { zh: '取消', en: 'Cancel' },
  'projectBrain.watchRefresh': { zh: '重新评估', en: 'Re-check' },
  'projectBrain.watchPromote': { zh: '提升为章程', en: 'Promote to charter' },
  'projectBrain.watchResolveBtn': { zh: '标记解决', en: 'Resolve' },
  'projectBrain.watchReopen': { zh: '重新打开', en: 'Reopen' },
  'projectBrain.watchDelete': { zh: '删除', en: 'Delete' },
  // ── Tool display names for the project-brain family ──
  // These render in the tool-round header. Without them the UI showed the raw
  // snake_case identifier (e.g. "project_charter_commit"), which is an internal
  // symbol leaking into the product surface.
  'tool.project_charter_read': { zh: '读取项目章程', en: 'Read project charter' },
  'tool.project_charter_propose': { zh: '提议章程修订', en: 'Propose charter amendment' },
  'tool.project_charter_commit': { zh: '提交章程决策（已改为人工审核）', en: 'Commit charter decision (now human-reviewed)' },
  'tool.project_board_read': { zh: '读取协作看板', en: 'Read project board' },
  'tool.project_board_post': { zh: '发布看板任务', en: 'Post board epic' },
  'tool.project_board_claim': { zh: '认领看板任务', en: 'Claim board epic' },
  'tool.project_board_complete': { zh: '标记看板任务完成', en: 'Complete board epic' },
  'tool.project_board_block': { zh: '标记看板任务受阻', en: 'Block board epic' },
  'tool.project_peer_status': { zh: '查看兄弟会话状态', en: 'Peer conversation status' },
  'tool.project_feed_read': { zh: '读取项目动态', en: 'Read project activity feed' },
  'tool.project_message': { zh: '给兄弟会话发消息', en: 'Message a peer conversation' },
  'tool.project_intervene': { zh: '干预兄弟会话', en: 'Intervene in a peer conversation' },
  'tool.list_conversations': { zh: '列出历史会话', en: 'List conversations' },
  'tool.get_conversation': { zh: '读取历史会话', en: 'Read a conversation' },
  'projectBrain.peersHere': { zh: '当前在场 {n} 个', en: '{n} here now' },
  'projectBrain.peerSubAgent': { zh: '子代理 {id}', en: 'sub-agent {id}' },
  'projectBrain.peerUntitled': { zh: '会话 {id}', en: 'conversation {id}' },
  'projectBrain.peerAdvancing': { zh: '推进《{epic}》', en: 'advancing «{epic}»' },
  'projectBrain.peerEditing': { zh: '正在编辑 {file}', en: 'editing {file}' },
  'projectBrain.peerThread': { zh: '跨会话消息', en: 'Cross-conversation messages' },
  'projectBrain.peerNudge': { zh: '发消息', en: 'Nudge' },
  'projectBrain.peerNudgePlaceholder': { zh: '给这个会话发一条建议（它会在下一轮看到）…', en: 'Send this conversation an advisory note (it sees it on its next turn)…' },
  'projectBrain.peerNudgeSend': { zh: '发送', en: 'Send' },
  'projectBrain.peerNudgeCancel': { zh: '取消', en: 'Cancel' },
  'projectBrain.peerNudgeSent': { zh: '已发送', en: 'Sent' },
  'projectBrain.peerNudgeRateLimited': { zh: '发送太频繁，请稍后再试', en: 'Too many messages — try again shortly' },
  'projectBrain.peerNudgeFailed': { zh: '发送失败', en: 'Send failed' },
  'projectBrain.peerStop': { zh: '终止', en: 'Stop' },
  'projectBrain.peerStopConfirm': { zh: '确定终止“{who}”正在运行的任务吗？只会终止其任务，不会影响主机进程。', en: 'Hard-abort the running task(s) of "{who}"? This stops its task only — it never touches the host process.' },
  'projectBrain.peerStopConfirmOk': { zh: '终止任务', en: 'Stop the task' },
  'projectBrain.peerStopped': { zh: '已终止', en: 'Stopped' },
  'projectBrain.peerStopFailed': { zh: '终止失败', en: 'Stop failed' },
  'projectBrain.charterSoon': { zh: '北极星 · 即将推出', en: 'North star — coming soon' },
  'projectBrain.boardSoon': { zh: '协作任务板 · 即将推出', en: 'Coordination board — coming soon' },
  'projectBrain.activityEmpty': { zh: '暂无动态', en: 'No activity yet' },
  'projectBrain.charterEmpty': { zh: '尚无章程', en: 'No charter yet' },
  'projectBrain.boardEmpty': { zh: '任务板为空', en: 'Board is empty' },
  'projectBrain.noProject': { zh: '当前会话未绑定项目 —— 绑定项目（Studio）后，项目大脑会显示在这里。', en: 'No project is attached to this conversation — attach one (Studio) to open its Brain.' },
  'projectBrain.committedDecisions': { zh: '已确认的决策', en: 'Committed decisions' },
  'projectBrain.pendingProposals': { zh: '提议中（待你审核）', en: 'Proposed (awaiting your review)' },
  'projectBrain.healthNoGoal': { zh: '尚未设定北极星目标——以下决策仅为实现级意图，不是项目目标。', en: 'No north-star goal is set — the decisions below are implementation-level intent only.' },
  'projectBrain.healthStats': { zh: '{n} 条决策 · 每轮注入 {m} 条', en: '{n} decisions · {m} shown per turn' },
  'projectBrain.summaryPlaceholder': { zh: '一句话规则（必填）', en: 'One-line summary (required)' },
  'projectBrain.commit': { zh: '确认', en: 'Commit' },
  'projectBrain.committing': { zh: '确认中…', en: 'Committing…' },
  'projectBrain.reject': { zh: '驳回', en: 'Reject' },
  'projectBrain.commitFailed': { zh: '确认失败', en: 'Commit failed' },
  'projectBrain.rejectFailed': { zh: '驳回失败', en: 'Reject failed' },
  'projectBrain.answerFailed': { zh: '提交回答失败', en: 'Submitting the answer failed' },
  'projectBrain.saveFailed': { zh: '保存失败', en: 'Save failed' },
  'projectBrain.deleteFailed': { zh: '删除失败', en: 'Delete failed' },
  'projectBrain.boardActionFailed': { zh: '任务板操作失败', en: 'Board action failed' },
  'projectBrain.loadFailed': { zh: '项目大脑加载失败', en: 'Loading the project brain failed' },
  'projectBrain.previewFailed': { zh: '会话预览加载失败', en: 'Conversation preview failed' },
  'projectBrain.editNorthStar': { zh: '编辑北极星目标', en: 'Edit north star' },
  'projectBrain.editDecision': { zh: '编辑决策', en: 'Edit decision' },
  'projectBrain.deleteDecision': { zh: '删除决策', en: 'Delete decision' },
  'projectBrain.deleteCharter': { zh: '删除章程', en: 'Delete charter' },
  'projectBrain.save': { zh: '保存', en: 'Save' },
  // The decision's summary IS the line the per-turn injection renders; the
  // label says so, because a human editing only the body used to leave every
  // sibling conversation reading the old rule.
  'projectBrain.summaryLabel': { zh: '规则行（每个会话读到的就是这一行）', en: 'Rule line (this is what every conversation reads)' },
  'projectBrain.saving': { zh: '保存中…', en: 'Saving…' },
  'projectBrain.cancel': { zh: '取消', en: 'Cancel' },
  'projectBrain.confirmDelete': { zh: '确认删除？', en: 'Confirm?' },
  'projectBrain.laneOpen': { zh: '待认领', en: 'Open' },
  'projectBrain.laneClaimed': { zh: '推进中', en: 'In progress' },
  'projectBrain.laneHeld': { zh: '占用中（请勿编辑）', en: 'Held (do not edit)' },
  'projectBrain.heldBy': { zh: '占用者', en: 'held by' },
  'projectBrain.laneDone': { zh: '已完成', en: 'Done' },
  'projectBrain.laneBlocked': { zh: '受阻（等待外部条件）', en: 'Blocked (waiting on a gate)' },
  'projectBrain.blockedRetry': { zh: '自动重试于', en: 'auto-retry in' },
  'projectBrain.blockedCount': { zh: '已阻塞 %d 次', en: 'blocked %d×' },
  'projectBrain.laneAwaiting': { zh: '需要你的回答', en: 'Awaiting your answer' },
  'projectBrain.needsYourDecision': { zh: '需要你的决定', en: 'Your decision needed' },
  'projectBrain.awaitingAnswerMeta': { zh: '等待你的回答', en: 'waiting for your answer' },
  'projectBrain.answerPlaceholder': { zh: '输入你的回答（或直接点上方选项）…', en: 'Type your answer (or pick an option above)…' },
  'projectBrain.answerSubmit': { zh: '提交回答', en: 'Submit answer' },
  'projectBrain.yourAnswer': { zh: '你的回答', en: 'Your answer' },
  'projectBrain.blockNoteSubmit': { zh: '标记阻塞', en: 'Mark blocked' },
  'projectBrain.blockNoteCancel': { zh: '取消', en: 'Cancel' },
  'projectBrain.autoStart': { zh: '约 30 秒后自动拉起', en: 'auto-starts ~30s' },
  'projectBrain.autoStartTitle': { zh: '项目大脑心跳（约 30 秒）会自动拉起这个任务', en: 'The project brain heartbeat (~30s) will pick this up automatically' },
  'projectBrain.newEpic': { zh: '新建任务', en: 'New epic' },
  'projectBrain.newEpicNoConv': { zh: '打开一个会话后才能发布任务', en: 'Open a conversation to post an epic' },
  'projectBrain.newEpicPrompt': { zh: '任务标题', en: 'New epic title' },
  'projectBrain.actComplete': { zh: '完成', en: 'Done' },
  'projectBrain.actBlock': { zh: '受阻', en: 'Block' },
  'projectBrain.actReopen': { zh: '重开', en: 'Reopen' },
  'projectBrain.blockReasonPrompt': { zh: '为什么受阻？', en: 'Why is this blocked?' },
  'projectBrain.peerNone': { zh: '当前没有活跃的协作会话。', en: 'No active peers right now.' },
  'projectBrain.peerRound': { zh: '第 {n} 轮', en: 'round {n}' },
  'projectBrain.proposalPending': { zh: '等待人工审核 — 在项目大脑面板确认或驳回', en: 'Awaiting human review — commit or reject in the Project Brain panel' },
  'projectBrain.boardVerb.read': { zh: '读取任务板', en: 'read board' },
  'projectBrain.boardVerb.post': { zh: '发布', en: 'posted' },
  'projectBrain.boardVerb.claim': { zh: '认领', en: 'claimed' },
  'projectBrain.boardVerb.complete': { zh: '完成', en: 'completed' },
  'projectBrain.boardVerb.block': { zh: '标记受阻', en: 'blocked' },
  'projectBrain.boardVerb.reopen': { zh: '重开', en: 'reopened' },
  'projectBrain.boardUntitled': { zh: '（未命名任务）', en: '(untitled epic)' },
  'projectBrain.boardFailed': { zh: '失败', en: 'failed' },
  // ── Transcript tool-card: activity-feed + peer-message/intervene delivery ──
  'projectBrain.thisConv': { zh: '本对话', en: 'this conversation' },
  'projectBrain.pdMessage': { zh: '发送消息', en: 'Message' },
  'projectBrain.pdIntervene': { zh: '建议干预', en: 'Advisory intervention' },
  'projectBrain.pdHardIntervene': { zh: '强制干预', en: 'Hard intervention' },
  'projectBrain.pdOutcome.delivered': { zh: '已送达', en: 'Delivered' },
  'projectBrain.pdOutcome.rate_limited': { zh: '限流中', en: 'Rate-limited' },
  'projectBrain.pdOutcome.denied': { zh: '被拒绝', en: 'Denied' },
  'projectBrain.pdOutcome.failed': { zh: '发送失败', en: 'Failed' },
  'projectBrain.dispatched': { zh: '自动', en: 'auto' },
  'projectBrain.dispatchedTitle': { zh: '由项目大脑自动启动', en: 'Started autonomously by the project brain' },
  'projectBrain.kind.started': { zh: '开始', en: 'Started' },
  'projectBrain.kind.completed': { zh: '完成', en: 'Completed' },
  'projectBrain.kind.aborted': { zh: '已中止', en: 'Aborted' },
  'projectBrain.kind.run_concluded': { zh: '自动运行收尾', en: 'Run concluded' },
  'projectBrain.kind.blocked': { zh: '受阻', en: 'Blocked' },
  'projectBrain.kind.answered': { zh: '已回答', en: 'Answered' },
  'projectBrain.kind.decided': { zh: '决策', en: 'Decided' },
  'projectBrain.kind.proposed_decision': { zh: '提议决策', en: 'Proposed decision' },
  'projectBrain.kind.claimed': { zh: '认领', en: 'Claimed' },
  'projectBrain.kind.dismissed': { zh: '驳回提议', en: 'Dismissed' },
  'projectBrain.kind.note': { zh: '备注', en: 'Note' },
  'projectBrain.legendTitle': { zh: '图例', en: 'Legend' },
  'projectBrain.translateToggle': { zh: '翻译内容', en: 'Translate content' },
  'projectBrain.translateToggleTitle': { zh: '将章程、任务、动态等内容按界面语言翻译显示（原文始终保留，悬停可见）', en: 'Show charter / board / activity content translated to the UI language (the original is always preserved — hover to see it)' },
  // ── Conv-meta tool cards: localized peer status tokens ──
  'projectBrain.stGenerating': { zh: '生成中', en: 'generating' },
  'projectBrain.stWorking': { zh: '工作中', en: 'working' },
  'projectBrain.stIdle': { zh: '空闲', en: 'idle' },
  'projectBrain.stWorkingPhase': { zh: '工作中（{phase}）', en: 'working ({phase})' },
  // ── Conv-meta tool cards: localized header labels ("what the tool did") ──
  'brainHead.boardRead': { zh: '查看了团队看板', en: 'Checked the team board' },
  'brainHead.boardMutate': { zh: '更新了团队看板', en: 'Updated the team board' },
  'brainHead.charterRead': { zh: '查看了项目章程', en: 'Read the project charter' },
  'brainHead.charterPropose': { zh: '提交了一项章程决策提议', en: 'Proposed a charter decision' },
  'brainHead.peerStatus': { zh: '查看了当前还有谁在协作', en: 'Checked who else is working now' },
  'brainHead.feedRead': { zh: '回顾了团队近期动态', en: 'Reviewed recent team activity' },
  'brainHead.message': { zh: '向另一个会话发送了消息', en: 'Sent a note to another conversation' },
  'brainHead.intervene': { zh: '向另一个会话提示了工作重叠', en: 'Flagged an overlap to another conversation' },
  'brainHead.listConvs': { zh: '搜索了历史会话', en: 'Searched past conversations' },
  'brainHead.getConv': { zh: '打开了一个历史会话', en: 'Opened a past conversation' },
  'brainHead.claimPath': { zh: '预定了要编辑的文件', en: 'Reserved files for editing' },
  'brainHead.releasePath': { zh: '释放了文件预定', en: 'Released a file reservation' },
  'brainHead.commit': { zh: '提交了本对话的改动', en: "Committed this conversation's work" },
  // ── Checklist (todo_write) card ──
  'todo.head': { zh: '任务清单', en: 'Checklist' },
  'todo.cleared': { zh: '任务清单已清空', en: 'Checklist cleared' },
  'todo.emptyBody': { zh: '暂无清单项。', en: 'No checklist items.' },
  // ── Conv-meta tool cards: at-a-glance count chip for collapsed reads ──
  'brainChip.peers': { zh: '{n} 个活跃', en: '{n} active' },
  'brainChip.openEpics': { zh: '{n} 项待认领', en: '{n} open' },
  'brainChip.events': { zh: '{n} 条动态', en: '{n} events' },
  'brainChip.msgs': { zh: '{n} 条消息', en: '{n} messages' },
  // ── Conv-meta tool cards: localized source chips ──
  'brainSrc.board': { zh: '团队看板', en: 'Team board' },
  'brainSrc.charter': { zh: '章程', en: 'Charter' },
  'brainSrc.conversations': { zh: '会话', en: 'Conversations' },
  'brainSrc.peer': { zh: '团队', en: 'Team' },
  'brainSrc.git': { zh: 'Git', en: 'Git' },
  // ── Conv-meta tool cards: "why this ran / what it means" captions ──
  'brainWhy.peerStatus': { zh: '本项目中此刻正在运行的其他会话——用于避免重复正在进行中的工作。', en: 'Sibling conversations of this project that are running right now — used to avoid duplicating work already in progress.' },
  'brainWhy.boardRead': { zh: '本项目所有会话共享的待办看板——谁在做什么，避免重复工作。', en: 'The shared to-do board across all conversations of this project — who is doing what, so work isn\'t duplicated.' },
  'brainWhy.boardMutate': { zh: '更新共享待办看板，让其他会话看到这次认领／变更。', en: 'Updates the shared to-do board so sibling conversations see this claim / change.' },
  'brainWhy.feedRead': { zh: '本项目其他会话近期活动的时间线。', en: 'A recent timeline of what other conversations of this project have been doing.' },
  'brainWhy.charterRead': { zh: '项目的共享目标与已确定的决策，所有会话都以此对齐。', en: 'The project\'s shared goal and committed decisions that every conversation aligns to.' },
  'brainWhy.charterPropose': { zh: '提议一项决策，交由人工确认为项目级共享意图——确认前仅为建议。', en: 'Proposes a decision for the human to commit as shared project-wide intent — advisory until approved.' },
  'brainWhy.message': { zh: '给同项目某个会话的一条建议性留言，在它下一轮时送达——不会打断正在进行的回合。', en: 'An advisory note to a sibling conversation, delivered on its next turn — it never interrupts a running turn.' },
  'brainWhy.intervene': { zh: '提醒同项目某个会话重新核对看板（建议性）；强制终止需人工明确批准。', en: 'Nudges a sibling conversation to re-check the board (advisory); a hard stop needs explicit human approval.' },
  'brainWhy.claimPath': { zh: '在共享看板上预定特定文件／路径，让其他会话在本会话工作期间暂缓编辑——这是有时效、会自动过期的建议性占用，而非硬锁。', en: 'Reserves specific files/paths on the shared board so sibling conversations hold off editing them while this conversation works — a durational, auto-expiring advisory lease, not a hard lock.' },
  'brainWhy.releasePath': { zh: '清除先前的文件／路径占用，让其他会话可以再次编辑这些路径。', en: 'Clears a previously-held file/path reservation so sibling conversations may edit those paths again.' },
  'brainWhy.commit': { zh: '只提交本对话可证明为自己所写的文件（与本会话上一次写入逐字节一致）；同时带有其他会话未提交改动的文件会被保留，绝不一并提交。', en: "Commits ONLY the files this conversation provably authored (byte-identical to its own last edit); files also carrying a sibling's uncommitted changes are held back, never swept in." },
  'brainWhy.getConv': { zh: '打开另一个历史会话的完整记录——它的消息、工具调用与结果——以便复用早先工作中的决策或上下文。', en: 'Opens the full transcript of another past conversation — its messages, tool calls, and results — so the agent can reuse decisions or context from earlier work.' },
  'brainWhy.listConvs': { zh: '按标题和内容搜索你的其他会话，找到可供引用的相关历史讨论。', en: 'Searches your other conversations by title and content to find a relevant past discussion to reference.' },
  // ── Conversation digest card (get_conversation) ──
  'convDigest.roleUser': { zh: '用户', en: 'User' },
  'convDigest.roleAssistant': { zh: '助手', en: 'Assistant' },
  'convDigest.roleSystem': { zh: '系统', en: 'System' },
  'convDigest.msgCount': { zh: '{n} 条消息', en: '{n} messages' },
  'convDigest.images': { zh: '{n} 张图片', en: '{n} image' },
  'convDigest.pdfs': { zh: '{n} 个 PDF', en: '{n} PDF' },
  'convDigest.noText': { zh: '（无文本）', en: '(no text)' },
  'convDigest.empty': { zh: '该会话没有消息。', en: 'This conversation has no messages.' },
  'convDigest.truncated': { zh: '… 已省略较早的消息——点工具行右侧 </> 按钮查看完整请求记录。', en: '… earlier messages omitted — use the </> button on a tool row for the full request record.' },
  'convDigest.updated': { zh: '更新于 {t}', en: 'updated {t}' },
  'convDigest.expand': { zh: '展开', en: 'expand' },
  'convDigest.omitted': { zh: '… 省略 {n} 条消息 …', en: '… {n} messages omitted …' },
  'convDigest.summary': { zh: '摘要', en: 'summary' },
  'convDigest.raw': { zh: 'RAW · 调试', en: 'RAW · debug' },
  'convDigest.rev': { zh: '版本', en: 'rev' },
  'convDigest.rawTip': { zh: '原始调试读取——每条消息附带底层元数据（模型、token、结束原因、消息 ID）。', en: 'Raw debug read — shows per-message low-level metadata (model, tokens, finish reason, id).' },
  'convDigest.metaModel': { zh: '模型', en: 'model' },
  'convDigest.metaTokens': { zh: 'token 输入/输出', en: 'tokens in/out' },
  'convDigest.metaFinish': { zh: '结束原因', en: 'finish reason' },
  'convDigest.metaId': { zh: '消息 ID', en: 'message id' },
  // ── Commit result card (project_commit) ──
  'commitCard.outCommitted': { zh: '已提交', en: 'committed' },
  'commitCard.outPlanned': { zh: '仅预览', en: 'plan only' },
  'commitCard.outVerifyMismatch': { zh: '校验不一致', en: 'verify mismatch' },
  'commitCard.outFailed': { zh: '未提交', en: 'not committed' },
  'commitCard.shaTitle': { zh: '提交哈希', en: 'commit hash' },
  'commitCard.committedHead': { zh: '已提交（{n}）—— 确属本会话', en: 'Committed ({n}) — provably yours' },
  'commitCard.wouldCommitHead': { zh: '将提交（{n}）—— 确属本会话', en: 'Would commit ({n}) — provably yours' },
  'commitCard.heldHead': { zh: '已保留（{n}）—— 未提交', en: 'Held back ({n}) — not committed' },
  'commitCard.noneClean': { zh: '没有可证明属于本会话的文件。', en: 'No files were provably attributable to this conversation.' },
  'projectBrain.justNow': { zh: '刚刚', en: 'just now' },
  'projectBrain.minutesAgo': { zh: '{n} 分钟前', en: '{n}m ago' },
  'projectBrain.hoursAgo': { zh: '{n} 小时前', en: '{n}h ago' },
  'projectBrain.daysAgo': { zh: '{n} 天前', en: '{n}d ago' },
  // ── Per-conversation Influence lens ──
  'projectBrain.influenceTitle': { zh: '本对话如何被项目大脑影响', en: 'How this conversation is influenced' },
  'projectBrain.infCharterBound': { zh: '受章程约束', en: 'bound by charter' },
  'projectBrain.infOwns': { zh: '你在推进 {n} 项', en: '{n} owned by you' },
  'projectBrain.infAvoid': { zh: '需避让 {n} 项', en: '{n} to avoid' },
  'projectBrain.infPending': { zh: '{n} 项待你决策', en: '{n} awaiting you' },
  'projectBrain.infCharterHead': { zh: '受章程约束', en: 'Bound by the charter' },
  'projectBrain.infMineHead': { zh: '你正在推进的任务', en: 'Epics you are advancing' },
  'projectBrain.infAvoidHead': { zh: '避免重复 —— 其他对话正在推进', en: 'Avoid duplicating — advanced by a sibling' },
  'projectBrain.infOpenHead': { zh: '待认领 —— 你可以接手', en: 'Open — you could claim' },
};

/* ── Missing-translation tripwire ──────────────────────────────────────
 * Records keys that had to fall back out of the active language. One-shot
 * per (key, lang) so a render loop cannot flood the console.
 *
 * Why this exists: see the comment inside t(). A language pack that omits
 * keys currently degrades SILENTLY to Chinese. This makes the degrade
 * audible without changing what the user sees.
 *
 * Exposed as window.i18nMissingKeys() so a test — or a developer switching
 * to English — can assert the set is empty instead of eyeballing the UI.
 */
var _i18nMissing = Object.create(null);

function _reportMissingTranslation(key, lang) {
  var mark = lang + '\u0000' + key;
  if (_i18nMissing[mark]) return;
  _i18nMissing[mark] = true;
  try {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[i18n] missing "' + lang + '" for key "' + key +
                   '" — fell back to zh. A language pack that omits this key ' +
                   'would render Chinese in a ' + lang + ' UI.');
    }
  } catch (e) { /* console unavailable — the record below still stands */ }
}

/* Snapshot of every key that fell back, as ['lang:key', …]. Test seam. */
function i18nMissingKeys() {
  return Object.keys(_i18nMissing).map(function (m) {
    return m.replace('\u0000', ':');
  });
}

function resetI18nMissingKeysForTests() {
  _i18nMissing = Object.create(null);
}

/**
 * Get translated text for a key. Falls back to the key itself if not found.
 * Supports interpolation: t('key', { count: 5 }) replaces {count} in the string.
 *
 * @param {string} key - Translation key
 * @param {Object} [params] - Optional interpolation parameters
 * @returns {string} Translated text
 */
function t(key, params) {
  var entry = _i18n[key];
  /* Missing-translation resolution — MUST stay observable.
   *
   * The old expression was `entry[_i18nLang] || entry.zh || key`. That silently
   * renders Chinese whenever the active language lacks a key, with no error and
   * no console trace. Today every key carries both languages so the branch is
   * unreachable; the moment a single-language pack ships (Epic-E sub-part 1,
   * measured worth 7.6% of the compressed first paint) it becomes reachable for
   * EVERY key the pack omits — and an English UI would quietly fill with
   * Chinese. A defect with no failure signal cannot be caught by any test or by
   * a user who does not read the other language.
   *
   * So the fallback still HAPPENS (never regress the UI to a raw key), but it
   * is now REPORTED once per key. That converts "silently wrong" into "wrong
   * and traceable", which is the precondition for shipping language packs at
   * all. Reporting is one-shot per key so a hot render loop cannot flood.
   */
  var text;
  if (!entry) {
    text = key;
  } else if (entry[_i18nLang] != null) {
    text = entry[_i18nLang];
  } else {
    _reportMissingTranslation(key, _i18nLang);
    text = entry.zh != null ? entry.zh : key;
  }
  if (params) {
    for (var k in params) {
      if (params.hasOwnProperty(k)) {
        text = text.replace(new RegExp('\\{' + k + '\\}', 'g'), params[k]);
      }
    }
  }
  return text;
}

/**
 * Does the in-memory dictionary already carry `lang`? Checks entries until
 * one is found (a single-language pack answers on the first entry; a dual
 * dictionary — dev fallback / pre-split bundle — also answers immediately).
 */
function _i18nHasLang(lang) {
  for (var k in _i18n) {
    if (!_i18n.hasOwnProperty(k)) continue;
    var e = _i18n[k];
    if (e && typeof e === 'object') return e[lang] != null;
  }
  return false;
}

/* Merge a fetched pack's dictionary into _i18n. Entry shape is {key:{lang:…}}
 * so merging is a per-entry union: the boot language's entries stay, the
 * fetched language's entries join them. Idempotent by construction. */
function _i18nMergeDict(packDict) {
  for (var k in packDict) {
    if (!packDict.hasOwnProperty(k)) continue;
    var cur = _i18n[k];
    if (cur && typeof cur === 'object' && typeof packDict[k] === 'object') {
      for (var l in packDict[k]) { cur[l] = packDict[k][l]; }
    } else {
      _i18n[k] = packDict[k];
    }
  }
}

/* Fetch a language pack and merge its dictionary. Resolves true on success.
 * The pack is a FULL per-language i18n.js: loading it re-executes module
 * init (idempotent — same localStorage value, same cookie) and REASSIGNS the
 * global _i18n to its single-language dict, so we save ours first and merge
 * the pack's dict back over it. */
function _i18nFetchPack(lang) {
  return new Promise(function (resolve) {
    var urls = (typeof window !== 'undefined') && window.__I18N_PACK_URLS__;
    var url = urls && urls[lang];
    if (!url) { resolve(false); return; }
    var prevDict = _i18n;
    var s = document.createElement('script');
    s.src = url;
    s.onload = function () {
      var packDict = _i18n;
      _i18n = prevDict;
      _i18nMergeDict(packDict);
      resolve(true);
    };
    s.onerror = function () { resolve(false); };
    (document.head || document.documentElement).appendChild(s);
  });
}

/**
 * Set the UI language and re-apply all translations.
 * Async: when the target language is not in memory (single-language pack
 * boot), its pack is fetched and merged first. On fetch failure the switch
 * still proceeds — t() falls back per key to zh and the tripwire
 * (_reportMissingTranslation) makes every miss observable. That is the
 * designed degrade: visibly wrong-language where uncovered, never broken,
 * never silent.
 * @param {'zh'|'en'} lang
 */
async function setLanguage(lang) {
  if (lang !== 'zh' && lang !== 'en') return;
  if (!_i18nHasLang(lang)) {
    await _i18nFetchPack(lang);
  }
  _i18nLang = lang;
  localStorage.setItem('tofu_ui_lang', lang);
  /* Mirror immediately so the NEXT page load is served the matching bundle.
   * Without this the server would keep shipping the previous language's pack. */
  _syncLangCookie(lang);
  _applyI18n();
}

/**
 * Apply translations to all elements with data-i18n attributes.
 * Also handles data-i18n-placeholder, data-i18n-title.
 */
function _applyI18n() {
  // Text content
  document.querySelectorAll('[data-i18n]').forEach(function(el) {
    var key = el.getAttribute('data-i18n');
    if (key) el.textContent = t(key);
  });
  // innerHTML (for entries that contain HTML tags)
  document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-html');
    if (key) el.innerHTML = t(key);
  });
  // Placeholder
  document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-placeholder');
    if (key) el.placeholder = t(key);
  });
  // Title (tooltip)
  document.querySelectorAll('[data-i18n-title]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-title');
    if (key) el.title = t(key);
  });
  // Update html lang attribute
  document.documentElement.lang = _i18nLang === 'zh' ? 'zh-CN' : 'en';
  // Sync language dropdown if it exists
  var langSelect = document.getElementById('settingLanguage');
  if (langSelect) langSelect.value = _i18nLang;
  // Sync language picker cards
  _syncLangPicker(_i18nLang);
}

/**
 * Handler for language dropdown change in settings.
 * @param {'zh'|'en'} lang
 */
function _onLanguageChange(lang) {
  /* setLanguage is now async (may fetch the other language's pack). Everything
   * below reads t() at REPAINT time, so it must run after the merge — chain
   * it, or the conversation list / open chat would render one language behind. */
  Promise.resolve(setLanguage(lang)).then(function () {
  _syncLangPicker(lang);
  // Re-render dynamic content that uses t()
  if (typeof renderConversationList === 'function') renderConversationList();
  // ★ Repaint the open conversation so message chrome (tool labels,
  //   finish-info, timestamps) re-renders with the new language. The
  //   former call was to renderMessages(), which never existed — the
  //   whole-chat repaint is renderChat(conv). (caught by tsc --checkJs)
  if (typeof getActiveConv === 'function') {
    var _activeConv = getActiveConv();
    if (_activeConv) window.ConvView.replaceAll(_activeConv.id, { forceScroll: true });
  }
  if (typeof _refreshOptimizerPanel === 'function') {
    try { _refreshOptimizerPanel(); } catch (e) { /* panel may not be open */ }
  }
  // Project Brain content-translation overlay: the UI language flip changes the
  // translation target, so re-run the overlay (and re-sync its toggle label).
  if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n) {
    try {
      if (typeof ProjectBrainI18n.initToggle === 'function') ProjectBrainI18n.initToggle();
      if (typeof ProjectBrainI18n.applyAll === 'function') ProjectBrainI18n.applyAll();
    } catch (e) { /* panel may not be open */ }
  }
  if (typeof _timerPanelOpen !== 'undefined' && _timerPanelOpen && typeof _refreshTimerPanel === 'function') {
    try { _refreshTimerPanel(); } catch (e) { /* panel may not be open */ }
  }
  // Re-render Paper Reader dynamic content (library, titles, Q&A, landing,
  // and the active right-hand tab — Q&A / Report / Babel PDF).
  if (typeof paperMode !== 'undefined' && paperMode) {
    try {
      if (typeof _renderPaperLibrary === 'function') _renderPaperLibrary();
      if (typeof _updatePaperTitles === 'function') _updatePaperTitles();
      if (typeof _renderPaperQA === 'function') _renderPaperQA();
      if (typeof _paperFileName !== 'undefined' && !_paperFileName && typeof _showPaperLanding === 'function') _showPaperLanding();
      // Refresh the Report tab so JS-rendered strings (reading-time bar
      // labels, TOC heading "Contents", finish tag) follow the new language.
      // Prefer the stream painter when a stream exists (handles tool rounds /
      // thinking too); reset its dedup markers so the body actually re-renders.
      // Otherwise repaint a cached/finished report directly.
      if (typeof _reportView === 'function') {
        ['report', 'review'].forEach(function(_kind) {
          var _v = _reportView(_kind);
          if (_v.stream) {
            _v.stream._lastRenderedLen = -1;
            _v.stream._lastRenderedStatus = '';
            _v.stream._lastToolKey = '';
            var _rcS = document.getElementById(_v.containerId);
            var _prevTopS = _rcS ? _rcS.scrollTop : 0;
            if (typeof _paintReportFromState === 'function') _paintReportFromState(_v);
            if (_rcS) _rcS.scrollTop = _prevTopS;
          } else {
            var _rc = document.getElementById(_v.containerId);
            if (_rc && _v.cache && typeof _renderFinalReport === 'function') {
              // _renderFinalReport rebuilds innerHTML (resets scrollTop to 0).
              // Preserve the reader's scroll position across the relabel.
              var _prevTop = _rc.scrollTop;
              _renderFinalReport(_rc, _v.cache, undefined, _v);
              _rc.scrollTop = _prevTop;
            }
          }
        });
      }
      // Refresh the Babel PDF tab if it is the active panel (rebuilds its
      // static chrome — subtitle, Original button, empty state).
      var _babelPanel = document.querySelector('.paper-tab-panel[data-tab="translate"]');
      if (_babelPanel && _babelPanel.style.display !== 'none' && typeof _initBabelPdfTab === 'function') _initBabelPdfTab();
    } catch (e) { /* paper reader may not be initialised */ }
  }
  // Re-render the MCP catalog grid (dynamic panel) when its tab is open.
  // Uses the cached _mcpCatalog — a pure repaint, no network refetch.
  if (typeof _renderMcpCatalog === 'function') {
    var _mcpGrid = document.getElementById('mcpCatalogGrid');
    if (_mcpGrid && _mcpGrid.offsetParent !== null) {
      try {
        if (typeof _renderMcpCategoryBar === 'function') _renderMcpCategoryBar();
        _renderMcpCatalog();
        if (typeof _renderMcpInstalled === 'function') _renderMcpInstalled();
        if (typeof _mcpUpdateToolCount === 'function') _mcpUpdateToolCount();
      } catch (e) { /* MCP tab may not be initialised */ }
    }
  }
  // Re-render the compaction viewer drawer's JS-rendered content (meta rows +
  // active tab) when it's open. Its static chrome (data-i18n) is already
  // handled by the _applyI18n() scan above; this covers the dynamic parts.
  if (typeof _cvOnLanguageChange === 'function') {
    try { _cvOnLanguageChange(); } catch (e) { /* drawer may not be open */ }
  }
  // Re-render the Skills tab (cards/pagination/actions are JS-rendered from
  // cached _skillsCatalog/_skillsInstalled) when its grid is visible.
  if (typeof _skillsRender === 'function') {
    var _skGrid = document.getElementById('skillsCatalogGrid');
    if (_skGrid && _skGrid.offsetParent !== null) {
      try { _skillsRender(); } catch (e) { /* skills tab may not be initialised */ }
    }
  }
  // Re-render the Memory modal (cards/stats/toggle button are JS-rendered from
  // cached _memoryCache) when the modal is open.
  var _memModal = document.getElementById('memoryModal');
  if (_memModal && _memModal.classList.contains('open')) {
    try {
      if (typeof _updateMemoryModalBtn === 'function') _updateMemoryModalBtn();
      if (typeof _renderMemoryCards === 'function' && typeof _memoryCache !== 'undefined') {
        _updateMemoryStats(_memoryCache);
        _renderMemoryCards(_memoryCache);
      }
    } catch (e) { /* memory modal may not be initialised */ }
  }
  if (typeof _renderProvidersTab === 'function') {
    try { _renderProvidersTab(); } catch (e) { /* tab may not be initialised */ }
  }
  if (typeof _renderPresetsTab === 'function' && typeof _serverConfig !== 'undefined' && _serverConfig) {
    try { _renderPresetsTab(_serverConfig); } catch (e) { /* tab may not be initialised */ }
  }
  });
}

/** Sync visual language picker cards to the given lang */
function _syncLangPicker(lang) {
  document.querySelectorAll('.lang-option').forEach(function(el) {
    el.classList.toggle('active', el.getAttribute('data-lang') === lang);
  });
}

// Apply translations once DOM is ready
document.addEventListener('DOMContentLoaded', function() {
  _applyI18n();
});
