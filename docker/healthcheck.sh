#!/usr/bin/env bash
# Cheap in-image liveness check: the interpreter, the PDF backend and the
# scratch directory must all work. Deliberately does not call the network.
set -euo pipefail

python - <<'PY'
import sys
import pypdfium2
import docpipe
import docpipe.cli  # noqa: F401
if sys.version_info < (3, 12):
    raise SystemExit(f"python too old: {sys.version}")
PY

test -w "${DOCPIPE_WORK:-/work}"
