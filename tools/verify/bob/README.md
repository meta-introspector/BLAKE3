# Bob's check: prototype and design

Bob receives a tarball from us and distrusts its authors. He wants to
check that its assembly kernels implement C2SP's BLAKE3 specification,
on Arm as Arm's own instruction specification defines the machine.
His *reliance set* is everything whose mistakes or sabotage could lead
him to a wrong conclusion. The goal is to reduce that set: a prover may
be complicated or wrong when a small, sound checker validates its proofs.
(Zooko, October 22, 2026: the prover is free to be anything; what Bob must
trust is the verifier.)

## Current result and the v0.8 decision

This directory supplies an XML extractor, lexer, and ASL1 parser in Lean.
It is development groundwork. ASL execution, instruction correctness
lemmas, and a machine-code-to-compression theorem remain to be built.
The existing proofs still rely on Python's instruction models and Z3,
as [the verification README](../README.md) describes. Issue #130 remains
open; this prototype supplies no executable-verification command for v0.8.

The checks establish three different things:

- **Compiled differential tests:** all 5,448 instruction sections in Arm's
  2025-09 ASL1 release have the same serialized trees as ASLRef. Of 1,738
  shared sections, 1,737 match; both parsers reject the `ProcState`
  declaration, which has a semicolon inside its record fields. ASLRef's
  entire standard library also matches. Its CLI performs minimal inference
  even with `--no-type-check`; three shared sections print the matching
  tree and then fail inference on missing external identifiers. The report
  separates those from successful exits.
- **Kernel-checked literal trees:** for the execute sections of ADD
  (shifted register), EOR (shifted register), and EXTR, Lean checks that
  extracting, lexing, and parsing the complete XML file's literal bytes
  produces the proposed literal tree. The only reported axiom is
  `propext`. Changes to the tree's operator, the input's operator, and the
  selected section are each rejected. This checks the parser's computation;
  ASL's meaning and the correctness of the parser's definition remain
  separate obligations.
- **Small kernel-reduced tests:** `Dev/Tests.lean` checks negative integer
  bounds, complete-input consumption, exhausted fuel, malformed input,
  qualifiers, accessor desugaring, and EAC1's omitted parameters. These
  examples are authored here and contain no Arm excerpts.

ASLRef's serialization omits qualifiers and source positions. Matching
serialized trees covers the fields in that format. The parser targets the
release above; general ASL conformance is outside this result.

**First executable target:** `blake3_hybrid_c1`, the scalar AArch64 kernel
used by incremental hashing and single-chunk hashing. Assembled with
Clang 19.1.7, its code has 220 instruction words (880 bytes), followed by a
112-byte message schedule. Its disassembly has 21 mnemonics, including
aliases and several operand forms. A useful first theorem would cover one
block, its output, memory accesses, ABI preservation, and return. Its
callers and the tree construction stay outside that kernel theorem.

**Next go/no-go experiment:** interpret the real ADD, EOR, and EXTR decode
and execute sections under explicit register and system-state assumptions,
and prove their instruction lemmas. Keep the existing model and prover as
test oracles. Completing Bob's verifier also needs a sound symbolic checker
connected to C2SP's compression definition. A parser result alone provides
too little assurance to ship as an executable verifier.

## Run the prototype checks

Use this directory's pinned Lean toolchain (`lake` installs it). Obtain
Arm's archive as described below; keep it and generated certificates
outside the repository. The tested herdtools7 revision for ASLRef and its
standard library is `fb94bc582f38e2ddb83b6e9eec9bc3dedade3f67`.

```sh
lake build
lake env lean --trust=0 Dev/Tests.lean

# Both output directories must be new; they contain private Arm-derived text.
python3 Dev/compare.py /path/to/ISA_A64_xml_A_profile_2025-09_96-2025-09_ASL1 \
  /path/to/aslref.exe /path/to/herdtools7/asllib/libdir/stdlib.asl \
  /tmp/bob-comparison
python3 Dev/test_certificate.py /path/to/ISA_A64_xml_A_profile_2025-09_96-2025-09_ASL1 \
  /tmp/bob-certificates
```

The comparison uses CPython and its standard library (tested with 3.11.2). It compares
7,187 sections including the standard library and exits nonzero on any
acceptance or serialized-tree disagreement. Python's XML library extracts
the reference text independently of `Bob.Xml`. Each instruction section
gets a trailing `pass` on both sides so comment-only sections form valid
procedure bodies. ASLRef's CLI reverses top-level declarations before
serialization; the development printer mirrors that operation while the
parser preserves source order. Fresh-name numbering is compared too.

To propose and check a single literal parse certificate:

```sh
lake build bob-parse-certificate
.lake/build/bin/bob-parse-certificate /path/to/eor_log_shift.xml 1 > /tmp/eor.lean
lake env lean --trust=0 /tmp/eor.lean
```

Read the generated statement and compare its `input` bytes with the chosen
file. The generator proposes both literals; Lean checks their relation
under our parser. These commands make no claim about executable bytes.

A diagnostic run of the final EOR certificate check took 7,499,267,296 ns
wall time and peaked at 1,184,743,424 bytes of resident memory on the VM.
`clocks::child::run` measured the direct Lean process; `clocks::load`
reported quiet. Per-thread core cycle counts are unavailable there. This
is one run's resource observation; a full kernel proof's cost remains to
be measured. An earlier launcher nested Lean under
`timeout` and `lake`, causing the load monitor to count Lean's work as
other load; that reading supplies no speed evidence.

## Intended reliance set

The following describes the proposed completed checker, beyond the
prototype above.

1. **Lean's kernel.** Proofs use kernel reduction. `native_decide` would
   add Lean's compiler to the reliance set. Proofs use at most
   Lean's standard axioms. Independent implementations such as `lean4lean`
   and `nanoda` could check exported proofs too; that integration remains
   to be supplied.
2. **A statement Bob elaborates and reads.** Lean's front end, notation,
   and instances can affect what text means. A short statement file would
   define the claim independently of our proof files, and the checker
   would require the proof's theorem to be exactly that term. It must name
   the executable bytes, entry point, memory and alignment preconditions,
   output, allowed writes, ABI preservation, and termination. It must hold
   at every permitted code placement and for every allowed resolution of
   Arm's unspecified choices.
3. **C2SP's compression definition.** The existing `tools/verify/lean`
   project connects symbolic compression terms to C2SP's Lean definition.
   The new checker must connect its result to that same definition.
4. **Arm's ASL, obtained independently.** The source and licensing are
   below. Bob checks its identity and the literal inputs to the proof.
5. **Our ASL parser and interpreter definitions.** Kernel computation
   establishes what these definitions do; their agreement with ASL remains
   a reliance. The parser's corpus tests provide evidence. An interpreter
   would need its own comparisons with ASLRef and the CPU, with explicit
   treatment of state, faults, and nondeterminism.
6. **The bytes Bob runs.** He extracts them from his build with
   his tools and compares them with the theorem's bytes. Loader behaviour,
   dispatch, and the Rust callers remain separate obligations. The current
   project proves assembly kernels and tests Rust.
7. **Hardware.** The CPU must agree with its instruction specification.
   Instruction/address-trace independence would be a separate theorem;
   elapsed-time independence additionally needs instruction timing
   guarantees on Bob's CPU with DIT unchanged. For x86-64, the instruction
   model is also an external reliance: ACL2's x86 model remains a candidate.

## Arm's source and the semantic boundary

Arm's archive is available at:

`https://developer.arm.com/-/cdn-downloads/permalink/Exploration-Tools-A64-ISA/ISA_A64/ISA_A64_xml_A_profile-2026-03_96.tar.gz`

Fetched October 22, 2026:

- SHA-256: `9d138aa4dd6db8b04bd6ad3a378205537bf67172a5f99180729517d1445f1906`
- BLAKE3: `cc29573c01df924dce36a89389f49ab1e9ff747530ab008c6ba87f8915a95109`

It includes `ISA_A64_xml_A_profile_2025-09_96-2025-09_ASL1/`, with instruction
XML and `shared_pseudocode.xml` (1,738 sections, about 45,600 lines).
Its notice forbids reproducing any part without Arm's written permission.
The repository therefore contains neither its text nor derived test
fixtures; the commands read Bob's local copy. Check the archive against
both hashes before extracting it. Arm's BSD edition supplies encodings
without instruction semantics, so it cannot replace this download.

ASLRef's `asllib/libdir/stdlib.asl` supplies standard functions including
`UInt`, `ZeroExtend`, `LSL`, and `ROR`. It is Arm's BSD-3-Clause code and
may be redistributed under that license. The ASLRef-derived grammar, AST,
and serialization code here retains its notice in `ASLREF-LICENSE.txt`.

The parser computes on lists of byte values. Earlier kernel probes found
numeric literals far cheaper to reduce than strings' characters; the
certificate probe above exercises this representation on real input.
Structural fuel bounds all parser recursion. Exhausted fuel or unconsumed
tokens reject the input. Unsupported interpreter features must likewise
stop the future check.

Arm's ASL describes address translation, exceptions, debug paths, system
state, and features as well as arithmetic. By name alone, ADD, EOR, and
EXTR reach 537 shared blocks (11,420 lines). The proposed theorem would
state its boundary explicitly: successful flat-memory accesses within the
caller's regions, user-mode system state, and the required CPU features.
Each omitted machine facility must appear as an explicit assumption.

The direct-ASL route keeps translation machinery out of Bob's reliance
set. The earlier Sail probe found dropped tuple assignments and an
incorrect `MOD` translation (rems-project/sail#1772 and
rems-project/sail-arm#33). Our Python models remain useful test oracles;
using them as the checker semantics would leave their transcription in
Bob's reliance set.

The intended proof architecture interprets ASL for each instruction lemma,
then uses those lemmas in a faster symbolic checker. A once-proved
soundness theorem would justify that checker, including its normalization
of additions, xors, and rotations. Each kernel certificate would establish
that the checker accepts its bytes. Neither the instruction lemmas nor
that soundness theorem exists here yet.

## Validation scope of this change

The parser build, corpus comparison, kernel-reduced examples, and positive
and negative certificate checks exercise the changed code. The crate's
Rust suites and performance-regression gate were skipped: this change
leaves the library's sources, build, and machine code unchanged.
