#!/usr/bin/env bash
# docpipe-agent entrypoint.
#
#   docker run … docpipe-agent docpipe run --in /in --out /out     # batch
#   docker run -it … docpipe-agent dsh                             # the agent
#   docker run … docpipe-agent selftest                            # image check
#   docker run … docpipe-agent shell                               # debug shell
#   docker run … docpipe-agent python -c 'import docpipe'          # arbitrary exec
set -euo pipefail

warn() { printf 'docpipe: %s\n' "$*" >&2; }

# Mounted volumes are often owned by another uid; say so plainly instead of
# failing later with a confusing EACCES from deep inside a writer.
#
# The probe WRITES rather than asking `test -w`. On this host's overlayfs,
# `access(2)` reports a directory in a lower layer as unwritable while writing to
# it succeeds, so a `test -w` check warned on every container start about
# directories that worked perfectly. A writability question is answered by
# writing.
check_writable() {
    local dir="$1"
    local probe="$dir/.docpipe-write-probe.$$"
    if [ ! -d "$dir" ]; then
        warn "WARNING: $dir does not exist; mount it or create it."
        return 1
    fi
    if ! (umask 077 && : > "$probe") 2>/dev/null; then
        warn "WARNING: $dir is not writable by uid $(id -u)."
        warn "         Re-run with --user \$(id -u):\$(id -g) or chown the mounted directory."
        return 1
    fi
    rm -f "$probe"
    return 0
}

for dir in "${DOCPIPE_WORK:-/work}" "${DOCPIPE_OUT:-/out}"; do
    check_writable "$dir" || true
done

if [ -n "${DOCPIPE_OFFLINE:-}" ]; then
    warn "DOCPIPE_OFFLINE is set: no model calls will be made this run."
fi

cmd="${1:-}"
case "$cmd" in
    docpipe)
        shift
        exec docpipe "$@"
        ;;
    dsh)
        shift
        exec dsh "$@"
        ;;
    selftest)
        exec docpipe selftest
        ;;
    shell | bash | sh)
        shift
        exec /bin/bash "$@"
        ;;
    "")
        exec docpipe --help
        ;;
esac

# Anything that resolves to a real binary runs as-is; otherwise treat the
# arguments as a docpipe subcommand so short invocations work.
if command -v "$cmd" >/dev/null 2>&1; then
    exec "$@"
fi
exec docpipe "$@"
