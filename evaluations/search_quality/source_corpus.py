"""Pinned external reference URLs for retrieval diagnostics, never QA grading.

Read only local Google SimpleQA Verified CSV bytes; no download or model calls.
The official benchmark measures unaided knowledge. This adapter discards its
answers and uses only questions, topic labels and recorded source URLs.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import io
from pathlib import Path
import re

from .contracts import url_key

DATASET = 'google/simpleqa-verified'
REVISION = 'ed392714d14fa5225b07c2b456453983bc75f690'
CSV_SHA256 = 'fb990cca0fae8515ae2f51b794d5fa791d095c1f919ecdbe22d0c1642e9ab966'
DATASET_ROWS = 1000
DATASET_BYTES = 4 * 1024 * 1024
FIELDS = ('original_index', 'problem', 'answer', 'topic', 'answer_type',
          'multi_step', 'requires_reasoning', 'urls')
TOPICS = frozenset({'Politics', 'Art', 'TV shows', 'History', 'Music', 'Other',
                    'Geography', 'Sports', 'Science and technology', 'Video games'})


def read_bytes(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(DATASET_BYTES + 1)
    if len(raw) > DATASET_BYTES:
        raise ValueError('Reference corpus exceeds byte budget')
    return raw


def parse_rows(raw):
    """Validate every row; preserve URL punctuation rather than guessing repairs."""
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8'), newline=''))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError('Unexpected reference corpus columns')
    rows, seen = [], set()
    for row in reader:
        if set(row) != set(FIELDS) or not all(isinstance(v, str) for v in row.values()):
            raise ValueError('Malformed reference corpus row')
        identifier = row['original_index']
        if not re.fullmatch(r'0|[1-9][0-9]*', identifier) or identifier in seen:
            raise ValueError('Invalid or duplicate reference row identity')
        seen.add(identifier)
        question = row['problem']
        if not question.strip() or len(question) > 500 or row['topic'] not in TOPICS:
            raise ValueError('Invalid question or topic')
        flags = {}
        for name in ('multi_step', 'requires_reasoning'):
            value = row[name].casefold()
            if value not in {'true', 'false'}:
                raise ValueError('Invalid reference annotation flag')
            flags[name] = value == 'true'
        urls = [url.strip() for url in re.split(r',\s*(?=https?://)', row['urls'], flags=re.I)]
        if not 2 <= len(urls) <= 16 or any(
            len(url) > 4000 or not url_key(url) or any(c.isspace() for c in url) for url in urls
        ):
            raise ValueError('Invalid recorded reference URLs')
        rows.append({'id': f'simpleqa-verified-{identifier}', 'topic': row['topic'],
                     'query': question, 'reference_urls': urls,
                     'answer_type': row['answer_type'], **flags})
    return rows


def load_corpus(path):
    raw = read_bytes(path)
    if hashlib.sha256(raw).hexdigest() != CSV_SHA256:
        raise ValueError('Reference corpus checksum does not match the pinned release')
    rows = parse_rows(raw)
    if len(rows) != DATASET_ROWS or {r['topic'] for r in rows} != TOPICS:
        raise ValueError('Reference corpus population does not match the pinned release')
    return rows


def select_cases(rows, per_topic=2, seed=20261021):
    """Topic-balanced hash sampling, independent of row order and search outcomes."""
    if not 1 <= per_topic <= 5:
        raise ValueError('per_topic must be 1..5')
    buckets = defaultdict(list)
    for row in rows:
        buckets[row['topic']].append(row)
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate sampling identity')
    key = lambda row: (hashlib.sha256(f'{seed}:{row["id"]}'.encode()).hexdigest(), row['id'])
    selected = []
    for topic in sorted(buckets):
        if len(buckets[topic]) < per_topic:
            raise ValueError('A topic has fewer rows than the declared sample')
        selected.extend(sorted(buckets[topic], key=key)[:per_topic])
    return sorted(selected, key=key)


def dataset_identity():
    return {'name': DATASET, 'revision': REVISION, 'csv_sha256': CSV_SHA256,
            'population_rows': DATASET_ROWS, 'license': 'MIT',
            'source': f'https://huggingface.co/datasets/{DATASET}/tree/{REVISION}',
            'purpose': 'Recorded-source retrieval coverage; not the official SimpleQA score',
            'answers_used': False}
