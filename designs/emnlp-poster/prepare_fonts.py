#!/usr/bin/env python3
"""Make licensed static poster fonts with distinct PostScript identities.

Only used when refreshing bundled assets; build_poster.py needs no fontTools.
Input is the existing repository font source. No fonts are downloaded.
"""

import argparse
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent/"assets")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    names = {400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold"}
    for family, filename, weights in [
        ("Inter", "Inter-latin.woff2", [400, 600, 700]),
        ("Newsreader", "Newsreader-latin.woff2", [400, 500]),
    ]:
        for weight in weights:
            variable = TTFont(args.font_source/filename)
            axes = {a.axisTag: weight if a.axisTag == "wght" else
                    48 if a.axisTag == "opsz" else a.defaultValue
                    for a in variable["fvar"].axes}
            font = instantiateVariableFont(variable, axes, inplace=False)
            font.flavor = None
            # Instancing otherwise leaves all weights with one PostScript name.
            # ReportLab then aliases them to the first registered dynamic face.
            family_name = f"Tofu Poster {family}"
            style = names[weight]
            ps_name = f"TofuPoster{family}-{style}"
            values = {1: family_name, 2: style, 3: ps_name,
                      4: f"{family_name} {style}", 6: ps_name,
                      16: family_name, 17: style}
            for name_id, value in values.items():
                for platform_id, encoding_id, language_id in [(3, 1, 0x409), (1, 0, 0)]:
                    font["name"].setName(value, name_id, platform_id, encoding_id, language_id)
            font.save(args.output/f"{family}-{weight}.ttf")
            print(ps_name)


if __name__ == "__main__":
    main()
