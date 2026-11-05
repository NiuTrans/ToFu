"""The one typed client-fault exception.

``BadRequest`` means the caller's request was understood but cannot be
accepted: a missing or ill-typed field, or an operation that is invalid for
the resource's current state. Its message is authored for the caller and is
safe to display. HTTP boundaries answer it with 400, its message and its
``field``: ``lib.http_transport.error_handlers``, ``lib.api_response.safe_route``
and explicit route handlers such as the conversation-sync command boundary.

Raise it only where the code knows the rejected value came from the caller.
A builtin ``ValueError`` or ``TypeError`` stays a server fault, because Python
raises those for programming errors too; boundaries answer them with a
redacted, logged 500. ``BadRequest`` subclasses ``ValueError`` so in-process
callers that already catch ``ValueError`` keep their behavior.
"""

from __future__ import annotations


class BadRequest(ValueError):
    """The caller's request is invalid; ``field`` names the offending input.

    ``field`` is the empty string when the rejection is not field-scoped.
    """

    def __init__(self, message: str, *, field: str = ''):
        super().__init__(message)
        self.field = field

    def to_envelope(self) -> dict:
        env = {'kind': 'bad_request', 'detail': str(self)}
        if self.field:
            env['field'] = self.field
        return env


__all__ = ['BadRequest']
