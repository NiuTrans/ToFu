"""Technical punctuation must survive ranking, passage and subject matching."""

from copy import deepcopy

import pytest

from tofu_search.lexical import matching_text
from tofu_search.passages import select_relevant_passages
from tofu_search.search.prefetch_gate import query_terms
from tofu_search.search.quality import classify_relevance
from tofu_search.search.rerank import rerank_by_bm25

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('query', 'other'), [
    ('C++ atomics', 'C# atomics'),
    ('C# atomics', 'C++ atomics'),
    ('O(N)', 'O(1)'),
    ('O(1)', 'Version 1'),
    ('O(n log n)', 'log n'),
    ('C++20 atomics', 'C++17 atomics'),
])
def test_technical_subject_beats_candidate_with_same_ordinary_words(query, other):
    rows = [
        {'url': 'https://example.test/wrong', 'title': other, 'snippet': f'Details of {other}.'},
        {'url': 'https://example.test/right', 'title': query, 'snippet': f'Details of {query}.'},
    ]
    original = deepcopy(rows)
    assert rerank_by_bm25(query, rows, 2)[0] is rows[1]
    assert rows == original


@pytest.mark.parametrize(('query', 'fact'), [
    ('C++', 'C++ supports this operation through its standard library.'),
    ('C#', 'C# supports this operation through its standard library.'),
    ('O(N)', 'Lookup takes O (N) time and retains all values.'),
    ('O(1)', 'Lookup takes O(1) time and retains all values.'),
    ('O(n log n)', 'The sorting phase takes O(n log n) time.'),
])
def test_short_technical_query_reaches_late_evidence_without_rewriting_source(query, fact):
    body = ('Introduction, navigation and unrelated context.\n\n' * 30
            + fact + '\n\n' + 'General closing remarks.\n\n' * 20)
    excerpt = select_relevant_passages(body, query, 180)
    assert fact in excerpt
    assert len(excerpt) <= 180


def test_quoted_language_symbols_are_distinct_subjects():
    query = '"C++" atomics'
    wrong = classify_relevance({'title': 'C# atomics', 'snippet': 'C# atomic operations.'}, query)
    right = classify_relevance({'title': 'C++ atomics', 'snippet': 'C++ atomic operations.'}, query)
    assert wrong['status'] != 'lexical_match'
    assert right['status'] == 'lexical_match'


def test_location_operators_and_exclusions_do_not_introduce_technical_subjects():
    assert query_terms('"C++" site:csharp.example -C#') == query_terms('C++')
    assert query_terms('C++')


def test_technical_features_preserve_ordinary_identifier_words_and_versions():
    assert query_terms('counter') <= query_terms('counter++')
    assert query_terms('20') <= query_terms('C++20')
    assert query_terms('C++') <= query_terms('C++20')


def test_subject_matching_keeps_negations_and_distinct_notation_names():
    assert matching_text('not supported') != matching_text('supported')
    assert matching_text('O(n log n)') != matching_text('O(nlogn)')
    assert matching_text('O ( N )') == matching_text('O(N)')
    assert matching_text('O（N）') == matching_text('O(N)')


def test_literal_match_does_not_confuse_spaced_names_with_one_variable():
    result = classify_relevance({'title': 'Cost is O(nlogn).'}, '"O(n log n)"')
    assert result['status'] != 'lexical_match'


def test_malformed_notation_keeps_following_identifier_evidence():
    assert query_terms('quasar') <= query_terms('O(' + 'counter ' * 200 + 'quasar')
    assert matching_text('guide#section') == matching_text('guide section')
