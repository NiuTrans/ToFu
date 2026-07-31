"""Constants for the typed error envelope: the closed ``kind`` enum, the
per-kind severity / retryable classification sets, the bilingual hint
strings, and the ``_TITLES`` title/hint table indexed by kind.

See :mod:`lib.error_envelope` (the package ``__init__``) for the full
envelope-shape documentation.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# Closed enum of valid `kind` values.  Used to validate envelopes at
# construction time so a typo (e.g. 'rateLimit') doesn't silently leak
# through to the UI as a generic error.
KINDS = frozenset({
    'quota', 'ratelimit', 'permission', 'no_slot', 'dispatch_exhausted',
    'timeout', 'network', 'endpoint_unreachable', 'content_filter', 'invalid_image',
    'prompt_too_long', 'stream_only', 'model_limit',
    'tool_rounds_exhausted', 'tool_timeout',
    'premature_close', 'abnormal_stop', 'aborted', 'server_offline',
    'internal', 'generic',
    'bad_request', 'upstream_error', 'worker_lost', 'budget_exceeded',
    'content_refused',
    'tool_not_available',
})

# Default severities — warnings are recoverable / user-actionable, errors
# usually mean the task ended in a non-recoverable state.
_WARNING_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network', 'endpoint_unreachable',
    'tool_rounds_exhausted', 'tool_timeout',
    'premature_close', 'abnormal_stop',
    'aborted', 'server_offline',
    'upstream_error', 'worker_lost', 'budget_exceeded', 'content_refused',
    'tool_not_available',
})

# Kinds where retrying THE SAME REQUEST is genuinely likely to help
# (transient).  The frontend uses this to gate a "Retry" button.
_RETRYABLE_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network', 'endpoint_unreachable',
    'premature_close', 'abnormal_stop', 'server_offline',
    'tool_timeout', 'upstream_error', 'worker_lost', 'content_refused',
})


_SETTINGS_HINT_CN = (
    '• 打开 「设置 → Keys / Providers」，检查是否有 Key 被自动停用（429/余额耗尽），'
    '手动重新启用或添加新 Key。\n'
    '• 或者稍等几分钟，让 API 限额窗口重置后再试。\n'
    '• 若问题持续，可在设置中切换到其他可用模型 / Provider。'
)

_SETTINGS_HINT_EN = (
    '• Open "Settings → Keys / Providers" — check if any key was auto-disabled '
    '(429 / quota exhausted) and re-enable or add a new key.\n'
    '• Or wait a few minutes for the API rate-limit window to reset, then retry.\n'
    '• If the issue persists, switch to another available model / provider in Settings.'
)

_PERMISSION_HINT_CN = (
    '• 打开 「设置 → Keys」 检查该 Provider 的 Key 是否填写正确、是否被停用。\n'
    '• 若该 Key 对当前模型没有访问权限，请更换为其它模型或申请开通。'
)

_PERMISSION_HINT_EN = (
    '• Open "Settings → Keys" and verify the key for this provider is correct and enabled.\n'
    '• If the key does not have access to this model, switch models or request access.'
)

_TIMEOUT_HINT_CN = '• 稍后重试。若持续超时，可在 「设置 → 模型默认」 切换到响应更快的模型。'
_TIMEOUT_HINT_EN = ('• Retry shortly. If timeouts persist, switch to a faster model '
                    'in "Settings → Model defaults".')

_NETWORK_HINT_CN = '• 检查本机网络 / 代理设置，然后重试。'
_NETWORK_HINT_EN = '• Check your network / proxy settings, then retry.'

_UNREACHABLE_HINT_CN = (
    '• 无法连接到模型服务端点（连接被拒绝或超时）——可能是本机代理/网络中断，'
    '也可能是自建/BYO 服务已宕机、端口未监听或防火墙不通。\n'
    '• 先检查本机网络/代理后重试；若确认服务可达仍失败，可在 '
    '「设置 → 模型默认」 切换到其他可用模型。'
)
_UNREACHABLE_HINT_EN = (
    '• The model endpoint could not be reached (connection refused or timed out) — '
    'this can be a local proxy/network outage OR the self-hosted / BYO server '
    'being down, the port not listening, or a firewall blocking it.\n'
    '• Check your network / proxy and retry; if the server is confirmed reachable, '
    'switch to another available model in "Settings → Model defaults".'
)

# generic must NOT reuse the Settings→Keys hint: an unclassified failure has
# NO evidence pointing at keys/quota, and sending the user there is the exact
# misattribution loop seen in production (kind=generic fired 46× on 2026-07-25
# for what were really upstream 400s and transport faults).
_GENERIC_HINT_CN = (
    '• 展开下方错误详情查看原始原因。\n'
    '• 若反复出现，请查看服务器日志（logs/error.log）定位根因；'
    '若确认是 Key/配额问题，再前往 「设置 → Keys / Providers」 处理。'
)
_GENERIC_HINT_EN = (
    '• Expand the error detail below for the underlying cause.\n'
    '• If it recurs, check the server logs (logs/error.log) for the root cause; '
    'only if it proves to be a key/quota problem, go to "Settings → Keys / Providers".'
)


# ── Title / hint table indexed by kind ────────────────────────────────
# Each entry is (cn_title, en_title, cn_hint, en_hint).  Hints can be
# multi-line; titles are one line.

_TITLES: dict[str, tuple[str, str, str, str]] = {
    'quota':              ('⚠️ API Key 余额/配额已用尽',
                            'API key quota exhausted',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'ratelimit':          ('⚠️ API 请求已达限频（429）',
                            'API rate-limited (HTTP 429)',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'permission':         ('⚠️ API Key 被拒绝（401/403，无权限或已失效）',
                            'API key rejected (401/403, invalid or lacking permission)',
                            _PERMISSION_HINT_CN, _PERMISSION_HINT_EN),
    'no_slot':            ('⚠️ 当前没有可用的 API Key',
                            'No available API key slot',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'dispatch_exhausted': ('⚠️ 该模型所有 Key 的重试次数都已用尽',
                            'All keys for this model have been exhausted',
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
    'timeout':            ('⚠️ 请求超时',
                            'Request timed out',
                            _TIMEOUT_HINT_CN, _TIMEOUT_HINT_EN),
    'network':            ('⚠️ 网络连接错误',
                            'Network connection error',
                            _NETWORK_HINT_CN, _NETWORK_HINT_EN),
    'endpoint_unreachable': ('⚠️ 模型服务端点无法连接',
                            'Model endpoint unreachable',
                            _UNREACHABLE_HINT_CN, _UNREACHABLE_HINT_EN),
    'content_filter':     ('⚠️ 该回复被模型安全过滤器拦截',
                            'Response blocked by the model\'s safety filter',
                            '• 尝试换一种方式提问，或切换到其他模型。',
                            '• Try rephrasing the question or switching to another model.'),
    'invalid_image':      ('⚠️ 图像内容被拒绝',
                            'Image rejected by the API',
                            '• 缩小或减少图片，再发送。',
                            '• Reduce image size or count and try again.'),
    'prompt_too_long':    ('⚠️ 上下文已超过模型上限',
                            'Context exceeds the model\'s limit',
                            '• 已尝试自动压缩仍失败，请清理对话历史或切换到上下文更大的模型。',
                            '• Auto-compaction did not free enough room — '
                            'trim the history or switch to a larger-context model.'),
    'stream_only':        ('⚠️ 该模型仅支持流式调用',
                            'Model only supports streaming',
                            '• 请切换到其他模型（系统已自动避开该模型）。',
                            '• Switch to another model (this one is auto-excluded).'),
    'model_limit':        ('⚠️ 输出长度超过模型上限',
                            'Output exceeds model max_tokens',
                            '• 系统已记住新的上限，可重试。',
                            '• The new limit has been recorded — retry.'),
    'tool_rounds_exhausted': ('⚠️ 工具调用轮数已达上限',
                               'Tool call round limit reached',
                               '• 模型未在限定轮数内得出最终答复，可点击 Continue 续写。',
                               '• The model did not finish within the per-task budget — '
                               'click Continue to extend.'),
    'tool_timeout':       ('⚠️ 工具调用连续超时',
                            'Repeated tool-execution timeouts',
                            '• 工具持续超时，建议简化任务或在 「设置 → 工具」 中调高超时时间。',
                            '• The tool keeps timing out — simplify the request or '
                            'raise the tool-timeout in Settings.'),
    'premature_close':    ('⚠️ 网关/代理过早关闭流',
                            'Gateway closed the stream prematurely',
                            '• 重试已用完，回复可能不完整。可点击 Retry 重新生成。',
                            '• Retries exhausted — the response may be incomplete. '
                            'Click Retry to regenerate.'),
    'abnormal_stop':      ('⚠️ API 流异常终止（缺失 finish 标记）',
                            'Stream ended without finish marker',
                            '• 回复可能不完整。可点击 Retry 重新生成。',
                            '• The reply may be truncated. Click Retry to regenerate.'),
    'aborted':            ('⏹️ 用户已中止',
                            'Stopped by user',
                            '', ''),
    'server_offline':     ('⚠️ 服务器离线',
                            'Server offline',
                            '• 等待服务器恢复后页面会自动重连，并尝试拉取已生成的内容。',
                            '• When the server comes back, this page will reconnect '
                            'automatically and try to recover any content that was generated.'),
    'internal':           ('⚠️ 内部错误',
                            'Internal error',
                            '• 请查看服务器日志（logs/error.log）了解详情。',
                            '• Check the server logs (logs/error.log) for details.'),
    'generic':            ('⚠️ 模型调用失败',
                            'LLM call failed',
                            _GENERIC_HINT_CN, _GENERIC_HINT_EN),
    # Deterministic HTTP-400 payload rejection. The hint must say explicitly
    # "NOT a key/quota problem" — that misdirection is what this kind exists
    # to kill (2026-07-25: our own message-assembly bug produced an upstream
    # 400 and users were sent to Settings → Keys).
    'bad_request':        ('⚠️ 请求被上游 API 拒绝（HTTP 400）',
                            'Request rejected by the API (HTTP 400)',
                            '• 这不是 Key / 配额 / 429 问题——上游判定请求内容无效。展开下方错误详情查看具体原因。\n'
                            '• 若反复出现且原因不明，请查看服务器日志（logs/error.log）。',
                            '• This is NOT a key / quota / 429 problem — the API rejected the request '
                            'payload itself. Expand the error detail below for the exact reason.\n'
                            '• If it recurs with no clear cause, check the server logs (logs/error.log).'),
    # Vendor / gateway outage. RetryableAPIError (5xx-after-retries) and
    # RateLimitError(is_gateway=True) (vendor 401/403/429) both land here.
    'upstream_error':     ('⚠️ 上游模型服务暂时不可用',
                            'Upstream model service temporarily unavailable',
                            '• 模型厂商或网关侧故障（不是本机 Key 问题），稍后重试通常可自行恢复。\n'
                            '• 若持续数分钟仍失败，可在 「设置 → 模型默认」 临时切换到其他可用模型。',
                            '• The model vendor or gateway is failing (not a problem with your API keys) — '
                            'retrying shortly usually recovers.\n'
                            '• If it keeps failing for several minutes, temporarily switch to another '
                            'available model in "Settings → Model defaults".'),
    # Deliberate stop at the conversation's cost budget cap (orchestrator
    # budget gate). NOT retryable — the same request hits the same cap;
    # recovery is a config change (raise cap / cheaper model), not a retry.
    'budget_exceeded':    ('⚠️ 任务费用已达预算上限，已主动停止',
                            'Task stopped at the cost budget cap',
                            '• 本次任务的花费达到会话设置的预算上限（maxBudgetUsd）而主动停止，已生成的内容已保留。\n'
                            '• 如需继续：提高该会话的预算上限后点击 Continue，或换用更低成本的模型。',
                            '• The task stopped itself when its spend reached the conversation\'s budget cap '
                            '(maxBudgetUsd); generated content is preserved.\n'
                            '• To continue: raise the cap for this conversation and click Continue, '
                            'or switch to a cheaper model.'),
    # Translation engine content-quality guards refused every candidate
    # output after the retry budget (wrong-language flip / no-op echo /
    # over-generated contamination). NOT a server crash — the distinction
    # matters to the user: 500 says "we broke", this says "the models
    # produced unusable output and we refused to commit it".
    'content_refused':    ('⚠️ 翻译质量校验未通过',
                            'Translation rejected by quality check',
                            '• 模型多次输出错误语言 / 空结果 / 失控文本，系统拒绝采用并已自动重试。稍后再试通常会命中正常模型。\n'
                            '• 若反复出现，可在 「设置 → 模型默认」 为翻译换一个更稳定的模型。',
                            '• The models repeatedly produced wrong-language / empty / runaway output; '
                            'it was rejected and retried automatically. Retrying shortly usually lands a healthy model.\n'
                            '• If it recurs, switch the translation model in "Settings → Model defaults".'),
    # Stall-reaped background task (TaskRuntime.reap_if_stalled). Retryable
    # by construction: the worker is presumed dead, so re-running the task
    # is the designed recovery — the hint says so explicitly.
    'worker_lost':        ('⚠️ 任务工作进程丢失（长时间无进展）',
                            'Task worker lost (no progress)',
                            '• 后台工作进程长时间没有产出任何进展，已被判定死亡——重新发起任务是安全的，已生成的部分内容可能丢失。\n'
                            '• 若反复出现，请查看服务器日志（logs/error.log）确认进程是否被异常终止。',
                            '• The background worker produced no progress for too long and was declared dead — '
                            'retrying the task is safe; partial output may have been lost.\n'
                            '• If it recurs, check the server logs (logs/error.log) to see whether the process was killed.'),
    # A tool the model kept calling is NOT in this turn's dispatched toolset, so
    # the call could never execute and retrying is guaranteed to fail again
    # (docs/INTENT_STALL_MEASUREMENT.md §4 measured 3 such tasks in 7 days:
    # project_board_complete / code_exec). Before this kind they reported
    # status=done + error=none — a task that substantively FAILED looked like a
    # success and the user only saw the conversation stop mid-thought. The hint
    # points at the TOOLSET, never at Settings → Keys (charter forbids that
    # misdirection: 46 wrong navigations/day measured in production).
    'tool_not_available': ('⚠️ 模型调用了本轮不存在的工具，任务未完成',
                            'Model called a tool that is not available this turn',
                            '• 这不是 Key / 配额问题——模型反复调用了一个**本轮工具集里没有**的工具，'
                            '该调用从未执行，任务因此停在半途。展开下方详情可看到具体工具名。\n'
                            '• 常见原因：该能力在本会话未开启（如代码执行 / 浏览器 / 项目工具）。'
                            '在工具栏打开对应开关后重试，或直接告诉模型改用已有的工具。',
                            '• This is NOT a key / quota problem — the model repeatedly called a tool that '
                            'is **not in this turn\'s toolset**, so it never executed and the task stopped '
                            'part-way. Expand the detail below for the exact tool name.\n'
                            '• Usually the capability is switched OFF for this conversation (e.g. code '
                            'execution / browser / project tools). Enable it in the toolbar and retry, or '
                            'tell the model to use a tool it actually has.'),
}
