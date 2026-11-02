"""The SME2 kernel with an integer lane (c/blake3_sme2_hybrid_aarch64.S,
18 chunks a group: 16 on SME2, 2 on the core's integer units), its block
loop proved once a pass, as prove_block_loop.py proves the plain SME2
kernel's. Each pass hashes one block of each SME2 chunk and two blocks of
one integer chunk: chunk 16 in passes 0 to 7, chunk 17 in passes 8 to 15,
so the loop is proved in two halves, each an induction over its passes.

    python3 tools/verify/prove_hybrid_loop.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from z3 import Array, BitVec, BitVecSort, BitVecVal, Concat, Extract, Select, ULE, UGE, ULT, ZeroExt, simplify
import aarch64
import induction
import prove_sme2
import prove_block_loop as pbl
from aarch64 import Ptr, Region, Unproved
from prove_hybrid import byte_symbols
import lean_spec as spec

HEAD = 0x208   # the block loop's head (blake3_servil_sme2x2_hash_chunks_512_block)
EXIT = 0x2930  # after its last pass in the loop (pass 14): pass 15 follows
INT = (1, 2, 3, 4, 5, 6, 11, 16)   # the integer chunk's state, words 0-7 (w1-w6, w11, w16)


def setup(obj, groups=1):
    """A group of 18 chunks, each an array read at any offset; with
    groups = 2, more to come."""
    lanes = [Array(f"msg{l}", BitVecSort(64), BitVecSort(8)) for l in range(18)]
    key = byte_symbols("key", 32)
    regions = {}
    for l in range(18):
        r = Region(f"input{l}", 1024, False, (lambda a: lambda o: Select(a, aarch64.as_bv(o)))(lanes[l]))
        r.symbolic_initial = True
        regions[f"input{l}"] = r
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    regions["out"] = Region("out", 32 * 18 * groups, True, None)
    table = Region("table", 8 * 18 * groups, False, None)
    for i in range(18 * groups):
        for j in range(8):
            table.bytes[8 * i + j] = (Ptr(f"input{i % 18}", 0), j)
    regions["table"] = table
    s = prove_sme2.HybridSetup(obj, regions)
    m = s.m
    counter = BitVec("counter", 64)
    flags, start, end = (BitVec(x, 8) for x in ("flags", "start", "end"))
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    m.x[3] = Concat(BitVec("junk", 32), BitVecVal(0, 8), end, start, flags)
    m.x[4], m.x[5] = Ptr("out", 0), BitVecVal(groups, 64)
    m.lengths = {"len_k"}
    m.assumptions = []
    return s, m, lanes, key, (counter, flags, start, end)


def comp(h, lane, off, counter, flags):
    return spec.compress(h, pbl.block_words(lane, off), counter, BitVecVal(64, 32), ZeroExt(24, flags))[:8]


def term_of_key(text, m):
    """A memory key (aarch64.key: an offset's normal form, printed) as a
    term again, its symbols the machine's length symbols."""
    import re
    from z3 import parse_smt2_string
    names = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)) & set(m.lengths)
    decls = {n: BitVec(n, 64) for n in names}
    return parse_smt2_string(f"(assert (= {text} (_ bv0 64)))", decls=decls)[0].arg(0)


def stored(m, at):
    """The 8 words the output holds at byte `at`, whatever form its offsets
    take (the code computes them from the pass), or None."""
    out = m.regions["out"]
    found = {}
    for o, b in out.bytes.items():
        oo = BitVecVal(o, 64) if isinstance(o, int) else term_of_key(o, m)
        for j in range(32):
            if j not in found and (o == at + j if isinstance(o, int) else aarch64.holds(m, oo == at + j)):
                found[j] = b
                break
    if len(found) != 32:
        return None
    return spec.words([found[j] for j in range(32)])


def integer_state(m):
    return [Extract(31, 0, m.x[r]) if m.x[r].size() == 64 else m.x[r] for r in INT]


def half_a(obj, groups=1):
    """Passes 0 to 7: pass 0 checked as it runs, 1 to 7 by induction."""
    from z3 import BitVec as BV
    s, m, lanes, key, (counter, flags, start, end) = setup(obj, groups)
    s0 = induction.run_to(m, s.entry, HEAD)
    s1 = induction.run_to(induction.snapshot(s0), HEAD, HEAD)
    s2 = induction.run_to(induction.snapshot(s1), HEAD, HEAD)
    kw = spec.words(key)
    for l in range(16):
        pbl.compare(s0, [s0.v[i][l] for i in range(8)], kw, f"lane {l} entering the loop (the key)")
        pbl.compare(s1, [s1.v[i][l] for i in range(8)], comp(kw, lanes[l], 0, counter + l, flags | start), f"lane {l} after pass 0")
    c = comp(comp(kw, lanes[16], 0, counter + 16, flags | start), lanes[16], 64, counter + 16, flags)
    pbl.compare(s1, integer_state(s1), c, "chunk 16 after pass 0 (its blocks 0 and 1)")
    print("pass 0: each SME2 chunk's block 0 and chunk 16's blocks 0 and 1, under the key, with the start flag", flush=True)
    k = BV("len_k", 64)
    gen = pbl.BlockGeneralized(s1, s2, k)
    K = k + 1
    passes = 0
    while True:
        passes += 1
        mk = gen.machine(s1, k)
        mk.assumptions = [ULE(k, 6)]   # pass K = 1 + k, 1 to 7
        mk.lengths = {"len_k"}
        h = [[mk.v[i][l] for i in range(8)] for l in range(16)]
        hi = integer_state(mk)
        ends = induction.paths(mk, HEAD, {HEAD, EXIT})
        forget = set()
        import canon
        for e, _ in ends:
            canon.set_context(e.assumptions, e.lengths)
            forget.update(b for b in induction.mismatches(e, gen, k + 1)
                          if not (b[0] == "v" and b[1] < 8) and not (b[0] == "x" and b[1] in INT))
            canon.set_context([], set())
        print(f"  pass {passes}: {len(ends)} paths, {len(forget)} positions left unknown {sorted(map(str, forget))[:8]}", flush=True)
        if not forget:
            break
        for pos in forget:
            gen.forget(pos)
    for e, _ in ends:
        if e.stopped_at != HEAD:
            raise Unproved("half A leaves the loop")
        last = aarch64.holds(e, K == 7)
        if not last and not aarch64.holds(e, K != 7):
            raise Unproved("a path in half A leaves open whether its pass is chunk 16's last")
        for l in range(16):
            pbl.compare(e, [e.v[i][l] for i in range(8)], comp(h[l], lanes[l], 64 * K, counter + l, flags), f"lane {l} after pass K")
        c = comp(comp(hi, lanes[16], 128 * K, counter + 16, flags), lanes[16], 128 * K + 64, counter + 16, flags | end if last else flags)
        pbl.compare(e, integer_state(e), c, "chunk 16 after pass K (its blocks 2K and 2K + 1)")
        if not last and len(e.regions["out"].bytes) != len(mk.regions["out"].bytes):
            raise Unproved("a pass of half A before pass 7 writes the output")
        if last:
            if len(e.regions["out"].bytes) != 32 + len(mk.regions["out"].bytes):
                raise Unproved("pass 7 writes the output beyond chunk 16's chaining value")
            got = stored(e, 32 * 16)
            if got is None:
                raise Unproved("pass 7 leaves chunk 16's chaining value unstored")
            pbl.compare(e, got, c, "chunk 16's chaining value, stored at pass 7")
    gen.entry = entry_of(s0)
    return gen, s1, f"half A: {len(ends)} paths of passes 1 to 7, each the standard's compressions (chunk 16 stored at pass 7)"


def half_b(obj, gen_a, s1_a, groups=1):
    """Passes 8 to 14: pass 8 run from half A's invariant at pass 8 (every
    position it leaves unknown fresh), switching to chunk 17; 9 to 14 by
    induction, out of the loop after 14."""
    from z3 import BitVec as BV
    s, m, lanes, key, (counter, flags, start, end) = setup(obj, groups)
    s8 = gen_a.machine(s1_a, BitVecVal(7, 64))   # K = 1 + 7
    s8.assumptions, s8.lengths = [], {"len_k"}
    h8 = [[s8.v[i][l] for i in range(8)] for l in range(16)]
    s9 = induction.run_to(induction.snapshot(s8), HEAD, HEAD)
    s10 = induction.run_to(induction.snapshot(s9), HEAD, HEAD)
    kw = spec.words(key)
    for l in range(16):
        pbl.compare(s9, [s9.v[i][l] for i in range(8)], comp(h8[l], lanes[l], 64 * 8, counter + l, flags), f"lane {l} after pass 8")
    c = comp(comp(kw, lanes[17], 0, counter + 17, flags | start), lanes[17], 64, counter + 17, flags)
    pbl.compare(s9, integer_state(s9), c, "chunk 17 after pass 8 (its blocks 0 and 1)")
    print("pass 8: each SME2 chunk's block 8, and chunk 17's blocks 0 and 1 under the key, with the start flag", flush=True)
    k = BV("len_k", 64)
    gen = pbl.BlockGeneralized(s9, s10, k)
    K = k + 9
    passes = 0
    while True:
        passes += 1
        mk = gen.machine(s9, k)
        mk.assumptions = [ULE(k, 5)]   # pass K = 9 + k, 9 to 14
        mk.lengths = {"len_k"}
        h = [[mk.v[i][l] for i in range(8)] for l in range(16)]
        hi = integer_state(mk)
        ends = induction.paths(mk, HEAD, {HEAD, EXIT})
        forget = set()
        import canon
        for e, _ in ends:
            canon.set_context(e.assumptions, e.lengths)
            forget.update(b for b in induction.mismatches(e, gen, k + 1)
                          if not (b[0] == "v" and b[1] < 8) and not (b[0] == "x" and b[1] in INT))
            canon.set_context([], set())
        print(f"  pass {passes}: {len(ends)} paths, {len(forget)} positions left unknown {sorted(map(str, forget))[:8]}", flush=True)
        if not forget:
            break
        for pos in forget:
            gen.forget(pos)
    for e, _ in ends:
        if len(e.regions["out"].bytes) != len(mk.regions["out"].bytes):
            raise Unproved("a pass of half B writes the output")
        if e.stopped_at == EXIT and not aarch64.holds(e, K == 14):
            raise Unproved("the loop exits before pass 14")
        for l in range(16):
            pbl.compare(e, [e.v[i][l] for i in range(8)], comp(h[l], lanes[l], 64 * K, counter + l, flags), f"lane {l} after pass K")
        c = comp(comp(hi, lanes[17], 128 * (K - 8), counter + 17, flags), lanes[17], 128 * (K - 8) + 64, counter + 17, flags)
        pbl.compare(e, integer_state(e), c, "chunk 17 after pass K (its blocks 2K - 16 and 2K - 15)")
    return gen, s9, f"half B: {len(ends)} paths of passes 9 to 14, each the standard's compressions, out of the loop after 14"


def changes(gen_a, gen_b):
    """What the loop changes, by its two inductions: general registers,
    vector registers, predicates, and stack bytes."""
    xs = sorted({r for g in (gen_a, gen_b) for r, v in g.x.items() if isinstance(v, tuple)})
    stack = sorted({o for g in (gen_a, gen_b) for o, b in g.mem.get("stack", {}).items() if induction.generalized(b)})
    preds = sorted({q for g in (gen_a, gen_b) for q, v in (g.p or {}).items() if v is None})
    return xs, stack, preds


def summary(gen_a, gen_b, entry, chains, chain16, chain17, lanes, key, group=lambda: 0):
    """The block loop by its two halves' proofs, from a group's first
    arrival (pass 0) to its exit after pass 14. It checks the loop's whole
    entry state is half A's, moved to group `group()`'s chunks, table
    entries, counter and output: the key in z0-z7, each SME2 chunk's
    block 0 in ZA, x7, x9 and x12-x15 as on entry, x0 and the stack slots
    the loop reads (`entry`, half A's values at group 0, with a function
    moving them to group K). It leaves z0-z7 the SME2 chunks' chains over
    blocks 0 to 14; chunk 16's chaining value stored at output 16, as pass
    7 stores it; chunk 17's chain over its blocks 0 to 13 in its state
    registers, its pointer and counter as pass 8 sets them; block 15 in ZA;
    and the rest the loop changes unknown."""
    from z3 import BitVec as BV
    xs, stack, preds = changes(gen_a, gen_b)
    fresh = [0]
    def unknown(size):
        fresh[0] += 1
        return BV(f"after_hybrid_loop{fresh[0]}", size)
    def same_ptr(m, got, want):
        return isinstance(got, Ptr) and isinstance(want, Ptr) and got.region == want.region and \
            aarch64.holds(m, aarch64.as_bv(got.offset) == aarch64.as_bv(want.offset))
    def run(m):
        K = aarch64.as_bv(group())
        base = K * 1024
        if aarch64.concrete(m.x[7]) != 0:
            raise Unproved("the block loop entered other than at its first pass")
        for r, v in ((9, 64), (12, 0), (13, 4), (14, 8), (15, 12)):
            if not aarch64.holds(m, aarch64.as_bv(m.x[r]) == v):
                raise Unproved(f"x{r} entering the block loop differs from half A's")
        sp = m.x["sp"]
        for where, want in entry(K):
            x0 = isinstance(where, str)
            got = m.x[0] if x0 else m.load(sp + where, 8 if where < 0xc8 else 4)
            ok = same_ptr(m, got, want) if isinstance(want, Ptr) else \
                not isinstance(got, Ptr) and aarch64.holds(m, aarch64.as_bv(got) == want)
            if not ok:
                raise Unproved(f"{where if x0 else hex(where)} entering the block loop differs from half A's")
        kw = spec.words(key)
        for l in range(16):
            pbl.compare(m, [m.v[i][l] for i in range(8)], kw, f"lane {l} entering the block loop (the key)")
        names = [a.decl().name() for a in lanes[:16]]
        seen = set()
        for row, cells in enumerate(m.za[0]):
            for col, cell in enumerate(cells):
                w = pbl.lane_word(cell)
                if w is None or w[0].decl().name() not in names:
                    raise Unproved(f"ZA tile 0 cell {row},{col} holds no SME2 chunk's bytes entering the loop")
                j = aarch64.concrete(simplify(aarch64.as_bv(w[1]) - base))
                if j is None:
                    j = next((c for c in range(0, 64, 4) if aarch64.holds(m, aarch64.as_bv(w[1]) == base + c)), None)
                if j is None:
                    raise Unproved(f"ZA tile 0 cell {row},{col} holds other than the group's block 0 entering the loop")
                seen.add((w[0].decl().name(), j))
        if len(seen) != 256:
            raise Unproved("ZA tile 0 holds other than every SME2 chunk's block 0 entering the loop")
        # Chunk 17's pointer and counter as pass 8 sets them (table entry
        # 17, the counter saved at sp + 0xa8, plus 17), the pointer two
        # blocks a pass on to pass 15's blocks 14 and 15.
        table17 = m.load(m.x[0] + 8 * 17, 8)
        if not isinstance(table17, Ptr):
            raise Unproved("table entry 17 is no pointer entering the loop")
        saved = m.load(sp + 0xa8, 8)
        out = m.load(sp + 0xb8, 8)
        for r in xs:
            m.x[r] = unknown(64)
        m.x[7] = BitVecVal(15, 64)
        m.x[9] = BitVecVal(64 * 16, 64)
        m.x[27] = Ptr(table17.region, aarch64.as_bv(table17.offset) + 128 * 7)
        m.x[28] = aarch64.as_bv(saved) + 17
        for w, r in enumerate(INT):
            m.x[r] = ZeroExt(32, chain17(base)[w])
        for r in range(32):
            m.v[r] = [chains(l, base)[r] for l in range(16)] if r < 8 else [unknown(32) for _ in range(16)]
        for row, cells in enumerate(m.za[0]):
            for col, cell in enumerate(cells):
                w = pbl.lane_word(cell)
                o = aarch64.as_bv(w[1]) + 64 * 15
                m.za[0][row][col] = Concat(*[Select(w[0], o + j) for j in reversed(range(4))])
        for q in preds:
            m.p[q] = None
        st = m.regions["stack"]
        for o in stack:
            st.bytes[o] = unknown(8)
        c16 = chain16(base)
        for w in range(8):
            m.store(out + 32 * 16 + 4 * w, c16[w], 4)
        return EXIT
    return run


def entry_of(s0):
    """Half A's entry state at group 0 (x0 and the stack slots the loop
    reads), as a function moving it to group K: the table 144 bytes a
    group on, the counter 18 a group, the output 576 bytes; the key and
    the flags words the same."""
    sp = s0.x["sp"]
    at0 = {"x0": s0.x[0]}
    for o in (0xa0, 0xa8, 0xb8, 0xc8, 0xcc, 0xd0):
        at0[o] = s0.load(sp + o, 8 if o < 0xc8 else 4)
    def moved(K):
        def on(v, by):
            return Ptr(v.region, aarch64.as_bv(v.offset) + by) if isinstance(v, Ptr) else aarch64.as_bv(v) + by
        return [("x0", on(at0["x0"], 144 * K)), (0xa0, at0[0xa0]), (0xa8, on(at0[0xa8], 18 * K)),
                (0xb8, on(at0[0xb8], 576 * K)), (0xc8, at0[0xc8]), (0xcc, at0[0xcc]), (0xd0, at0[0xd0])]
    return moved


def prove_one_group(obj, gen_a, gen_b):
    """The hybrid kernel on one group, its block loop by its proofs: each
    output the standard's chaining value of its chunk, its last block's
    compression of the chain before it."""
    from z3 import BitVec as BV
    s, m, lanes, key, (counter, flags, start, end) = setup(obj)
    tab = [[BV(f"chain{l}_{w}", 32) for w in range(8)] for l in range(16)]
    c16 = [BV(f"chain16_{w}", 32) for w in range(8)]
    c17 = [BV(f"chain17_{w}", 32) for w in range(8)]
    m.summaries = {HEAD: summary(gen_a, gen_b, gen_a.entry, lambda l, b: tab[l], lambda b: c16, lambda b: c17, lanes, key)}
    ends = induction.paths(m, s.entry, set())
    for e, how in ends:
        if how != "return" or aarch64.concrete(e.x[0]) != 16:
            raise Unproved("a path ends other than at the kernel's return of 16")
        prove_sme2.prove_hybrid.check_abi(e)
        for l in range(16):
            got = stored(e, 32 * l)
            if got is None:
                raise Unproved(f"output {l} is not wholly written")
            pbl.compare(e, got, comp(tab[l], lanes[l], 64 * 15, counter + l, flags | end), f"output {l}")
        pbl.compare(e, stored(e, 32 * 16), c16, "output 16 (chunk 16's chaining value)")
        want17 = comp(comp(c17, lanes[17], 64 * 14, counter + 17, flags), lanes[17], 64 * 15, counter + 17, flags | end)
        got17 = stored(e, 32 * 17)
        if got17 is None:
            raise Unproved("output 17 is not wholly written")
        pbl.compare(e, got17, want17, "output 17 (chunk 17's last two blocks)")
    return f"one group: {len(ends)} paths, every output its chunk's chaining value"


def prove_every(obj, gen_a, gen_b):
    """The hybrid kernel at every group count from 2 to 2^40, in one
    induction over its group loop, the block loop by its proofs: group K's
    chunk l hashes the chunk table[18 K + l] points to, at counter +
    18 K + l, its output the standard's chaining value (the chain before
    its last block, or chunk 16's whole value, a function of the chunk)."""
    from z3 import BitVec as BV, URem, UDiv
    g, k = BitVec("len_groups", 64), BitVec("len_k", 64)
    lanes = [Array(f"msg{l}", BitVecSort(64), BitVecSort(8)) for l in range(18)]
    key = byte_symbols("key", 32)
    regions = {}
    for l in range(18):
        r = Region(f"lane{l}", g * 1024, False, (lambda a: lambda o: Select(a, aarch64.as_bv(o)))(lanes[l]))
        r.symbolic_initial = True
        regions[f"lane{l}"] = r
    regions["key"] = Region("key", 32, False, lambda o: key[o])
    regions["out"] = Region("out", g * 576, True, None)
    def entry(offset):
        """Table byte `offset`: byte offset % 8 of the pointer to group
        offset / 144's chunk offset % 144 / 8."""
        if isinstance(offset, int):
            group, within = offset // 144, offset % 144
            return (Ptr(f"lane{within // 8}", 1024 * group), within % 8)
        o = aarch64.as_bv(offset)
        within = aarch64.concrete(o - 144 * k)
        if within is not None and 0 <= within < 144:
            return (Ptr(f"lane{within // 8}", simplify(k * 1024, som=True, bv_sort_ac=True)), within % 8)
        within = induction.forced(aarch64._machine[0], URem(o, 144))
        if within is None:
            raise Unproved("a table read whose chunk the lengths leave open")
        return (Ptr(f"lane{within // 8}", simplify(UDiv(o - within, 144) * 1024, som=True, bv_sort_ac=True)), within % 8)
    table = Region("table", g * 144, False, entry)
    table.symbolic_initial = True
    regions["table"] = table
    s = prove_sme2.HybridSetup(obj, regions)
    m = s.m
    m.lengths = {"len_groups", "len_k", "counter"}
    m.assumptions = [UGE(g, 2), ULE(g, 1 << 40)]
    counter = BitVec("counter", 64)
    flags, start, end = (BitVec(x, 8) for x in ("flags", "start", "end"))
    m.x[0], m.x[1], m.x[2] = Ptr("table", 0), Ptr("key", 0), counter
    m.x[3] = Concat(BitVec("junk", 32), BitVecVal(0, 8), end, start, flags)
    m.x[4], m.x[5] = Ptr("out", 0), g
    funcs = [[Array(f"chain{l}_{w}", BitVecSort(64), BitVecSort(32)) for w in range(8)] for l in range(18)]
    chains = lambda l, base: [Select(f, aarch64.as_bv(base)) for f in funcs[l]]
    current = [0]
    m.summaries = {HEAD: summary(gen_a, gen_b, gen_a.entry, chains, lambda b: chains(16, b), lambda b: chains(17, b), lanes, key, lambda: current[0])}
    head = prove_sme2.loop_head(obj, lambda: prove_sme2.prove_chunks_x2(obj, 3, check=False), 2)
    def check_group(mach, kval):
        out = mach.regions["out"]
        keys = set()
        kv = aarch64.as_bv(kval) if not isinstance(kval, int) else BitVecVal(kval, 64)
        base = kv * 1024
        for l in range(18):
            c = counter + 18 * kv + l
            if l == 16:
                want = chains(16, base)
            elif l == 17:
                want = comp(comp(chains(17, base), lanes[17], base + 64 * 14, c, flags), lanes[17], base + 64 * 15, c, flags | end)
            else:
                want = comp(chains(l, base), lanes[l], base + 64 * 15, c, flags | end)
            offs = [aarch64.key(kv * 576 + 32 * l + j) for j in range(32)]
            keys.update(offs)
            if any(o not in out.bytes for o in offs):
                raise Unproved(f"group output {l} is not wholly written")
            pbl.compare(mach, spec.words([out.bytes[o] for o in offs]), want, f"group output {l}")
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
    return f"every group count: {kinds['head']} paths back to the group loop's head, {kinds['return']} out of it"


def main():
    obj = prove_sme2.assemble_hybrid()
    gen_a, s1_a, what = half_a(obj)
    print(what, flush=True)
    gen_b, s9, what = half_b(obj, gen_a, s1_a)
    print(what, flush=True)
    print(prove_one_group(obj, gen_a, gen_b), flush=True)
    print(prove_every(obj, gen_a, gen_b))


if __name__ == "__main__":
    main()
