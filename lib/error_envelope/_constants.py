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
})

# Default severities — warnings are recoverable / user-actionable, errors
# usually mean the task ended in a non-recoverable state.
_WARNING_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network', 'endpoint_unreachable',
    'tool_rounds_exhausted', 'tool_timeout',
    'premature_close', 'abnormal_stop',
    'aborted', 'server_offline',
})

# Kinds where retrying THE SAME REQUEST is genuinely likely to help
# (transient).  The frontend uses this to gate a "Retry" button.
_RETRYABLE_KINDS = frozenset({
    'ratelimit', 'no_slot', 'timeout', 'network', 'endpoint_unreachable',
    'premature_close', 'abnormal_stop', 'server_offline',
    'tool_timeout',
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
    '• 模型服务端点无法连接（连接被拒绝或超时），通常说明该自建/BYO 服务已宕机、'
    '端口未监听，或网络/防火墙不通。\n'
    '• 确认模型服务正在运行且可达后重试；或在 「设置 → 模型默认」 切换到其他可用模型。'
)
_UNREACHABLE_HINT_EN = (
    '• The model endpoint refused the connection or timed out — the self-hosted / '
    'BYO server is likely down, the port is not listening, or a firewall is blocking it.\n'
    '• Verify the model server is running and reachable, then retry; or switch to '
    'another available model in "Settings → Model defaults".'
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
    'endpoint_unreachable': ('⚠️ 模型服务端点无法连接（服务可能已宕机）',
                            'Model endpoint unreachable (server may be down)',
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
                            _SETTINGS_HINT_CN, _SETTINGS_HINT_EN),
}
