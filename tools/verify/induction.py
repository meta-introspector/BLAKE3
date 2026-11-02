"""Proofs for every count: induction over a loop (tools/verify/README.md).

A kernel or Rust path proved at each count up to a bound leaves the counts
past it unproved. Here a loop is proved for every count by induction: two
successive arrivals at its head are generalized into one state at a
symbolic iteration K (each position equal in both kept, each that differs
by a constant becoming start + K x difference, each other one an unknown);
then every path from that state is run, splitting where the lengths leave
a branch open, and each must either return to the head in the state for
K + 1 or leave the loop with its output right. Positions left unknown are
sound: a path whose result depends on one fails its comparison.
"""

import copy

import canon

from z3 import BitVec, BitVecVal, ZeroExt, Extract, Not, ULE, UGE, ULT, And, simplify, is_bv_value, is_bv, is_const, substitute, eq
import aarch64
from aarch64 import Ptr, Unproved, Undecided


def snapshot(m):
    """An independent copy of a machine's state (its code shared)."""
    c = copy.copy(m)
    c.x = dict(m.x)
    c.v = {k: list(v) for k, v in m.v.items()}
    c.regions = {}
    for name, r in m.regions.items():
        r2 = copy.copy(r)
        r2.bytes = dict(r.bytes)
        r2.offset_terms = dict(getattr(r, "offset_terms", {}))
        c.regions[name] = r2
    c.assumptions = list(m.assumptions)
    c.lengths = set(m.lengths)
    if hasattr(m, "k"):
        c.k = dict(m.k)
    if getattr(m, "za", None) is not None:
        c.za = [[list(row) for row in tile] for tile in m.za]
    if hasattr(m, "p"):
        c.p = {k: (list(v) if v is not None else None) for k, v in m.p.items()}
    return c


def _can_reach(code, heads):
    """The PCs in `code` from which control can still reach `heads`."""
    import re
    preds = {}
    for pc, item in code.items():
        mnem, ops = item[0], item[1]
        nxts = []
        if len(item) > 2:
            if mnem not in ("ret", "jmp") and item[2] in code:
                nxts.append(item[2])
        else:
            if mnem not in ("ret", "b") and (pc + 4) in code:
                nxts.append(pc + 4)
        for op in ops:
            for tok in re.findall(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]+\b", op):
                try:
                    t = int(tok, 16)
                    if t in code:
                        nxts.append(t)
                except ValueError:
                    pass
        for s in nxts:
            preds.setdefault(s, []).append(pc)
    reach = set(heads)
    stack = list(heads)
    while stack:
        cur = stack.pop()
        for p in preds.get(cur, ()):
            if p not in reach:
                reach.add(p)
                stack.append(p)
    return reach


def paths(m, pc, heads):
    """Every path from `pc`: (machine, "head" or "return"), splitting where
    a branch on lengths is open. `heads`: the pcs at which a path stops."""
    defer_exits = getattr(m, "_defer_exits", None)
    if defer_exits is not None:
        del m._defer_exits
    reach = _can_reach(m.code, heads) if (defer_exits is not None and heads and not getattr(m, "summaries", None)) else None
    work, done = [(m, pc, True)], []
    while work:
        m, pc, first = work.pop()
        while True:
            if pc in heads and not first:
                m.stopped_at = pc
                done.append((m, "head"))
                break
            if reach is not None and not first and pc not in reach:
                defer_exits.append((m, pc))
                break
            first = False
            # Code taken by its proved effect (a loop by its induction): the
            # summary sets the state and gives the address to go on from.
            summary = getattr(m, "summaries", {}).get(pc)
            if summary is not None:
                pc = summary(m)
                continue
            if pc not in m.code:
                raise Unproved(f"execution reached {pc:#x}, outside the code under proof")
            mnem, ops = m.code[pc][:2]
            aarch64.EXECUTED.add((mnem, tuple(ops)))
            try:
                # A machine with its own stepper (x86.py's) steps itself;
                # the AArch64 machines take the path they always have.
                nxt = m.step_at(pc) if hasattr(m, "step_at") else aarch64.step(m, pc, mnem, ops)
            except Undecided as e:
                for answer in (e.cond, Not(e.cond)):
                    b = snapshot(m)
                    b.assumptions.append(answer)
                    work.append((b, pc, True))      # resume at the split, without stopping
                break
            m.steps += 1
            if nxt == "ret":
                if hasattr(m, "step_at"):
                    done.append((m, "return"))
                    break
                to = m.x[30]
                if isinstance(to, Ptr) and to.region == "return":
                    done.append((m, "return"))
                    break
                if not (isinstance(to, Ptr) and to.region == "code"):
                    raise Unproved(f"return to {to}")
                nxt = to.offset
            pc = nxt if nxt is not None else pc + 4
    return done


def run_to(m, pc, head):
    """Run `m` from `pc` to its next arrival at `head` (one path only)."""
    ends = paths(m, pc, {head})
    if len(ends) != 1 or ends[0][1] != "head":
        raise Unproved("the run to the loop head splits or returns")
    return ends[0][0]


def generalized(b):
    """Whether an invariant's memory byte is one the induction generalized
    (unknown, None, or a step), as against a value or a pointer's byte."""
    return b is None or (isinstance(b, tuple) and isinstance(b[0], str))


class Generalized:
    """Two arrivals at a loop head made one state at iteration K."""
    def __init__(self, s0, s1, k):
        self.k = k
        self.wild = 0
        self.x = {r: self.unify(s0.x[r], s1.x[r]) for r in s0.x}
        self.v = {r: [self.unify(a, b) for a, b in zip(s0.v[r], s1.v[r])] for r in s0.v}
        self.masks = ({r: self.unify(s0.k[r], s1.k[r]) for r in s0.k}
                      if hasattr(s0, "k") else None)
        # SME2's state: the ZA tiles and the predicates.
        self.za = None
        if getattr(s0, "za", None) is not None and getattr(s1, "za", None) is not None:
            self.za = [[[self.unify(a, b) for a, b in zip(r0, r1)] for r0, r1 in zip(t0, t1)]
                       for t0, t1 in zip(s0.za, s1.za)]
        self.p = None
        if hasattr(s0, "p") and hasattr(s1, "p"):
            self.p = {k: (s0.p[k] if s0.p[k] == s1.p.get(k) and all(isinstance(x, bool) for x in (s0.p[k] or [])) else None)
                      for k in s0.p}
        self.mem = {}
        for name, r0 in s0.regions.items():
            if not r0.writable or name not in s1.regions:
                continue
            r1 = s1.regions[name]
            self.mem[name] = {}
            for off in set(r0.bytes) | set(r1.bytes):
                if off in r0.bytes and off in r1.bytes and same_term(r0.bytes[off], r1.bytes[off]):
                    self.mem[name][off] = r0.bytes[off]
                elif off in r0.bytes or off in r1.bytes:
                    self.mem[name][off] = None      # differs: unknown at K
        # 8-byte words that differ by a constant (a spilled counter or pointer).
        for name, mem in self.mem.items():
            r0, r1 = s0.regions[name], s1.regions[name]
            for off in [o for o, b in mem.items() if b is None and isinstance(o, int) and o % 8 == 0]:
                w0 = word(r0, off)
                if w0 is None:
                    continue
                w1 = word(r1, off)
                if w1 is None:
                    continue
                g = self.unify(w0, w1, wildcard=False)
                if g is not None:
                    for i in range(8):
                        mem[off + i] = ("word", g, i)
            # The same reconstruction at symbolic offsets (an entry stack
            # whose residue is a length symbol): group the differing byte
            # keys by the constant differences of their offset terms.
            terms = {o: r0.offset_terms[o] for o, b in mem.items()
                     if b is None and not isinstance(o, int) and o in r0.offset_terms and o in r1.offset_terms}
            # Round each differing byte down to its aligned word. A word
            # whose first bytes agree is not itself among the differing
            # keys, so grouping differing keys alone would start mid-word.
            bases = {}
            for term in terms.values():
                residue = forced(s0, aarch64.as_bv(term) & 7)
                if residue is not None:
                    base = simplify(aarch64.as_bv(term) - residue, som=True, bv_sort_ac=True)
                    bases[aarch64.key(base)] = base
            for base_key, base in bases.items():
                w0, w1 = symbolic_word(r0, base), symbolic_word(r1, base)
                if w0 is None or w1 is None:
                    continue
                g = self.unify(w0, w1, wildcard=False)
                if g is not None:
                    for i in range(8):
                        mem[aarch64.key(base + i)] = ("word", g, i, base_key)

    def forget(self, pos):
        """Make a position unknown in the invariant."""
        kind = pos[0]
        if kind == "x":
            self.x[pos[1]] = ("unknown", 64)
        elif kind == "v":
            self.v[pos[1]][pos[2]] = ("unknown", 32)
        elif kind == "mask":
            self.masks[pos[1]] = ("unknown", 16)
        elif kind == "za":
            self.za[pos[1]][pos[2]][pos[3]] = ("unknown", 32)
        elif kind == "p":
            self.p[pos[1]] = None
        elif kind == "mem":
            self.mem[pos[1]][pos[2]] = None
        elif kind == "word":
            mem = self.mem[pos[1]]
            for off, b in list(mem.items()):
                if isinstance(b, tuple) and b[0] == "word" and (b[3] if len(b) > 3 else off - b[2]) == pos[2]:
                    mem[off] = None
            if isinstance(pos[2], int):
                for i in range(8):
                    if pos[2] + i in mem:
                        mem[pos[2] + i] = None

    def fresh(self, size):
        self.wild += 1
        return BitVec(f"unknown{self.wild}", size)

    def unify(self, a, b, wildcard=True):
        """The general form of a pair: kept, a + K x d, or an unknown."""
        if isinstance(a, Ptr) and isinstance(b, Ptr) and a.region == b.region:
            if same_ptr(a, b):
                return a
            if isinstance(a.offset, int) and isinstance(b.offset, int):
                return ("ptr", a.region, a.offset, b.offset - a.offset)
            difference = constant_difference(aarch64.as_bv(a.offset), aarch64.as_bv(b.offset))
            if difference is not None:
                return ("ptr", a.region, a.offset, difference)
            return None if not wildcard else ("unknown", 64)
        if isinstance(a, Ptr) or isinstance(b, Ptr):
            return ("unknown", 64) if wildcard else None
        # A hash's state (BLAKE3's xors and rotations) is kept only when it is
        # the same term in both: it is never a step.
        if mixes(a) or mixes(b):
            return a if eq(a, b) else (("unknown", a.size()) if wildcard else None)
        if same_term(a, b):
            return a
        d = constant_difference(a, b)
        if d is not None:
            return ("step", a, d)
        # A difference of parameters alone (a counter's step): the same each
        # iteration, if the step from K to K + 1 proves it.
        diff = simplify(b - a)
        if not canon.has_data(diff) and not mixes(diff):
            return ("step", a, diff)
        return ("unknown", a.size()) if wildcard else None

    def at(self, kval):
        """The value forms at iteration `kval` (a 64-bit term)."""
        def inst(g, size=64):
            if isinstance(g, tuple):
                if g[0] == "ptr":
                    return Ptr(g[1], simplify(aarch64.as_bv(g[2]) + kval * g[3], som=True, bv_sort_ac=True))
                if g[0] == "step":
                    a, d = g[1], g[2]
                    kk = kval if a.size() == 64 else Extract(a.size() - 1, 0, kval)
                    return simplify(a + kk * d, som=True, bv_sort_ac=True)
                if g[0] == "unknown":
                    return None
            # A caller can state a counter's halves directly in terms of
            # K, then prove the initial state and the step against them.
            return substitute(g, (self.k, kval)) if is_bv(g) else g
        return inst

    def machine(self, template, kval):
        """A machine at the head, iteration `kval`, unknowns as fresh symbols."""
        m = aarch64_snapshot_blank(template)
        inst = self.at(kval)
        for r, g in self.x.items():
            v = inst(g)
            m.x[r] = v if v is not None else self.fresh(64)
        for r, lanes in self.v.items():
            m.v[r] = [(inst(g) if inst(g) is not None else self.fresh(32)) for g in lanes]
        if self.masks is not None:
            m.k = {r: (inst(g) if inst(g) is not None else self.fresh(16)) for r, g in self.masks.items()}
        for name, mem in self.mem.items():
            region = m.regions[name]
            region.bytes = {}
            words = {}
            for off, b in mem.items():
                if isinstance(b, tuple) and b[0] == "word":
                    w = words.setdefault(b[3] if len(b) > 3 else off - b[2], inst(b[1]))
                    region.bytes[off] = Extract(8 * b[2] + 7, 8 * b[2], w) if not isinstance(w, Ptr) else (w, b[2])
                elif b is not None:
                    region.bytes[off] = b
                # unknown bytes stay unwritten: a read of one fails the proof
        m.nzcv = None
        if hasattr(m, "flags"):
            m.flags = None
        if self.za is not None:
            m.za = [[[(inst(g) if inst(g) is not None else self.fresh(32)) for g in row] for row in tile] for tile in self.za]
        if self.p is not None:
            m.p = {k: (list(v) if v is not None else None) for k, v in self.p.items()}
        return m


_mixes = {}


def mixes(t):
    """Whether `t` holds an xor or a rotation (BLAKE3's mixing)."""
    from z3 import Z3_OP_BXOR, Z3_OP_ROTATE_LEFT, Z3_OP_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT, Z3_OP_EXT_ROTATE_RIGHT
    k = t.get_id()
    r = _mixes.get(k)
    if r is None:
        kinds = (Z3_OP_BXOR, Z3_OP_ROTATE_LEFT, Z3_OP_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT, Z3_OP_EXT_ROTATE_RIGHT)
        r = _mixes[k] = (t, t.decl().kind() in kinds or any(mixes(c) for c in t.children()))
    return r[1]


def constant_difference(a, b):
    """`b - a` when it is the same for every value of the symbols, else None:
    by simplification, or by the solver (byte shuffles of a sum hide a
    constant difference from the simplifier). Values at random points first
    settle cheaply most differences that are not constant; terms holding
    BLAKE3's mixing are not tried."""
    import random
    from z3 import Solver, unsat, substitute, BitVecVal
    d = aarch64.concrete(b - a)
    if d is not None:
        return d
    # Positions holding a hash's state (its xors and rotations) change by no
    # constant: only data moved or added to is worth the solver.
    if mixes(a) or mixes(b):
        return None
    diff = simplify(b - a)
    syms = {}
    canon.symbols_of(diff, syms, set())
    values = set()
    for r in range(3):
        point = [(v, canon.sample(n, v, ("difference", r))) for n, v in syms.items()]
        values.add(simplify(substitute(diff, *point)).as_long())
        if len(values) > 1:
            return None
    value = values.pop()
    s = Solver()
    s.add(diff != BitVecVal(value, diff.size()))
    return value if s.check() == unsat else None


def aarch64_snapshot_blank(template):
    m = snapshot(template)
    return m


def word(r, off):
    parts = [r.bytes.get(off + i) for i in range(8)]
    if any(p is None for p in parts):
        return None
    if all(isinstance(p, tuple) for p in parts):
        return parts[0][0] if all(p[1] == i and p[0] is parts[0][0] for i, p in enumerate(parts)) else None
    if any(isinstance(p, tuple) for p in parts):
        return None
    from z3 import Concat
    return simplify(Concat(*reversed(parts)))


def symbolic_word(r, base):
    """The 8-byte word at a symbolic offset term, as word() at an int."""
    parts = [r.bytes.get(aarch64.key(base + i)) for i in range(8)]
    if any(p is None for p in parts):
        return None
    if all(isinstance(p, tuple) for p in parts):
        return parts[0][0] if all(p[1] == i and p[0] is parts[0][0] for i, p in enumerate(parts)) else None
    if any(isinstance(p, tuple) for p in parts):
        return None
    from z3 import Concat
    return simplify(Concat(*reversed(parts)))


def same_term(a, b):
    if isinstance(a, tuple) or isinstance(b, tuple):
        return a == b if not (isinstance(a, tuple) and isinstance(b, tuple)) else (a[1] == b[1] and same_ptr(a[0], b[0]))
    if isinstance(a, Ptr) or isinstance(b, Ptr):
        return isinstance(a, Ptr) and isinstance(b, Ptr) and same_ptr(a, b)
    return eq(a, b) or eq(simplify(a), simplify(b))


def same_ptr(a, b):
    return a.region == b.region and aarch64.key(a.offset) == aarch64.key(b.offset)


_forced = {}


def forced(m, term):
    """`term`'s value when the run's assumptions force one (a model's value,
    and no other possible), else None."""
    from z3 import Solver, sat
    import terms
    c = aarch64.concrete(term)
    if c is not None:
        return c
    rel = aarch64.relevant_assumptions(m.assumptions, term)
    k = (tuple(a.get_id() for a in rel), term.get_id())
    if k in _forced:
        return _forced[k]
    for a in rel:
        terms.keep(a)
    terms.keep(term)
    s = Solver()
    s.add(*rel)
    if s.check() != sat:
        _forced[k] = None
        return None
    v = s.model().eval(term, model_completion=True)
    res = v.as_long() if aarch64.unsatisfiable(rel, Not(term == v)) else None
    _forced[k] = res
    return res


def mismatches(m, g, kval):
    """The positions where the machine differs from the generalized state at
    iteration `kval` (none: it is that state)."""
    from prove_hybrid import same
    inst = g.at(kval)
    def agree(a, b):
        if b is None:
            return True
        if isinstance(a, tuple) or isinstance(b, tuple):
            # A byte of a stored pointer.
            return (isinstance(a, tuple) and isinstance(b, tuple) and a[1] == b[1]
                    and agree(a[0], b[0]))
        if isinstance(b, Ptr) or isinstance(a, Ptr):
            return (isinstance(a, Ptr) and isinstance(b, Ptr) and a.region == b.region
                    and aarch64.holds(m, aarch64.as_bv(a.offset) == aarch64.as_bv(b.offset)))
        if eq(a, b):
            return True
        # Public arithmetic needs one equality proof, rather than hash
        # normalization's search among other leaves with similar samples.
        if aarch64.lengths_only(m, a - b):
            sub1 = [(g.k, BitVecVal(1, 64)), (BitVec("len_at", 64), BitVecVal(1, 64))]
            if not aarch64.holds(m, substitute(a == b, *sub1)):
                return False
            return aarch64.holds(m, a == b)
        return same(a, b)
    bad = []
    for r, gv in g.x.items():
        if not agree(m.x[r], inst(gv)):
            bad.append(("x", r))
    for r, lanes in g.v.items():
        for l, gv in enumerate(lanes):
            if not agree(m.v[r][l], inst(gv)):
                bad.append(("v", r, l))
    if g.masks is not None:
        for r, gv in g.masks.items():
            if not agree(m.k[r], inst(gv)):
                bad.append(("mask", r))
    if g.za is not None:
        for t, tile in enumerate(g.za):
            for row, cells in enumerate(tile):
                for col, gv in enumerate(cells):
                    if not agree(m.za[t][row][col], inst(gv)):
                        bad.append(("za", t, row, col))
    if g.p is not None:
        for k, v in g.p.items():
            if v is not None and m.p.get(k) != v:
                bad.append(("p", k))
    for name, mem in g.mem.items():
        if name == "out":
            continue
        region = m.regions[name]
        for off, b in mem.items():
            if b is None or (isinstance(b, tuple) and b[0] == "word"):
                continue
            if off not in region.bytes or not agree(region.bytes[off], b):
                bad.append(("mem", name, off))
        for off in {b[3] if len(b) > 3 else o - b[2] for o, b in mem.items() if isinstance(b, tuple) and b[0] == "word"}:
            w = symbolic_word(region, region.offset_terms[off]) if isinstance(off, str) else word(region, off)
            if w is None or not agree(w, inst(mem[off][1])):
                bad.append(("word", name, off))
    return bad


def loop_step(g, start, kval, assumptions, head, check):
    """Every path of one iteration from `g` at `kval` (induction.py): `check`
    each path's end (`check(end, how)`); a path back at the head must be in the
    state for kval + 1, within the next iteration's assumptions and with
    its unsigned iteration count advancing without wrap. Positions where
    it differs become unknown in `g`, and
    the step is proved again: the invariant needs only what the iterations
    rely on (a weaker invariant still holds at the start). Returns the
    counts of paths."""
    assert is_bv(kval) and is_const(kval) and not is_bv_value(kval) and kval.size() == 64, "a symbolic 64-bit iteration variable"
    passes = 0
    while True:
        passes += 1
        mk = g.machine(start, kval)
        mk.assumptions = assumptions
        deferred = []
        mk._defer_exits = deferred
        ends = paths(mk, head, {head})
        forget = set()
        for end, how in ends:
            if how == "head":
                canon.set_context(end.assumptions, end.lengths)
                forget.update(mismatches(end, g, kval + 1))
        canon.set_context([], set())
        if not forget:
            for dm, dpc in deferred:
                ends.extend(paths(dm, dpc, {head}))
        print(f"  induction pass {passes}: {len(ends)} paths, {len(forget)} positions left unknown {sorted(map(str, forget))[:8]}", flush=True)
        if not forget:
            break
        for pos in forget:
            g.forget(pos)
    # The invariant settled: each path's own obligations.
    kinds = {"head": 0, "return": 0}
    for end, how in ends:
        kinds[how] += 1
        if how == "head":
            successor = [substitute(a, (kval, kval + 1)) for a in assumptions]
            if not aarch64.holds(end, And(ULT(kval, kval + 1), *successor)):
                raise Unproved("back edge leaves the next iteration's domain or wraps its count")
        canon.set_context(end.assumptions, end.lengths)
        check(end, how)
    canon.set_context([], set())
    return kinds
