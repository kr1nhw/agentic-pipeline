#!/usr/bin/env bash
# Build docpipe-agent on the remote build host and tag it locally there.
#
# Why the remote: the run target is linux/amd64 with 8.5 TB of scratch, the bases are
# already cached there, and Docker Hub is unreachable while public.ecr.aws is not.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$repo_root/.remote.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$repo_root/.remote.env"
  set +a
fi

remote_root="${DOCPIPE_REMOTE_ROOT:-/data/mm-lbfs}"
image="${DOCPIPE_IMAGE:-docpipe-agent}"
tag="${DOCPIPE_IMAGE_TAG:-0.1.0}"
version="$(python3 -c "import re,sys;print(re.search(r'^version = \"([^\"]+)\"', open('$repo_root/pyproject.toml',encoding='utf8').read(), re.M).group(1))")"
context="$remote_root/build/docpipe"
tar="$remote_root/build/docpipe-context.tar.gz"
build_log="$remote_root/logs/build-$(date +%Y%m%dT%H%M%S).log"

no_cache=""
extra_args=()
for arg in "$@"; do
  case "$arg" in
    --no-cache) no_cache="--no-cache" ;;
    *) extra_args+=("$arg") ;;
  esac
done

echo "==> generating requirements.txt from pyproject.toml"
# pyproject.toml stays the single source of truth; the Dockerfile consumes this
# generated file so dependency installation is its own cacheable layer and no
# heredoc is needed (this build host has no BuildKit).
"$repo_root/tools/export_requirements.py" "$repo_root" > "$repo_root/requirements.txt"
wc -l < "$repo_root/requirements.txt" | tr -d ' ' | xargs -I{} echo "    {} requirement line(s)"

echo "==> packaging build context"
# tar on the fly keeps unreadable/ignored paths (.remote.env, dataset, out/) out of the
# image without relying on a second tool being installed.
tar -czf - -C "$repo_root" \
  --exclude='.git' --exclude='.remote.env' --exclude='.env' \
  --exclude='dataset' --exclude='out' --exclude='work' --exclude='cache' \
  --exclude='dist' --exclude='build' --exclude='docs' --exclude='tests' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.DS_Store' \
  Dockerfile .dockerignore pyproject.toml README.md requirements.txt docpipe presets skills tools docker \
  | "$repo_root/tools/rssh.sh" "mkdir -p '$context' && tar -xzf - -C '$context'"

echo "==> building $image:$tag on the remote"
"$repo_root/tools/rssh.sh" "cd '$context' && docker build $no_cache \
    --build-arg DOCPIPE_VERSION='$version' \
    ${extra_args[*]:-} \
    -t '$image:$tag' -t '$image:latest' . 2>&1 | tee '$build_log'; exit \${PIPESTATUS[0]}"

echo "==> verifying image"
"$repo_root/tools/rssh.sh" "docker run --rm '$image:$tag' selftest"
"$repo_root/tools/rssh.sh" "docker images '$image' --format '{{.Repository}}:{{.Tag}} {{.Size}}'"
echo "==> build log: $build_log"
