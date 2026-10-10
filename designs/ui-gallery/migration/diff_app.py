#!/usr/bin/env python3
"""Pixel-diff two capture_app.py output directories.

A value-identical token sweep must leave every pixel pair equal, allowing
only for sub-pixel text rasterization noise: we count a pixel as changed
when any channel differs by more than a small tolerance, and report the
changed-pixel ratio per surface.

Usage: python3 designs/ui-gallery/migration/diff_app.py <before-dir> <after-dir>
Exit 1 when any surface exceeds the noise threshold.
"""

import sys
from pathlib import Path

from PIL import Image, ImageChops

TOLERANCE = 8          # per-channel delta above this counts as changed
NOISE_THRESHOLD = 0.001  # 0.1% of pixels may differ (font raster noise)


def changed_ratio(before: Path, after: Path) -> float:
    a = Image.open(before).convert("RGB")
    b = Image.open(after).convert("RGB")
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b)
    histogram = diff.histogram()
    # 256 bins per channel, laid out as [R][G][B]
    changed = 0
    for channel in range(3):
        bins = histogram[channel * 256:(channel + 1) * 256]
        changed += sum(count for value, count in enumerate(bins) if value > TOLERANCE)
    pixels = a.size[0] * a.size[1]
    # a pixel differing in 3 channels counts thrice — divide to normalize
    return changed / 3 / pixels


def main() -> None:
    before_dir, after_dir = Path(sys.argv[1]), Path(sys.argv[2])
    failures = []
    for before in sorted(before_dir.glob("*.png")):
        after = after_dir / before.name
        if not after.exists():
            print(f"[MISSING] {before.name}")
            failures.append(before.name)
            continue
        ratio = changed_ratio(before, after)
        ok = ratio <= NOISE_THRESHOLD
        print(f"[{'PASS' if ok else 'FAIL'}] {before.name}: {ratio * 100:.4f}% pixels changed")
        if not ok:
            failures.append(before.name)
    if failures:
        raise SystemExit(f"{len(failures)} surface(s) exceed noise threshold")
    print("All surfaces pixel-stable.")


if __name__ == "__main__":
    main()
