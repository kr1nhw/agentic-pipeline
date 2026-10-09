#!/usr/bin/env bash
# Run a local Python file on the remote host.
#
# Quoting a Python script through `ssh 'python3 - <<PY ... PY'` breaks as soon as
# the script contains a nested quote, and the failure mode is a SyntaxError on
# the *remote* line number, which reads as if the script were wrong. Upload the
# file and run it by path instead.
#
#     tools/rpy.sh tools/probe_review.py [args]        # host python3 (stdlib only)
#     tools/rpy.sh --docker tools/probe_review.py      # inside the product image
#
# `--docker` is the mode for anything importing the pipeline's dependencies
# (pyarrow, pypdfium2, pdfplumber...). The host python3 has only the standard
# library, so a missing import there is an environment fact, not a code bug.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_ROOT="${DOCPIPE_REMOTE_ROOT:-/data/mm-lbfs}"
IMAGE="${DOCPIPE_IMAGE:-docpipe-agent:0.1.0}"

mode="host"
if [[ "${1:-}" == "--docker" ]]; then
  mode="docker"
  shift
fi

script="$1"
shift || true
[[ -f "$script" ]] || { echo "no such script: $script" >&2; exit 2; }

# Quote every argument: document names contain spaces, and an unquoted path is
# split by the remote shell into separate arguments, which surfaces as the script
# failing to parse its own second word ("invalid literal for int(): 'Optimization'").
args=""
for value in "$@"; do
  args="$args $(printf '%q' "$value")"
done

name="$(basename "$script")"
tar -czf - -C "$(dirname "$script")" "$name" | "$HERE/rssh.sh" \
  "mkdir -p $REMOTE_ROOT/tmp && tar -xzf - -C $REMOTE_ROOT/tmp && chmod 755 $REMOTE_ROOT/tmp && chmod 644 $REMOTE_ROOT/tmp/* && echo uploaded >&2"

if [[ "$mode" == "docker" ]]; then
  # The image's own entrypoint is for the agent runtime; run the script directly.
  # Mount the script at a path inside the container rather than reusing the host
  # path: the image runs as uid 1000 and the host tree is root-owned, so a host
  # path is readable by root and invisible to the container.
  # Run as root: these are read-only diagnostics over root-owned run output, and
  # the host tree above the mount points is not traversable by uid 1000.
  # Mount the working tree over the installed package, exactly as a run does.
  # Without this the probe imports the copy baked into the image, which lags the
  # checkout by however many commits were made since the last build — so a probe
  # can fail on a signature that exists, or pass on behaviour that is gone.
  # Ask the HOST whether the tree is there. Testing a remote path with a local
  # `[ -d ]` is always false, so the mount was silently dropped and every probe
  # ran the copy baked into the image.
  code_mount=""
  if "$HERE/rssh.sh" "test -d $REMOTE_ROOT/code/docpipe" >/dev/null 2>&1; then
    code_mount="-v $REMOTE_ROOT/code/docpipe:/opt/venv/lib/python3.12/site-packages/docpipe:ro"
  else
    echo "warning: no $REMOTE_ROOT/code/docpipe on the host; probing the image's baked copy" >&2
  fi
  "$HERE/rssh.sh" "docker run --rm --user 0 --entrypoint python3 \
    -v $REMOTE_ROOT/tmp/$name:/tmp/$name:ro \
    -v $REMOTE_ROOT:$REMOTE_ROOT:ro $code_mount -w $REMOTE_ROOT \
    $IMAGE /tmp/$name$args"
else
  "$HERE/rssh.sh" "cd $REMOTE_ROOT && python3 $REMOTE_ROOT/tmp/$name$args"
fi
