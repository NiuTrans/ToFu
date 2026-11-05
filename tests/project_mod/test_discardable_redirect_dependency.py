"""Command admission keeps discarded redirects separate from actual writes."""
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('source, expected', [
    ('ls -d . 2>/dev/null', ('ls -d .', '2>/dev/null')),
    ('git grep needle >/dev/null 2>&1', ('git grep needle', '>/dev/null 2>&1')),
    ('printf ">" 1>&2', ('printf ">"', '1>&2')),
])
def test_trailing_discarded_output_keeps_exact_executable_source(source, expected):
    from lib.project_mod.command_analysis import command_segment_discardable_redirect_split
    assert command_segment_discardable_redirect_split(source) == expected


@pytest.mark.parametrize('source', [
    '', 'ls -d .', 'printf x >result.txt', 'printf >/dev/null x',
    "sh <<'EOF'\nprintf x\nEOF", 'printf x 2>&file',
])
def test_file_writes_interior_redirects_and_heredocs_are_not_discarded(source):
    from lib.project_mod.command_analysis import command_segment_discardable_redirect_split
    assert command_segment_discardable_redirect_split(source) is None

