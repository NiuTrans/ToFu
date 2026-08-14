"""Structured validation issues with rolling string-list compatibility.

Validators report one issue once.  Existing consumers continue receiving
``errors`` and ``warnings`` as string lists, while inspection-aware consumers
also receive stable ``severity``/``code``/JSON-Pointer ``path`` diagnostics.
"""

from __future__ import annotations

from collections.abc import Iterable


class ValidationIssueList(list[str]):
    """A normal string list that retains structured metadata on insertion."""

    def __init__(self, severity: str):
        super().__init__()
        self.severity = severity
        self.diagnostics: list[dict[str, str]] = []

    def add(self, message: object, *, code: str = '', path: str = '') -> None:
        text = str(message)
        super().append(text)
        self.diagnostics.append({
            'severity': self.severity,
            'code': str(code or ''),
            'path': str(path or ''),
            'message': text,
        })

    def append(self, message: str) -> None:
        self.add(message)

    def extend(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.add(message)


def report_validation_issue(
    target: list,
    message: object,
    *,
    code: str = '',
    path: str = '',
) -> None:
    """Report through the structured collector or a legacy plain list."""
    add = getattr(target, 'add', None)
    if callable(add):
        add(message, code=code, path=path)
    else:
        target.append(str(message))


def validation_diagnostics(*issue_lists: list) -> list[dict[str, str]]:
    """Return detached diagnostics from structured issue lists."""
    diagnostics: list[dict[str, str]] = []
    for issues in issue_lists:
        for diagnostic in getattr(issues, 'diagnostics', ()):
            diagnostics.append(dict(diagnostic))
    return diagnostics


def report_nested_validation_verdict(
    errors: list,
    warnings: list,
    verdict: dict,
    *,
    message_prefix: str,
    path_prefix: str,
    fallback_code_prefix: str = 'nested.child',
) -> None:
    """Project a child verdict once while preserving legacy string messages."""
    diagnostics = verdict.get('diagnostics') or []
    for severity, target, key in (
        ('error', errors, 'errors'),
        ('warning', warnings, 'warnings'),
    ):
        metadata = [item for item in diagnostics
                    if isinstance(item, dict)
                    and item.get('severity') == severity]
        for index, message in enumerate(verdict.get(key) or []):
            diagnostic = metadata[index] if index < len(metadata) else {}
            report_validation_issue(
                target, f'{message_prefix}{message}',
                code=(diagnostic.get('code')
                      or f'{fallback_code_prefix}.{severity}'),
                path=join_json_pointer(
                    path_prefix, diagnostic.get('path') or ''),
            )


def join_json_pointer(prefix: str, child: str) -> str:
    """Join two already-encoded JSON Pointer paths."""
    left = str(prefix or '').rstrip('/')
    right = str(child or '')
    if not right:
        return left
    if not right.startswith('/'):
        right = '/' + right
    return left + right


def json_pointer_token(value: object) -> str:
    """Encode one RFC 6901 reference token."""
    return str(value).replace('~', '~0').replace('/', '~1')


def json_pointer_path(prefix: str, *tokens: object) -> str:
    """Append encoded reference tokens to a JSON Pointer prefix."""
    path = str(prefix or '').rstrip('/')
    for token in tokens:
        path += '/' + json_pointer_token(token)
    return path


__all__ = [
    'ValidationIssueList',
    'report_validation_issue',
    'validation_diagnostics',
    'report_nested_validation_verdict',
    'join_json_pointer',
    'json_pointer_token',
    'json_pointer_path',
]
