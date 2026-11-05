"""Authenticated-download interception belongs to actual shell invocations.

All URLs, cookie values and shell commands are synthetic parser inputs; these
specifications never issue HTTP or replay authentication material.
"""
from __future__ import annotations

import pytest

from lib.project_mod.download_intent import parse_authenticated_download_command

pytestmark = pytest.mark.unit

_LITERAL_DIAGNOSTIC = '''python3 - <<'PY'
commands = [
    'cd /tmp && curl -s https://files.test/api | python3 -c "print(1)"',
    'python3 x.py >> out',
]
for command in commands:
    print(command)
PY
git grep -n "def _redirect_only_reader" -B2 -A3 -- lib/project_mod/run_command.py
'''


@pytest.mark.parametrize('command', [
    pytest.param(_LITERAL_DIAGNOSTIC, id='python-heredoc-and-grep-B2'),
    pytest.param(
        'git grep -n -B2 "example curl -o out" -- lib',
        id='git-grep-quoted-curl-example'),
    pytest.param(
        'printf ok # curl -b sid=synthetic -o out https://files.test/data',
        id='shell-comment-is-data'),
    pytest.param(
        "curl -b 'sid=synthetic' https://files.test/api # -o not-an-option",
        id='comment-does-not-become-curl-output'),
    pytest.param(
        "printf '%s' '$(curl -b sid=synthetic -o out https://files.test/data)'",
        id='single-quoted-substitution-is-data'),
    pytest.param(
        'python3 - <<\'PY\'\n# curl -b sid=synthetic -o out https://files.test/data\n'
        'print("ok")\nPY\necho "$(date +%s)"\n',
        id='literal-heredoc-before-executable-substitution'),
    pytest.param(
        "cat <<'TEXT'\ncurl -b sid=synthetic -o out https://files.test/data\nTEXT\n"
        'printf "%s" "`date`"\n',
        id='literal-heredoc-before-backticks'),
    pytest.param(
        'git checkout -b fix && curl -s https://files.test/api > out.json',
        id='git-belongs-to-git'),
    pytest.param(
        'grep -B3 err log.txt; curl -sS https://files.test/api 2>&1',
        id='grep-B-belongs-to-grep'),
    pytest.param(
        "curl -b 'sid=synthetic' https://files.test/api 2>&1",
        id='cookie-api-descriptor-duplication'),
    pytest.param(
        "curl -b 'sid=synthetic' https://files.test/api > /dev/null",
        id='cookie-api-discarded-output'),
    pytest.param(
        'sort -b names.txt > sorted.txt && wget https://files.test/data',
        id='sort-b-belongs-to-sort'),
    pytest.param(
        'grep -n "cookie:" notes.md; curl -s https://files.test/api > out.json',
        id='cookie-search-pattern-is-data'),
    pytest.param(
        'curl -s https://files.test/api | '
        'python3 -c "print(\'cookie: sid=synthetic\')" > out.json',
        id='downstream-python-is-data'),
    pytest.param(
        "curl -b 'sid=synthetic' https://files.test/api; printf done > out.txt",
        id='later-redirect-belongs-to-printf'),
    pytest.param(
        "curl -b 'sid=synthetic' https://files.test/api | grep -b id > out.txt",
        id='pipeline-flags-belong-to-filter'),
    pytest.param(
        "curl -s https://files.test/api; curl -b 'sid=synthetic' https://files.test/api",
        id='distinct-curl-invocations'),
    pytest.param(
        'printf "%s\\n" "curl -b sid=synthetic -o out https://files.test/data"',
        id='quoted-printf-data'),
    pytest.param(
        'printf "%s\\n" curl -b sid=synthetic -o out https://files.test/data',
        id='unquoted-printf-operands'),
    pytest.param(
        "cat <<'TEXT'\ncurl -b 'sid=synthetic' -o out https://files.test/data\nTEXT\n",
        id='literal-cat-stdin'),
    pytest.param(
        'python3 -c "print(\'curl -b sid=synthetic -o out https://files.test/data\')"',
        id='quoted-inline-program'),
    pytest.param(
        "sh report.sh -c 'curl -b sid=synthetic -o out https://files.test/data'",
        id='shell-script-arguments-are-not-c-source'),
    pytest.param(
        "cat <<'TEXT'\nliteral \\\ncurl -b sid=synthetic -o out https://files.test/data\nTEXT\n",
        id='literal-heredoc-backslash-newline'),
    pytest.param(
        "cd /tmp && curl -b 'sid=synthetic' -X POST -o out https://files.test/api",
        id='wrapped-post-is-not-get-download'),
    pytest.param(
        "cd /tmp && wget --load-cookies cookies.txt -O - https://files.test/api",
        id='wrapped-wget-stdout'),
    pytest.param(
        'command -v curl; printf "%s" "Cookie: sid=synthetic" > out',
        id='discovery-does-not-invoke-curl'),
])
def test_cookie_intent_never_combines_other_commands_or_literal_data(command):
    assert parse_authenticated_download_command(command) is None


@pytest.mark.parametrize('command', [
    "cd /tmp && curl -b 'sid=synthetic' -o out https://files.test/data",
    "git grep -B2 x; curl -H 'Cookie: sid=synthetic' -O https://files.test/data",
    "curl -s https://files.test/api | curl -b 'sid=synthetic' -o out https://files.test/data",
    "curl -s https://files.test/api; wget --load-cookies cookies.txt https://files.test/data",
    "HTTPS_PROXY=https://proxy.test curl -b 'sid=synthetic' -o out https://files.test/data",
    "env MODE=check curl -b 'sid=synthetic' -o out https://files.test/data",
    "command curl -b 'sid=synthetic' -o out https://files.test/data",
    "curl -b 'sid=synthetic' -o out https://files.test/data # comment",
    "printf '%s' \"`curl -b 'sid=synthetic' -o out https://files.test/data`\"",
    "curl -b 'sid=synthetic' -o out https://files.test/data & printf done",
    "printf done & curl -b 'sid=synthetic' -o out https://files.test/data",
    "2>err MODE=check /usr/bin/curl -b 'sid=synthetic' https://files.test/data",
    "cd /tmp && /usr/bin/curl --cookie=synthetic --output=out https://files.test/data",
    "cd /tmp && wget --load-cookies=cookies.txt https://files.test/data",
    "wget --load-cookies cookies.txt https://files.test/data; wget -O - https://files.test/api",
    "bash -lc \"curl -bsid=synthetic -oout https://files.test/data\"",
    "cat <<TEXT\n$(curl -b 'sid=synthetic' -o out https://files.test/data)\nTEXT\n",
    "cat <<TEXT\n'$(curl -b sid=synthetic -o out https://files.test/data)'\nTEXT\n",
    "cat <<'SH' | sh\ncurl -b sid=synthetic -o out https://files.test/data\nSH\n",
    "cat <<'SH' | sh 2>/dev/null\ncurl -b sid=synthetic -o out https://files.test/data\nSH\n",
    "cat <<'SH' | bash --norc\ncurl -b sid=synthetic -o out https://files.test/data\nSH\n",
    "cat <<'SH' | sh -o errexit\ncurl -b sid=synthetic -o out https://files.test/data\nSH\n",
    "cat <<'SH' | sh -s -- argument\ncurl -b sid=synthetic -o out https://files.test/data\nSH\n",
    'printf "%s" "$(printf ok #)\ncurl -b sid=synthetic -o out https://files.test/data\n)"',
    "curl -b sid=synthetic https://files.test/data > out",
    "eval \"curl -b 'sid=synthetic' -o out https://files.test/data\"",
    "exec curl -b 'sid=synthetic' -o out https://files.test/data",
    "sudo -u nobody curl -b 'sid=synthetic' -o out https://files.test/data",
    "timeout 5 curl -b 'sid=synthetic' -o out https://files.test/data",
    "for f in one two; do curl -b 'sid=synthetic' -o \"$f\" https://files.test/data; done",
    "bash -c \"cd /tmp && curl -b 'sid=synthetic' -o out https://files.test/data\"",
    "printf '%s' \"$(curl -b 'sid=synthetic' -o out https://files.test/data)\"",
    "sh <<'SH'\ncurl -b 'sid=synthetic' -o out https://files.test/data\nSH\n",
    _LITERAL_DIAGNOSTIC + "curl -b 'sid=synthetic' -o out https://files.test/data\n",
    "curl -b 'sid=synthetic -o out https://files.test/data",
])
def test_real_wrapped_cookie_file_downloads_remain_blocked(command):
    intent = parse_authenticated_download_command(command)
    assert intent is not None
    assert not intent.redirectable
    assert intent.block_reason
    assert 'synthetic' not in repr(intent)
    assert 'cookies.txt' not in repr(intent)


def test_standalone_url_fragment_remains_literal_download_url():
    url = 'https://files.test/data#part'
    intent = parse_authenticated_download_command(
        f'curl -b sid=synthetic -o out {url}')
    assert intent is not None and intent.redirectable
    assert intent.url == url


def test_literal_diagnostic_passes_shared_pre_spawn_adapter():
    from lib.tasks_pkg.handlers.authenticated_download import (
        maybe_redirect_authenticated_download,
    )

    assert maybe_redirect_authenticated_download(
        task={}, cfg={}, command=_LITERAL_DIAGNOSTIC) is None


_SESSION_JAR_PROBE = (
    'cd /tmp && rm -f tj.jar && curl -s -m 20 -c tj.jar -A "Mozilla/5.0 (iPhone; CPU)" '
    'https://files.test/ -o /dev/null -w "home=%{http_code}\\n" && '
    'curl -s -m 20 -b tj.jar -H "Referer: https://files.test/" '
    '"https://files.test/api/feed" -o feed.json -w "api=%{http_code}\\n"; '
    'head -c 300 feed.json; echo; '
    "awk '!/^#/ && NF {print $6}' tj.jar | tr '\\n' ' '"
)


@pytest.mark.parametrize('command', [
    pytest.param(_SESSION_JAR_PROBE, id='anonymous-session-jar-api-probe'),
    pytest.param(
        'curl -s -c jar https://files.test/ > /dev/null\n'
        'wget --load-cookies jar -O out https://files.test/data\n',
        id='curl-jar-loaded-by-later-wget'),
    pytest.param(
        'wget -q --save-cookies=jar -O /dev/null https://files.test/ && '
        'curl --cookie jar --output out https://files.test/data',
        id='wget-jar-loaded-by-later-curl'),
    pytest.param(
        'curl -c jar https://files.test/ && curl -b jar -c jar https://files.test/login'
        ' && curl -b jar -o out https://files.test/data',
        id='session-walk-keeps-its-own-jar'),
    pytest.param(
        'JAR=$(mktemp) && curl -c "$JAR" https://files.test/ && '
        'curl -b "$JAR" -o out https://files.test/data',
        id='same-jar-variable'),
    pytest.param(
        "curl -b 'sid=synthetic' -o /dev/null -w '%{http_code}' https://files.test/api",
        id='status-probe-discards-output'),
    pytest.param(
        "cd /tmp && curl -b 'sid=synthetic' -o - https://files.test/api | head",
        id='stdout-output-is-api-inspection'),
    pytest.param(
        'cd /tmp && wget --load-cookies cookies.txt -O /dev/null https://files.test/api',
        id='wget-discarded-output'),
])
def test_own_session_jars_and_discarded_output_are_not_cookie_downloads(command):
    assert parse_authenticated_download_command(command) is None


@pytest.mark.parametrize('command', [
    pytest.param(
        'curl -b jar -o out https://files.test/data; curl -c jar https://files.test/',
        id='jar-loaded-before-it-is-written'),
    pytest.param(
        'cd /tmp && curl -b jar -c jar -o out https://files.test/data',
        id='jar-named-only-by-its-reader'),
    pytest.param(
        "curl -b 'sid=synthetic' -c jar https://files.test/ && "
        'curl -b jar -o out https://files.test/data',
        id='jar-written-from-cookie-data'),
    pytest.param(
        'curl -b cookies.txt -c jar https://files.test/ && '
        'curl -b jar -o out https://files.test/data',
        id='jar-written-from-foreign-cookie-file'),
    pytest.param(
        "curl -c jar https://files.test/; curl -H 'Cookie: sid=synthetic' -c jar "
        'https://files.test/; curl -b jar -o out https://files.test/data',
        id='jar-rewritten-from-cookie-header'),
    pytest.param(
        "curl -c jar https://files.test/ && curl -b jar -b 'sid=synthetic' "
        '-o out https://files.test/data',
        id='own-jar-plus-cookie-data'),
    pytest.param(
        "cd /tmp && curl -H 'Cookie: sid=synthetic' -b '' -o out https://files.test/data",
        id='empty-cookie-engine-flag-keeps-header'),
    pytest.param(
        'curl -c jar https://files.test/; curl -c "$(cat name)" https://files.test/; '
        'curl -b jar -o out https://files.test/data',
        id='substitution-may-rewrite-any-jar'),
    pytest.param(
        'curl -c "$(printf jar)" https://files.test/ && '
        'curl -b "$(printf jar)" -o out https://files.test/data',
        id='substitution-placeholders-are-not-paths'),
])
def test_cookie_jars_without_own_session_provenance_remain_blocked(command):
    intent = parse_authenticated_download_command(command)
    assert intent is not None
    assert not intent.redirectable
    assert intent.block_reason
    assert 'synthetic' not in repr(intent)


def test_standalone_jar_without_provenance_is_still_redirected():
    intent = parse_authenticated_download_command(
        'curl -b jar -c jar -o out https://files.test/data')
    assert intent is not None and intent.redirectable
    assert intent.url == 'https://files.test/data'
