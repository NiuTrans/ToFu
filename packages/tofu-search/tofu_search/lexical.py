"""Shared lexical evidence for ranking, fetch admission and page excerpts.

Pure Unicode tokenization and query-subject parsing; no search, provider or host
imports. This is lexical matching, not semantic retrieval or factual validation.
"""

import re
import unicodedata

_CJK = r'\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af'
_CJK_RUN = re.compile(f'[{_CJK}]+')
_WORD = re.compile(f'[{_CJK}]+|[^\\W_{_CJK}]+', re.UNICODE)
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'where', 'which', 'who', 'why', 'new', 'get', 'set',
    'one', 'two', 'any', 'www', 'http', 'https', 'com', 'org', 'html',
})
_QUERY_PART = re.compile(r'(?:[+-]?[A-Za-z]+:|[+-])?"[^"]*"|\S+')
_STRUCTURED_OPERATOR = re.compile(
    r'^[+-]?(?:site|filetype|ext|inurl|intitle|intext|allinurl|allintitle|'
    r'allintext|before|after|daterange|related|cache|source|language|lang):', re.I)


def tokenize(text: str) -> list[str]:
    """Unicode words and contiguous CJK bigrams, with stable normalization.

    CJK runs never join across punctuation, whitespace or another script.
    Retain digits (including single-digit versions), accents and non-Latin
    scripts. Normalize the matching representation only, never source excerpts.
    """
    tokens = []
    for match in _WORD.finditer(unicodedata.normalize('NFKC', text or '').casefold()):
        word = match.group()
        if _CJK_RUN.fullmatch(word):
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) <= 2:
                tokens.extend(word)
        elif word not in _STOP_WORDS and (len(word) > 1 or word.isdigit()):
            tokens.append(word)
    return tokens


def query_subject(query: str) -> str:
    """Remove retrieval constraints, preserving quoted subject literals.

    Operator operands and negative terms cannot prove positive relevance.
    Unknown colon words remain ordinary subjects.
    """
    subjects = []
    for match in _QUERY_PART.finditer(query or ''):
        part = match.group()
        if part.startswith('"'):
            subjects.append(part[1:-1])
        elif (part.strip('()') not in {'OR', 'AND', 'NOT'}
              and not _STRUCTURED_OPERATOR.match(part.lstrip('('))
              and not part.lstrip('(').startswith('-')):
            subjects.append(part)
    return ' '.join(subjects)
