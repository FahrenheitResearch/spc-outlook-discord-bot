#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 ./spc_outlook_bot.py "$@"
