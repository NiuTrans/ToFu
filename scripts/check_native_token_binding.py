"""Certify an explicitly pinned local native token archive or library and joined mapping release."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import random
import tiktoken
from lib.agent_core.native_token_count import NativeTokenCounter, NativeTokenCountError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', type=Path)
    source.add_argument('--library', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--application-version')
    parser.add_argument('--binary-bytes', type=int, required=True)
    args = parser.parse_args()
    if args.archive is not None and not args.application_version:
        parser.error('--archive requires --application-version')
    rng = random.Random(77)
    parts = ['hello', '世界', '🙂', '\n', '<|endoftext|>', ' café', '\x00', 'a'*127]
    texts = [''] + [''.join(rng.choices(parts, k=rng.randrange(1, 30))) for _ in range(1000)]
    checks, observations = 0, []
    for iteration in range(3):
        counter = (NativeTokenCounter.from_library(library=args.library, sha256=args.sha256,
            binary_bytes=args.binary_bytes) if args.library is not None else
            NativeTokenCounter(archive=args.archive, sha256=args.sha256,
                application_version=args.application_version, binary_bytes=args.binary_bytes))
        inode = os.fstat(counter._image.fileno()).st_ino
        def mappings():
            return sum(int(line.split()[4]) == inode
                       for line in Path('/proc/self/maps').read_text().splitlines())
        with counter:
            for encoding in ['cl100k_base', 'o200k_base']:
                oracle = tiktoken.get_encoding(encoding)
                def run(batch):
                    expected = [len(oracle.encode(text, disallowed_special=())) for text in batch]
                    if counter.count_batch(batch, encoding=encoding) != expected:
                        raise RuntimeError('native token binding parity failed')
                    return len(batch)
                checks += run(texts)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    checks += sum(pool.map(run, [texts[i:i+100] for i in range(0, len(texts), 100)]))
            before = mappings()
        after = mappings()
        if before == 0 or after != 0 or not counter._image.closed:
            raise RuntimeError('native token binding mapping release failed')
        try:
            counter.count_batch([], encoding='cl100k_base')
        except NativeTokenCountError:
            pass
        else:
            raise RuntimeError('native token binding accepted a closed call')
        observations.append({'iteration': iteration, 'segments_before': before, 'segments_after': after})
    print(json.dumps({'format': 'tofu.native-token-binding-installed-check/v1',
        'image_source': 'installed_library' if args.library is not None else 'archive',
        'archive_sha256': args.sha256 if args.archive is not None else None,
        'token_library_sha256': args.sha256 if args.library is not None else None,
        'exact_comparisons': checks,
        'concurrent_callers': 4, 'observations': observations,
        'allocator_rss_return_proven': False, 'performance_admitted': False}))


if __name__ == '__main__':
    main()
