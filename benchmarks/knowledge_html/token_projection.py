"""Bounded per-call repeated-token work experiment; no production mutation.

Entry: optimized_tokens, IndexedHtmlCorpus. Regex/term limits remain owned by
knowledge.store. The immutable reference uses the current fast HTML parser in
both arms, so upload measurements isolate search-token work.
"""
import ast as ast
import hashlib as hashlib
from pathlib import Path as Path
import subprocess as subprocess

from benchmarks.knowledge_html.durable import DurableHtmlCorpus as DurableHtmlCorpus, semantic_projection as semantic_projection
from lib.knowledge import store as store

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_COMMIT = 'f2ea9f0ea'


def load_reference_tokens():
    source = subprocess.check_output(['/usr/bin/git', 'show',
                                     f'{REFERENCE_COMMIT}:lib/knowledge/store.py'], cwd=ROOT)
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'search_tokens')
    namespace = dict(vars(store))
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<immutable-search-tokens>', 'exec'), namespace)
    return namespace['search_tokens'], hashlib.sha256(source).hexdigest()


def optimized_tokens(text: str, *, cap: int = 256) -> list[str]:
    tokens = []
    seen = set()
    processed_words = set()
    processed_runs = set()

    def add(value):
        value = value.strip().lower()
        if (value and len(value) <= store._MAX_TERM_CHARS
                and value not in seen and len(tokens) < cap):
            seen.add(value)
            tokens.append(value)

    for word in store._WORD_RE.findall(text or ''):
        if len(word) <= store._MAX_TERM_CHARS:
            normalized = word.lower()
            if normalized in processed_words:
                continue
            if len(processed_words) < cap:
                processed_words.add(normalized)
        add(word)
        if len(tokens) >= cap:
            return tokens
        for part in store.re.split(r'[_.+/#-]+', word):
            if len(part) >= 2:
                add(part)
                if len(tokens) >= cap:
                    return tokens
    for run in store._CJK_RE.findall(text or ''):
        if len(run) <= store._MAX_TERM_CHARS:
            if run in processed_runs:
                continue
            if len(processed_runs) < cap:
                processed_runs.add(run)
        if len(run) == 1:
            add(run)
            if len(tokens) >= cap:
                return tokens
            continue
        for width in (2, 3):
            if len(run) >= width:
                for index in range(len(run) - width + 1):
                    add(run[index:index + width])
                    if len(tokens) >= cap:
                        return tokens
    return tokens


class IndexedHtmlCorpus(DurableHtmlCorpus):
    def __init__(self, root, engine, *, variant):
        if variant not in {'reference', 'candidate'}:
            raise ValueError('unknown token variant')
        super().__init__(root, engine, variant='candidate')
        try:
            reference, self.reference_source_sha256 = load_reference_tokens()
            self.add_document.__globals__['search_tokens'] = (
                reference if variant == 'reference' else optimized_tokens)
        except BaseException:
            self.close()
            raise
