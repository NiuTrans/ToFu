"""Unit tests for package façade imports.

Migrated from debug/test_refactoring.py. Validates that all decomposed
packages (lib/search/, lib/browser/, lib/pdf_parser/, lib/memory/) expose
their public APIs correctly through __init__.py façades, that all consumer
import sites work, and that Flask route registration is complete.
"""

import pytest

# ═══════════════════════════════════════════════════════════
#  1. lib/search/
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSearchFacade:
    def test_package_import(self):
        import lib.search  # noqa: F401

    def test_public_api(self):
        from lib.search import format_search_for_tool_response, perform_web_search
        assert callable(perform_web_search)
        assert callable(format_search_for_tool_response)

    def test_optional_subs(self):
        from lib.search import dedup_by_content, rerank_by_bm25, search_via_browser
        assert callable(dedup_by_content)
        assert callable(rerank_by_bm25)
        assert callable(search_via_browser)

    def test_engines(self):
        from lib.search.engines.bing import search_bing
        from lib.search.engines.brave import search_brave
        from lib.search.engines.ddg import search_ddg_api, search_ddg_html
        from lib.search.engines.searxng import search_searxng
        assert callable(search_ddg_html)

    def test_common(self):
        from lib.search._common import HEADERS, clean_text
        assert isinstance(HEADERS, dict)
        assert callable(clean_text)

    def test_all_has_public_names(self):
        import lib.search
        assert 'perform_web_search' in lib.search.__all__
        assert 'format_search_for_tool_response' in lib.search.__all__


# ═══════════════════════════════════════════════════════════
#  2. lib/browser/
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBrowserFacade:
    def test_package_import(self):
        import lib.browser  # noqa: F401

    def test_queue_api(self):
        from lib.browser import (
            get_connected_clients,
            get_pending_commands,
            is_extension_connected,
            mark_poll,
            resolve_batch,
            resolve_command,
            send_browser_command,
            wait_for_commands,
        )
        assert callable(send_browser_command)
        assert callable(is_extension_connected)

    def test_dispatch(self):
        from lib.browser import BROWSER_HANDLERS, execute_browser_tool
        assert callable(execute_browser_tool)
        assert isinstance(BROWSER_HANDLERS, dict)
        assert len(BROWSER_HANDLERS) >= 16

    def test_display(self):
        from lib.browser import browser_tool_display
        assert callable(browser_tool_display)
        r = browser_tool_display('browser_list_tabs', {})
        assert '🌐' in r

    def test_fetch(self):
        from lib.browser import fetch_url_via_browser
        assert callable(fetch_url_via_browser)

    def test_advanced(self):
        from lib.browser import ADVANCED_BROWSER_TOOL_NAMES, ADVANCED_BROWSER_TOOLS
        assert isinstance(ADVANCED_BROWSER_TOOLS, list)
        assert len(ADVANCED_BROWSER_TOOLS) == 3
        assert isinstance(ADVANCED_BROWSER_TOOL_NAMES, set)

    def test_all_completeness(self):
        import lib.browser
        for name in ['send_browser_command', 'execute_browser_tool',
                     'browser_tool_display', 'fetch_url_via_browser',
                     'BROWSER_HANDLERS', 'ADVANCED_BROWSER_TOOLS']:
            assert name in lib.browser.__all__, f'{name} not in __all__'


# ═══════════════════════════════════════════════════════════
#  3. lib/pdf_parser/
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPdfParserFacade:
    def test_package_import(self):
        import lib.pdf_parser  # noqa: F401

    def test_core(self):
        from lib.pdf_parser import extract_pdf_text, parse_pdf
        assert callable(parse_pdf)
        assert callable(extract_pdf_text)

    def test_vlm(self):
        from lib.pdf_parser import get_vlm_task, start_vlm_task, vlm_parse_pdf
        assert callable(start_vlm_task)

    def test_images(self):
        from lib.pdf_parser import detect_and_clip_figures, render_pdf_pages
        assert callable(render_pdf_pages)

    def test_math(self):
        from lib.pdf_parser import postprocess_math_blocks
        assert callable(postprocess_math_blocks)

    def test_common(self):
        from lib.pdf_parser._common import HAS_PYMUPDF4LLM, MAX_PDF_BYTES
        assert isinstance(MAX_PDF_BYTES, int)
        assert isinstance(HAS_PYMUPDF4LLM, bool)

    def test_all_completeness(self):
        import lib.pdf_parser
        for name in ['parse_pdf', 'extract_pdf_text']:
            assert name in lib.pdf_parser.__all__


# ═══════════════════════════════════════════════════════════
#  4. lib/memory/
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSkillsFacade:
    def test_package_import(self):
        import lib.memory  # noqa: F401

    def test_storage_crud(self):
        from lib.memory import (
            create_memory,
            delete_memory,
            get_eligible_memories,
            get_enabled_memories,
            get_memory,
            list_all_memories,
            list_memories,
            merge_memories,
            toggle_memory,
            update_memory,
        )
        assert callable(create_memory)
        assert callable(list_all_memories)

    def test_injection(self):
        from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS, build_memory_context
        assert callable(build_memory_context)
        assert isinstance(MEMORY_ACCUMULATION_INSTRUCTIONS, str)
        assert len(MEMORY_ACCUMULATION_INSTRUCTIONS) > 100

    def test_tools(self):
        from lib.memory import ALL_MEMORY_TOOLS, MEMORY_TOOL_NAMES
        assert isinstance(ALL_MEMORY_TOOLS, list)
        assert len(ALL_MEMORY_TOOLS) == 5
        assert 'create_memory' in MEMORY_TOOL_NAMES
        assert 'merge_memories' in MEMORY_TOOL_NAMES
        assert 'search_memories' in MEMORY_TOOL_NAMES

    def test_constants(self):
        from lib.memory import GLOBAL_MEMORY_SUBDIR, MIN_DESCRIPTION_LENGTH, PROJECT_MEMORY_SUBDIR
        assert isinstance(GLOBAL_MEMORY_SUBDIR, str)
        assert isinstance(MIN_DESCRIPTION_LENGTH, int)

    def test_all_completeness(self):
        import lib.memory
        for name in ['create_memory', 'update_memory', 'delete_memory', 'merge_memories',
                     'search_memories', 'SEARCH_MEMORIES_TOOL',
                     'ALL_MEMORY_TOOLS', 'MEMORY_TOOL_NAMES', 'build_memory_context',
                     'MEMORY_ACCUMULATION_INSTRUCTIONS']:
            assert name in lib.memory.__all__


# ═══════════════════════════════════════════════════════════
#  5. Consumer import sites (cross-module)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConsumerImports:
    """Verify that all real import sites across the codebase resolve correctly."""

    def test_executor_search(self):
        from lib.search import format_search_for_tool_response, perform_web_search
        assert callable(perform_web_search)

    def test_executor_browser(self):
        from lib.browser import execute_browser_tool
        assert callable(execute_browser_tool)

    def test_model_config_browser(self):
        from lib.browser import ADVANCED_BROWSER_TOOL_NAMES, ADVANCED_BROWSER_TOOLS
        assert isinstance(ADVANCED_BROWSER_TOOLS, list)

    def test_tool_display_browser(self):
        from lib.browser import browser_tool_display
        assert callable(browser_tool_display)

    def test_routes_browser(self):
        from lib.browser import (
            get_connected_clients,
            get_pending_commands,
            is_extension_connected,
            mark_poll,
            resolve_batch,
            wait_for_commands,
        )
        assert callable(mark_poll)

    def test_browser_fetch(self):
        from lib.browser import fetch_url_via_browser, is_extension_connected
        assert callable(fetch_url_via_browser)

    def test_pdf_upload(self):
        from lib.pdf_parser import get_vlm_task, parse_pdf, start_vlm_task
        assert callable(parse_pdf)

    def test_pdf_fetch(self):
        from lib.pdf_parser import extract_pdf_text
        assert callable(extract_pdf_text)

    def test_skills_executor(self):
        from lib.memory import create_memory, delete_memory, merge_memories, update_memory
        assert callable(create_memory)

    def test_skills_model_config(self):
        from lib.memory import ALL_MEMORY_TOOLS, MEMORY_TOOL_NAMES
        assert isinstance(ALL_MEMORY_TOOLS, list)

    def test_skills_injection(self):
        from lib.memory import build_memory_context
        assert callable(build_memory_context)


# ═══════════════════════════════════════════════════════════
#  6. Flask route registration
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFlaskRouteRegistration:
    def test_all_critical_routes_registered(self):
        from flask import Flask

        from routes import ALL_BLUEPRINTS

        test_app = Flask(__name__)
        for bp in ALL_BLUEPRINTS:
            test_app.register_blueprint(bp)

        with test_app.app_context():
            rules = [r.rule for r in test_app.url_map.iter_rules()]

        critical = [
            '/api/conversations',
            '/api/conversations/<conv_id>',
            '/api/conversations/search',
            '/api/images/upload',
            '/api/images/<filename>',
            '/api/images/generate',
            '/api/pdf/parse',
            '/api/pdf/vlm-parse',
            '/api/translate/start',
            '/api/translate',
            '/api/translate/poll/<task_id>',
            '/api/translate/poll_batch',
            '/api/me',
            '/api/health',
            '/api/pricing',
            '/api/server-config',
            '/api/features',
            '/api/log/compress',
            '/',
        ]

        missing = [ep for ep in critical if ep not in rules]
        assert not missing, f'Missing routes: {missing}'

    def test_new_blueprints_in_all(self):
        from routes import ALL_BLUEPRINTS
        names = [bp.name for bp in ALL_BLUEPRINTS]
        assert 'conversations' in names
        assert 'upload' in names
        assert 'translate' in names
        assert 'common' in names


# ═══════════════════════════════════════════════════════════
#  7. Stale file checks
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoStaleFiles:
    def test_no_stale_monoliths(self):
        import os
        for path in ['lib/search.py', 'lib/browser.py', 'lib/browser_advanced.py',
                     'lib/pdf_parser.py', 'lib/skills.py']:
            assert not os.path.isfile(path), f'Stale file still exists: {path}'

    def test_packages_have_init(self):
        import os
        for pkg in ['lib/search/__init__.py', 'lib/browser/__init__.py',
                     'lib/pdf_parser/__init__.py', 'lib/memory/__init__.py']:
            assert os.path.isfile(pkg), f'{pkg} not found'
