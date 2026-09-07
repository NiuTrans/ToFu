"""Regression: ``_ContentWithDisplayResults`` must survive copy / deepcopy /
pickle reconstruction.

Root cause (conv mrova3t92jffm7 — "both-failed" turn crash)
-----------------------------------------------------------
``_ContentWithDisplayResults`` is a ``str`` subclass that ``web_search`` /
``fetch_url`` streaming pre-exec returns and stores in the task's tool-result
cache AS-IS (it *is* a ``str``, so ``_prepare_cache_value`` never stringifies
it). It then rides inside a tool message's ``content`` all the way to
``lib/llm_dispatch/api.py``, where the per-slot body adaptation does::

    body['messages'] = copy.deepcopy(body['messages'])

For a ``str`` subclass, copy/deepcopy/pickle reconstruct the instance via
``cls.__new__(cls, <str value>)`` — a SINGLE positional arg
(``str.__getnewargs__`` returns a 1-tuple). If ``display_results`` were a
REQUIRED positional arg, that reconstruction raised::

    _ContentWithDisplayResults.__new__() missing 1 required positional
    argument: 'display_results'

killing the whole turn (and, because both models in the fallback chain are
Claude and hit the same deepcopy, surfacing as the misleading
``both-failed (opus-4.8→opus-4.7)`` / "check your API keys" error).

The fix makes ``display_results`` optional. deepcopy/pickle restore the
instance ``__dict__`` AFTER ``__new__``, so the real metadata is preserved
regardless of the default.
"""
from __future__ import annotations

import copy
import os
import pickle
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestContentWithDisplayResultsCopyable:

    def _make(self):
        from lib.tasks_pkg.streaming_tool_executor import _ContentWithDisplayResults
        c = _ContentWithDisplayResults('FORMATTED-LLM-TEXT', [{'url': 'https://a.com'}])
        c.search_diag = {'reason': 'ok'}
        c.engine_breakdown = {'bing': ['https://a.com']}
        c.vertical = {'batch': [{'query': 'q'}]}
        return c

    def test_deepcopy_preserves_value_and_metadata(self):
        c = self._make()
        d = copy.deepcopy(c)
        assert str(d) == 'FORMATTED-LLM-TEXT'
        assert d.display_results == [{'url': 'https://a.com'}]
        assert d.search_diag == {'reason': 'ok'}
        assert d.engine_breakdown == {'bing': ['https://a.com']}
        assert d.vertical == {'batch': [{'query': 'q'}]}

    def test_shallow_copy_survives(self):
        c = self._make()
        s = copy.copy(c)
        assert str(s) == 'FORMATTED-LLM-TEXT'
        assert s.display_results == [{'url': 'https://a.com'}]

    def test_pickle_roundtrip_survives(self):
        c = self._make()
        p = pickle.loads(pickle.dumps(c))
        assert str(p) == 'FORMATTED-LLM-TEXT'
        assert p.display_results == [{'url': 'https://a.com'}]

    def test_deepcopy_inside_message_list(self):
        """The exact shape that crashed: the subclass nested inside a
        ``messages`` list content, deep-copied as in lib/llm_dispatch/api.py."""
        c = self._make()
        messages = [
            {'role': 'user', 'content': 'hi'},
            {'role': 'tool', 'tool_call_id': 't1', 'content': c},
        ]
        cloned = copy.deepcopy(messages)   # must NOT raise
        assert cloned[1]['content'] == 'FORMATTED-LLM-TEXT'
        assert cloned[1]['content'].display_results == [{'url': 'https://a.com'}]

    def test_neuter_default_still_defaults_list(self):
        """Constructing WITHOUT display_results (the copy/pickle path) yields a
        usable empty list, not None — downstream does ``len(display_results)``."""
        from lib.tasks_pkg.streaming_tool_executor import _ContentWithDisplayResults
        c = _ContentWithDisplayResults('x')
        assert c.display_results == []
        assert len(c.display_results) == 0
