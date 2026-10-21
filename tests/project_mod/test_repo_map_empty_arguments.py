"""`repo_map` has no required filters; the schema must say so.

Live repro (conv mvhq694eghjd9e, task 470883e7, round 10): deepseek-v4.1-flash
called ``repomap_placeholder_none`` with ``{}`` — it wanted ``repo_map`` with no
filters. An absent ``required`` key means the same thing to a JSON-Schema
validator, but it reads as "unstated" to the model, which then improvised the
omission into the tool name. Declaring the empty set plus the empty-argument
form gives "no filters" a shape the model can copy instead of invent.
"""
import pytest

from lib.tools.project import PROJECT_TOOL_REPO_MAP

pytestmark = pytest.mark.unit


def _parameters():
    return PROJECT_TOOL_REPO_MAP['function']['parameters']


def test_every_filter_is_optional_so_the_empty_call_stays_valid():
    assert _parameters()['required'] == []
    assert set(_parameters()['properties']) == {'path', 'focus'}


def test_description_names_the_empty_argument_form():
    description = PROJECT_TOOL_REPO_MAP['function']['description']
    assert 'optional' in description
    assert '`{}`' in description
