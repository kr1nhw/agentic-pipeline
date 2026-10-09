#!/usr/bin/env bash
# Publish the image's skill catalogue to the harness skill root.
#
# Skills live once, at /opt/skills, and are symlinked into
# ${DSH_HOME}/skills — the "user-dsh" discovery root that the filesystem skill
# provider scans for every profile, not just the docpipe preset. Keeping one
# source of truth means the skills the agent reads and the skills we ship can
# never drift.
set -euo pipefail

src="${1:-/opt/skills}"
dst="${2:-${DSH_HOME:-/opt/dsh-home}/skills}"

mkdir -p "$dst"
linked=0
for skill_dir in "$src"/*/; do
    [ -f "${skill_dir}SKILL.md" ] || continue
    name="$(basename "$skill_dir")"
    ln -sfn "${skill_dir%/}" "$dst/$name"
    linked=$((linked + 1))
done

printf 'docpipe: linked %d skill(s) into %s\n' "$linked" "$dst"
if [ "$linked" -eq 0 ]; then
    printf 'docpipe: WARNING: no SKILL.md found under %s\n' "$src" >&2
fi
