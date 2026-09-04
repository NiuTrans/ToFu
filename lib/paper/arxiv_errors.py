"""Typed arXiv request failures without the feed/parser dependency graph."""


class ArxivQuerySyntaxError(ValueError):
    """Raised when built arXiv syntax reaches the free-text entry point."""


__all__ = ['ArxivQuerySyntaxError']
