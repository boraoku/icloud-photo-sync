#!/usr/bin/env bash
# Create the virtualenv and install icloud-photo-sync (editable) + dev deps.
#
# Safe to run in any copy of this folder. A virtualenv is NOT relocatable — its
# activate script and pyvenv.cfg hardcode the absolute path it was created at —
# so if you copy the whole folder (with its .venv) to a new location, that .venv
# still points back at the original. This script detects such a stale/foreign
# .venv and rebuilds it for the current directory, so each copy runs independently.
set -euo pipefail

cd "$(dirname "$0")"
HERE="$PWD"

PYTHON="${PYTHON:-python3}"
echo "Using $($PYTHON --version) at $(command -v "$PYTHON")"

# A correctly-built venv has this directory's path baked into bin/activate. If it
# doesn't (because .venv was copied in from another location), rebuild it —
# otherwise `source .venv/bin/activate` would silently put the *other* copy's
# environment on PATH.
if [ -d .venv ] && ! grep -qF "$HERE/.venv" .venv/bin/activate 2>/dev/null; then
    echo "This .venv was created for a different path; rebuilding it for $HERE."
    rm -rf .venv
fi

if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/python -m pip install -e ".[dev]"

cat <<EOF

Done. This copy's environment lives at:
  $HERE/.venv

Activate THIS copy from THIS folder (each copy has its own .venv):

  source .venv/bin/activate
  icloud-photo-sync login          # one-time: sign in (handles 2FA)
  cd /path/to/your/photo/folder    # photos download into ./YYYY/MM here
  icloud-photo-sync sync           # start; Ctrl-C to stop; re-run to resume

EOF
