"""Universal evaluation entry point; execution stays in explicit runner adapters.

`python -m evaluations catalog` inspects pinned suites without Harbor installed.
`python -m evaluations harbor ...` uses the existing audited Harbor lifecycle.
No dynamic imports from untrusted manifests, provider calls, or parallel runner.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m evaluations")
    parser.add_argument("adapter", choices=("catalog", "harbor"))
    # Only parse the adapter here; --help after it belongs to that adapter.
    args = parser.parse_args(values[:1])
    remainder = values[1:]
    if args.adapter == "catalog":
        if remainder:
            parser.error("catalog takes no arguments")
        from evaluations.catalog import BENCHMARKS
        from rootless_vm.harness_profiles import harness_profiles
        print(json.dumps({
            "schema_version": 1,
            "benchmarks": [definition.to_dict() for definition in BENCHMARKS.values()],
            "execution_adapters": ["harbor"],
            "harnesses": [profile.to_dict() for profile in harness_profiles()],
        }, indent=2))
        return 0
    from evaluations.swebench.cli import main as harbor_main
    return harbor_main(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
