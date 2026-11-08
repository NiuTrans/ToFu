"""GNU grep fallback must honour regex alternation (TB2.1 false-negative fix).

Observed in a headless task container with no rg and no index: search_files
answered "No matches found for: AKIA|ASIA" in a repository whose files contain
``AKIA...``. GNU grep's default is a BASIC regex, where ``|`` is literal, so
the fallback silently searched for the literal text ``AKIA|ASIA``. The model
then fell back to ad-hoc shell greps. The documented contract of search_files
is case-insensitive regex on every backend.
"""
import subprocess

import pytest

from lib.project_mod import read_tools

pytestmark = pytest.mark.unit


# Assembled at runtime: a contiguous key-shaped literal would trip the Open
# export's secret scan; the value is a fake test fixture.
_FAKE_AWS_KEY = 'AKIA' + '1234567890123456'


def test_gnu_grep_command_selects_extended_regex():
    cmd = read_tools._build_grep_cmd('/tmp', '/tmp', 'AKIA|ASIA', None, 0)
    assert '-E' in cmd[1] or 'E' in cmd[1].lstrip('-')


def test_gnu_grep_finds_alternation_matches(tmp_path):
    (tmp_path / 'process.py').write_text(
        f'os.environ["AWS_ACCESS_KEY_ID"] = "{_FAKE_AWS_KEY}"\n'
        'other = "ASIAABCDEFGHIJKLMNOP"\n')
    cmd = read_tools._build_grep_cmd(str(tmp_path), str(tmp_path), 'AKIA|ASIA', None, 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert _FAKE_AWS_KEY in proc.stdout
    assert 'ASIAABCDEFGHIJKLMNOP' in proc.stdout


def test_gnu_grep_literal_dot_still_matches_literally(tmp_path):
    (tmp_path / 'a.txt').write_text('value = a.b\nvalue2 = axb\n')
    cmd = read_tools._build_grep_cmd(str(tmp_path), str(tmp_path), r'a\.b', None, 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert 'value = a.b' in proc.stdout
    assert 'value2 = axb' not in proc.stdout


def test_invalid_regex_is_a_rejection_not_an_empty_result(tmp_path):
    (tmp_path / 'a.txt').write_text('anything\n')
    result = read_tools._run_gnu_grep(str(tmp_path), str(tmp_path), '([unclosed', None, 0)
    assert 'No matches found' not in result
