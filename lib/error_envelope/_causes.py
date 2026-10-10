"""Cause translation for executor-startup failures.

A ``task_start_failed`` envelope's fixed title only says THAT the executor
did not start — users also need to know WHY. This module translates the
scheduling / worker-entry exception into the user-facing cause: a concise
technical clause for ``detail`` and, for recognized subsystem failures, a
bilingual recovery ``hint``.

Classification is import-free (type-name / attribute probes only), mirroring
``_classify_exception``: this package must not depend on the storage or
executor implementations.
"""

from __future__ import annotations

# Budget for the embedded exception message so the composed detail (caller
# sentence + cause clause) stays inside make_envelope's 300-char detail cap.
_CAUSE_MESSAGE_MAX = 140

# Caller-supplied hints render verbatim on every client, so they carry the
# same 'heading + CN + EN' shape make_envelope builds for default hints.
_STORAGE_HINT = (
    '解决办法 / How to fix:\n'
    '• 本次请求已安全终止，没有后台任务仍在运行。失败原因：存储服务在任务启动时'
    '未能完成读写（存储进程崩溃、被重启或暂时过载）。请点击 Retry；若反复出现，'
    '请查看 logs/error.log 中的存储错误，必要时重启服务。\n\n'
    '• This request was settled safely and no background task is still '
    'running. Startup failed because the storage service could not complete '
    'startup I/O (the storage process crashed, was restarted, or is '
    'temporarily overloaded). Click Retry; if it recurs, check the storage '
    'errors in logs/error.log and restart the server if needed.'
)

_SHUTDOWN_HINT = (
    '解决办法 / How to fix:\n'
    '• 服务器正在关闭或重启，新任务未能启动——这不是您的请求本身的问题。'
    '请等待服务恢复后点击 Retry。\n\n'
    '• The server was shutting down or restarting, so the new task could '
    'not start — this is not a problem with your request. Wait for the '
    'server to come back, then click Retry.'
)


def _clip(text: str) -> str:
    collapsed = ' '.join(str(text).split())
    if len(collapsed) <= _CAUSE_MESSAGE_MAX:
        return collapsed
    return collapsed[: _CAUSE_MESSAGE_MAX - 1] + '…'


def task_start_failure_cause(error: BaseException) -> dict:
    """Translate an executor-startup exception into its user-facing cause.

    Returns ``{'detail': str, 'hint': str | None}``: ``detail`` is a concise
    English cause clause the caller appends to its fixed startup-failure
    sentence; ``hint`` is a bilingual recovery override, or ``None`` to keep
    the envelope kind's default hint.
    """
    type_name = type(error).__name__.lower()
    message = _clip(str(error) or type(error).__name__)

    # Storage authority failures carry a stable typed taxonomy (codes in
    # lib/storage/errors.py). A peer that closes the connection mid-frame
    # died while answering — an availability crash, not a framing bug,
    # whatever the historical protocol-error label suggests.
    if 'storageerror' in type_name:
        code = str(getattr(error, 'code', '') or '')
        if code == 'database_protocol_error':
            return {
                'detail': (
                    'the storage process dropped the connection '
                    'mid-response (it crashed or was restarting): '
                    f'{message}'),
                'hint': _STORAGE_HINT,
            }
        if code in ('database_unavailable', 'database_timeout',
                    'database_busy'):
            return {
                'detail': (
                    'the storage service was temporarily unavailable '
                    f'during startup ({code}): {message}'),
                'hint': _STORAGE_HINT,
            }
        return {
            'detail': f'storage startup I/O failed ({code}): {message}',
            'hint': _STORAGE_HINT,
        }

    lowered = message.lower()
    if ('shutdown' in lowered or 'shutting down' in lowered
            or 'cannot schedule new futures' in lowered):
        return {
            'detail': (
                'the server was shutting down or restarting: '
                f'{message}'),
            'hint': _SHUTDOWN_HINT,
        }

    return {'detail': message, 'hint': None}


__all__ = ['task_start_failure_cause']
