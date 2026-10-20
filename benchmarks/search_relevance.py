#!/usr/bin/env python3
"""Offline search ranking/excerpt measurement over labeled synthetic evidence.

Run each package revision in a fresh process using --package-root. Labels and
expected facts are authored independently of production tokenization/scoring.
Reports source/dataset digests, raw timings, rank quality and delivered facts.
This component benchmark does not measure live recall or agent task success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path


def _row(name, text, *, title="Reference", snippet=""):
    return {"url": f"https://{name}.example/article", "title": title,
            "snippet": snippet, "full_content": text}


def cases():
    """Small adversarial fixtures, not a random sample of web search demand."""
    noise = "Introduction, account navigation, subscription and unrelated background.\n\n"
    result = []

    def add(name, query, rows, grades, facts, top_k=3, budget=600):
        result.append({"id": name, "query": query, "rows": rows, "grades": grades,
                       "facts": facts, "top_k": top_k, "budget_chars": budget})

    for position in (0, 400, 1_000):
        answer = "Quasar latency is 17 milliseconds with caching enabled."
        add(f"body-position-{position}", "quasar latency caching", [
            _row("partial", "Latency is the time needed to complete an operation."),
            _row("answer", noise * position + answer + "\n\n" + noise * 30),
            _row("noise", "Vegetables grow in well watered gardens."),
            _row("catalog", "Index and navigation."),
        ], {"https://answer.example/article": 3, "https://partial.example/article": 1},
            [answer])

    languages = [
        ("russian", "кэширование задержка", "Кэширование уменьшает задержка до 17 мс."),
        ("arabic", "التخزين المؤقت", "يقلل التخزين المؤقت زمن الاستجابة."),
        ("french", "mémoire fidélité", "La mémoire conserve la fidélité des observations."),
        ("chinese", "缓存延迟", "缓存延迟为17毫秒。"),
    ]
    for language, query, answer in languages:
        for position in (0, 400):
            add(f"{language}-{position}", query, [
                _row("noise", "Navigation and unrelated material."),
                _row("answer", noise * position + answer),
                _row("other", "The weather is pleasant."),
            ], {"https://answer.example/article": 3}, [answer], top_k=1)

    add("constraint-bait", "quasar site:example.org filetype:html -obsolete", [
        _row("noise", "example org obsolete html source site", title="example org"),
        _row("answer", "Quasar uses a bounded cache."),
        _row("other", "Gardening"),
    ], {"https://answer.example/article": 3}, ["Quasar uses a bounded cache."], top_k=1)
    add("navigation-bait", "quasar latency caching", [
        _row("noise", "Unrelated prose.\n--- Page Links ---\n"
             + "quasar latency caching\n" * 100),
        _row("answer", "Quasar latency is 17 milliseconds with caching enabled."),
        _row("other", "Gardening"),
    ], {"https://answer.example/article": 3},
        ["Quasar latency is 17 milliseconds with caching enabled."], top_k=1)
    add("short-candidate-pool", "quasar latency", [
        _row("noise", "Gardening and cooking."),
        _row("answer", "Quasar latency is 17 milliseconds."),
    ], {"https://answer.example/article": 3}, ["Quasar latency is 17 milliseconds."],
        top_k=5)

    repeated = "Quasar latency is 17 milliseconds. " + "Background material. " * 8
    for budget in (160, 600, 1_500):
        add(f"complementary-facts-{budget}", "quasar latency throughput", [
            _row("answer", "\n\n".join([repeated] * 20
                 + ["Throughput reaches 900 requests per second."])),
            _row("noise", "Gardening"),
        ], {"https://answer.example/article": 3},
            ["Quasar latency is 17 milliseconds.",
             "Throughput reaches 900 requests per second."], top_k=1, budget=budget)

    add("quoted-literal", '"site:example.org"', [
        _row("noise", "Other operators."),
        _row("answer", "The literal site:example.org scopes the query."),
        _row("other", "An empty result is inconclusive."),
    ], {"https://answer.example/article": 3},
        ["The literal site:example.org scopes the query."], top_k=1)
    add("version-identifier", "nova v3 cache", [
        _row("old", "Nova v2 cache is obsolete."),
        _row("answer", "Nova v3 cache has a new protocol."),
        _row("other", "Product catalog"),
    ], {"https://answer.example/article": 3, "https://old.example/article": 1},
        ["Nova v3 cache has a new protocol."], top_k=1)
    # Symbol-bearing subjects must survive the shared matching representation.
    # Labels describe these synthetic documents, independently of tokenizer code.
    for name, query, fact, decoy in (
        ('cpp', 'C++ memory model', 'C++ memory model reference.', 'C# memory model reference.'),
        ('csharp', 'C# memory model', 'C# memory model reference.', 'C++ memory model reference.'),
        ('linear', 'O(N)', 'Atlas lookup takes O (N) time.', 'Atlas lookup takes O(1) time.'),
        ('constant', 'O(1)', 'Atlas lookup takes O(1) time.', 'Atlas version 1 reference.'),
        ('nlogn', 'O(n log n)', 'Atlas sort takes O(n log n) time.', 'Atlas log n reference.'),
        ('cpp-version', 'C++20', 'The C++20 revision includes this example.',
         'The C++17 revision includes this example.'),
    ):
        add('technical-' + name, query, [
            _row('decoy', decoy),
            _row('answer', noise * 400 + fact + '\n\n' + noise * 30),
        ], {'https://answer.example/article': 3}, [fact], top_k=1, budget=240)
    return result


def metrics(urls, grades, excerpts, facts, top_k):
    """Independent graded ranking metrics plus literal delivered-fact recall."""
    values = [grades.get(url, 0) for url in urls[:top_k]]
    relevant = [i + 1 for i, grade in enumerate(values) if grade >= 2]

    def dcg(scores):
        return sum((2 ** score - 1) / math.log2(i + 2) for i, score in enumerate(scores))

    ideal = dcg(sorted(grades.values(), reverse=True)[:top_k])
    return {
        "hit_at_1": float(bool(values and values[0] >= 2)),
        "reciprocal_rank": 1 / relevant[0] if relevant else 0.0,
        "ndcg": dcg(values) / ideal if ideal else 0.0,
        "fact_recall": sum(any(fact in text for text in excerpts) for fact in facts) / len(facts),
    }


def measure(package_root: Path, repeats: int):
    package_root = package_root.resolve(strict=True)
    sys.path.insert(0, str(package_root))
    import tofu_search
    from tofu_search.passages import select_relevant_passages
    from tofu_search.search.rerank import rerank_by_bm25

    if Path(tofu_search.__file__).resolve().parent != package_root / "tofu_search":
        raise RuntimeError("Imported package does not match the requested source")
    logging.disable(logging.CRITICAL)
    dataset = cases()
    canonical = json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()
    owners = [
        "passages.py", "search/rerank.py", "search/authority.py",
        "search/prefetch_gate.py", "lexical.py",
    ]
    sources = {}
    for owner in owners:
        path = package_root / "tofu_search" / owner
        sources[owner] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    rows = []
    for case in dataset:
        clocks = []
        for _ in range(repeats):
            before = json.dumps(case["rows"], sort_keys=True)
            start = time.perf_counter()
            ranked = rerank_by_bm25(case["query"], case["rows"], case["top_k"])
            excerpts = [select_relevant_passages(
                item.get("full_content") or item.get("snippet", ""), case["query"],
                case["budget_chars"]) for item in ranked]
            clocks.append((time.perf_counter() - start) * 1_000)
            if any(len(text) > case["budget_chars"] for text in excerpts):
                raise AssertionError("Character budget exceeded")
            if json.dumps(case["rows"], sort_keys=True) != before:
                raise AssertionError("Search sources were mutated")
        urls = [item["url"] for item in ranked]
        rows.append({
            "id": case["id"], **metrics(urls, case["grades"], excerpts, case["facts"], case["top_k"]),
            "returned_urls": urls, "excerpt_chars": sum(map(len, excerpts)),
            "latency_ms": clocks,
        })
    scores = {key: statistics.mean(row[key] for row in rows)
              for key in ("hit_at_1", "reciprocal_rank", "ndcg", "fact_recall")}
    return {
        "scope": "synthetic ranking and excerpt regression; no live retrieval or agent claim",
        "pid": os.getpid(), "python": sys.version, "package_root": str(package_root),
        "source_sha256": sources, "dataset_sha256": hashlib.sha256(canonical).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_count": len(rows), "repeats": repeats, "metrics": scores, "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = measure(args.package_root, args.repeats)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("scope", "case_count", "metrics")}))


if __name__ == "__main__":
    main()
