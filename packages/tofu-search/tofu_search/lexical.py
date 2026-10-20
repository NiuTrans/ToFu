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
# Bound failed lookahead per word. Longer expressions still retain word tokens;
# recognition must not repeatedly scan an entire malformed document suffix.
_COMPLEXITY_NOTATION_CHARS = 128


def _lexemes(text):
    """Unicode words plus bounded symbolic identifiers and complexity notation.

    These are lexical features, not a programming-language or math evaluator.
    Malformed or long expressions retain the ordinary word interpretation.
    """
    text = unicodedata.normalize('NFKC', text or '').casefold()
    if not any(char in text for char in ('+', '#', '(')):
        yield from _WORD.findall(text)
        return
    consumed = 0
    for match in _WORD.finditer(text):
        if match.start() < consumed:
            continue
        word, end = match.group(), match.end()
        if word in {'o', 'ω', 'θ'}:
            opening = end
            while opening < len(text) and text[opening] in ' \t':
                opening += 1
            if opening < len(text) and text[opening] == '(':
                depth, value = 0, False
                for index in range(opening, min(len(text), opening + _COMPLEXITY_NOTATION_CHARS)):
                    char = text[index]
                    if not (char.isalnum() or char in '()_+-*/^!., \t'):
                        break
                    value = value or char.isalnum()
                    depth += (char == '(') - (char == ')')
                    if depth == 0:
                        if value:
                            consumed = index + 1
                            yield text[match.start():consumed]
                        break
                if consumed > end:
                    continue
        suffix = '++' if text.startswith('++', end) else '#' if text.startswith('#', end) else ''
        following = text[end + len(suffix):end + len(suffix) + 1]
        if (suffix and following not in {'+', '#'}
                and not (suffix == '#' and (following.isalpha() or following == '_'))):
            consumed = end + len(suffix)
            yield word + suffix
        else:
            yield word


def _canonical_lexeme(lexeme):
    if '(' in lexeme:
        # Spaces around operators do not distinguish notation. Spaces between
        # names still do: n log n must not become the variable nlogn.
        return re.sub(r'\s*([()+*/^!,\-])\s*', r'\1', ' '.join(lexeme.split()))
    return lexeme


def matching_text(text):
    """Boundary-preserving subject matching, including stop words and symbols."""
    return ' '.join(_canonical_lexeme(unit) for unit in _lexemes(text))


def tokenize(text: str) -> list[str]:
    """Unicode words and contiguous CJK bigrams, with stable normalization.

    CJK runs never join across punctuation, whitespace or another script.
    Retain digits (including single-digit versions), accents and non-Latin
    scripts. Normalize the matching representation only, never source excerpts.
    """
    tokens = []
    for unit in _lexemes(text):
        technical = '(' in unit or unit.endswith(('++', '#'))
        if technical:
            tokens.append(_canonical_lexeme(unit))
        # Keep ordinary word evidence too (e.g. 'counter' in counter++).
        words = _WORD.findall(unit) if technical else (unit,)
        for word in words:
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
