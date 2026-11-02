#!/bin/sh
# Usage: sh supercop/make-tarball.sh COMMIT
# Pins crypto_hash/blake3/rust_servil to this fork's COMMIT, which must be on
# GitHub (cargo fetches it from there), locks its dependencies, and writes
# supercop/blake3-servil-<commit>.tar.gz in place of the last tarball.
set -e
cd "$(dirname "$0")"
rev=$(git rev-parse "$1")
d=crypto_hash/blake3/rust_servil
sed -i "s/rev = \"[0-9a-f]*\"/rev = \"$rev\"/" $d/Cargo.toml
rm -f $d/Cargo.lock
(cd $d && CARGO_TARGET_DIR=/tmp/make-tarball cargo generate-lockfile)
grep -q "rev=$rev" $d/Cargo.lock
tarball=blake3-servil-$(echo $rev | cut -c1-7).tar.gz
rm -f blake3-servil-*.tar.gz
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01 00:00Z' -czf "$tarball" $d
echo "$tarball:"; tar -tzf "$tarball"
