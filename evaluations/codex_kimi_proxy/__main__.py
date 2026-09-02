"""CLI for the isolated loopback benchmark proxy."""

from __future__ import annotations

import argparse
import os

from .server import ProxyConfig, serve


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--upstream-base-url",
        default=os.environ.get("KIMI_CHAT_BASE_URL", ""))
    parser.add_argument(
        "--metrics-jsonl", default=os.environ.get("TOFU_PROXY_METRICS", ""))
    parser.add_argument(
        "--trial-metrics-dir",
        default=os.environ.get("TOFU_PROXY_TRIAL_METRICS_DIR", ""),
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--require-trial-header",
        action="store_true",
        help="Reject Responses calls that lack the formal per-trial correlation token",
    )
    args = parser.parse_args()
    config = ProxyConfig(
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=os.environ.get("KIMI_API_KEY", ""),
        metrics_jsonl=args.metrics_jsonl,
        trial_metrics_dir=args.trial_metrics_dir,
        timeout_seconds=args.timeout_seconds,
        require_trial_header=args.require_trial_header,
    )
    serve(config, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
