"""Unit tests for lib/artifacts/pdf_export.py — wrapping + script stripping.

These tests target the deterministic preprocessing layer.  Browser-based
rendering is covered by the smoke test in test_artifacts_versioning.py.

Run:  pytest tests/test_artifacts_pdf_export.py -v
"""
from __future__ import annotations

import pytest

pytest_plugins = ('tests._artifact_sidecar',)


class TestStripScripts:
    def test_inline_script_removed(self):
        from lib.artifacts.pdf_export import _strip_scripts
        out = _strip_scripts(
            '<html><body><script>alert(1)</script><p>ok</p></body></html>'
        )
        assert 'alert' not in out
        assert '<p>ok</p>' in out

    def test_multiline_script_removed(self):
        from lib.artifacts.pdf_export import _strip_scripts
        out = _strip_scripts('<script>\nfor (let i=0;i<10;i++){doBad();}\n</script><h1>safe</h1>')
        assert 'doBad' not in out
        assert '<h1>safe</h1>' in out

    def test_case_insensitive(self):
        from lib.artifacts.pdf_export import _strip_scripts
        out = _strip_scripts('<SCRIPT>alert(1)</SCRIPT>x')
        assert 'alert' not in out

    def test_empty_input(self):
        from lib.artifacts.pdf_export import _strip_scripts
        assert _strip_scripts('') == ''
        assert _strip_scripts(None) == ''


class TestMarkdownToHtml:
    def test_renders_h1_paragraph(self):
        from lib.artifacts.pdf_export import _render_markdown_to_html
        out = _render_markdown_to_html('# Title\n\nBody text.\n')
        assert '<h1>' in out
        assert 'Title' in out
        assert '<p>' in out

    def test_does_not_pass_raw_html(self):
        """html=False in markdown_it config — model HTML in markdown
        source MUST be escaped, not passed through, since the document
        renders in our browser pool."""
        from lib.artifacts.pdf_export import _render_markdown_to_html
        out = _render_markdown_to_html('Hello <script>alert(1)</script> world.')
        assert '<script>' not in out
        assert 'alert(1)' in out  # text content escaped, but visible


class TestBuildPrintHtml:
    def test_markdown_wrapped_in_template(self):
        from lib.artifacts.pdf_export import _build_print_html
        artifact = {'format': 'markdown', 'content': '# Hi\n', 'title': 'Hi.md'}
        out = _build_print_html(artifact)
        assert out.startswith('<!doctype html>')
        assert '<title>Hi.md</title>' in out
        assert '<h1>' in out
        assert '@page' not in out  # no @page rule (margins set via Playwright)

    def test_html_full_document_passed_through_after_strip(self):
        from lib.artifacts.pdf_export import _build_print_html
        body = '<!doctype html><html><body><script>alert(1)</script><h1>x</h1></body></html>'
        out = _build_print_html({'format': 'html', 'content': body, 'title': 'x'})
        assert 'alert' not in out
        assert '<h1>x</h1>' in out
        assert out.lstrip().startswith('<!doctype html>')

    def test_html_fragment_wrapped(self):
        from lib.artifacts.pdf_export import _build_print_html
        body = '<div><p>hi</p></div>'
        out = _build_print_html({'format': 'html', 'content': body, 'title': 'frag'})
        assert out.startswith('<!doctype html>')
        assert '<div><p>hi</p></div>' in out

    def test_svg_wrapped(self):
        from lib.artifacts.pdf_export import _build_print_html
        body = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="15"/></svg>'
        out = _build_print_html({'format': 'svg', 'content': body, 'title': 'circle'})
        assert '<svg' in out
        assert '<title>circle</title>' in out

    def test_unknown_format_plaintext_pre(self):
        from lib.artifacts.pdf_export import _build_print_html
        out = _build_print_html({'format': 'mystery', 'content': '<x>x', 'title': 't'})
        assert '<pre>&lt;x&gt;x</pre>' in out


class TestRenderError:
    def test_missing_artifact_raises_not_found(self, flask_app):
        from lib.artifacts.core import ArtifactNotFoundError
        from lib.artifacts.pdf_export import render_artifact_pdf
        with flask_app.app_context():
            with pytest.raises(ArtifactNotFoundError):
                render_artifact_pdf('does-not-exist-' + 'x' * 16)
