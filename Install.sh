#!/usr/bin/env bash
# Compatibility entrypoint; install.sh owns all install policy and options.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"
