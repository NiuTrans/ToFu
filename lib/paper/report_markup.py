"""Render paper-report Markdown while preserving math for browser KaTeX.

Entry point: ``render_report_markdown``. Python-Markdown owns parsing and
placeholder restoration; this module owns report math delimiters only. It
does not sanitize report HTML or change the export route's CSP/ownership policy.
"""

from __future__ import annotations

import html
import re


_MATH_PATTERNS = (
    r"\$\$[\s\S]+?\$\$",
    r"\\\[[\s\S]+?\\\]",
    # A literal dollar in one table cell must not consume the next cell.
    r"\$(?!\$)((?:[^$\\\n|]|\\.)+?)\$(?!\$)",
    r"\\\(.+?\\\)",
)
# Python-Markdown runs preprocessors in descending priority: normalization
# (30), fenced_code (25), html_block (20). Stash math after the first two,
# before HTML parsing, so normalization cannot erase its own placeholders.
_REPORT_MATH_PRIORITY = 21


def render_report_markdown(source: str) -> str:
    """Preserve formula text and code with the existing report extensions."""
    import markdown
    from markdown.preprocessors import Preprocessor

    class ReportMathPreprocessor(Preprocessor):
        def run(self, lines: list[str]) -> list[str]:
            def stash(match: re.Match[str]) -> str:
                return self.md.htmlStash.store(html.escape(match.group(0)))

            protected = '\n'.join(lines)
            for pattern in _MATH_PATTERNS:
                protected = re.sub(pattern, stash, protected)
            return protected.split('\n')

    renderer = markdown.Markdown(
        extensions=['tables', 'fenced_code', 'attr_list', 'sane_lists'],
        output_format='html5',
    )
    renderer.preprocessors.register(
        ReportMathPreprocessor(renderer), 'report_math', _REPORT_MATH_PRIORITY,
    )
    return renderer.convert(source)
