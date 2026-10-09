#!/usr/bin/env bash
# Run the pipeline over each document in a REMOTE directory, one container per file.
#
# Why per file: a single run over a mixed directory reports one duration for the
# whole batch and no progress until it ends, so a document that stalls takes the
# rest of the corpus with it and leaves nothing to measure. One container per
# document yields a duration per document and confines a stall to the file that
# caused it.
#
#     tools/run_each.sh /data/mm-lbfs/corpus-new/pdf /data/mm-lbfs/out/each [timeout]
#
# The source directory lives on the run host. Listing it locally finds nothing and
# the script then exits successfully having done no work, which is exactly the
# silent no-op this comment exists to prevent.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:?usage: run_each.sh <remote-source-dir> <out-subdir> [timeout]}"
OUT="${2:?usage: run_each.sh <remote-source-dir> <out-subdir> [timeout]}"
LIMIT="${3:-900}"
IMAGE="${DOCPIPE_IMAGE:-docpipe-agent:0.1.0}"
MODE="${DOCPIPE_ARGS:---offline}"

files="$("$HERE/rssh.sh" "ls -1 $SRC" | tr -d '\r')"
if [ -z "$files" ]; then
  echo "no files in $SRC" >&2
  exit 2
fi

while IFS= read -r name <&3; do
  [ -n "$name" ] || continue
  slug="$(printf '%s' "$name" | tr -c 'a-zA-Z0-9._-' '-' | cut -c1-58)"
  printf '=== %s\n' "$name"
  "$HERE/rssh.sh" "
    rm -rf /tmp/one-in /data/mm-lbfs/out/$OUT/$slug
    mkdir -p /tmp/one-in /data/mm-lbfs/out/$OUT/$slug
    cp '$SRC/$name' /tmp/one-in/
    chmod -R 755 /tmp/one-in /data/mm-lbfs/out/$OUT/$slug
    chown -R 1000:1000 /tmp/one-in /data/mm-lbfs/out/$OUT/$slug
    start=\$(date +%s)
    timeout --signal=KILL $LIMIT docker run --rm --name one-run --no-healthcheck \
      --memory 8g --cpus 10 --security-opt seccomp=unconfined \
      -e PYTHONUNBUFFERED=1 \
      -v /data/mm-lbfs/code/docpipe:/opt/venv/lib/python3.12/site-packages/docpipe:ro \
      -v /tmp/one-in:/in:ro -v /data/mm-lbfs/out/$OUT/$slug:/out \
      $IMAGE docpipe run --in /in --out /out --work /work $MODE \
      > /data/mm-lbfs/logs/one-$slug.log 2>&1 < /dev/null
    code=\$?
    echo \"  wall=\$(( \$(date +%s) - start ))s exit=\$code\"
    grep -E 'document_done|document_start|documents  |units  |route |vision calls|tokens |gate failures|needs review' /data/mm-lbfs/logs/one-$slug.log | head -10
  "
done 3<<< "$files"
echo "=== all documents attempted"
