"""Canonical report-body selection after a streamed model response.

Stream callbacks are a live presentation channel and may replay text after a
transport retry.  The terminal assistant message is the durable response
authority.  This pure boundary keeps persistence independent from callback
delivery semantics.
"""


def resolve_canonical_report_body(
    streamed_body: str,
    terminal_body: str | None,
) -> tuple[str, bool]:
    """Return ``(canonical_body, live_projection_needs_reset)``.

    A missing terminal body means dispatch never supplied a durable response,
    so the streamed projection remains the only available body.  Any supplied
    terminal body—including an intentional empty string—is authoritative.
    """
    if terminal_body is None:
        return streamed_body, False
    return terminal_body, terminal_body != streamed_body


__all__ = ["resolve_canonical_report_body"]
