"""Read-only SSO provider diagnosis: python -m tofu_mtsso_auth [--probe]."""
import argparse
import json
from . import provider_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Explicitly probe the installed official SSO capability")
    args = parser.parse_args()
    result = provider_status(probe=args.probe)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
