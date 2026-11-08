"""Annotate source lines with uniqueness and o200k token counts (task authoring aid).

Usage: python3 annotate.py <file> [start] [end]
Each line: <lineno> <U|D><count> <ntok> | <text>
U = stripped line unique in file, D<n> = appears n times. ntok = o200k tokens of stripped line.
"""
import sys

import tiktoken

ENC = tiktoken.get_encoding('o200k_base')


def main():
    path = sys.argv[1]
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 9
    text = open(path, encoding='utf-8').read()
    lines = text.split('\n')
    counts = {}
    for line in lines:
        counts[line.strip()] = counts.get(line.strip(), 0) + 1
    for no, line in enumerate(lines, 1):
        if no < start or no > end:
            continue
        s = line.strip()
        c = counts[s]
        tag = 'U ' if c == 1 else f'D{c}'
        print(f'{no:4d} {tag:<3} {len(ENC.encode(s)):3d} | {line}')


if __name__ == '__main__':
    main()
