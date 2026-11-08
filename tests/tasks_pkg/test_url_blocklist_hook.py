"""The pre-tool hook refuses shell/browser transfers of a blocklisted URL.

Without this, search.skip_domains guarded fetch_url only: the same URL stayed
reachable through ``run_command`` (curl/wget/urlopen/git clone) and browser
navigation, which is how an evaluation arm downloaded hidden tests.
"""
import pytest

from lib.search.url_access import blocked_text_message, blocked_url_message, url_argument_blocklist_hook
from lib.tasks_pkg import tool_hooks

pytestmark = pytest.mark.unit

ENTRIES = {'github.com/harbor-framework', 'raw.githubusercontent.com/harbor-framework',
           'marginlab.ai', 'github.com/*/terminal-bench', 'raw.githubusercontent.com/*/terminal-bench', 'x.com'}


@pytest.fixture(autouse=True)
def entries(monkeypatch):
    import lib
    monkeypatch.setattr(lib, 'SKIP_DOMAINS', ENTRIES, raising=False)


@pytest.mark.parametrize('command', [
    "curl -sSL https://raw.githubusercontent.com/harbor-framework/terminal-bench/main/tests/x.py",
    "curl -o /tmp/t.mp4 https://github.com/harbor-framework/terminal-bench-2-1/raw/main/tests/t.mp4",
    "python -c \"from urllib.request import urlopen; urlopen('https://marginlab.ai/explorers/x').read()\"",
    "git clone https://github.com/laude-institute/terminal-bench /tmp/x",
    "wget https://github.com/lynl7130/terminal-bench/archive/main.tar.gz",
])
def test_shell_transfers_of_blocked_urls_are_refused(command):
    refusal = blocked_text_message(command)
    assert refusal is not None
    assert 'skip_domains' in refusal
    assert 'another transport' in refusal


@pytest.mark.parametrize('command', [
    "curl -sSL https://raw.githubusercontent.com/python/cpython/main/README.rst",
    "git clone https://github.com/povray/povray.git /tmp/povray",
    "ls /app/terminal-bench-notes && grep -r harbor-framework /app/src",
    "python -c \"print('marginlab.ai is mentioned in prose, not fetched')\"",
    "curl -sSL https://www.dropbox.com/s/abc/file.pdf -o /tmp/f.pdf",
])
def test_legitimate_commands_stay_allowed(command):
    assert blocked_text_message(command) is None


def test_hook_blocks_run_command_and_leaves_other_tools_alone():
    result = url_argument_blocklist_hook(
        'run_command', {'command': 'curl -sSL https://marginlab.ai/explorers/terminal-bench/x'}, {})
    assert result is not None and result.action == 'block'
    assert 'skip_domains' in result.message
    assert url_argument_blocklist_hook(
        'run_command', {'command': 'curl -sSL https://example.com/public'}, {}) is None
    assert url_argument_blocklist_hook('read_files', {'path': 'https://marginlab.ai/x'}, {}) is None


def test_hook_covers_browser_navigation():
    result = url_argument_blocklist_hook(
        'browser_navigate', {'url': 'https://github.com/harbor-framework/terminal-bench'}, {})
    assert result is not None and result.action == 'block'


def test_hook_is_registered_and_runs_before_execution():
    verdict = tool_hooks.run_pre_hooks(
        'run_command', {'command': 'curl -sSL https://marginlab.ai/explorers/x'}, {})
    assert verdict is not None and verdict.action == 'block'
    assert verdict.additional_context


def test_browser_navigate_handler_refuses_a_blocked_url():
    from lib.browser.handlers._tabs import _handle_navigate

    class Runtime:
        route_key = 'test'

        def send(self, *args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError('navigation must not be sent for a blocked URL')

    refusal = _handle_navigate({'url': 'https://marginlab.ai/explorers/terminal-bench/x'}, Runtime())
    assert 'skip_domains' in refusal


def test_blocked_url_message_names_the_entry_and_the_setting():
    message = blocked_url_message('https://github.com/harbor-framework/x')
    assert "github.com/harbor-framework" in message
    assert 'search.skip_domains' in message
    assert 'Settings' in message


def test_url_token_extraction_does_not_swallow_python_syntax():
    command = ("python - <<'EOF'\n"
               "u = 'https://github.com/harbor-framework/terminal-bench-2-1/main/tests/test_video.mp4'\n"
               "urlopen(u, timeout=60).read()\n"
               "EOF")
    refusal = blocked_text_message(command)
    assert refusal is not None
    assert 'github.com/harbor-framework' in refusal
