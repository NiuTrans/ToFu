#!/usr/bin/env python3
"""Comprehensive test for the 5 refactorings.

Tests:
  1. Package imports (top-level)
  2. Specific name resolution through façades
  3. Internal cross-module references
  4. All external import sites (every file that imports from refactored modules)
  5. Flask route registration
  6. __all__ completeness
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0

def check(label, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f'  ✅ {label}')
    except Exception as e:
        FAIL += 1
        print(f'  ❌ {label}: {e}')


# ══════════════════════════════════════════════════════
#  1. lib/search/
# ══════════════════════════════════════════════════════
print('\n═══ 1. lib/search/ ═══')

# 1a. Package import
check('import lib.search', lambda: __import__('lib.search'))

# 1b. Public API names available through façade
def _search_names():
    from lib.search import perform_web_search, format_search_for_tool_response
    assert callable(perform_web_search)
    assert callable(format_search_for_tool_response)
check('search public API (perform_web_search, format_search_for_tool_response)', _search_names)

# 1c. Optional sub-module names available
def _search_optional():
    from lib.search import dedup_by_content, rerank_by_bm25, search_via_browser
    assert callable(dedup_by_content)
    assert callable(rerank_by_bm25)
    assert callable(search_via_browser)
check('search optional subs (dedup, rerank, browser_fallback)', _search_optional)

# 1d. Internal engine imports (orchestrator → engines)
def _search_engines():
    from lib.search.engines.ddg import search_ddg_html, search_ddg_api
    from lib.search.engines.brave import search_brave
    from lib.search.engines.bing import search_bing
    from lib.search.engines.searxng import search_searxng
    assert callable(search_ddg_html)
check('search engines internal imports', _search_engines)

# 1e. _common accessible
def _search_common():
    from lib.search._common import HEADERS, clean_text
    assert isinstance(HEADERS, dict)
    assert callable(clean_text)
check('search _common (HEADERS, clean_text)', _search_common)

# 1f. __all__ consistency
def _search_all():
    import lib.search
    assert 'perform_web_search' in lib.search.__all__
    assert 'format_search_for_tool_response' in lib.search.__all__
check('search __all__ has public names', _search_all)


# ══════════════════════════════════════════════════════
#  2. lib/browser/
# ══════════════════════════════════════════════════════
print('\n═══ 2. lib/browser/ ═══')

check('import lib.browser', lambda: __import__('lib.browser'))

def _browser_queue():
    from lib.browser import (send_browser_command, is_extension_connected,
                             get_pending_commands, wait_for_commands,
                             resolve_command, resolve_batch,
                             mark_poll, get_connected_clients,
                             _set_active_client, _get_active_client,
                             _last_poll_time, _commands, _commands_lock)
    assert callable(send_browser_command)
    assert callable(is_extension_connected)
check('browser queue API (send, is_connected, pending, resolve, etc.)', _browser_queue)

def _browser_dispatch():
    from lib.browser import execute_browser_tool, BROWSER_HANDLERS
    assert callable(execute_browser_tool)
    assert isinstance(BROWSER_HANDLERS, dict)
    assert len(BROWSER_HANDLERS) >= 16, f'Only {len(BROWSER_HANDLERS)} handlers'
check('browser dispatch (execute_browser_tool, BROWSER_HANDLERS)', _browser_dispatch)

def _browser_display():
    from lib.browser import browser_tool_display
    assert callable(browser_tool_display)
    r = browser_tool_display('browser_list_tabs', {})
    assert '🌐' in r
check('browser display (browser_tool_display)', _browser_display)

def _browser_fetch():
    from lib.browser import fetch_url_via_browser
    assert callable(fetch_url_via_browser)
check('browser fetch (fetch_url_via_browser)', _browser_fetch)

def _browser_advanced():
    from lib.browser import (ADVANCED_BROWSER_TOOLS, ADVANCED_BROWSER_TOOL_NAMES,
                             right_click_menu_select, hover_and_click,
                             fill_form_sequential, wait_and_find_element)
    assert isinstance(ADVANCED_BROWSER_TOOLS, list)
    assert len(ADVANCED_BROWSER_TOOLS) == 3
    assert isinstance(ADVANCED_BROWSER_TOOL_NAMES, set)
    assert len(ADVANCED_BROWSER_TOOL_NAMES) == 3
check('browser advanced (tools + names)', _browser_advanced)

def _browser_handlers():
    from lib.browser.handlers import (
        _handle_list_tabs, _handle_read_tab, _handle_execute_js,
        _handle_screenshot, _handle_get_cookies, _handle_get_history,
        _handle_create_tab, _handle_close_tab, _handle_navigate,
        _handle_get_interactive_elements, _handle_click, _handle_keyboard,
        _handle_hover, _handle_wait, _handle_summarize_page,
        _handle_get_app_state)
    assert callable(_handle_list_tabs)
check('browser handlers (16 individual handlers)', _browser_handlers)

def _browser_all():
    import lib.browser
    for name in ['send_browser_command', 'execute_browser_tool',
                 'browser_tool_display', 'fetch_url_via_browser',
                 'BROWSER_HANDLERS', 'ADVANCED_BROWSER_TOOLS']:
        assert name in lib.browser.__all__, f'{name} not in __all__'
check('browser __all__ completeness', _browser_all)


# ══════════════════════════════════════════════════════
#  3. lib/pdf_parser/
# ══════════════════════════════════════════════════════
print('\n═══ 3. lib/pdf_parser/ ═══')

check('import lib.pdf_parser', lambda: __import__('lib.pdf_parser'))

def _pdf_core():
    from lib.pdf_parser import parse_pdf, extract_pdf_text
    assert callable(parse_pdf)
    assert callable(extract_pdf_text)
check('pdf_parser core (parse_pdf, extract_pdf_text)', _pdf_core)

def _pdf_vlm():
    from lib.pdf_parser import start_vlm_task, get_vlm_task, vlm_parse_pdf
    assert callable(start_vlm_task)
    assert callable(get_vlm_task)
    assert callable(vlm_parse_pdf)
check('pdf_parser vlm (start_vlm_task, get_vlm_task, vlm_parse_pdf)', _pdf_vlm)

def _pdf_images():
    from lib.pdf_parser import render_pdf_pages, detect_and_clip_figures, resize_image_bytes
    assert callable(render_pdf_pages)
    assert callable(detect_and_clip_figures)
check('pdf_parser images (render_pdf_pages, detect_and_clip_figures)', _pdf_images)

def _pdf_math():
    from lib.pdf_parser import postprocess_math_blocks
    assert callable(postprocess_math_blocks)
check('pdf_parser math (postprocess_math_blocks)', _pdf_math)

def _pdf_postprocess():
    from lib.pdf_parser import strip_manuscript_line_numbers, cleanup_markdown
    assert callable(strip_manuscript_line_numbers)
    assert callable(cleanup_markdown)
check('pdf_parser postprocess (strip_manuscript_line_numbers, cleanup_markdown)', _pdf_postprocess)

def _pdf_common():
    from lib.pdf_parser._common import MAX_PDF_BYTES, HAS_PYMUPDF4LLM
    assert isinstance(MAX_PDF_BYTES, int)
    assert isinstance(HAS_PYMUPDF4LLM, bool)
check('pdf_parser _common (MAX_PDF_BYTES, HAS_PYMUPDF4LLM)', _pdf_common)

def _pdf_all():
    import lib.pdf_parser
    for name in ['parse_pdf', 'extract_pdf_text']:
        assert name in lib.pdf_parser.__all__, f'{name} not in __all__'
check('pdf_parser __all__ completeness', _pdf_all)


# ══════════════════════════════════════════════════════
#  4. lib/skills/
# ══════════════════════════════════════════════════════
print('\n═══ 4. lib/skills/ ═══')

check('import lib.skills', lambda: __import__('lib.skills'))

def _skills_storage():
    from lib.skills import (create_skill, update_skill, delete_skill, merge_skills,
                            list_all_skills, list_skills, get_skill,
                            get_enabled_skills, get_eligible_skills, toggle_skill)
    assert callable(create_skill)
    assert callable(list_all_skills)
    assert callable(get_skill)
check('skills storage CRUD', _skills_storage)

def _skills_injection():
    from lib.skills import build_skills_context, SKILL_ACCUMULATION_INSTRUCTIONS
    assert callable(build_skills_context)
    assert isinstance(SKILL_ACCUMULATION_INSTRUCTIONS, str)
    assert len(SKILL_ACCUMULATION_INSTRUCTIONS) > 100
check('skills injection (build_skills_context, INSTRUCTIONS)', _skills_injection)

def _skills_tools():
    from lib.skills import ALL_SKILL_TOOLS, SKILL_TOOL_NAMES
    assert isinstance(ALL_SKILL_TOOLS, list)
    assert len(ALL_SKILL_TOOLS) == 4
    assert isinstance(SKILL_TOOL_NAMES, set)
    assert 'create_skill' in SKILL_TOOL_NAMES
    assert 'merge_skills' in SKILL_TOOL_NAMES
check('skills tools (ALL_SKILL_TOOLS, SKILL_TOOL_NAMES)', _skills_tools)

def _skills_constants():
    from lib.skills import GLOBAL_SKILLS_SUBDIR, PROJECT_SKILLS_SUBDIR, MIN_DESCRIPTION_LENGTH
    assert isinstance(GLOBAL_SKILLS_SUBDIR, str)
    assert isinstance(MIN_DESCRIPTION_LENGTH, int)
check('skills constants (GLOBAL_SKILLS_SUBDIR, etc.)', _skills_constants)

def _skills_all():
    import lib.skills
    for name in ['create_skill', 'update_skill', 'delete_skill', 'merge_skills',
                 'ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES', 'build_skills_context',
                 'SKILL_ACCUMULATION_INSTRUCTIONS']:
        assert name in lib.skills.__all__, f'{name} not in __all__'
check('skills __all__ completeness', _skills_all)


# ══════════════════════════════════════════════════════
#  5. routes/ (new Blueprints)
# ══════════════════════════════════════════════════════
print('\n═══ 5. routes/ (new Blueprints) ═══')

def _routes_conversations():
    from routes.conversations import conversations_bp
    assert conversations_bp.name == 'conversations'
check('routes/conversations.py imports', _routes_conversations)

def _routes_upload():
    from routes.upload import upload_bp
    assert upload_bp.name == 'upload'
check('routes/upload.py imports', _routes_upload)

def _routes_translate():
    from routes.translate import translate_bp
    assert translate_bp.name == 'translate'
check('routes/translate.py imports', _routes_translate)

def _routes_common():
    from routes.common import common_bp, _db_safe, DEFAULT_USER_ID
    from routes.common import _invalidate_meta_cache, _refresh_meta_cache_if_stale
    assert common_bp.name == 'common'
    assert callable(_db_safe)
    assert DEFAULT_USER_ID == 1
check('routes/common.py shared utilities', _routes_common)

def _routes_init():
    from routes import ALL_BLUEPRINTS
    names = [bp.name for bp in ALL_BLUEPRINTS]
    assert 'conversations' in names, f'conversations not in {names}'
    assert 'upload' in names, f'upload not in {names}'
    assert 'translate' in names, f'translate not in {names}'
    assert 'common' in names, f'common not in {names}'
check('routes/__init__ registers all new Blueprints', _routes_init)


# ══════════════════════════════════════════════════════
#  6. External consumer imports (every real import site)
# ══════════════════════════════════════════════════════
print('\n═══ 6. External consumer imports ═══')

# search consumers
def _consumer_search_executor():
    # lib/tasks_pkg/executor.py
    from lib.search import perform_web_search, format_search_for_tool_response
check('executor → lib.search', _consumer_search_executor)

# browser consumers
def _consumer_browser_executor():
    # lib/tasks_pkg/executor.py
    from lib.browser import execute_browser_tool
check('executor → lib.browser.execute_browser_tool', _consumer_browser_executor)

def _consumer_browser_model_config():
    # lib/model_config.py
    from lib.browser import ADVANCED_BROWSER_TOOLS, ADVANCED_BROWSER_TOOL_NAMES
check('model_config → lib.browser.ADVANCED_BROWSER_TOOLS', _consumer_browser_model_config)

def _consumer_browser_tool_display():
    # lib/tools/tool_display.py
    from lib.browser import browser_tool_display
check('tool_display → lib.browser.browser_tool_display', _consumer_browser_tool_display)

def _consumer_browser_routes():
    # routes/browser.py
    from lib.browser import (mark_poll, get_connected_clients, get_pending_commands,
                             wait_for_commands, resolve_batch, is_extension_connected,
                             _last_poll_time, _commands, _commands_lock)
check('routes/browser → lib.browser queue internals', _consumer_browser_routes)

def _consumer_browser_fetch():
    # lib/fetch/ uses this
    from lib.browser import fetch_url_via_browser, is_extension_connected
check('fetch → lib.browser.fetch_url_via_browser', _consumer_browser_fetch)

# pdf_parser consumers
def _consumer_pdf_upload():
    from lib.pdf_parser import parse_pdf, start_vlm_task, get_vlm_task
check('upload route → lib.pdf_parser', _consumer_pdf_upload)

def _consumer_pdf_fetch():
    from lib.pdf_parser import extract_pdf_text
check('fetch → lib.pdf_parser.extract_pdf_text', _consumer_pdf_fetch)

# skills consumers
def _consumer_skills_executor():
    from lib.skills import (create_skill, update_skill, delete_skill, merge_skills,
                            list_all_skills, get_skill, toggle_skill)
check('executor → lib.skills CRUD', _consumer_skills_executor)

def _consumer_skills_model_config():
    from lib.skills import ALL_SKILL_TOOLS, SKILL_TOOL_NAMES, SKILL_ACCUMULATION_INSTRUCTIONS
check('model_config → lib.skills tools', _consumer_skills_model_config)

def _consumer_skills_injection():
    from lib.skills import build_skills_context
check('orchestrator → lib.skills.build_skills_context', _consumer_skills_injection)

# translate route consumers (cross-import from common)
def _consumer_translate_common():
    from routes.common import _db_safe, DEFAULT_USER_ID
    from routes.common import _invalidate_meta_cache, _refresh_meta_cache_if_stale
check('conversations/upload → routes.common shared utils', _consumer_translate_common)


# ══════════════════════════════════════════════════════
#  7. Flask app route registration
# ══════════════════════════════════════════════════════
print('\n═══ 7. Flask route registration ═══')

def _flask_routes():
    # Build a minimal Flask app with just blueprint registration (no DB init)
    from flask import Flask
    test_app = Flask(__name__)
    from routes import ALL_BLUEPRINTS
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
        '/api/errors/recent',
        '/api/errors/digest',
        '/api/server-config',
        '/api/features',
        '/api/log/compress',
        '/',
    ]

    missing = [ep for ep in critical if ep not in rules]
    if missing:
        raise AssertionError(f'Missing routes: {missing}')
    print(f'    ({len(rules)} total routes, {len(critical)} critical verified)')

check('All critical API routes registered', _flask_routes)


# ══════════════════════════════════════════════════════
#  8. No stale old files remain
# ══════════════════════════════════════════════════════
print('\n═══ 8. Stale file check ═══')

def _no_stale():
    stale = []
    for path in [
        'lib/search.py',
        'lib/browser.py',
        'lib/browser_advanced.py',
        'lib/pdf_parser.py',
        'lib/skills.py',
    ]:
        if os.path.isfile(path):
            stale.append(path)
    if stale:
        raise AssertionError(f'Stale files still exist: {stale}')
check('No stale monolithic .py files', _no_stale)

def _packages_exist():
    for pkg in ['lib/search/__init__.py', 'lib/browser/__init__.py',
                'lib/pdf_parser/__init__.py', 'lib/skills/__init__.py']:
        assert os.path.isfile(pkg), f'{pkg} not found'
check('All 4 new packages have __init__.py', _packages_exist)


# ══════════════════════════════════════════════════════
#  Summary
# ══════════════════════════════════════════════════════
print(f'\n{"="*50}')
print(f'Results: {PASS} passed, {FAIL} failed')
if FAIL:
    print('⚠️  FAILURES DETECTED — see above')
    sys.exit(1)
else:
    print('✅ ALL TESTS PASSED')
    sys.exit(0)
