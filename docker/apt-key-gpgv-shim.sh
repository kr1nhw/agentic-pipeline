#!/bin/sh
# Build-time shim that keeps apt's signature verification real without gnupg.
#
# WHY THIS EXISTS
# ---------------
# Debian bookworm's apt delegates InRelease verification to the deprecated
# `apt-key`, which dearmors the forced keyring with `gpg` before running `gpgv`.
# The `python:3.12-slim-bookworm` base ships `gpgv` (apt depends on it) but NOT
# `gpg`. apt-key therefore verifies against an empty keyring, and every
# repository fails with NO_PUBKEY naming keys that are demonstrably present:
#
#   $ gpgv --keyring /usr/share/keyrings/debian-archive-keyring.gpg InRelease
#   gpgv: Good signature from "Debian Archive Automatic Signing Key (12/bookworm)"
#
# This shim performs exactly the verification apt-key would have performed —
# gpgv, against the same keyring — skipping the dearmor step that needs gnupg.
# It is wired in with `-o Dir::Bin::apt-key=…` for the bootstrap install only;
# gnupg itself is installed as part of that transaction, after which apt's real
# apt-key works and this file is removed from the image.
#
# Do not ship this in a runtime image, and do not use it to accept an unsigned
# repository: it verifies, it does not bypass.
set -eu

keyring=/etc/apt/trusted.gpg
while [ "$#" -gt 0 ]; do
    case "$1" in
        --keyring)
            keyring="$2"
            shift 2
            ;;
        --quiet | --readonly)
            shift
            ;;
        verify)
            shift
            exec gpgv --keyring "$keyring" "$@"
            ;;
        *)
            shift
            ;;
    esac
done

echo "apt-key-gpgv-shim: unsupported invocation: $*" >&2
exit 2
