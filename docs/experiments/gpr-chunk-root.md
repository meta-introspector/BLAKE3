# The GPR single-chunk kernel: kept for study

Sam Shackleton's single-chunk root kernel keeps BLAKE3's state in
general-purpose registers instead of SIMD lanes (BLAKE3 #150). John Servil
proved it as assembled. This release ships the SIMD single-chunk kernel
alone (Zooko, October 24, 2026). This page keeps what we learned, so the
kernel can come back when the evidence changes.

## Where it is

- **The kernel:** tag `archive/gpr-chunk-root-87d837a` on
  devonjonte/BLAKE3, Sam's review head. One generator,
  `tools/gen_x86_gpr_chunk_root.py`, emits
  `c/blake3_gpr_chunk_root_x86-64_unix.S`: 920 instructions, 2996 bytes,
  keeping callee-saved registers in XMM8-13. Build with `BLAKE3_DIAGNOSTIC_GPR` set to try it.
- **The proof:** tag `archive/gpr-proof-ce874a5` (`tools/verify`).
- **The evidence:** devonjonte/bench-hashes, branch `data`:
  `2026-10-24/gpr-i7` (Intel) and the links in #150 (the EPYC).

## What it does

Change in time against the SIMD kernel (negative is faster):

| | EPYC 9255 (Zen 5) | i7-12700K P cores (Golden Cove) |
|---|---|---|
| warm, 64 B to 1 KiB | -36% | +20% to +25% |
| after idling | -39% to -53% | +22% to +45% |
| after other work, 64 B / 256 B / 1 KiB | +50% / level / -24% | +10% / +7% / +10% |
| `hash()` after other work, 64 B / 256 B / 1 KiB | +78% / +28% / -9% | not measured apart |
| batches and streams | level (the wide kernels) | level |

Why, on the i7 (front-end counters): after other work, its extra 2 KB of
code costs what a 2 KB run of no-ops costs the SIMD kernel, the fetch.
Warm, size costs nothing; it executes 1.8 times the instructions, and
most of their micro-ops come from the legacy decoders, not the
decoded-uop cache. Zen 5's wider integer core runs it faster.

## Why this release does without it

- **The machines of 2027.** Most x86 machines in use will be Intel
  hybrids, whose P cores descend from Golden Cove, with many E cores, and
  servers rich in Zen 3 and Zen 4. The kernel loses on the Golden Cove we
  measured, for a reason (instruction count, decode) that should weigh on
  narrower integer cores too. It wins on Zen 5, a minority.
- **Who calls it.** The kernel serves one message of up to 1 KiB. The
  callers most sensitive to speed use batches and streams, which use the
  wide kernels either way. Its main callers are `hash()` on small
  messages between other work, the cold case, where it costs even on
  Zen 5.
- **The alternative was a CPU-family rule** (GPR on Zen 5 only), resting
  on one machine, added just before a release.

## To measure before bringing it back

- Lion Cove and Skymont (wider cores), the E cores (Gracemont), Zen 3,
  Zen 4 and Zen 6.
- On Zen 5, the size where it starts to win in every state (it does at
  1 KiB; 64 B after other work loses).
- A variant that stays in the decoded-uop cache on Intel and is smaller
  (the counters point at both).
