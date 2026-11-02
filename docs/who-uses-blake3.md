# Who uses BLAKE3

BLAKE3 is load-bearing in software most people run every day. Each entry below names what the
project does with BLAKE3 and links the source or document that shows it. Every link was read on
October 25, 2026.

## Systems infrastructure

### Compilers, linkers and build systems

- **LLVM: lld, Clang, Windows debug info.** lld computes every ELF
  build-id it is asked for as `md5`, `sha1`, or plain `--build-id` with
  BLAKE3: the flag names survive only as lengths, 16 and 20 bytes, so
  a `--build-id=sha1` from lld contains no SHA-1 ("in practice people
  use 'md5' and 'sha1' just for different lengths. Implement them with
  the more efficient BLAKE3":
  [`lld/ELF/Writer.cpp`](https://github.com/llvm/llvm-project/blob/main/lld/ELF/Writer.cpp);
  `--build-id` alone means `sha1`:
  [`lld/ELF/Options.td`](https://github.com/llvm/llvm-project/blob/main/lld/ELF/Options.td)).
  Which means the build-id in nearly every Android, ChromeOS, Fuchsia
  and Chrome binary is a BLAKE3 hash: Android's build passes
  `--build-id=md5` to lld
  ([`soong/cc/config/global.go`](https://android.googlesource.com/platform/build/soong/+/refs/heads/main/cc/config/global.go)),
  Chrome's official builds pass `--build-id=sha1`
  ([`build/config/compiler/BUILD.gn`](https://chromium.googlesource.com/chromium/src/+/main/build/config/compiler/BUILD.gn)),
  Clang's Fuchsia driver always passes `--build-id`
  ([`clang/lib/Driver/ToolChains/Fuchsia.cpp`](https://github.com/llvm/llvm-project/blob/main/clang/lib/Driver/ToolChains/Fuchsia.cpp)),
  and ChromeOS builds its Clang with `ENABLE_LINKER_BUILD_ID=ON` and lld
  as the default linker
  ([`sys-devel/llvm/llvm-9999.ebuild`](https://chromium.googlesource.com/chromiumos/overlays/chromiumos-overlay/+/main/sys-devel/llvm/)).
  Chromium's crash reporting reads them back
  ([`base/debug/elf_reader.cc`](https://github.com/chromium/chromium/blob/main/base/debug/elf_reader.cc)).
  Elsewhere in LLVM: the global type hashes of Windows PDB debug info
  (`/DEBUG:GHASH`) are truncated BLAKE3
  ([`TypeHashing.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/DebugInfo/CodeView/TypeHashing.cpp),
  [`lld/COFF/DebugTypes.cpp`](https://github.com/llvm/llvm-project/blob/main/lld/COFF/DebugTypes.cpp)),
  Clang's module dependency scanner hashes build contexts with it
  ([`ModuleDepCollector.cpp`](https://github.com/llvm/llvm-project/blob/main/clang/lib/DependencyScanning/ModuleDepCollector.cpp)),
  and LLVM's content-addressable store is built on it
  ([`llvm/lib/CAS`](https://github.com/llvm/llvm-project/tree/main/llvm/lib/CAS)).
- **Swift.** The compiler's index records and constant uniquing hash
  with BLAKE3
  ([`lib/Index/IndexRecord.cpp`](https://github.com/swiftlang/swift/blob/main/lib/Index/IndexRecord.cpp),
  [`lib/IRGen/GenConstant.cpp`](https://github.com/swiftlang/swift/blob/main/lib/IRGen/GenConstant.cpp)).
- **rustc and Cargo.** Opt-in: `-Z src-hash-algorithm=blake3` hashes
  source files for debug info
  ([`rustc_span/src/lib.rs`](https://github.com/rust-lang/rust/blob/master/compiler/rustc_span/src/lib.rs)),
  and Cargo's `-Zchecksum-freshness` decides rebuilds by BLAKE3 checksums
  ([`Cargo.toml`](https://github.com/rust-lang/cargo/blob/master/Cargo.toml)).
- **Meta: Sapling and Buck2.** Sapling and Mononoke, Meta's source
  control, address blobs and their content store by BLAKE3
  ([`eden/scm/lib/types/src/blake3.rs`](https://github.com/facebook/sapling/blob/main/eden/scm/lib/types/src/blake3.rs),
  [`mononoke_types/src/hash.rs`](https://github.com/facebook/sapling/blob/main/eden/mononoke/mononoke_types/src/hash.rs));
  Buck2's action digests
  ([`cas_digest.rs`](https://github.com/facebook/buck2/blob/main/app/buck2_common/src/cas_digest.rs)).
- **Bazel and the Remote Execution API.** `DigestFunction.BLAKE3` in the
  protocol
  ([`remote_execution.proto`](https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto))
  and Bazel's implementation
  ([`Blake3HashFunction.java`](https://github.com/bazelbuild/bazel/blob/master/src/main/java/com/google/devtools/build/lib/vfs/bazel/Blake3HashFunction.java)).
### Graphics

- **Mesa.** The shader disk cache is keyed by BLAKE3
  ([`src/util/disk_cache.c`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/main/src/util/disk_cache.c),
  [`src/util/blake3`](https://gitlab.freedesktop.org/mesa/mesa/-/tree/main/src/util/blake3)).
  Which would mean nearly every Linux GPU driver benefits from this:
  Intel, AMD, and the open Nvidia and Arm drivers, on desktops,
  Chromebooks, Android devices and the Steam Deck.
- **Unreal Engine.** `FIoHash`, the hash behind its content-addressed
  data (cooked content, the derived-data cache, Zen), is BLAKE3-160
  (`Engine/Source/Runtime/Core/Public/IO/IoHash.h`; the engine's
  repository needs an Epic account, and public mirrors show the file).

### Storage and file systems

- **OpenZFS.** `checksum=blake3` is an on-disk checksum since 2.2
  ([`zfsprops.7`](https://github.com/openzfs/zfs/blob/master/man/man7/zfsprops.7),
  [Checksums](https://openzfs.github.io/openzfs-docs/Basic%20Concepts/Checksums.html)).
- **Ceph.** The object gateway deduplicates by BLAKE3 fingerprints and
  offers it as a checksum type
  ([`rgw_dedup_store.h`](https://github.com/ceph/ceph/blob/main/src/rgw/driver/rados/rgw_dedup_store.h),
  [`rgw_cksum.h`](https://github.com/ceph/ceph/blob/main/src/rgw/rgw_cksum.h)).
- **Google TensorStore.** Its OCDBT storage format hashes with BLAKE3
  ([`storage_generation.cc`](https://github.com/google/tensorstore/blob/master/tensorstore/kvstore/ocdbt/non_distributed/storage_generation.cc)).
- **XET and Hugging Face.** XET is a content-addressable storage
  protocol specified in an IETF Internet-Draft, whose one defined suite,
  `XET-BLAKE3-GEARHASH-LZ4`, uses BLAKE3 for all of its hashing
  ([draft-denis-xet](https://datatracker.ietf.org/doc/draft-denis-xet/),
  [source](https://github.com/jedisct1/draft-denis-xet)). Hugging Face
  runs it as the storage behind the Hub and its Buckets, so every model
  and dataset there is chunked and addressed by BLAKE3
  ([xet-core](https://github.com/huggingface/xet-core/blob/main/Cargo.toml),
  [Xet on the Hub](https://huggingface.co/blog/xet-on-the-hub),
  [Buckets](https://x.com/ClementDelangue/status/2039695447506210905)).
- **Nix.** A store hash algorithm
  ([`hash.hh`](https://github.com/NixOS/nix/blob/master/src/libutil/include/nix/util/hash.hh)).
### Networks and ledgers

- **Solana.** The accounts hash, and a BLAKE3 syscall for on-chain
  programs
  ([`accounts-db/Cargo.toml`](https://github.com/anza-xyz/agave/blob/master/accounts-db/Cargo.toml),
  [`blake3-hasher`](https://github.com/anza-xyz/solana-sdk/tree/master/blake3-hasher)).
- **iroh.** Content addressing and verified streaming (Bao) on BLAKE3
  ([iroh-blobs](https://github.com/n0-computer/iroh-blobs/blob/main/Cargo.toml)).
- **Wasmer** keys its module cache with BLAKE3
  ([`lib/cache/Cargo.toml`](https://github.com/wasmerio/wasmer/blob/main/lib/cache/Cargo.toml)).

## Post-quantum zero-knowledge proofs

The post-quantum proof systems, which are built on hash functions, pick
BLAKE3 where a byte-oriented hash is needed (Merkle commitments, the
Fiat-Shamir transcript, hashing inside the proven program): Polygon's
**Plonky3** (a BLAKE3 AIR among its primitives:
[`Cargo.toml`](https://github.com/Plonky3/Plonky3/blob/main/Cargo.toml)),
Succinct's **SP1** zkVM
([`Cargo.toml`](https://github.com/succinctlabs/sp1/blob/dev/Cargo.toml)),
StarkWare's **stwo** prover
([`Cargo.toml`](https://github.com/starkware-libs/stwo/blob/dev/Cargo.toml)),
a16z's **Jolt** zkVM
([`Cargo.toml`](https://github.com/a16z/jolt/blob/main/Cargo.toml)),
the **WHIR** polynomial commitment scheme
([`Cargo.toml`](https://github.com/WizardOfMenlo/whir/blob/main/Cargo.toml)),
Polygon's **Miden VM**
([`Cargo.toml`](https://github.com/0xMiden/miden-vm/blob/main/Cargo.toml)),
Meta's **Winterfell** STARK prover
([`crypto/Cargo.toml`](https://github.com/facebook/winterfell/blob/main/crypto/Cargo.toml)),
**Aztec**'s Barretenberg
([`crypto/blake3s`](https://github.com/AztecProtocol/aztec-packages/tree/master/barretenberg/cpp/src/barretenberg/crypto/blake3s)),
**Binius64**, Irreducible's binary-field SNARK (BLAKE3 Merkle trees,
with AVX-512 and NEON prover kernels:
[`crates/hash/src/blake3.rs`](https://github.com/binius-zk/binius64/blob/main/crates/hash/src/blake3.rs),
[`crates/hash-prover/src/blake3`](https://github.com/binius-zk/binius64/tree/main/crates/hash-prover/src/blake3)),
and Succinct's **Flock**, the binary-field STARK built on Binius64
([`Cargo.toml`](https://github.com/succinctlabs/flock/blob/main/Cargo.toml)).
BLAKE3's security margin is part of the reason: no attack faster than
brute force reaches past 2 of its 7 rounds, where SHA-256 has attacks
on up to 39 of its 64 ([hashsma.sh](https://hashsma.sh/)).

The generation before chose BLAKE2: **Zcash**'s halo2, bellman, Sapling
and Orchard
([`librustzcash`](https://github.com/zcash/librustzcash/blob/main/Cargo.toml)),
**arkworks**
([`Cargo.toml`](https://github.com/arkworks-rs/algebra/blob/master/Cargo.toml)),
**Mina**'s kimchi
([`Cargo.toml`](https://github.com/o1-labs/proof-systems/blob/master/kimchi/Cargo.toml)),
zkSync's **Boojum**
([`Cargo.toml`](https://github.com/matter-labs/zksync-crypto/blob/main/crates/boojum/Cargo.toml)),
and **Filecoin**'s storage proofs
([`Cargo.toml`](https://github.com/filecoin-project/rust-fil-proofs/blob/master/Cargo.toml)).

## BLAKE2

BLAKE3's predecessor runs in places of the same rank. The **Linux
kernel**'s random number generator keeps its entropy pool in a BLAKE2s
state
([`drivers/char/random.c`](https://github.com/torvalds/linux/blob/master/drivers/char/random.c));
**WireGuard**'s handshake is `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`
([`noise.c`](https://github.com/torvalds/linux/blob/master/drivers/net/wireguard/noise.c));
**Jujutsu** hashes its content with BLAKE2b
([`Cargo.toml`](https://github.com/jj-vcs/jj/blob/main/Cargo.toml));
**Zcash** and **Argon2** are built on BLAKE2b.
