"""Prove the SME2 kernels in c/blake3_sme2_aarch64.S equal to BLAKE3's
compression function (tools/verify/README.md), at the 512-bit streaming
vector length of Apple M4 (`cntw` = 16; the kernels return at once on any
other).

    python3 tools/verify/prove_sme2.py [KERNEL...]
    python3 tools/verify/prove_sme2.py every   # the chunk kernel at every group count (2 minutes)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from z3 import BitVec, BitVecVal, Concat, ZeroExt, Extract, If, LShR, simplify
import aarch64
import induction
from aarch64 import Region, Ptr, Unproved
import prove_hybrid
from prove_hybrid import Setup, byte_symbols, same
import lean_spec as spec

ROOT_DIR = prove_hybrid.ROOT_DIR
# An assembler that knows SME2 (Clang/LLVM 17 and later).
CLANG = next(c for c in ("clang-19", "clang-18", "clang-17", "clang") if shutil.which(c))


def assemble():
    obj = os.path.join(tempfile.mkdtemp(), "sme2.o")
    subprocess.run([CLANG, "-c", "-march=armv9-a+sme2", os.path.join(ROOT_DIR, "c/blake3_sme2_aarch64.S"), "-o", obj], check=True)
    return obj


class SmeSetup(Setup):
    def __init__(self, obj, entry, regions):
        syms = aarch64.symbols(obj)
        real = aarch64.disassemble
        aarch64.disassemble = lambda o, a, b: real(o, a, b, llvm=True)
        try:
            super().__init__(obj, entry, "blake3_servil_sme2_text_end", regions, start_name="blake3_servil_sme2_hash16_chunks_512")
        finally:
            aarch64.disassemble = real


def check_out(out, start, cvs):
    for i, cv in enumerate(cvs):
        if any(start + 32 * i + j not in out.bytes for j in range(32)):
            raise Unproved(f"output {i} is not wholly written")
        got = spec.words([out.bytes[start + 32 * i + j] for j in range(32)])
        for w in range(8):
            if not same(got[w], cv[w]):
                raise Unproved(f"output {i}, word {w} differs from the specification")


def chunk_cvs(chunks, key, counters, flags, start, end):
    """The chunk kernel's statement: prove_hybrid's table_cvs for chunks of
    16 whole blocks, chunk i at counters[i]. The proofs below state it with
    this."""
    return prove_hybrid.table_cvs(chunks, key, counters, flags, start, end, BitVecVal(64, 8), 16)


def parent_cvs(pairs, key, counter, flags, n):
    """The parent kernel's statement: block i of `pairs` (64 bytes) under
    `key` at `counter`, block length 64, the 32-bit `flags`."""
    return [spec.compress(spec.words(key), spec.words(pairs[64 * i:64 * i + 64]), counter, BitVecVal(64, 32), flags)[:8]
            for i in range(n)]


def prove_chunks(obj, groups, stored, at, check=True):
    """hash16_chunks_512(inputs, key, counter, flags, out, groups), or with
    `at` hash16_chunks_at_512 (x2 a table of a counter per group): 16
    chunks a group, lane l of group g at counter + 16 g + l (or
    counters[g] + l); flags = flags | start << 8 | end << 16 | stored << 24
    with anything above bit 31 (ignored); the last group stores its first
    `stored` values (0: all). out holds exactly what is stored."""
    n = 16 * groups
    chunks = [byte_symbols(f"in{i}_", 1024) for i in range(n)]
    key = byte_symbols("key", 32)
    regions = {f"input{i}": Region(f"input{i}", 1024, False, (lambda b: lambda o: b[o])(chunks[i])) for i in range(n)}
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    kept = 16 * (groups - 1) + (stored or 16)
    regions["out"] = Region("out", 32 * kept, True, None)
    table = Region("table", 8 * n, False, None)
    for i in range(n):
        for j in range(8):
            table.bytes[8 * i + j] = (Ptr(f"input{i}", 0), j)
    regions["table"] = table
    counters = [BitVec(f"counters{g}", 64) for g in range(groups)]
    if at:
        cb = [Extract(8 * j + 7, 8 * j, counters[g]) for g in range(groups) for j in range(8)]
        regions["counters"] = Region("counters", 8 * groups, False, lambda o: cb[o])
    s = SmeSetup(obj, "blake3_servil_sme2_hash16_chunks_at_512" if at else "blake3_servil_sme2_hash16_chunks_512", regions)
    counter = BitVec("counter", 64)
    flags, start, end = (BitVec(x, 8) for x in ("flags", "start", "end"))
    m = s.m
    m.x[0], m.x[1] = Ptr("table", 0), Ptr("key", 0)
    m.x[2] = Ptr("counters", 0) if at else counter
    m.x[3] = Concat(BitVec("junk", 32), BitVecVal(stored, 8), end, start, flags)
    m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    s.run_and_check_abi()
    if aarch64.concrete(m.x[0]) != 16:
        raise Unproved(f"returned {m.x[0]}, not 16")
    if not check:
        return m.steps
    cs = [(counters[i // 16] if at else counter + 16 * (i // 16)) + i % 16 for i in range(kept)]
    check_out(m.regions["out"], 0, chunk_cvs(chunks[:kept], key, cs, flags, start, end))
    return m.steps


def prove_messages(obj, groups, blocks, stored, check=True):
    """hash16_messages_512(inputs, key, counter, flags, out, groups,
    blocks): 16 messages a group of `blocks` whole blocks (2 to 16), every
    lane of group g at counter + g * step (step = flags bits 32..39), the
    last block recording last_len bytes (flags bits 40..47: any of 1 to 63,
    or 0 for 64, the contract's range, as a 6-bit symbol),
    start on block 0, end on the last; stored as the chunk kernel's."""
    n = 16 * groups
    msgs = [byte_symbols(f"in{i}_", 64 * blocks) for i in range(n)]
    key = byte_symbols("key", 32)
    regions = {f"input{i}": Region(f"input{i}", 64 * blocks, False, (lambda b: lambda o: b[o])(msgs[i])) for i in range(n)}
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    kept = 16 * (groups - 1) + (stored or 16)
    regions["out"] = Region("out", 32 * kept, True, None)
    table = Region("table", 8 * n, False, None)
    for i in range(n):
        for j in range(8):
            table.bytes[8 * i + j] = (Ptr(f"input{i}", 0), j)
    regions["table"] = table
    s = SmeSetup(obj, "blake3_servil_sme2_hash16_messages_512", regions)
    counter = BitVec("counter", 64)
    flags, start, end, step = (BitVec(x, 8) for x in ("flags", "start", "end", "step"))
    last_len = BitVec("last_len", 6)
    m = s.m
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    m.x[3] = Concat(BitVecVal(0, 18), last_len, step, BitVecVal(stored, 8), end, start, flags)
    m.x[4], m.x[5], m.x[6] = Ptr("out", 0), BitVecVal(groups, 64), BitVecVal(blocks, 64)
    s.run_and_check_abi()
    if aarch64.concrete(m.x[0]) != 16:
        raise Unproved(f"returned {m.x[0]}, not 16")
    if not check:
        return m.steps
    zero = BitVecVal(0, 8)
    cvs = []
    for i in range(kept):
        g = i // 16
        c = counter + ZeroExt(56, step) * g
        cv = spec.words(key)
        for b in range(blocks):
            f = ZeroExt(24, flags | (start if b == 0 else zero) | (end if b == blocks - 1 else zero))
            length = If(last_len == 0, BitVecVal(64, 32), ZeroExt(26, last_len)) if b == blocks - 1 else BitVecVal(64, 32)
            cv = spec.compress(cv, spec.words(msgs[i][64 * b:64 * b + 64]), c, length, f)[:8]
        cvs.append(cv)
    check_out(m.regions["out"], 0, cvs)
    return m.steps


def prove_parents(obj, groups, check=True):
    """hash16_parents_512(pairs, key, counter, flags, out, groups): parent
    i compresses pairs[64 i..] under key at counter (unchanged), block
    length 64, the 32-bit flags word."""
    n = 16 * groups
    pairs = byte_symbols("pair", 64 * n)
    key = byte_symbols("key", 32)
    regions = {"pairs": Region("pairs", 64 * n, False, lambda o: pairs[o]),
               "key": Region("key", 32, False, lambda o: key[o]),
               "out": Region("out", 32 * n, True, None)}
    s = SmeSetup(obj, "blake3_servil_sme2_hash16_parents_512", regions)
    counter, flags = BitVec("counter", 64), BitVec("flags", 32)
    m = s.m
    m.x[0], m.x[1], m.x[2] = Ptr("pairs", 0), Ptr("key", 0), counter
    m.x[3] = ZeroExt(32, flags)
    m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    s.run_and_check_abi()
    if aarch64.concrete(m.x[0]) != 16:
        raise Unproved(f"returned {m.x[0]}, not 16")
    if not check:
        return m.steps
    check_out(m.regions["out"], 0, parent_cvs(pairs, key, counter, flags, n))
    return m.steps


def prove_xof(obj, groups, check=True):
    """xof16_512(cv, block, counter, flags_len, out, groups): output block
    i, the 16 words of the compression of `block` under cv at counter + i,
    flags the low byte of flags_len and block length its second."""
    cvb, block = byte_symbols("cv", 32), byte_symbols("block", 64)
    regions = {"cv": Region("cv", 32, False, lambda o: cvb[o]),
               "block": Region("block", 64, False, lambda o: block[o]),
               "out": Region("out", 1024 * groups, True, None)}
    s = SmeSetup(obj, "blake3_servil_sme2_xof16_512", regions)
    counter, flags, length = BitVec("counter", 64), BitVec("flags", 8), BitVec("last_len", 8)
    m = s.m
    m.x[0], m.x[1], m.x[2] = Ptr("cv", 0), Ptr("block", 0), counter
    m.x[3] = ZeroExt(48, Concat(length, flags))
    m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    s.run_and_check_abi()
    if aarch64.concrete(m.x[0]) != 16:
        raise Unproved(f"returned {m.x[0]}, not 16")
    if not check:
        return m.steps
    out = m.regions["out"]
    for i in range(16 * groups):
        want = spec.compress(spec.words(cvb), spec.words(block), counter + i, ZeroExt(24, length), ZeroExt(24, flags))
        if any(64 * i + j not in out.bytes for j in range(64)):
            raise Unproved(f"output block {i} is not wholly written")
        got = spec.words([out.bytes[64 * i + j] for j in range(64)])
        for w in range(16):
            if not same(got[w], want[w]):
                raise Unproved(f"output block {i}, word {w} differs from the specification")
    return m.steps


def loop_head(obj, run, taken):
    """The head of the loop whose backward jump a concrete `run` takes
    `taken` times, the only such."""
    import collections
    jumps, last, step = collections.Counter(), [None], aarch64.step
    def trace(m, pc, mnem, ops):
        if last[0] is not None and pc < last[0]:
            jumps[pc] += 1
        last[0] = pc
        return step(m, pc, mnem, ops)
    aarch64.step = trace
    try:
        run()
    finally:
        aarch64.step = step
    heads = [pc for pc, n in jumps.items() if n == taken]
    if len(heads) != 1:
        raise Unproved(f"expected one loop taken {taken} times, found {sorted(map(hex, heads))}")
    return heads[0]


def prove_parents_every(obj):
    """hash16_parents_512 for 2..2^40 groups (1 and 2 also have concrete
    proofs), by induction over its group loop. Every byte of every group's
    pairs is independent: parent i of group K compresses pairs[1024 K +
    64 i..] under the key and unchanged counter into out[32 (16 K + i)..]."""
    from z3 import ULE, UGE, ULT, Array, BitVecSort, Select, Lambda, substitute
    pairs = Array("pairs", BitVecSort(64), BitVecSort(8))
    key = byte_symbols("key", 32)
    g, k = BitVec("len_groups", 64), BitVec("len_k", 64)
    def pair_byte(offset):
        return Select(pairs, aarch64.as_bv(offset))
    regions = {"pairs": Region("pairs", g * 1024, False, pair_byte),
               "key": Region("key", 32, False, lambda o: key[o]),
               "out": Region("out", g * 512, True, None)}
    regions["pairs"].symbolic_initial = True
    s = SmeSetup(obj, "blake3_servil_sme2_hash16_parents_512", regions)
    m = s.m
    m.lengths = {"len_groups", "len_k"}
    m.assumptions = [UGE(g, 2), ULE(g, 1 << 40)]
    counter, flags = BitVec("counter", 64), BitVec("flags", 32)
    m.x[0], m.x[1], m.x[2] = Ptr("pairs", 0), Ptr("key", 0), counter
    m.x[3] = ZeroExt(32, flags)
    m.x[4], m.x[5] = Ptr("out", 0), g
    head = loop_head(obj, lambda: prove_parents(obj, 3, check=False), 2)
    def group_cvs(kval):
        group = aarch64.as_bv(kval)
        return [spec.compress(spec.words(key), spec.words([pair_byte(1024 * group + 64 * i + j) for j in range(64)]),
                              counter, BitVecVal(64, 32), flags)[:8] for i in range(16)]
    def check_group(mach, kval):
        out = mach.regions["out"]
        keys = set()
        for i, cv in enumerate(group_cvs(kval)):
            base = BitVecVal(kval * 512, 64) if isinstance(kval, int) else aarch64.as_bv(kval * 512)
            offs = [aarch64.key(base + 32 * i + j) for j in range(32)]
            keys.update(offs)
            if any(o not in out.bytes for o in offs):
                raise Unproved(f"group output {i} is not wholly written")
            got = spec.words([out.bytes[o] for o in offs])
            for w in range(8):
                if not same(got[w], cv[w]):
                    raise Unproved(f"group output {i}, word {w}, differs from the specification")
        if set(out.bytes) != keys:
            raise Unproved("the group writes output bytes outside its own")
    # The group loop jumps back to the kernel's entry: the first arrival at
    # its head is the call itself.
    s0 = induction.snapshot(m) if head == s.entry else induction.run_to(m, s.entry, head)
    if s0.regions["out"].bytes:
        raise Unproved("output written before the group loop")
    s1 = induction.run_to(induction.snapshot(s0), head, head)
    check_group(s1, 0)
    gen = induction.Generalized(s0, s1, k)
    # The kernel preloads the next group's words into ZA. State their
    # addresses at K explicitly; arbitrary bytes have no constant step.
    index = BitVec("pair_index", 64)
    shifted = Lambda([index], Select(pairs, index + 1024 * k))
    for tile, rows in enumerate(s0.za):
        for row, values in enumerate(rows):
            for lane, value in enumerate(values):
                if pairs.decl().name() in aarch64.term_symbols(value):
                    gen.za[tile][row][lane] = simplify(substitute(value, (pairs, shifted)))
    bad = induction.mismatches(s0, gen, BitVecVal(0, 64))
    if bad:
        raise Unproved(f"the initial state differs from the group invariant at {bad}")
    def check(endm, how):
        check_group(endm, k)
        if how == "return":
            if not aarch64.holds(endm, g == k + 1):
                raise Unproved("a path leaves the group loop before the last group")
            if aarch64.concrete(endm.x[0]) != 16:
                raise Unproved(f"returned {endm.x[0]}, not 16")
            prove_hybrid.check_abi(endm)
    kinds = induction.loop_step(gen, s0, k, list(s0.assumptions) + [ULT(k, g)], head, check)
    return f"{kinds['head']} paths back to the group loop's head, {kinds['return']} out of it"


def prove_xof_every(obj):
    """xof16_512 at every number of groups from 2 to 2^40 (1 and 2 are the
    concrete cases), by induction over its group loop: group K's output
    block i, at out[1024 K + 64 i..], is the compression of `block` under cv
    at counter + 16 K + i. Every group reads the same cv and block."""
    from z3 import ULE, UGE, ULT
    cvb, block = byte_symbols("cv", 32), byte_symbols("block", 64)
    g, k = BitVec("len_groups", 64), BitVec("len_k", 64)
    regions = {"cv": Region("cv", 32, False, lambda o: cvb[o]),
               "block": Region("block", 64, False, lambda o: block[o]),
               "out": Region("out", g * 1024, True, None)}
    s = SmeSetup(obj, "blake3_servil_sme2_xof16_512", regions)
    m = s.m
    # The counter takes part in conditions (its carry), each decided by the solver.
    m.lengths = {"len_groups", "len_k", "counter"}
    m.assumptions = [UGE(g, 2), ULE(g, 1 << 40)]
    counter, flags, length = BitVec("counter", 64), BitVec("flags", 8), BitVec("last_len", 8)
    m.x[0], m.x[1], m.x[2] = Ptr("cv", 0), Ptr("block", 0), counter
    m.x[3] = ZeroExt(48, Concat(length, flags))
    m.x[4], m.x[5] = Ptr("out", 0), g
    head = loop_head(obj, lambda: prove_xof(obj, 3, check=False), 2)
    def check_group(mach, kval):
        out = mach.regions["out"]
        keys = set()
        for i in range(16):
            want = spec.compress(spec.words(cvb), spec.words(block), counter + kval * 16 + i, ZeroExt(24, length), ZeroExt(24, flags))
            base = BitVecVal(kval * 1024, 64) if isinstance(kval, int) else aarch64.as_bv(kval * 1024)
            offs = [aarch64.key(base + 64 * i + j) for j in range(64)]
            keys.update(offs)
            if any(o not in out.bytes for o in offs):
                raise Unproved(f"group output block {i} is not wholly written")
            got = spec.words([out.bytes[o] for o in offs])
            for w in range(16):
                if not same(got[w], want[w]):
                    raise Unproved(f"group output block {i}, word {w}, differs from the specification")
        if set(out.bytes) != keys:
            raise Unproved("the group writes output bytes outside its own")
    s0 = induction.snapshot(m) if head == s.entry else induction.run_to(m, s.entry, head)
    if s0.regions["out"].bytes:
        raise Unproved("output written before the group loop")
    s1 = induction.run_to(induction.snapshot(s0), head, head)
    check_group(s1, 0)
    gen = induction.Generalized(s0, s1, k)
    def check(endm, how):
        check_group(endm, k)
        if how == "return":
            if not aarch64.holds(endm, g == k + 1):
                raise Unproved("a path leaves the group loop before the last group")
            if aarch64.concrete(endm.x[0]) != 16:
                raise Unproved(f"returned {endm.x[0]}, not 16")
            prove_hybrid.check_abi(endm)
    kinds = induction.loop_step(gen, s0, k, list(s0.assumptions) + [ULT(k, g)], head, check)
    return f"{kinds['head']} paths back to the group loop's head, {kinds['return']} out of it"


# The SME2 kernel with an integer lane (c/blake3_sme2_hybrid_aarch64.S,
# tools/gen_sme2_hybrid.py): 16 SME2 lanes and 2 integer ones a group.
HYBRID_GROUP = 18


def assemble_hybrid():
    obj = os.path.join(tempfile.mkdtemp(), "sme2_hybrid.o")
    subprocess.run([CLANG, "-c", "-march=armv9-a+sme2", os.path.join(ROOT_DIR, "c/blake3_sme2_hybrid_aarch64.S"), "-o", obj], check=True)
    return obj


def text_end(obj):
    """The end of the object's .text (the hybrid file has no end symbol of
    its own)."""
    sizes = subprocess.run([aarch64.LLVM_OBJDUMP, "-h", obj], capture_output=True, text=True, check=True).stdout
    return next(int(l.split()[2], 16) for l in sizes.splitlines() if " .text " in l)


class HybridSetup(Setup):
    """Setup for the hybrid file: its code from the kernel's entry to the
    end of .text."""
    def __init__(self, obj, regions):
        end = text_end(obj)
        real = aarch64.disassemble
        aarch64.disassemble = lambda o, a, b: real(o, a, end, llvm=True)
        try:
            super().__init__(obj, "blake3_servil_sme2x2_hash_chunks_512", "blake3_servil_sme2x2_hash_chunks_512", regions)
        finally:
            aarch64.disassemble = real


def prove_chunks_x2(obj, groups, check=True):
    """sme2x2_hash_chunks_512(inputs, key, counter, flags, out, groups): 18
    chunks a group, chunk i of group g at counter + 18 g + i; flags = flags
    | start << 8 | end << 16 (bits 24-31 zero, anything above ignored); out
    receives 18 chaining values a group, in order."""
    n = HYBRID_GROUP * groups
    chunks = [byte_symbols(f"in{i}_", 1024) for i in range(n)]
    key = byte_symbols("key", 32)
    regions = {f"input{i}": Region(f"input{i}", 1024, False, (lambda b: lambda o: b[o])(chunks[i])) for i in range(n)}
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    regions["out"] = Region("out", 32 * n, True, None)
    table = Region("table", 8 * n, False, None)
    for i in range(n):
        for j in range(8):
            table.bytes[8 * i + j] = (Ptr(f"input{i}", 0), j)
    regions["table"] = table
    s = HybridSetup(obj, regions)
    counter = BitVec("counter", 64)
    flags, start, end = (BitVec(x, 8) for x in ("flags", "start", "end"))
    m = s.m
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    m.x[3] = Concat(BitVec("junk", 32), BitVecVal(0, 8), end, start, flags)
    m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    s.run_and_check_abi()
    if aarch64.concrete(m.x[0]) != 16:
        raise Unproved(f"returned {m.x[0]}, not 16")
    if not check:
        return m.steps
    check_out(m.regions["out"], 0, chunk_cvs(chunks, key, [counter + i for i in range(n)], flags, start, end))
    return m.steps
# The always-run cases prove the chunk and parent kernels at 1 and 2 groups
# (the concrete cases) and 2 to 2^40 (the inductions over the group loop,
# every and parents_every).
ALWAYS = ("every", "parents_every")


def cases(only):
    out = []
    for groups in (1, 2):
        for stored in (0, 1, 15):
            out += [("chunks", groups, stored), ("chunks_at", groups, stored)]
    for blocks in range(2, 17):
        out.append(("messages", 1, (blocks, 0)))
    for stored in (1, 15):
        out.append(("messages", 1, (5, stored)))
    out.append(("messages", 2, (3, 7)))
    out += [("parents", 1, None), ("parents", 2, None), ("xof", 1, None), ("xof", 2, None)]
    out += [("chunks_x2", 1, None), ("chunks_x2", 2, None)]
    # The inductions: every and parents_every always (the contracts rest on
    # them); the rest when named.
    out += [("every", None, None), ("parents_every", None, None), ("xof_every", None, None)]
    # The message kernel at every group count and every length, in one
    # induction with its block loop by its own proof (prove_block_loop).
    out.append(("messages_every", None, None))
    # The kernel with an integer lane at every group count, likewise
    # (prove_hybrid_loop).
    out.append(("x2_every", None, None))
    # A name selects its cases; name:N the one whose argument is N.
    named = lambda c: c[0] in only or f"{c[0]}:{c[2]}" in only
    return [c for c in out if (named(c) if only else not c[0].endswith("every") or c[0] in ALWAYS)]


OBJ = None
HYBRID_OBJ = [None]


def prove_case(case):
    global OBJ
    if OBJ is None:
        OBJ = assemble()
    name, groups, arg = case
    t = time.time()
    try:
        if name in ("chunks", "chunks_at"):
            steps = prove_chunks(OBJ, groups, arg, name == "chunks_at")
            what = f"{groups} groups, the last storing {arg or 16}"
        elif name == "messages":
            blocks, stored = arg
            steps = prove_messages(OBJ, groups, blocks, stored)
            what = f"{groups} groups of {blocks}-block messages, the last block of any length, the last group storing {stored or 16}"
        elif name == "every":
            import prove_block_loop
            step = prove_block_loop.prove(OBJ, chunks=True)
            what = prove_block_loop.prove_every(OBJ, chunks=True)
            return case, f"proved (the chunk kernel's block loop, {step}; {what}, the last group storing 16, {time.time() - t:.0f} s)"
        elif name == "messages_every":
            import prove_block_loop
            step = prove_block_loop.prove(OBJ)
            what = prove_block_loop.prove_every(OBJ)
            return case, f"proved (the message kernel's block loop, {step}; the kernel at {what}, {time.time() - t:.0f} s)"
        elif name == "x2_every":
            import prove_hybrid_loop as hl
            if HYBRID_OBJ[0] is None:
                HYBRID_OBJ[0] = assemble_hybrid()
            gen_a, s1, half_a = hl.half_a(HYBRID_OBJ[0])
            gen_b, _, half_b = hl.half_b(HYBRID_OBJ[0], gen_a, s1)
            what = hl.prove_every(HYBRID_OBJ[0], gen_a, gen_b)
            return case, f"proved (the kernel with an integer lane: its block loop, {half_a}; {half_b}; the kernel at {what}, {time.time() - t:.0f} s)"
        elif name == "xof_every":
            what = prove_xof_every(OBJ)
            return case, f"proved (the extended-output kernel at every group count: {what}, {time.time() - t:.0f} s)"
        elif name == "parents_every":
            what = prove_parents_every(OBJ)
            return case, f"proved (the parent kernel at every group count: {what}, {time.time() - t:.0f} s)"
        elif name == "chunks_x2":
            if HYBRID_OBJ[0] is None:
                HYBRID_OBJ[0] = assemble_hybrid()
            steps, what = prove_chunks_x2(HYBRID_OBJ[0], groups), f"the kernel with an integer lane, {groups} groups of 18"
        elif name == "parents":
            steps, what = prove_parents(OBJ, groups), f"{groups} groups"
        else:
            steps, what = prove_xof(OBJ, groups), f"{groups} groups"
    except Unproved as e:
        return case, f"NOT PROVED: {e}"
    return case, f"proved ({what}, {steps} instructions, {time.time() - t:.1f} s)"




def main():
    global OBJ
    import workers
    OBJ = assemble()
    HYBRID_OBJ[0] = assemble_hybrid()
    # The longest cases first, so none is left to run alone at the end: the
    # inductions of the kernel with an integer lane (about 12 minutes) and
    # of the chunk kernel (2).
    first = ("x2_every", "every")
    todo = sorted(cases(set(sys.argv[1:])),
                  key=lambda c: first.index(c[0]) if c[0] in first else len(first))
    failed = 0
    # Every CPU: the largest case holds about 600 MB (x2_every under
    # CPython, October 22).
    for (name, _, _), verdict in workers.each(prove_case, todo, len(todo)):
        print(f"{name}: {verdict}", flush=True)
        failed += verdict.startswith("NOT")
    print(f"{len(todo) - failed} of {len(todo)} cases proved")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
