"""A normal form for the terms the kernels compute, so that a kernel's
output and the specification's are compared by identity, without search.

BLAKE3's compression is sums, xors, and rotations of 32-bit words; the
kernels add byte shuffles (loads, stores, spills, lane moves), which are
concatenations and extractions of whole words. `canon` flattens and sorts
sums and xors (xor cancelling pairs), keeps rotations, undoes byte
shuffles that put a word back together, and hands every small term with
no sum, xor, or rotation in it (the inputs: block words, key words, the
counter's halves, the flags) to Z3's simplifier, which settles those
reliably. Two terms with one normal form are equal; the converse does not
hold, so a mismatch is a failed proof, never a false one.
"""

import sys
from z3 import (If, is_bool, Z3_OP_BADD, Z3_OP_BXOR, Z3_OP_ROTATE_RIGHT, Z3_OP_ROTATE_LEFT, Z3_OP_EXTRACT,
                Z3_OP_CONCAT, Z3_OP_ZERO_EXT, Z3_OP_EXT_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT,
                Z3_OP_UNINTERPRETED, Z3_OP_ITE, BitVecVal, BitVec, Solver, unsat, sat, Or, substitute, simplify,
                is_bv_value, is_true, is_false)
import random
import re
import terms

sys.setrecursionlimit(1_000_000)

_ids = {}          # normal-form tuple -> small integer
# Caches keyed by Z3's ast id: terms.py keeps every node for the process,
# so an id names one term for the process's life (a freed ast's id would
# be reused, and a stale entry would equate two different terms).
_memo = {}         # z3 ast id -> normal form id
_arith = {}        # z3 ast id -> whether it holds a sum, xor, or rotation

ARITH = (Z3_OP_BADD, Z3_OP_BXOR, Z3_OP_ROTATE_RIGHT, Z3_OP_ROTATE_LEFT, Z3_OP_EXT_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT)
_nodes = {}        # normal-form id -> its tuple


def node(n):
    return _nodes[n]


def intern(t):
    n = _ids.get(t)
    if n is None:
        n = _ids[t] = len(_ids)
        _nodes[n] = t
    return n


# The parameters' symbols (counters, flags, lengths): a term over them alone
# is a leaf for the solver, whatever its arithmetic (the counter's halves
# with their carry). The proofs name every input byte otherwise.
PARAMS = re.compile(r"(counter|counters|flags|start|end|last_len|plast|lanes|step|junk)\w*$")
_data = {}         # z3 ast id -> whether it holds a symbol other than the parameters


def has_data(t):
    return _has_data(terms.node(t))


def _has_data(n):
    r = _data.get(n.id)
    if r is None:
        if not n.kids:
            r = (n.kind == Z3_OP_UNINTERPRETED
                 and not PARAMS.match(n.name())
                 and n.name() not in _context["lengths"])
        else:
            r = any(_has_data(c) for c in n.kids)
        _data[n.id] = r
    return r


def has_arith(t):
    """Whether `t` holds round arithmetic: a 32-bit sum, xor, or rotation
    (the counter's 64-bit sums are leaves, for the solver)."""
    return _has_arith(terms.node(t))


def _has_arith(n):
    r = _arith.get(n.id)
    if r is None:
        r = _arith[n.id] = (n.kind in ARITH and n.size == 32) or any(_has_arith(c) for c in n.kids)
    return r


_leaves = {}       # fingerprint -> [(simplified leaf, id)]
_leaf_by_id = {}   # simplified leaf ast id -> normal-form id
_values = {}       # symbol name -> random values, one per fingerprint round
_samples = {}      # (symbol name, sort key, round) -> Z3 ExprRef
ROUNDS = 3
# Rounds whose free symbols (those the assumptions leave alone: data,
# counters, flags) have their low 32-bit word at 2**32 - j for each j in
# EDGES, so a word plus a count c carries exactly when c >= j. At random
# points a carry almost never happens, so leaves that differ only in where
# one happens (the counter's halves, `If(lo <= lo + c, hi, hi + 1)` for
# different c) shared a fingerprint: the solver's work on such pairs, each
# found different, was most of the induction proofs' time (October 22,
# 2026).
EDGES = range(1, 17)


def symbols_of(t, out, seen):
    """Adds each uninterpreted constant in `t` to `out`, by name."""
    _symbols_of(terms.node(t), out, seen)


def _symbols_of(n, out, seen):
    if n.id in seen:
        return
    seen.add(n.id)
    if not n.kids and n.kind == Z3_OP_UNINTERPRETED:
        out[n.name()] = n.expr
    for c in n.kids:
        _symbols_of(c, out, seen)


# The context of the comparisons (induction.py): the path's assumptions on
# its length symbols, and points that satisfy them. Leaves are compared
# under the assumptions, and fingerprinted with the length symbols at those
# points (so leaves equal under the assumptions share a fingerprint).
_context = {"assumptions": [], "points": [], "lengths": set(), "key": None}
_used_assumptions = False
_last_nonempty_lset = None
_points_cache = {}
_pieces_memo = {}


def set_context(assumptions, lengths):
    """Compare under `assumptions` on the symbols named `lengths`; the
    normal forms and their caches start afresh when the context requires it."""
    global _used_assumptions
    lset = frozenset(lengths)
    lset_changed = _context.get("lset") != lset
    if lset_changed:
        _data.clear()
        _context["lset"] = lset
    clear_canon = lset_changed or _used_assumptions
    _used_assumptions = False
    _leaves.clear()
    _pieces_memo.clear()
    if clear_canon:
        _ids.clear(); _nodes.clear(); _memo.clear(); _leaf_by_id.clear(); _leaf_terms.clear(); _free_leaves.clear(); _zero_leaves.clear()
    free = {x for a in assumptions for x in terms.symbols(a)} - set(lengths)
    assert not free, f"assumptions on symbols other than the lengths: {sorted(free)}"
    if not assumptions:
        _context["assumptions"], _context["points"], _context["lengths"] = [], [], set(lengths)
        return
    ckey = (tuple(terms.ast_id(a) for a in assumptions), lset)
    points = _points_cache.get(ckey)
    if points is None:
        for a in assumptions:
            terms.keep(a)
        points = []
        if assumptions:
            s = Solver()
            s.add(*assumptions)
            for _ in range(ROUNDS):
                if s.check() != sat:
                    break
                model = s.model()
                point = {d.name(): model[d] for d in model.decls() if d.name() in lengths}
                points.append(point)
                s.add(Or([BitVec(n, v.size()) != v for n, v in point.items()]) if point else False)
        _points_cache[ckey] = points
    _context["assumptions"], _context["points"] = list(assumptions), points
    _context["lengths"] = set(lengths)
    _context["key"] = ckey


def sample(n, v, r):
    """Symbol `v` (named `n`) at sample point `r`: a deterministic sample
    seeded by `(n, r)`; an array (a message read at any offset) a linear
    function of its index, so bytes at different offsets differ. Only a
    fingerprint: equal leaves are proved equal by the solver."""
    from z3 import is_array, Lambda, Extract, BitVec as BV
    arr = is_array(v)
    skey = (n, (v.domain().size(), v.range().size()) if arr else v.size(), r)
    cached = _samples.get(skey)
    if cached is not None:
        return cached
    if arr:
        i = BV("sample_index", v.domain().size())
        a, b = (_values.setdefault((n, r, j), random.Random(f"0:{n}:{r}:{j}").getrandbits(v.domain().size())) for j in (0, 1))
        # High bits mix every index bit into a byte fingerprint. Taking
        # the low byte made offsets 256 bytes apart collide at every
        # point, forcing needless solver calls between unrelated chunks.
        cached = Lambda([i], Extract(v.domain().size() - 1, v.domain().size() - v.range().size(), i * a + b))
    else:
        cached = BitVecVal(_values.setdefault((n, r), random.Random(f"0:{n}:{r}").getrandbits(v.size())) % (1 << v.size()), v.size())
    _samples[skey] = cached
    return cached


def edge_sample(n, v, j):
    """Symbol `v` (named `n`) in edge round `j`: a deterministic sample
    whose low 32 bits are 2**32 - j; an array, or a narrower value, as
    `sample`."""
    from z3 import is_array
    if is_array(v) or v.size() < 32:
        return sample(n, v, ("edge", j))
    skey = (n, v.size(), ("edge", j))
    cached = _samples.get(skey)
    if cached is None:
        x = _values.setdefault((n, "edge", j), random.Random(f"0:{n}:edge:{j}").getrandbits(v.size()) & ~0xffffffff | (1 << 32) - j)
        cached = _samples[skey] = BitVecVal(x, v.size())
    return cached


def leaf(t):
    """A term with no rounds in it: equal leaves get one id. Leaves whose
    values agree at sample points are proved equal by the solver, under the
    context's assumptions; the points only choose which pairs to try. The
    polynomial normal form first makes most equal leaves identical. A Bool
    (a mask lane's compare, in the AVX-512 XOF at four blocks and more) is
    canonicalised as the one-bit vector it selects."""
    from z3 import is_array
    if is_bool(t):
        t = If(t, BitVecVal(1, 1), BitVecVal(0, 1))
    t = simplify(t, som=True, bv_sort_ac=True)
    tid = t.get_id()
    cached = _leaf_by_id.get(tid)
    if cached is not None:
        return cached
    terms.keep(t)
    if is_bv_value(t):
        n = intern(("leaf", t.sexpr()))
        _leaf_by_id[tid] = n
        _record_leaf(n, t, True)
        return n
    syms = {}
    symbols_of(t, syms, set())
    print_ = []
    points = _context["points"]
    for r in range(ROUNDS):
        point = points[r] if r < len(points) else {}
        sub = [(v, point[n] if n in point else sample(n, v, r)) for n, v in syms.items()]
        print_.append(simplify(substitute(t, *sub)).as_long() if sub else t.as_long())
    # The edge rounds take the lengths at a point that satisfies the
    # assumptions, so leaves equal under them still share a fingerprint.
    # Only terms with a >=32-bit free bit-vector symbol (such as a counter)
    # vary across EDGES.
    point = points[0] if points else {}
    if any(n not in point and not is_array(v) and v.size() >= 32 for n, v in syms.items()):
        for j in EDGES:
            sub = [(v, point[n] if n in point else edge_sample(n, v, j)) for n, v in syms.items()]
            print_.append(simplify(substitute(t, *sub)).as_long() if sub else t.as_long())
    key = (t.size(), tuple(print_))
    for other, n in _leaves.get(key, []):
        if other.eq(t):
            _leaf_by_id[tid] = n
            return n
        global _used_assumptions
        s = Solver()
        s.set("timeout", 60_000)
        s.add(*_context["assumptions"], other != t)
        if s.check() == unsat:
            if _context["assumptions"]:
                _used_assumptions = True
            _leaf_by_id[tid] = n
            return n
    n = intern(("leaf", t.sexpr()))
    _leaves.setdefault(key, []).append((t, n))
    _leaf_by_id[tid] = n
    _record_leaf(n, t, False)
    return n


_leaf_terms = {}   # leaf id -> its simplified term
_free_leaves = {}  # data-free leaf id -> its simplified term
_zero_leaves = set()


def _record_leaf(n, t, is_const_val=False):
    _leaf_terms[n] = t
    if is_const_val:
        _free_leaves[n] = t
        if t.as_long() == 0:
            _zero_leaves.add(n)
    elif not has_data(t):
        _free_leaves[n] = t


def constants_merged(ids, op, unit):
    """`ids` with their data-free leaves (constants and parameters) made one
    leaf, combined by `op` (a sum's or an xor's), left out when it is `unit`'s
    value: however the summands were nested or folded, one normal form."""
    free = [x for x in ids if x in _free_leaves]
    if not free:
        return ids
    if len(free) == 1:
        return [x for x in ids if x != free[0]] if (unit == 0 and free[0] in _zero_leaves) else ids
    r = _free_leaves[free[0]]
    for x in free[1:]:
        r = op(r, _free_leaves[x])
    r = simplify(r)
    fset = set(free)
    rest = [x for x in ids if x not in fset]
    return rest if is_bv_value(r) and r.as_long() == unit else rest + [leaf(r)]


def summands(n):
    """The terms a sum adds, through nested sums."""
    if n.kind == Z3_OP_BADD:
        return [x for c in n.kids for x in summands(c)]
    return [n]


def pieces(n):
    """`n` as a list of (node, hi, lo) bit ranges, most significant first,
    seeing through concatenations, extractions, and zero extensions."""
    r = _pieces_memo.get(n.id)
    if r is not None:
        return r
    r = _pieces_memo[n.id] = merged(_pieces_raw(n))
    return r


def _pieces_raw(n):
    kind = n.kind
    if kind == Z3_OP_CONCAT:
        return [p for c in n.kids for p in pieces(c)]
    if kind == Z3_OP_EXTRACT:
        hi, lo = n.params
        inner = n.kids[0]
        out, pos = [], inner.size
        for (x, h, l) in pieces(inner):
            w = h - l + 1
            top, bot = pos - 1, pos - w        # this piece's bits in the operand
            pos -= w
            a, b = min(top, hi), max(bot, lo)
            if a >= b:
                out.append((x, l + (a - bot), l + (b - bot)))
        return out
    if kind == Z3_OP_ZERO_EXT:
        k = n.params[0]
        return [(terms.node(BitVecVal(0, k)), k - 1, 0)] + pieces(n.kids[0])
    return [(n, n.size - 1, 0)]


def xors(n):
    """The terms an xor combines, through nested xors."""
    if n.kind == Z3_OP_BXOR:
        return [x for c in n.kids for x in xors(c)]
    return [n]


def xor_all(ts):
    r = ts[0]
    for x in ts[1:]:
        r = r ^ x
    return r


def merged(ps):
    """Adjacent ranges of one term joined."""
    out = []
    for x, h, l in ps:
        if out and out[-1][0] is x and out[-1][2] == h + 1:
            out[-1] = (x, out[-1][1], l)
        else:
            out.append((x, h, l))
    return out


def canon(t):
    """The normal form's id of the term t."""
    return _canon_memo(terms.node(t))


def _canon_memo(n):
    r = _memo.get(n.id)
    if r is None:
        r = _memo[n.id] = _canon(n)
    return r


def _canon(n):
    if not _has_arith(n) or not _has_data(n):
        return leaf(n.expr)
    kind = n.kind
    if kind == Z3_OP_ITE:
        # A select whose condition is constant on this path (the AVX-512
        # XOF's mask lanes at four blocks and more, whose compare the
        # path's lengths settle) is the arm it selects.
        c = simplify(n.kids[0].expr)
        if is_true(c):
            return _canon_memo(n.kids[1])
        if is_false(c):
            return _canon_memo(n.kids[2])
    if kind in (Z3_OP_CONCAT, Z3_OP_EXTRACT, Z3_OP_ZERO_EXT):
        ps = merged(pieces(n))
        if len(ps) == 1:
            x, h, l = ps[0]
            if l == 0 and h == x.size - 1:
                return _canon_memo(x)
        if len(ps) == 2 and ps[0][0] is ps[1][0]:
            # x's low k bits above its high bits: x rotated right by k.
            (x, h1, l1), (_, h2, l2) = ps
            if l1 == 0 and h2 == x.size - 1 and l2 == h1 + 1:
                return intern(("ror", l2, _canon_memo(x)))
        return intern(("cat", tuple((_canon_memo(x), h, l) for x, h, l in ps)))
    if kind == Z3_OP_BADD:
        # Children flatten through _canon_memo; constants_merged folds all
        # data-free leaves (lengths, parameters, constants) into one leaf.
        ids = []
        for c in n.kids:
            k = _canon_memo(c)
            ids.extend(node(k)[1] if node(k)[0] == "add" else [k])
        ids = constants_merged(ids, lambda a, b: a + b, 0)
        if not ids:
            return leaf(BitVecVal(0, n.size))
        return ids[0] if len(ids) == 1 else intern(("add", tuple(sorted(ids))))
    if kind == Z3_OP_BXOR:
        seen = {}
        for c in n.kids:
            k = _canon_memo(c)
            for x in (node(k)[1] if node(k)[0] == "xor" else [k]):
                seen[x] = seen.get(x, 0) ^ 1
        odd = constants_merged([x for x, o in seen.items() if o], lambda a, b: a ^ b, 0)
        if not odd:
            return leaf(BitVecVal(0, n.size))
        return odd[0] if len(odd) == 1 else intern(("xor", tuple(sorted(odd))))
    if kind in (Z3_OP_ROTATE_RIGHT, Z3_OP_ROTATE_LEFT, Z3_OP_EXT_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT):
        if kind in (Z3_OP_EXT_ROTATE_RIGHT, Z3_OP_EXT_ROTATE_LEFT):
            amount = simplify(n.kids[1].expr)
            if not is_bv_value(amount):
                raise ValueError("rotation by a variable amount")
            k = amount.as_long() % n.size
        else:
            k = n.params[0] % n.size
        if kind in (Z3_OP_ROTATE_LEFT, Z3_OP_EXT_ROTATE_LEFT):
            k = (n.size - k) % n.size
        inner = _canon_memo(n.kids[0])
        if node(inner)[0] == "ror":
            k, inner = (k + node(inner)[1]) % n.size, node(inner)[2]
        return inner if k == 0 else intern(("ror", k, inner))
    return intern(("op", n.name(), n.params, tuple(_canon_memo(c) for c in n.kids)))
