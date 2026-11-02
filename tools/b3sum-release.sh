#!/bin/sh
# b3sum's release binary for this machine, from a clean tree at a release
# tag: statically linked on Linux (the recommended build, b3sum/README.md),
# as the platform links elsewhere (macOS). Writes dist/b3sum-VERSION-TARGET
# and its SHA-256 and BLAKE3 digests beside it, for a GitHub release
# (CONTRIBUTING.md, "Releases").
#
#     sh tools/b3sum-release.sh
set -eu
cd "$(git rev-parse --show-toplevel)"
test -z "$(git status --porcelain --untracked-files=no)" || { echo "b3sum-release: the tree has changes; release from a clean tree" >&2; exit 1; }
version=$(git describe --tags --exact-match 2>/dev/null) || { echo "b3sum-release: HEAD is no release tag" >&2; exit 1; }
target=$(rustc -vV | sed -n 's/^host: //p')
case "$target" in
	*-linux-gnu) export RUSTFLAGS="-C target-feature=+crt-static" ;;
esac
cargo build --release --locked --manifest-path b3sum/Cargo.toml --target "$target" --target-dir target/b3sum-release
mkdir -p dist
out="dist/b3sum-$version-$target"
cp "target/b3sum-release/$target/release/b3sum" "$out"
# The binary checks itself against BLAKE3's published digest of nothing.
test "$(printf '' | "$out" --no-names)" = af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262
name=$(basename "$out")
(cd dist && { sha256sum "$name" 2>/dev/null || shasum -a 256 "$name"; } > "$name.sha256" && "./$name" "$name" > "$name.b3")
file "$out"
echo "b3sum-release: $out, with .sha256 and .b3 beside it"
