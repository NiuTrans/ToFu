#!/usr/bin/env python3
"""test_embeddings.py — Verify all embedding models work correctly.

Tests:
  1. Single-text embedding per model (dimension check, non-zero)
  2. Batch embedding (multiple texts, correct count & order)
  3. Cosine similarity (self-sim ≈ 1, dissimilar < 1)
  4. Semantic search (ranking correctness)
  5. Edge cases (empty input, single char)

Usage:
    python debug/test_embeddings.py
"""

import os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.embeddings import (
    embed_text, embed_texts, cosine_similarity, semantic_search,
    AVAILABLE_EMBEDDING_MODELS, DEFAULT_MODEL,
)

PASS = 0
FAIL = 0

def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name}  — {detail}')


def test_single_embed(model, expected_dim):
    """Test embedding a single text returns the right dimension and non-zero vector."""
    print(f'\n--- Single embed: {model} (expected dim={expected_dim}) ---')
    t0 = time.time()
    vec = embed_text('Hello, world!', model=model, timeout=30)
    elapsed = (time.time() - t0) * 1000

    check(f'returns list', isinstance(vec, list), f'got {type(vec).__name__}')
    check(f'dim={expected_dim}', len(vec) == expected_dim, f'got dim={len(vec)}')
    check(f'non-zero vector', any(v != 0.0 for v in vec), 'all zeros — API may have failed')
    check(f'all floats', all(isinstance(v, (int, float)) for v in vec[:10]), 'non-numeric values found')
    print(f'  ⏱  {elapsed:.0f}ms  vec[:5]={[round(v, 6) for v in vec[:5]]}')
    return vec


def test_batch_embed(model, expected_dim):
    """Test batch embedding returns correct count and ordering."""
    print(f'\n--- Batch embed: {model} ---')
    texts = [
        'The quick brown fox jumps over the lazy dog.',
        'Machine learning is transforming the world.',
        'Python is a popular programming language.',
        'The weather is sunny and warm today.',
    ]
    t0 = time.time()
    vecs = embed_texts(texts, model=model, timeout=30)
    elapsed = (time.time() - t0) * 1000

    check(f'returns {len(texts)} vectors', len(vecs) == len(texts), f'got {len(vecs)}')
    for i, v in enumerate(vecs):
        check(f'  vec[{i}] dim={expected_dim}', len(v) == expected_dim, f'got dim={len(v)}')
        check(f'  vec[{i}] non-zero', any(x != 0.0 for x in v), 'all zeros')

    # Different texts should produce different embeddings
    if len(vecs) >= 2 and any(x != 0.0 for x in vecs[0]) and any(x != 0.0 for x in vecs[1]):
        sim_01 = cosine_similarity(vecs[0], vecs[1])
        check(f'different texts ≠ identical (sim={sim_01:.4f})', sim_01 < 0.9999,
              'two different texts produced identical vectors')
    print(f'  ⏱  {elapsed:.0f}ms for {len(texts)} texts')
    return vecs


def test_cosine_similarity():
    """Test cosine similarity function."""
    print(f'\n--- Cosine similarity (math only) ---')
    # Self-similarity
    v1 = [1.0, 0.0, 0.0]
    check('self-sim = 1.0', abs(cosine_similarity(v1, v1) - 1.0) < 1e-9)

    # Orthogonal
    v2 = [0.0, 1.0, 0.0]
    check('orthogonal = 0.0', abs(cosine_similarity(v1, v2)) < 1e-9)

    # Opposite
    v3 = [-1.0, 0.0, 0.0]
    check('opposite = -1.0', abs(cosine_similarity(v1, v3) - (-1.0)) < 1e-9)

    # Zero vector
    v0 = [0.0, 0.0, 0.0]
    check('zero vector → 0.0', cosine_similarity(v1, v0) == 0.0)


def test_semantic_similarity(model):
    """Test that semantically similar texts have higher cosine similarity."""
    print(f'\n--- Semantic similarity: {model} ---')
    texts = [
        'I love programming in Python.',           # 0
        'Python is my favorite coding language.',   # 1  (similar to 0)
        'The cat sat on the mat.',                  # 2  (unrelated)
    ]
    vecs = embed_texts(texts, model=model, timeout=30)

    if all(any(x != 0.0 for x in v) for v in vecs):
        sim_01 = cosine_similarity(vecs[0], vecs[1])
        sim_02 = cosine_similarity(vecs[0], vecs[2])
        sim_12 = cosine_similarity(vecs[1], vecs[2])
        print(f'  sim(Python↔Python) = {sim_01:.4f}')
        print(f'  sim(Python↔cat)    = {sim_02:.4f}')
        print(f'  sim(Python2↔cat)   = {sim_12:.4f}')
        check(f'similar > dissimilar ({sim_01:.4f} > {sim_02:.4f})',
              sim_01 > sim_02, 'semantic similarity ranking incorrect')
        check(f'similar > dissimilar ({sim_01:.4f} > {sim_12:.4f})',
              sim_01 > sim_12, 'semantic similarity ranking incorrect')
    else:
        check('non-zero embeddings returned', False, 'got zero vectors')


def test_semantic_search_fn(model):
    """Test the semantic_search convenience function."""
    print(f'\n--- Semantic search: {model} ---')
    docs = [
        'How to train a neural network with PyTorch.',
        'Best Italian pasta recipes for beginners.',
        'Introduction to deep learning and backpropagation.',
        'Top 10 travel destinations in Europe.',
        'Understanding gradient descent optimization.',
    ]
    query = 'machine learning tutorial'
    t0 = time.time()
    results = semantic_search(query, docs, top_k=3, model=model, threshold=0.0)
    elapsed = (time.time() - t0) * 1000

    check(f'returns list', isinstance(results, list))
    check(f'returns ≤3 results', len(results) <= 3, f'got {len(results)}')
    if results:
        check(f'results have score', 'score' in results[0])
        check(f'results have index', 'index' in results[0])
        check(f'results sorted desc', all(results[i]['score'] >= results[i+1]['score']
              for i in range(len(results)-1)), 'not sorted')

        # Top result should be ML-related (index 0, 2, or 4)
        ml_indices = {0, 2, 4}
        top_idx = results[0]['index']
        check(f'top result is ML-related (idx={top_idx})', top_idx in ml_indices,
              f'expected one of {ml_indices}, got {top_idx}')

        print(f'  Results:')
        for r in results:
            print(f'    #{r["index"]} score={r["score"]:.4f} "{r["text"][:60]}"')
    print(f'  ⏱  {elapsed:.0f}ms')


def test_edge_cases(model):
    """Test edge cases."""
    print(f'\n--- Edge cases: {model} ---')

    # Empty list
    result = embed_texts([], model=model)
    check('empty list → empty list', result == [], f'got {result}')

    # Single character
    vec = embed_text('a', model=model, timeout=30)
    expected_dim = AVAILABLE_EMBEDDING_MODELS[model]['dim']
    check(f'single char → dim={expected_dim}', len(vec) == expected_dim, f'got dim={len(vec)}')

    # Long text
    long_text = 'word ' * 500
    vec = embed_text(long_text, model=model, timeout=30)
    check(f'long text (2500 chars) → dim={expected_dim}', len(vec) == expected_dim, f'got dim={len(vec)}')
    check(f'long text non-zero', any(v != 0.0 for v in vec), 'all zeros')


def main():
    global PASS, FAIL

    print('=' * 70)
    print(f'  Embedding Models Test Suite')
    print(f'  Models: {", ".join(AVAILABLE_EMBEDDING_MODELS.keys())}')
    print(f'  Default: {DEFAULT_MODEL}')
    print('=' * 70)

    # 0. Cosine similarity (pure math, no API)
    test_cosine_similarity()

    # 1-5. Per-model tests
    for model, info in AVAILABLE_EMBEDDING_MODELS.items():
        print(f'\n{"="*70}')
        print(f'  MODEL: {model}  (dim={info["dim"]}, status={info["status"]})')
        print(f'{"="*70}')

        expected_dim = info['dim']

        vec = test_single_embed(model, expected_dim)
        if vec and any(v != 0.0 for v in vec):
            # Only continue if the model is actually working
            test_batch_embed(model, expected_dim)
            test_semantic_similarity(model)
            test_edge_cases(model)
        else:
            print(f'  ⚠️  Skipping further tests — model may be down')

    # 6. Semantic search with default model
    print(f'\n{"="*70}')
    print(f'  SEMANTIC SEARCH (using default: {DEFAULT_MODEL})')
    print(f'{"="*70}')
    test_semantic_search_fn(DEFAULT_MODEL)

    # Summary
    total = PASS + FAIL
    print(f'\n{"="*70}')
    print(f'  RESULTS: {PASS}/{total} passed, {FAIL} failed')
    if FAIL == 0:
        print(f'  🎉 All tests passed!')
    else:
        print(f'  ⚠️  {FAIL} test(s) failed — check output above')
    print(f'{"="*70}')

    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
