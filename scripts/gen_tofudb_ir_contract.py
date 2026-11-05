#!/usr/bin/env python3
"""Generate storage contract outputs, or check them without writing.

Entry: main / --check. tofudb_contract_codegen owns input capture, validation
and projection. Publication starts only after every output has been built.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tofudb_contract_codegen.generation import OUTPUT_PATHS, build_outputs  # noqa: E402
from scripts.tofudb_contract_codegen.inputs import ContractInputs  # noqa: E402

SOURCE = ROOT / 'contracts/tofudb_ir_v1.json'
SEARCH_SOURCE = ROOT / 'contracts/conversation_search_v2.json'
OUTPUT = ROOT / OUTPUT_PATHS['ir']
MIGRATION_OUTPUT = ROOT / OUTPUT_PATHS['migration']
SEARCH_OUTPUT = ROOT / OUTPUT_PATHS['search']


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    outputs = build_outputs(ContractInputs(ROOT, SOURCE, SEARCH_SOURCE))
    stale = False
    for relative, rendered in outputs.items():
        path = ROOT / relative
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != rendered:
                print(f'{relative} is stale; run python3 scripts/gen_tofudb_ir_contract.py')
                stale = True
        else:
            path.write_text(rendered, encoding='utf-8')
    return int(stale)


if __name__ == '__main__':
    raise SystemExit(main())
