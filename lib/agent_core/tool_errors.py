"""Tool assembly failures shared by core execution and host repositories.

Defines typed failures only; concrete declaration persistence stays behind the
ConversationStore port. Consumers must stop assembly rather than drop schemas.
"""


class MCPDeclarationError(RuntimeError):
    """Assembly cannot safely establish the conversation's declaration wire."""
