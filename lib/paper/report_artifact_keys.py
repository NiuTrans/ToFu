"""Pure cache-key contracts for report second-pass artifacts.

Responsibility: construct the stable ``paper_reports.lang`` keys shared by
producers, storage reopen queries, and route projections. This module has no
I/O or model-runtime dependencies, so cache reads never import generation
engines merely to address persisted rows.
"""

from __future__ import annotations


def insight_lang_key(ui_lang: str) -> str:
    return f'insight:{ui_lang or "en"}'


def termfill_lang_key(ui_lang: str) -> str:
    return f'termfill:{ui_lang or "en"}'


def checkpoints_lang_key(ui_lang: str) -> str:
    return f'checkpoints:{ui_lang or "en"}'


def report_reopen_sibling_langs(ui_lang: str) -> tuple[str, str, str]:
    """Return every additive artifact needed to reopen one plain report."""
    return (
        insight_lang_key(ui_lang),
        termfill_lang_key(ui_lang),
        checkpoints_lang_key(ui_lang),
    )


__all__ = [
    'checkpoints_lang_key',
    'insight_lang_key',
    'report_reopen_sibling_langs',
    'termfill_lang_key',
]
