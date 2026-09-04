"""tests/test_attachment_paged_read.py — uploaded-attachment sliding-window reads.

Covers the "model only ever saw a truncated excerpt and could never read the
rest" fix: ``read_files`` resolves ``att_media_<id>`` / ``att_txt_<hash>``
refs as virtual line-paged text files (owner-scoped), the model-projection
header discloses exactly how much of the document was injected (and how to
page for more), the excerpt budget scales to the model's context window, and
the tool-round display names the user's exact original filename instead of
the raw ref.

Assertions:
  - Whole read / line-range read / out-of-bounds error / >60k continuation
    hint + follow-up page.
  - Owner scope enforced: no ``_userId`` → clean error; unknown id → clean
    error. Legacy ``att_txt_`` resolves through task messages.
  - Result-projection item and ``project_tool_display`` both render the
    exact stored filename, clearly marked as a user upload.
  - Projection header: 'search' mode discloses the injected fraction; a
    relevance miss is annotated as head-only fallback; a fully-shown small
    doc carries no paging disclosure.
  - ``document_text_budget`` floors at 48k for unknown models, scales with
    a known window, and clamps at 240k.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_attachment_paged_read.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

TEST_OWNER_USER_ID = 1


def _attach(isolated_knowledge, text: str, name: str) -> dict:
    document = isolated_knowledge.add_document(
        text.encode(), name, user_id=TEST_OWNER_USER_ID, scope='attachment')
    from lib.media_attachments import attachment_ref
    return attachment_ref(document)


def _read(ref: str, sl=None, el=None, *, task=None, result_items=None):
    from lib.project_mod.read_tools import tool_read_files
    spec = {'path': ref}
    if sl is not None:
        spec['start_line'] = sl
    if el is not None:
        spec['end_line'] = el
    return tool_read_files('.', [spec], result_items=result_items,
                           task=task or {
                               '_userId': TEST_OWNER_USER_ID, 'messages': []})


# ═══════════════════════════════════════════════════════════════════════
#  read_files on att_media_ refs — virtual line-paged file
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPagedReadBack:

    def test_whole_read_names_exact_file(self, isolated_knowledge):
        ref = _attach(isolated_knowledge, 'alpha\nbeta\ngamma', 'notes.txt')
        out = _read(f'att_media_{ref["attachmentId"]}')
        assert not out.startswith('Error:')
        assert 'File: notes.txt (user-uploaded attachment' in out
        assert '3 lines' in out
        assert 'alpha\nbeta\ngamma' in out

    def test_line_range_slices_like_a_real_file(self, isolated_knowledge):
        ref = _attach(isolated_knowledge, 'l1\nl2\nl3\nl4\nl5', 'ranged.txt')
        out = _read(f'att_media_{ref["attachmentId"]}', 2, 3)
        assert 'lines 2-3 of 5' in out
        assert 'l2\nl3' in out
        assert 'l1' not in out and 'l4' not in out

    def test_out_of_bounds_range_reports_total(self, isolated_knowledge):
        ref = _attach(isolated_knowledge, 'only\nthree\nlines', 'short.txt')
        out = _read(f'att_media_{ref["attachmentId"]}', 99, 120)
        assert out.startswith('Error:')
        assert '3 lines' in out

    def test_large_doc_truncates_with_continuation_hint(
            self, isolated_knowledge):
        from lib.project_mod.read_tools import _ATTACHMENT_READ_CHARS
        line = 'evidence line %05d with enough body to matter\n'
        raw = ''.join(line % i for i in range(4000))
        assert len(raw) > _ATTACHMENT_READ_CHARS
        ref = _attach(isolated_knowledge, raw, 'large.txt')
        att_ref = f'att_media_{ref["attachmentId"]}'

        first = _read(att_ref)
        assert '[showing first' in first
        assert f'path="{att_ref}"' in first
        assert 'start_line=' in first

        # The continuation page reaches content the first page never showed
        # (chunk separators shift virtual line numbers, so assert reachability
        # of deep content, not a 1:1 source-line mapping).
        assert 'evidence line 03' not in first
        tail = _read(att_ref, 4600)
        assert not tail.startswith('Error:')
        assert 'evidence line 03' in tail

    def test_missing_owner_fails_closed(self, isolated_knowledge):
        ref = _attach(isolated_knowledge, 'secret body', 'owned.txt')
        out = _read(f'att_media_{ref["attachmentId"]}',
                    task={'messages': []})
        assert out.startswith('Error:')
        assert 'secret body' not in out

    def test_unknown_ref_fails_clean(self):
        out = _read('att_media_doesnotexist0000')
        assert out.startswith('Error:')
        assert 'could not resolve' in out

    def test_legacy_att_txt_ref_reads_via_task_messages(self):
        from lib.attachments import attachment_text_ref
        pdf = {'name': 'legacy.pdf', 'text': 'legacy body\nsecond line',
               'pages': 2}
        ref = attachment_text_ref(pdf)
        out = _read(ref, task={'messages': [
            {'role': 'user', 'content': 'see attached', 'pdfTexts': [pdf]}]})
        assert 'File: legacy.pdf (user-uploaded attachment' in out
        assert 'legacy body' in out

    def test_result_projection_uses_exact_filename(self, isolated_knowledge):
        ref = _attach(isolated_knowledge, 'projection body',
                      'Exact Name 2026.txt')
        items: list = []
        out = _read(f'att_media_{ref["attachmentId"]}', result_items=items)
        assert not out.startswith('Error:')
        assert items and items[0]['path'] == 'Exact Name 2026.txt'
        assert items[0]['status'] == 'ok'

    def test_execute_tool_dispatch_threads_task(self, isolated_knowledge):
        # The real call path: execute_tool must forward `task` into
        # tool_read_files or the owner scope is lost.
        from lib.project_mod import execute_tool
        ref = _attach(isolated_knowledge, 'dispatch body', 'dispatch.txt')
        out = execute_tool(
            'read_files',
            {'reads': [{'path': f'att_media_{ref["attachmentId"]}'}]},
            '.', conv_id='c1', task_id='t1',
            task={'_userId': TEST_OWNER_USER_ID, 'messages': []})
        assert 'File: dispatch.txt (user-uploaded attachment' in out


# ═══════════════════════════════════════════════════════════════════════
#  project_tool_display — round title names the user's exact file
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDisplayTitle:

    def test_registered_ref_shows_exact_uploaded_name(self):
        from lib.attachments import register_attachment_name
        from lib.project_mod.tools import project_tool_display
        register_attachment_name('att_media_titlecase01', 'My Paper v2.pdf')
        disp = project_tool_display(
            'read_files', {'reads': [{'path': 'att_media_titlecase01'}]})
        assert disp == 'Read 1 file: uploaded attachment "My Paper v2.pdf"'

    def test_registered_ref_with_range_keeps_lines(self):
        from lib.attachments import register_attachment_name
        from lib.project_mod.tools import project_tool_display
        register_attachment_name('att_media_titlecase02', 'spec.docx')
        disp = project_tool_display('read_files', {'reads': [
            {'path': 'att_media_titlecase02', 'start_line': 10,
             'end_line': 40}]})
        assert 'uploaded attachment "spec.docx" L10-40' in disp

    def test_unregistered_ref_falls_back_to_ref_marker(self):
        from lib.project_mod.tools import project_tool_display
        disp = project_tool_display(
            'read_files', {'reads': [{'path': 'att_media_neverseen99'}]})
        assert 'uploaded attachment att_media_neverseen99' in disp

    def test_mixed_batch_marks_only_uploads(self):
        from lib.attachments import register_attachment_name
        from lib.project_mod.tools import project_tool_display
        register_attachment_name('att_media_titlecase03', 'data.csv')
        disp = project_tool_display('read_files', {'reads': [
            {'path': 'server.py'},
            {'path': 'att_media_titlecase03'}]})
        assert 'server.py' in disp
        assert 'uploaded attachment "data.csv"' in disp
        assert 'uploaded attachment "server.py"' not in disp


# ═══════════════════════════════════════════════════════════════════════
#  Model-projection header — injected fraction + paging instruction
# ═══════════════════════════════════════════════════════════════════════

def _header_of(projection: dict) -> str:
    return '\n'.join(
        block.get('text', '') for block in projection['blocks']
        if block.get('type') == 'text')


@pytest.mark.unit
class TestProjectionHeaderDisclosure:

    def test_search_mode_discloses_excerpt_fraction(self, isolated_knowledge):
        from lib.media_attachments import project_for_model
        filler = 'ordinary padding sentence for the body\n' * 2400  # >48k
        raw = filler + '\nzzqneedle conclusion lives here\n'
        ref = _attach(isolated_knowledge, raw, 'big-search.txt')
        projection = project_for_model(
            [ref], user_id=TEST_OWNER_USER_ID, query='zzqneedle',
            model='text-only-test')
        header = _header_of(projection)
        assert 'Showing' in header and 'chars' in header
        assert 'excerpts selected by relevance' in header
        assert f'read_files with path="att_media_{ref["attachmentId"]}"' \
            in header
        assert 'zzqneedle conclusion lives here' in header

    def test_head_fallback_annotated_when_search_misses(
            self, isolated_knowledge):
        from lib.media_attachments import project_for_model
        raw = 'ordinary padding sentence for the body\n' * 2400
        ref = _attach(isolated_knowledge, raw, 'big-head.txt')
        projection = project_for_model(
            [ref], user_id=TEST_OWNER_USER_ID, query='zzq-absent-term',
            model='text-only-test')
        header = _header_of(projection)
        assert 'Relevance search matched nothing' in header
        assert f'read_files with path="att_media_{ref["attachmentId"]}"' \
            in header

    def test_small_doc_shows_facts_without_paging_disclosure(
            self, isolated_knowledge):
        from lib.media_attachments import project_for_model
        ref = _attach(isolated_knowledge, 'tiny body', 'tiny.txt')
        projection = project_for_model(
            [ref], user_id=TEST_OWNER_USER_ID, query='tiny',
            model='text-only-test')
        header = _header_of(projection)
        assert 'Attachment 1: tiny.txt (document' in header
        assert f'attachment ref: att_media_{ref["attachmentId"]}' in header
        assert 'Showing' not in header
        assert 'matched nothing' not in header


# ═══════════════════════════════════════════════════════════════════════
#  Model-aware excerpt budget
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestModelAwareBudget:

    def test_unknown_model_keeps_floor(self):
        from lib.media_attachments import document_text_budget
        assert document_text_budget('text-only-test') == 48_000
        assert document_text_budget('') == 48_000

    def test_known_window_scales(self):
        from lib.media_attachments import document_text_budget
        # gpt-5.3-codex-spark: 128k window → 128000 * 0.12 * 4 = 61,440
        assert document_text_budget('gpt-5.3-codex-spark') == 61_440

    def test_large_window_clamps(self):
        from lib.media_attachments import document_text_budget
        # gpt-5.4: 1M window → 480,000 → clamped to the 240k ceiling
        assert document_text_budget('gpt-5.4') == 240_000

    def test_request_cap_is_double_the_per_attachment_budget(self):
        from lib.media_attachments import (
            document_text_budget,
            document_text_request_cap,
        )
        assert document_text_request_cap('text-only-test') == 96_000
        assert document_text_request_cap('gpt-5.4') \
            == 2 * document_text_budget('gpt-5.4')
