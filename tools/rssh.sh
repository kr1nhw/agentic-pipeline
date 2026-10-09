#!/usr/bin/env bash
# Password-authenticated SSH wrapper for the docpipe build/run host.
#
# OpenSSH refuses to read a password from a pipe, so the password is handed to an
# askpass helper through the environment (never argv, never a file on disk) and
# SSH_ASKPASS_REQUIRE=force makes ssh consult it without a controlling terminal.
#
#   tools/rssh.sh 'docker images'
#   DOCPIPE_SSH_EXTRA_OPTS='-t' tools/rssh.sh 'bash -l'
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$repo_root/.remote.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$repo_root/.remote.env"
  set +a
fi

: "${DOCPIPE_SSH_HOST:?DOCPIPE_SSH_HOST is not set (see .remote.env.example)}"
: "${DOCPIPE_SSH_PASSWORD:?DOCPIPE_SSH_PASSWORD is not set}"
ssh_user="${DOCPIPE_SSH_USER:-root}"

askpass="$(mktemp "${TMPDIR:-/tmp}/docpipe-askpass.XXXXXX")"
chmod 700 "$askpass"
cat > "$askpass" <<'ASKPASS'
#!/bin/sh
printf '%s\n' "$DOCPIPE_SSH_PASSWORD"
ASKPASS
cleanup() { rm -f "$askpass"; }
trap cleanup EXIT INT TERM

export DOCPIPE_SSH_PASSWORD

# Connection multiplexing, and not only for speed.
#
# The host became fully unreachable mid-project after roughly 25
# password-authenticated connections in a short window — the classic signature of
# fail2ban-style throttling. One authenticated master connection, reused by every
# later command, removes the repeated authentication that triggers it. The socket
# lives in the session temp directory, so it is not left behind on the host.
control_dir="${TMPDIR:-/tmp}/docpipe-ssh-$(id -u)"
mkdir -p "$control_dir"
chmod 700 "$control_dir"

# OpenSSH consults the askpass helper only when stdin is not a terminal. A pipe is
# not a terminal, and `tar | rssh 'tar -xzf -'` depends on that stdin staying
# connected, so only detach when a real terminal is attached.
ssh_opts=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o "ConnectTimeout=${DOCPIPE_SSH_TIMEOUT:-15}"
  -o ServerAliveInterval=30
  -o NumberOfPasswordPrompts=1
  -o PreferredAuthentications=password
  -o PubkeyAuthentication=no
  -o LogLevel=ERROR
  -o ControlMaster=auto
  -o "ControlPath=$control_dir/%r@%h:%p"
  -o "ControlPersist=${DOCPIPE_SSH_PERSIST:-600}"
)
# shellcheck disable=SC2206
[ -n "${DOCPIPE_SSH_EXTRA_OPTS:-}" ] && ssh_opts+=(${DOCPIPE_SSH_EXTRA_OPTS})

export SSH_ASKPASS="$askpass" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-localhost:0}"

target="$ssh_user@$DOCPIPE_SSH_HOST"

run_ssh() {
  if [ -t 0 ]; then
    ssh "${ssh_opts[@]}" "$target" "$@" < /dev/null
  else
    ssh "${ssh_opts[@]}" "$target" "$@"
  fi
}

if run_ssh "$@"; then
  exit 0
else
  status=$?
fi

# A multiplexed master that died between commands presents as
# "Connection timed out during banner exchange": the client keeps trying to reuse
# a socket whose peer is gone. Drop the sockets and try once more on a fresh
# connection. A piped payload has already been consumed by the failed attempt, so
# its retry fails loudly at the far end rather than succeeding with half a stream.
rm -f "$control_dir"/* 2>/dev/null || true
echo "rssh: first attempt failed (exit $status); retrying on a fresh connection" >&2
if [ -t 0 ]; then
  ssh "${ssh_opts[@]}" -o ControlMaster=no "$target" "$@" < /dev/null
else
  ssh "${ssh_opts[@]}" -o ControlMaster=no "$target" "$@"
fi
