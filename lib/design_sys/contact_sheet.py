"""Small labelled contact sheets for whole-deck / whole-film review."""

from __future__ import annotations

import os

__all__ = ['build_contact_sheet']


def build_contact_sheet(image_paths: list[str], out_path: str, *,
                        columns: int = 4, tile_width: int = 320,
                        label_prefix: str = 'Page') -> str:
    """Combine images in source order; missing paths are skipped."""
    from PIL import Image, ImageDraw

    items = []
    for number, path in enumerate(image_paths, 1):
        if not os.path.isfile(path):
            continue
        with Image.open(path) as src:
            image = src.convert('RGB')
        ratio = tile_width / max(1, image.width)
        items.append((number, image.resize(
            (tile_width, max(1, round(image.height * ratio))),
            Image.Resampling.LANCZOS)))
    if not items:
        raise ValueError('contact sheet has no readable images')
    columns = max(1, min(int(columns), len(items)))
    label_h, gutter = 34, 12
    tile_h = max(image.height for _, image in items)
    rows = (len(items) + columns - 1) // columns
    sheet = Image.new(
        'RGB',
        (columns * tile_width + (columns - 1) * gutter,
         rows * (tile_h + label_h) + (rows - 1) * gutter),
        '#181818')
    draw = ImageDraw.Draw(sheet)
    for slot, (number, image) in enumerate(items):
        row, col = divmod(slot, columns)
        x = col * (tile_width + gutter)
        y = row * (tile_h + label_h + gutter)
        draw.text((x + 8, y + 10), f'{label_prefix} {number}', fill='white')
        sheet.paste(image, (x, y + label_h))
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    sheet.save(out_path, format='PNG', optimize=True)
    return out_path
