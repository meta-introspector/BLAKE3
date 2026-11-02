"""The SME2 message kernel's block loop, one pass proved for every pass and
every message length, so the long proofs need not unroll it: from any hash state, with block K of every lane in ZA, a pass
leaves the standard's compression of that state with block K, and block
K + 1 in ZA.

    python3 tools/verify/prove_block_loop.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from z3 import Array, BitVec, BitVecSort, BitVecVal, Concat, Function, LShR, Select, ULE, UGE, ULT, ZeroExt, simplify
import aarch64
import induction
import prove_sme2
from aarch64 import Ptr, Region, Unproved
from prove_hybrid import byte_symbols
import lean_spec as spec

HEAD = 0x21c   # the block loop's head (cmp x5, #1)
EXIT = 0xcc0   # its exit: the last block's compression


def setup(obj, groups=1, chunks=False):
    """A group of 16 messages of n blocks (n symbolic, 2 to 16), each
    lane's bytes an array read at any offset; with groups = 2, more to come
    (the loop then prefetches the next group's table entries). With
    `chunks`, the chunk kernel's entry: 16 blocks, lane l at counter + l."""
    n = BitVecVal(16, 64) if chunks else BitVec("len_blocks", 64)
    lanes = [Array(f"msg{l}", BitVecSort(64), BitVecSort(8)) for l in range(16)]
    key = byte_symbols("key", 32)
    regions = {}
    for l in range(16):
        r = Region(f"input{l}", n * 64, False, (lambda a: lambda o: Select(a, aarch64.as_bv(o)))(lanes[l]))
        r.symbolic_initial = True
        regions[f"input{l}"] = r
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    regions["out"] = Region("out", 32 * 16, True, None)
    table = Region("table", 8 * 16 * groups, False, None)
    for i in range(16 * groups):
        for j in range(8):
            table.bytes[8 * i + j] = (Ptr(f"input{i % 16}", 0), j)
    regions["table"] = table
    s = prove_sme2.SmeSetup(obj, "blake3_servil_sme2_hash16_chunks_512" if chunks else "blake3_servil_sme2_hash16_messages_512", regions)
    m = s.m
    counter = BitVec("counter", 64)
    flags, start, end, step = (BitVec(x, 8) for x in ("flags", "start", "end", "step"))
    last_len = BitVec("last_len", 6)
    m.lengths = {"len_blocks", "len_k"}
    m.assumptions = [UGE(n, 2), ULE(n, 16)]
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    if chunks:
        m.x[3] = Concat(BitVec("junk", 32), BitVecVal(0, 8), end, start, flags)
        m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    else:
        m.x[3] = Concat(BitVecVal(0, 18), last_len, step, BitVecVal(0, 8), end, start, flags)
        m.x[4], m.x[5], m.x[6] = Ptr("out", 0), BitVecVal(groups, 64), n
    s.lane_counter = (lambda l: counter + l) if chunks else (lambda l: counter)
    return s, m, lanes, key, (n, counter, flags, start, end, step, last_len)


def lane_word(cell):
    """(array, first offset) when `cell` is four bytes of one lane's array,
    Select(a, o)..Select(a, o + 3) little-endian; else None."""
    from z3 import is_app_of, Z3_OP_CONCAT, Z3_OP_SELECT, simplify
    parts = []
    def flat(t):
        if is_app_of(t, Z3_OP_CONCAT):
            for c in t.children():
                flat(c)
        else:
            parts.append(t)
    flat(cell)
    if len(parts) != 4 or not all(is_app_of(p, Z3_OP_SELECT) for p in parts):
        return None
    arr = parts[0].arg(0)
    offs = [p.arg(1) for p in reversed(parts)]
    if not all(p.arg(0).eq(arr) for p in parts):
        return None
    if not all(simplify(offs[j] - offs[0] - j).eq(BitVecVal(0, 64)) for j in range(4)):
        return None
    return arr, offs[0]


class BlockGeneralized(induction.Generalized):
    """Generalized, with a ZA cell that holds four bytes of a lane at
    offset o in one arrival and o + 64 in the next kept as the lane's bytes
    at o + 64 K (block K's), where the base class would forget it."""
    def __init__(self, s0, s1, k):
        super().__init__(s0, s1, k)
        for t, tile in enumerate(s0.za):
            for row, cells in enumerate(tile):
                for col, a in enumerate(cells):
                    w0, w1 = lane_word(a), lane_word(s1.za[t][row][col])
                    if w0 and w1 and w0[0].eq(w1[0]) and aarch64.concrete(aarch64.as_bv(w1[1] - w0[1])) == 64:
                        self.za[t][row][col] = ("lanew", w0[0], w0[1])

    def at(self, kval):
        base = super().at(kval)
        def inst(g, size=64):
            if isinstance(g, tuple) and g[0] == "lanew":
                o = aarch64.as_bv(g[2] + 64 * kval)
                return Concat(*[Select(g[1], o + j) for j in reversed(range(4))])
            return base(g, size) if size != 64 else base(g)
        return inst


def block_words(lane, off):
    """The 16 message words of a block at `off` of `lane` (an array)."""
    return spec.words([Select(lane, aarch64.as_bv(off) + j) for j in range(64)])


def expect(h, lane, off, counter, flags):
    """A middle block's compression: the 8-word chaining value."""
    return spec.compress(h, block_words(lane, off), counter, BitVecVal(64, 32), ZeroExt(24, flags))[:8]


def compare(m, got, want, what):
    from prove_hybrid import same
    import canon
    canon.set_context(m.assumptions, m.lengths)
    try:
        for w in range(8):
            if not same(got[w], want[w]):
                raise Unproved(f"{what}: word {w} differs from the standard's compression")
    finally:
        canon.set_context([], set())


def prove(obj, chunks=False):
    """The step, with the last group (x5 = 1) and with more to come (2)."""
    return "; ".join(f"{groups} group{'s' if groups > 1 else ''} to go: {prove_step(obj, groups, chunks)}" for groups in (1, 2))


def prove_step(obj, groups, chunks=False):
    s, m, lanes, key, (n, counter, flags, start, end, step, last_len) = setup(obj, groups, chunks)
    lc = s.lane_counter
    s0 = induction.run_to(m, s.entry, HEAD)
    s1 = induction.snapshot(s0)
    s1.assumptions = s1.assumptions + [UGE(n, 3)]
    s1 = induction.run_to(s1, HEAD, HEAD)
    # The first pass: block 0 under the key, with the start flag.
    kw = spec.words(key)
    for l in range(16):
        h = [s0.v[i][l] for i in range(8)]
        compare(s0, h, kw, f"lane {l} at the head before block 0 (the key)")
        want = spec.compress(kw, block_words(lanes[l], 0), lc(l), BitVecVal(64, 32), ZeroExt(24, flags | start))[:8]
        compare(s1, [s1.v[i][l] for i in range(8)], want, f"lane {l} after block 0")
    print("the first pass: block 0 under the key, with the start flag, every lane", flush=True)
    # Two-block messages: the first pass is the last, out of the loop with
    # block 1 in ZA.
    two = induction.snapshot(s0)
    two.assumptions = [a for a in two.assumptions] + [n == 2]
    outs = [] if chunks else induction.paths(two, HEAD, {HEAD, EXIT})
    if chunks:
        pass
    elif [e.stopped_at for e, _ in outs] != [EXIT]:
        raise Unproved("with two blocks, the first pass does not leave the loop")
    for out, _ in outs:
        for l in range(16):
            want = spec.compress(kw, block_words(lanes[l], 0), counter, BitVecVal(64, 32), ZeroExt(24, flags | start))[:8]
            compare(out, [out.v[i][l] for i in range(8)], want, f"lane {l} after block 0 of two")
        for row, cells in enumerate(out.za[0]):
            for col, cell in enumerate(cells):
                w0, w1 = lane_word(s0.za[0][row][col]), lane_word(cell)
                if not (w0 and w1 and w0[0].eq(w1[0]) and aarch64.holds(out, aarch64.as_bv(w1[1]) == aarch64.as_bv(w0[1]) + 64)):
                    raise Unproved(f"with two blocks, ZA cell {row},{col} holds other than block 1 at the loop's exit")
        if not aarch64.holds(out, aarch64.as_bv(out.x[7]) == 1):
            raise Unproved("with two blocks, the loop's counter leaves other than 1")
    # Passes K >= 1 from the arrivals at K = 1 and 2 (four blocks or more for
    # the second arrival; the step itself covers three on).
    s1.assumptions = s1.assumptions + [UGE(n, 4)]
    s2 = induction.run_to(induction.snapshot(s1), HEAD, HEAD)
    k = BitVec("len_k", 64)
    gen = BlockGeneralized(s1, s2, k)
    kept = sum(1 for tile in gen.za for row in tile for c in row if isinstance(c, tuple) and c[0] == "lanew")
    print(f"generalized at K = 1 + k: {kept} ZA cells kept as block K's bytes", flush=True)
    K = k + 1
    passes = 0
    while True:
        passes += 1
        mk = gen.machine(s1, k)
        mk.assumptions = [UGE(n, 3), ULE(n, 16), ULE(k, 14), ULE(k + 3, n)]   # pass K = 1 + k <= n - 2
        mk.lengths = {"len_blocks", "len_k"}
        h = [[mk.v[i][l] for i in range(8)] for l in range(16)]
        ends = induction.paths(mk, HEAD, {HEAD, EXIT})
        forget = set()
        for end, _ in ends:
            # At the head and at the exit alike: ZA holds block K + 1, and the
            # rest the state for K + 1 (the exit's is the last block's).
            if True:
                import canon
                canon.set_context(end.assumptions, end.lengths)
                forget.update(b for b in induction.mismatches(end, gen, k + 1) if not (b[0] == "v" and b[1] < 8))
                canon.set_context([], set())
        print(f"  pass {passes}: {len(ends)} paths, {len(forget)} positions left unknown {sorted(map(str, forget))[:8]}", flush=True)
        if not forget:
            break
        for pos in forget:
            gen.forget(pos)
    for end, _ in ends:
        at = end.stopped_at
        for l in range(16):
            compare(end, [end.v[i][l] for i in range(8)], expect(h[l], lanes[l], 64 * K, lc(l), flags), f"lane {l} after block K ({at:#x})")
        if at == EXIT and not aarch64.holds(end, K + 2 == n):
            raise Unproved("the loop exits before its last pass")
    changed = []
    for r, g in gen.x.items():
        if isinstance(g, tuple):
            changed.append(("x", r, g[0]))
    for r, lanes_ in gen.v.items():
        kinds = {g[0] for g in lanes_ if isinstance(g, tuple)}
        if kinds:
            changed.append(("v", r, "/".join(sorted(kinds))))
    for t, tile in enumerate(gen.za):
        kinds = {c[0] for row in tile for c in row if isinstance(c, tuple)}
        if kinds:
            changed.append(("za", t, "/".join(sorted(kinds))))
    for name, mem in gen.mem.items():
        if any(induction.generalized(b) for b in mem.values()):
            changed.append(("mem", name, ""))
    if gen.p is not None:
        changed += [("p", q, "") for q, v in gen.p.items() if v is None]
    print("the loop changes:", changed, flush=True)
    return f"every pass K >= 1: {len(ends)} paths (back to the head with block K + 1 in ZA, or out after the last), each the standard's compression of block K"


def summary(n, chains, lanes, key, group_base=lambda: 0):
    """The block loop by its proof (prove): from a group's first arrival
    (pass 0, the key in z0-z7, block 0 in ZA) to its exit after n - 1
    passes: z0-z7 the chain over blocks 0 to n - 2 (`chains`, by lane),
    block n - 1 in ZA, the loop's counters at their end, and everything
    else it changes (x8, x10, z8-z31, p1) unknown."""
    from z3 import BitVec as BV
    fresh = [0]
    def unknown(size):
        fresh[0] += 1
        return BV(f"after_loop{fresh[0]}", size)
    def run(m):
        if aarch64.concrete(m.x[7]) != 0:
            raise Unproved("the block loop entered other than at its first pass")
        # What the loop's proof starts from: the key in z0-z7 and each
        # lane's block 0 in ZA, as the first pass found them (prove).
        kw = spec.words(key)
        for l in range(16):
            compare(m, [m.v[i][l] for i in range(8)], kw, f"lane {l} entering the block loop (the key)")
        # Each lane's block 0, the group's (at its base), every word once.
        base = aarch64.as_bv(group_base())
        names = [a.decl().name() for a in lanes]
        seen = set()
        for row, cells in enumerate(m.za[0]):
            for col, cell in enumerate(cells):
                w = lane_word(cell)
                if w is None or w[0].decl().name() not in names:
                    raise Unproved(f"ZA tile 0 cell {row},{col} holds no lane's bytes entering the loop")
                j = aarch64.concrete(simplify(aarch64.as_bv(w[1]) - base))
                if j is None:
                    j = next((c for c in range(0, 64, 4) if aarch64.holds(m, aarch64.as_bv(w[1]) == base + c)), None)
                if j is None or j % 4 or not 0 <= j < 64:
                    raise Unproved(f"ZA tile 0 cell {row},{col} holds other than the group's block 0 entering the loop")
                seen.add((w[0].decl().name(), j))
        if len(seen) != 256:
            raise Unproved("ZA tile 0 holds other than every lane's block 0 entering the loop")
        m.x[7] = m.x[6]
        m.x[9] = aarch64.as_bv(m.x[9]) + 64 * (n - 1)
        m.x[8], m.x[10] = unknown(64), unknown(64)
        for r in range(32):
            m.v[r] = [chains(l, base)[r] for l in range(16)] if r < 8 else [unknown(32) for _ in range(16)]
        for row, cells in enumerate(m.za[0]):
            for col, cell in enumerate(cells):
                w = lane_word(cell)
                if w is None:
                    raise Unproved(f"ZA tile 0 cell {row},{col} holds no lane's bytes at the loop's first pass")
                o = aarch64.as_bv(w[1]) + 64 * (n - 1)
                m.za[0][row][col] = Concat(*[Select(w[0], o + j) for j in reversed(range(4))])
        if m.p is not None and 1 in m.p:
            m.p[1] = None
        return EXIT
    return run


def prove_one_group(obj):
    """The message kernel on one group, every length from 2 to 16 blocks in
    one run, its block loop by its proof: lane l's output is the last
    block's compression (end flag, its length) of the chain over the blocks
    before it, written as the chain the loop's proof establishes."""
    from z3 import If, BitVec as BV
    s, m, lanes, key, (n, counter, flags, start, end, step, last_len) = setup(obj)
    table_ = [[BV(f"chain{l}_{w}", 32) for w in range(8)] for l in range(16)]
    chains = lambda l, base: table_[l]
    m.summaries = {HEAD: summary(n, chains, lanes, key)}
    ends = induction.paths(m, s.entry, set())
    length = If(last_len == 0, BitVecVal(64, 32), ZeroExt(26, last_len))
    for e, how in ends:
        if how != "return":
            raise Unproved("a path stops other than at the kernel's return")
        if aarch64.concrete(e.x[0]) != 16:
            raise Unproved(f"returned {e.x[0]}, not 16")
        prove_sme2.prove_hybrid.check_abi(e)
        out = e.regions["out"]
        for l in range(16):
            want = spec.compress(chains(l, 0), block_words(lanes[l], 64 * (n - 1)), counter, length, ZeroExt(24, flags | end))[:8]
            if any(32 * l + j not in out.bytes for j in range(32)):
                raise Unproved(f"output {l} is not wholly written")
            compare(e, spec.words([out.bytes[32 * l + j] for j in range(32)]), want, f"lane {l}'s output")
    return f"one group, every length from 2 to 16 blocks: {len(ends)} paths, each lane's output the last block's compression of its chain"


def prove_every(obj, chunks=False):
    """The message kernel at every group count from 2 to 2^40 and every
    length from 3 to 16 blocks, in one induction over its group loop (as
    prove_sme2.prove_messages_every for one length at a time), the block
    loop by its proof: group K's lane l hashes the message table[16 K + l]
    points to, at counter + step K, its output the last block's compression
    of the chain over the blocks before it (the chain a function of the
    lane and the group's chunk)."""
    from z3 import If, BitVec as BV
    n = BitVecVal(16, 64) if chunks else BitVec("len_blocks", 64)
    g, k = BitVec("len_groups", 64), BitVec("len_k", 64)
    lanes = [Array(f"msg{l}", BitVecSort(64), BitVecSort(8)) for l in range(16)]
    key = byte_symbols("key", 32)
    regions = {}
    for l in range(16):
        r = Region(f"lane{l}", g * 1024, False, (lambda a: lambda o: Select(a, aarch64.as_bv(o)))(lanes[l]))
        r.symbolic_initial = True
        regions[f"lane{l}"] = r
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    regions["out"] = Region("out", g * 512, True, None)
    def entry(offset):
        """Table byte `offset`: byte offset % 8 of the pointer to group
        offset / 128's chunk for lane (offset % 128) / 8."""
        if isinstance(offset, int):
            group, within = offset // 128, offset % 128
            return (Ptr(f"lane{within // 8}", 1024 * group), within % 8)
        within = aarch64.concrete(aarch64.as_bv(offset) - 128 * k)
        if within is not None and 0 <= within < 128:
            return (Ptr(f"lane{within // 8}", simplify(k * 1024, som=True, bv_sort_ac=True)), within % 8)
        within = induction.forced(aarch64._machine[0], offset & 127)
        if within is None:
            raise Unproved("a table read whose lane the lengths leave open")
        first = simplify(aarch64.as_bv(offset) - within % 8, som=True, bv_sort_ac=True)
        return (Ptr(f"lane{within // 8}", simplify(LShR(first, 7) * 1024, som=True, bv_sort_ac=True)), within % 8)
    table = Region("table", g * 128, False, entry)
    table.symbolic_initial = True
    regions["table"] = table
    s = prove_sme2.SmeSetup(obj, "blake3_servil_sme2_hash16_chunks_512" if chunks else "blake3_servil_sme2_hash16_messages_512", regions)
    m = s.m
    m.lengths = {"len_groups", "len_k", "len_blocks", "counter", "step"}
    m.assumptions = [UGE(g, 2), ULE(g, 1 << 40), UGE(n, 2), ULE(n, 16)]
    counter = BitVec("counter", 64)
    flags, start, end, step = (BitVec(x, 8) for x in ("flags", "start", "end", "step"))
    last_len = BitVec("last_len", 6)
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    if chunks:
        m.x[3] = Concat(BitVec("junk", 32), BitVecVal(0, 8), end, start, flags)
        m.x[4], m.x[5] = Ptr("out", 0), g
    else:
        m.x[3] = Concat(BitVecVal(0, 18), last_len, step, BitVecVal(0, 8), end, start, flags)
        m.x[4], m.x[5], m.x[6] = Ptr("out", 0), g, n
    # Each lane's chain, as a function of the group's chunk (an array, so
    # canon fingerprints it as it does the messages).
    funcs = [[Array(f"chain{l}_{w}", BitVecSort(64), BitVecSort(32)) for w in range(8)] for l in range(16)]
    chains = lambda l, base: [Select(f, aarch64.as_bv(base)) for f in funcs[l]]
    current = [0]
    group_base = lambda: aarch64.as_bv(current[0] * 1024) if not isinstance(current[0], int) else BitVecVal(current[0] * 1024, 64)
    m.summaries = {HEAD: summary(n, chains, lanes, key, group_base)}
    head = prove_sme2.loop_head(obj, (lambda: prove_sme2.prove_chunks(obj, 3, 0, False, check=False)) if chunks else (lambda: prove_sme2.prove_messages(obj, 3, 3, 0, check=False)), 2)
    length = BitVecVal(64, 32) if chunks else If(last_len == 0, BitVecVal(64, 32), ZeroExt(26, last_len))
    def check_group(mach, kval):
        out = mach.regions["out"]
        keys = set()
        kv = aarch64.as_bv(kval) if not isinstance(kval, int) else BitVecVal(kval, 64)
        for l in range(16):
            c = counter + 16 * kv + l if chunks else counter + ZeroExt(56, step) * kv
            base = kv * 1024
            want = spec.compress(chains(l, base), block_words(lanes[l], base + 64 * (n - 1)), c, length, ZeroExt(24, flags | end))[:8]
            offs = [aarch64.key(kv * 512 + 32 * l + j) for j in range(32)]
            keys.update(offs)
            if any(o not in out.bytes for o in offs):
                raise Unproved(f"group output {l} is not wholly written")
            compare(mach, spec.words([out.bytes[o] for o in offs]), want, f"group output {l}")
        if set(out.bytes) != keys:
            raise Unproved("the group writes output bytes outside its own")
    s0 = induction.run_to(m, s.entry, head)
    if s0.regions["out"].bytes:
        raise Unproved("output written before the group loop")
    s1 = induction.run_to(induction.snapshot(s0), head, head)
    check_group(s1, 0)
    gen = induction.Generalized(s0, s1, k)
    current[0] = k
    def check(endm, how):
        if how == "head":
            check_group(endm, k)
            return
        if not aarch64.holds(endm, g == k + 1):
            raise Unproved("a path leaves the group loop before the last group")
        if aarch64.concrete(endm.x[0]) != 16:
            raise Unproved(f"returned {endm.x[0]}, not 16")
        check_group(endm, k)
        prove_sme2.prove_hybrid.check_abi(endm)
    kinds = induction.loop_step(gen, s0, k, list(s0.assumptions) + [ULT(k, g)], head, check)
    what = "the chunk kernel at every group count" if chunks else "every group count and every length from 2 to 16 blocks"
    return f"{what}: {kinds['head']} paths back to the group loop's head, {kinds['return']} out of it"


def main():
    obj = prove_sme2.assemble()
    print(prove(obj), flush=True)
    print(prove_one_group(obj), flush=True)
    print(prove_every(obj))


if __name__ == "__main__":
    main()
