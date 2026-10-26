#!/usr/bin/env bash
set -euo pipefail

# Ubuntu's system Python drives setup; no ambient conda/venv is used.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x /usr/bin/python3 ]]; then
    echo "Ubuntu 24.04 system Python is required (sudo apt-get install python3)." >&2
    exit 1
fi
exec /usr/bin/python3 -B "$script_dir/reproduce.py" "$@"
