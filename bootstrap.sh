#!/usr/bin/env bash
# Create the virtualenv and install icloud-photo-sync (editable) + dev deps.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
echo "Using $($PYTHON --version) at $(command -v "$PYTHON")"

if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/python -m pip install -e ".[dev]"

cat <<'EOF'

Done.

  source .venv/bin/activate
  icloud-photo-sync login          # one-time: sign in (handles 2FA)
  cd /path/to/your/photo/folder    # photos download into ./YYYY/MM here
  icloud-photo-sync sync           # start; Ctrl-C to stop; re-run to resume

EOF
