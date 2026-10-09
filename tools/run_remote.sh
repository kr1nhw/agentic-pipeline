#!/usr/bin/env bash
# Run the docpipe image on the remote host.
#
#   tools/run_remote.sh run --in /in --out /out --offline
#   tools/run_remote.sh --it dsh            # the agent, interactively
#
# Inputs and outputs are bind mounts under $DOCPIPE_REMOTE_ROOT, so a run never
# copies a corpus into a container and never writes outside the remote workspace.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$repo_root/.remote.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$repo_root/.remote.env"
  set +a
fi

remote_root="${DOCPIPE_REMOTE_ROOT:-/data/mm-lbfs}"
image="${DOCPIPE_IMAGE:-docpipe-agent}:${DOCPIPE_IMAGE_TAG:-0.1.0}"
data_dir="${DOCPIPE_REMOTE_DATA:-$remote_root/dataset}"
out_dir="${DOCPIPE_REMOTE_OUT:-$remote_root/out}"
run_name="${DOCPIPE_RUN_NAME:-run-$(date +%Y%m%dT%H%M%S)}"

container_args=()
interactive=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --it) interactive="-it"; shift ;;
    --name) run_name="$2"; shift 2 ;;
    *) break ;;
  esac
done

# The host's docker is 18.09 and the node is shared with k3s and Neon smoke VMs,
# so runs are explicitly capped and never left running after a failure.
#
# `seccomp=unconfined` is required for the AGENT to boot, and only for the agent.
# Docker 18.09's default seccomp profile blocks `statx`, which glibc's async
# `realpath` uses, so `fs.promises.realpath("<profile>/")` returns EPERM and every
# Cordis profile aborts before any preset mounts. The extraction runtime needs no
# syscalls outside the default profile, so this flag is harmless there and
# essential for `dsh`.
mem_limit="${DOCPIPE_MEM_LIMIT:-8g}"
cpus="${DOCPIPE_CPUS:-12}"

# The image ships an unprivileged `docpipe` user (uid 1000) and was built to run as
# it, so the run exercises that path instead of quietly running as root. The bind
# mounts must belong to it: inputs only need to be readable, outputs writable.
ssh_cmd="mkdir -p '$out_dir/$run_name' '$remote_root/work' && \
chown 1000:1000 '$out_dir/$run_name' '$remote_root/work' && \
docker run --rm $interactive \
  --name docpipe-$run_name \
  --memory $mem_limit --cpus $cpus \
  --security-opt seccomp=unconfined \
  --user 1000:1000 \
  -v '$data_dir':/in:ro \
  -v '$out_dir/$run_name':/out \
  -v '$remote_root/work':/work \
  -e DEEPSEEK_API_KEY \
  -e DOCPIPE_OUT=/out \
  -e DOCPIPE_WORK=/work \
  -e DOCPIPE_MAX_RSS_MB=\${DOCPIPE_MAX_RSS_MB:-6144} \
  '$image' $*"

echo "==> image     $image"
echo "==> input     $data_dir (read-only)"
echo "==> output    $out_dir/$run_name"
echo "==> limits    memory $mem_limit, cpus $cpus"

# DEEPSEEK_API_KEY is forwarded from this machine's environment when present, so
# the key is never written to a file, an image layer, or the remote filesystem.
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" "$repo_root/tools/rssh.sh" "$ssh_cmd"
else
  echo "    (DEEPSEEK_API_KEY is not set locally: the run will be deterministic-only)"
  "$repo_root/tools/rssh.sh" "$ssh_cmd"
fi
