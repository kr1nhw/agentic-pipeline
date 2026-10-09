#!/usr/bin/env bash
# Copy the local dataset to the remote run host.
#
# Uses `tar | rssh 'tar -x'` rather than rsync: rssh already handles the password
# without a terminal, and piping through the same channel keeps one credential
# path instead of two. The dataset is small in bytes and large in value, so it is
# copied once and then mounted read-only into every run.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$repo_root/.remote.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$repo_root/.remote.env"
  set +a
fi

remote_root="${DOCPIPE_REMOTE_ROOT:-/data/mm-lbfs}"
src="${1:-$repo_root/dataset}"
dest="${2:-$remote_root/dataset}"

if [ ! -d "$src" ]; then
  echo "no such directory: $src" >&2
  exit 1
fi

echo "==> syncing $src -> $dest"
count=$(find "$src" -type f | wc -l | tr -d ' ')
bytes=$(du -sh "$src" | cut -f1)
echo "    $count file(s), $bytes"

"$repo_root/tools/rssh.sh" "mkdir -p '$dest'"
# macOS leaves `._name` AppleDouble sidecars next to every file; they are not
# documents and would be reported as unknown formats on every run.
COPYFILE_DISABLE=1 tar -cf - -C "$src" \
  --exclude='._*' --exclude='.DS_Store' --exclude='.Spotlight-V100' . \
  | "$repo_root/tools/rssh.sh" "tar -xf - -C '$dest'"

echo "==> remote listing"
"$repo_root/tools/rssh.sh" "ls -la '$dest' | head -25; echo; du -sh '$dest'"
