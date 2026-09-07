"""Tool-round label readability.

Pins the rules behind the display overhaul:
- no raw operation enums (``(3 edits: replace, replace, replace)``),
- no machine handles a human cannot read (conversation ids, tab ids,
  artifact digests) when a human-readable counterpart exists,
- bare function names (``schedule_create``) become friendly labels.
"""

import pytest

pytestmark = pytest.mark.unit


# ── edit_file: description headlines, op enums are gone ──────────────────

def test_edit_file_label_headlines_description_not_op_enums():
    from lib.project_mod.tools import project_tool_display

    label = project_tool_display('edit_file', {
        'description': 'refactor the display labels',
        'edits': [
            {'path': 'ui/tool_rounds_rich.js', 'operation': 'replace',
             'anchor': 'a', 'content': 'b'},
            {'path': 'ui/tool_rounds_rich.js', 'operation': 'replace',
             'anchor': 'c', 'content': 'd'},
            {'path': 'ui/tool_rounds_rich.js', 'operation': 'insert_before',
             'anchor': 'e', 'content': 'f'},
        ],
    })
    assert label == ('Edit ui/tool_rounds_rich.js (3 edits) — '
                     'refactor the display labels')
    assert 'replace' not in label.split(' — ')[0]


def test_edit_file_label_single_edit_and_multi_file_and_no_description():
    from lib.project_mod.tools import project_tool_display

    assert project_tool_display('edit_file', {
        'description': 'fix the typo',
        'edits': [{'path': 'a.py', 'operation': 'replace',
                   'anchor': 'x', 'content': 'y'}],
    }) == 'Edit a.py — fix the typo'

    assert project_tool_display('edit_file', {
        'description': 'rename the helper',
        'edits': [
            {'path': 'a.py', 'operation': 'replace',
             'anchor': 'x', 'content': 'y'},
            {'path': 'b.py', 'operation': 'replace',
             'anchor': 'x', 'content': 'y'},
        ],
    }) == 'Edit 2 files (2 edits) — rename the helper'

    assert project_tool_display('edit_file', {
        'edits': [{'path': 'a.py', 'operation': 'replace',
                   'anchor': 'x', 'content': 'y'},
                  {'path': 'a.py', 'operation': 'replace',
                   'anchor': 'z', 'content': 'w'}],
    }) == 'Edit a.py (2 edits)'


# ── conversation reference: title over id ────────────────────────────────

def test_conv_ref_labels_use_title_hint_and_fall_back_to_short_id():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label(
        'get_conversation',
        {'conversation_id': 'mt18xr3wfs0rbq',
         '_conv_title': '聊聊工具展示'},
    ) == 'Read conversation "聊聊工具展示"'
    assert tool_round_label(
        'get_conversation', {'conversation_id': 'mt18xr3wfs0rbq'},
    ) == 'Read conversation mt18xr3w'
    assert tool_round_label('get_conversation', {}) == 'Read conversation'


def test_list_conversations_label_names_keyword():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label('list_conversations', {}) == 'List conversations'
    assert tool_round_label(
        'list_conversations', {'keyword': 'display'},
    ) == 'List conversations matching "display"'


def test_retained_integration_controls_have_readable_labels():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label(
        'integration_checkpoint', {'note': 'before migration'},
    ) == 'Checkpoint Git integration'
    assert tool_round_label(
        'integration_submit', {'summary': 'verified migration'},
    ) == 'Submit Git integration: verified migration'


# ── scheduler / desktop / swarm: friendly labels, no bare fn names ───────

def test_scheduler_labels_are_friendly():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label(
        'schedule_create', {'name': 'nightly sync', 'schedule': '0 3 * * *'},
    ) == 'Create schedule "nightly sync" · 0 3 * * *'
    assert tool_round_label('schedule_list', {}) == 'List scheduled tasks'
    assert tool_round_label(
        'schedule_manage', {'action': 'pause', 'task_id': 'x1y2'},
    ) == 'Schedule: pause'
    assert tool_round_label(
        'await_task', {'action': 'wait', 'task_id': 'x1y2'},
    ) == 'Await task: wait'
    assert tool_round_label(
        'timer_create', {'check_instruction': '检查训练任务是否完成'},
    ) == 'Create timer watcher: 检查训练任务是否完成'
    assert tool_round_label(
        'timer_manage', {'action': 'cancel', 'timer_id': 'abc'},
    ) == 'Timer watcher: cancel'


def test_desktop_labels_name_the_salient_argument():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label(
        'desktop_list_files', {'path': '~/Documents'},
    ) == 'List local files: ~/Documents'
    assert tool_round_label(
        'desktop_open_app', {'app': 'code'},
    ) == 'Open app: code'
    assert tool_round_label(
        'desktop_run_command', {'command': 'git status'},
    ) == 'Run on desktop: git status'
    assert tool_round_label(
        'desktop_screenshot', {}) == 'Screenshot the desktop'
    assert tool_round_label(
        'desktop_gui_action', {'action': 'type', 'text': 'hello'},
    ) == 'Desktop GUI: type "hello"'
    assert tool_round_label(
        'desktop_system_info', {'type': 'processes'},
    ) == 'Desktop system info [processes]'


def test_swarm_get_agent_result_shows_full_agent_id():
    from lib.tasks_pkg.tool_display import tool_round_label

    assert tool_round_label(
        'get_agent_result', {'agent_id': 'agent-researcher-a1'},
    ) == 'Fetching result for agent-researcher-a1'


# ── swarm timeline brief: delegates to the round-label renderers ─────────

def test_format_tool_args_brief_delegates_to_round_labels():
    from lib.project_mod.tools import format_tool_args_brief

    # A tool the brief has no fast path for: the raw-args dump is replaced
    # by the same label the chat timeline renders.
    assert format_tool_args_brief(
        'browser_read_page', {'tab_id': 3}) == 'Read tab'
    assert format_tool_args_brief(
        'schedule_create', {'name': 'nightly', 'schedule': '0 3 * * *'}
    ) == 'Create schedule "nightly" · 0 3 * * *'
    # Fast paths are unchanged.
    assert format_tool_args_brief(
        'web_search', {'query': 'tofu'}) == 'tofu'
    # Unknown tools still degrade to a clipped args repr.
    brief = format_tool_args_brief('some_unknown_tool', {'zzz': 1})
    assert 'zzz' in brief


# ── browser tab-title cache ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_display_caches():
    from lib.browser import tab_titles
    from lib.browser import _resolve
    from lib.tasks_pkg.tool_display import _context
    tab_titles._reset_for_tests()
    with _resolve._work_tab_lock:
        _resolve._work_tabs.clear()
    with _context._conv_title_lock:
        _context._conv_title_cache.clear()
    yield
    tab_titles._reset_for_tests()
    with _resolve._work_tab_lock:
        _resolve._work_tabs.clear()
    with _context._conv_title_lock:
        _context._conv_title_cache.clear()


def test_tab_title_cache_unique_match_and_cross_device_ambiguity():
    from lib.browser import tab_titles

    tabs = [{'id': 42, 'title': 'Friday MCP Hub', 'url': 'https://f.example'}]
    tab_titles.ingest_tab_list('7', 'dev-a', tabs)
    assert tab_titles.tab_title('7', 42, client_ids=['dev-a']) == \
        'Friday MCP Hub'
    # Another owner never sees it.
    assert tab_titles.tab_title('8', 42, client_ids=['dev-a']) == ''
    # Same tab id on a second device with a DIFFERENT title → ambiguous → ''.
    tab_titles.ingest_tab_list(
        '7', 'dev-b', [{'id': 42, 'title': 'Other Page', 'url': ''}])
    assert tab_titles.tab_title('7', 42, client_ids=['dev-a', 'dev-b']) == ''
    # Same title on both devices is unambiguous.
    tab_titles.ingest_tab_list(
        '7', 'dev-b', [{'id': 42, 'title': 'Friday MCP Hub', 'url': ''}])
    assert tab_titles.tab_title(
        '7', 42, client_ids=['dev-a', 'dev-b']) == 'Friday MCP Hub'


def test_runtime_send_harvests_list_tabs_into_title_cache():
    from lib.browser import tab_titles
    from lib.browser.tool_runtime import BrowserToolRuntime

    def fake_sender(command, params=None, timeout=30, client_id=None,
                    owner_user_id=None):
        assert command == 'list_tabs'
        return ([{'id': 9, 'title': 'Dashboard', 'url': 'https://d.example'}],
                None)

    rt = BrowserToolRuntime(owner_user_id='7', client_id='dev-a',
                            sender=fake_sender)
    result, error = rt.send('list_tabs')
    assert error is None and isinstance(result, list)
    assert tab_titles.tab_title('7', 9, client_ids=['dev-a']) == 'Dashboard'


def test_browser_labels_render_tab_title_hint():
    from lib.browser.display import browser_tool_display

    assert browser_tool_display(
        'browser_read_page', {'tab_id': 42, '_tab_title': 'Friday MCP Hub'}
    ) == 'Read "Friday MCP Hub"'
    assert browser_tool_display(
        'browser_read_page', {'_tab_title': 'Friday MCP Hub'}
    ) == 'Read "Friday MCP Hub"'
    assert browser_tool_display(
        'browser_click', {'text': '登录', '_tab_title': 'Friday MCP Hub'}
    ) == 'Click "Friday MCP Hub": 登录'
    assert browser_tool_display(
        'browser_close_tab', {'_tab_title': 'Friday MCP Hub'}
    ) == 'Close "Friday MCP Hub"'
    # Long titles are clipped.
    long_title = 'T' * 80
    label = browser_tool_display(
        'browser_read_page', {'_tab_title': long_title})
    assert label == f'Read "{"T" * 59}…"'


# ── display-time enrichment (conversation + tab titles) ─────────────────

def test_enrich_display_args_resolves_conversation_title(monkeypatch):
    from lib.conversations import repository
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    monkeypatch.setattr(
        repository, 'get_conversation',
        lambda cid, *, user_id, include_messages=True:
            {'title': '工具展示讨论'} if cid == 'mt18xr3wfs0rbq' else None)

    args = {'conversation_id': 'mt18xr3wfs0rbq'}
    enriched = enrich_display_args(
        'get_conversation', args, task={'_userId': '7'})
    assert enriched['_conv_title'] == '工具展示讨论'
    # The original dict is never mutated — hints cannot leak into execution.
    assert '_conv_title' not in args
    # Unknown conversation → no hint, original object back.
    assert enrich_display_args(
        'get_conversation', {'conversation_id': 'nope'},
        task={'_userId': '7'}) == {'conversation_id': 'nope'}


def test_enrich_display_args_resolves_tab_title(monkeypatch):
    import lib.browser.queue as browser_queue
    from lib.browser import tab_titles
    from lib.browser._resolve import remember_work_tab
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    monkeypatch.setattr(
        browser_queue, 'get_connected_clients',
        lambda *, owner_user_id: [{'client_id': 'dev-a'}])
    tab_titles.ingest_tab_list(
        '7', 'dev-a', [{'id': 42, 'title': 'Friday MCP Hub', 'url': ''}])

    # Explicit tab id.
    enriched = enrich_display_args(
        'browser_read_page', {'tab_id': 42}, task={'_userId': '7'})
    assert enriched['_tab_title'] == 'Friday MCP Hub'
    # Implicit working tab.
    remember_work_tab(('7', 'dev-a'), 42)
    enriched = enrich_display_args(
        'browser_click', {'text': '登录'}, task={'_userId': '7'})
    assert enriched['_tab_title'] == 'Friday MCP Hub'
    # Unknown tab → no hint.
    assert enrich_display_args(
        'browser_read_page', {'tab_id': 999},
        task={'_userId': '7'}) == {'tab_id': 999}


def test_enrich_display_args_noops_without_owner_context():
    from lib.tasks_pkg.tool_display._context import enrich_display_args

    args = {'conversation_id': 'mt18xr3wfs0rbq'}
    assert enrich_display_args('get_conversation', args) is args
    assert enrich_display_args('get_conversation', args, task={}) is args
    assert enrich_display_args(
        'get_conversation', args, task={'_userId': 'not-a-number'}) is args


def test_build_tool_round_entry_renders_resolved_titles(monkeypatch):
    import lib.browser.queue as browser_queue
    from lib.browser import tab_titles
    from lib.conversations import repository
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry

    monkeypatch.setattr(
        repository, 'get_conversation',
        lambda cid, *, user_id, include_messages=True:
            {'title': '工具展示讨论'})
    _, entry, event = _build_tool_round_entry(
        'get_conversation', {'conversation_id': 'mt18xr3wfs0rbq'},
        'tc_1', '{}', 0, False, task={'_userId': '7'})
    assert entry['query'] == 'Read conversation "工具展示讨论"'
    assert event['query'] == entry['query']

    monkeypatch.setattr(
        browser_queue, 'get_connected_clients',
        lambda *, owner_user_id: [{'client_id': 'dev-a'}])
    tab_titles.ingest_tab_list(
        '7', 'dev-a', [{'id': 42, 'title': 'Friday MCP Hub', 'url': ''}])
    _, entry2, _ = _build_tool_round_entry(
        'browser_read_page', {'tab_id': 42},
        'tc_2', '{}', 0, False, task={'_userId': '7'})
    assert entry2['query'] == 'Read "Friday MCP Hub"'
