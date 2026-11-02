"""hash_many for every input count, by induction over full-width passes.

Independent array words encode every input's bytes. Each input pointer
has its own bounded region. The remainder is a symbolic count below the
SIMD width; the executor explores each tail path. Base runs cover counts
below one pass. The group invariant proves all larger counts whose output
and pointer-table sizes fit in a 64-bit address.
"""
from z3 import (Array, BitVecSort, BitVec, BitVecVal, Select, ZeroExt, ULT, ULE, UGE,
                simplify, substitute, is_bv, Solver, unsat, Concat, Extract)
import aarch64 as arm
import induction as ind
import x86
from aarch64 import Ptr, Region, Unproved
from prove_hybrid import byte_symbols, same, table_cvs
import lean_spec as spec


def pass_head(setup, so, width):
    """The full-width loop's back edge: sub count,W; cmp count,W; jae."""
    code = setup.m.code
    symbols = x86.symbols(so)
    end = min((pc for name, pc in symbols.items()
               if name.startswith(('blake3_', '_blake3_')) and pc > setup.entry), default=x86.text_end(so))
    pcs = sorted(pc for pc in code if setup.entry <= pc < end)
    heads = []
    for i, pc in enumerate(pcs):
        mnem, ops, _ = code[pc]
        if i < 2 or mnem not in ('jae', 'jnc'):
            continue
        target = int(ops[0].split()[0], 16)
        sub, sops, _ = code[pcs[i - 2]]
        cmp, cops, _ = code[pcs[i - 1]]
        if (target < pc and sub == 'sub' and cmp == 'cmp' and sops == cops
                and x86.imm(sops[1]) == width):
            heads.append(target)
    if len(heads) != 1:
        raise Unproved(f'expected one full-width pass loop, found {list(map(hex, heads))}')
    return heads[0]


def counter_invariant(general, first, second, counter, k, width):
    """State counter-derived positions as f(counter + W*K).

The pair of arrivals must establish f(counter+W), including both halves'
carry, before the formula enters the invariant. The caller also checks
the complete invariant at K=0 and the executor checks its successor.
"""
    def counter_only(t):
        if not is_bv(t):
            return False
        symbols = arm.term_symbols(t)
        return bool(symbols) and symbols <= {'counter'}

    def advance(a, b):
        if not counter_only(a) or not counter_only(b):
            return None
        next_value = substitute(a, (counter, counter + width))
        # This is a lemma for every counter, independent of any group or
        # remainder bounds. Keep their unrelated arithmetic out of it.
        solver = Solver()
        solver.add(b != next_value)
        if solver.check() != unsat:
            return None
        return substitute(a, (counter, counter + width * k))
    for r in first.x:
        value = advance(first.x[r], second.x[r])
        if value is not None:
            general.x[r] = value
    for r, lanes in first.v.items():
        for lane, a in enumerate(lanes):
            value = advance(a, second.v[r][lane])
            if value is not None:
                general.v[r][lane] = value
    for name, mem in general.mem.items():
        if name == 'out':
            continue
        r0, r1 = first.regions[name], second.regions[name]
        for off in sorted(o for o in mem if isinstance(o, int) and o % 8 == 0):
            a = ind.word(r0, off)
            if not counter_only(a):
                continue
            value = advance(a, ind.word(r1, off))
            if value is not None:
                for j in range(8):
                    mem[off + j] = ('word', value, j)
        # The same explicit counter formula at symbolic offsets.
        for base_key in {b[3] for b in mem.values() if isinstance(b, tuple) and b[0] == 'word' and len(b) > 3}:
            a = ind.symbolic_word(r0, r0.offset_terms[base_key])
            if not counter_only(a):
                continue
            value = advance(a, ind.symbolic_word(r1, r1.offset_terms[base_key]))
            if value is not None:
                base = r0.offset_terms[base_key]
                for j in range(8):
                    mem[arm.key(base + j)] = ('word', value, j, base_key)


def prove(so, isa, blocks, increment):
    import prove_x86 as p
    assert 1 <= blocks <= 16, 'hash_many takes 1..16 full blocks per input'
    width = p.WIDTH[isa]
    group, rem, k = (BitVec(n, 64) for n in ('len_groups', 'len_rem', 'len_k'))
    count = group * width + rem
    # The output is larger than the pointer table. Each input has its own
    # region, and may share physical bytes with another read-only input.
    max_count = ((1 << 64) - 1) // 32
    bound = max_count // width
    # (table-entry byte offset, input word byte offset), both 64 bits.
    # Distinct words remain independent even for enormous input counts.
    data = Array('inputs', BitVecSort(128), BitVecSort(32))
    key = byte_symbols('key', 32)
    counter = BitVec('counter', 64)
    flags, start, end = p.u8('flags'), p.u8('start'), p.u8('end')

    def input_byte(index, offset):
        j = offset % 4 if isinstance(offset, int) else ind.forced(arm._machine[0], arm.as_bv(offset) & 3)
        if j is None:
            raise Unproved('input word byte is unconstrained')
        word = Select(data, Concat(arm.as_bv(index), arm.as_bv(offset) - j))
        return Extract(8 * j + 7, 8 * j, word)

    def table_byte(offset):
        m = arm._machine[0]
        j = offset % 8 if isinstance(offset, int) else ind.forced(m, arm.as_bv(offset) & 7)
        if j is None:
            raise Unproved('table read byte is unconstrained')
        entry = simplify(arm.as_bv(offset) - j, som=True, bv_sort_ac=True)
        name = f'input@{arm.key(entry)}'
        if name not in m.regions:
            region = Region(name, 64 * blocks, False, lambda o: input_byte(entry, o))
            region.symbolic_initial = True
            m.regions[name] = region
        return (Ptr(name, 0), j)

    regions = {
        'table': Region('table', count * 8, False, table_byte),
        'key': Region('key', 32, False, lambda o: key[o]),
        'out': Region('out', count * 32, True, None),
    }
    regions['table'].symbolic_initial = True
    args = [Ptr('table', 0), count, BitVecVal(blocks, 64), Ptr('key', 0), counter,
            BitVecVal(int(increment), 64), ZeroExt(56, flags), ZeroExt(56, start), ZeroExt(56, end), Ptr('out', 0)]
    setup = p.Setup(so, f'blake3_servil_hash_many_{isa}', regions, args)
    machine = setup.m
    flag_words = [ZeroExt(24, f) for f in (flags | start | end, flags | start, flags | end, flags)] + [BitVecVal(64, 32)]
    p.d_row_facts(machine, counter, 2 * width if increment else 1, flag_words)
    machine.lengths = {'len_groups', 'len_rem', 'len_k', 'counter', 'len_stack'}
    bounds = [ULT(rem, width), ULE(group, bound), ULE(count, max_count)]
    machine.assumptions = list(machine.assumptions) + bounds
    head = pass_head(setup, so, width)

    lane_cache = {}

    def lane_info(idx):
        lkey = (arm.key(idx), arm.key(counter))
        info = lane_cache.get(lkey)
        if info is None:
            inp = [Extract(8 * (j % 4) + 7, 8 * (j % 4),
                           Select(data, Concat(idx * 8, BitVecVal(4 * (j // 4), 64))))
                   for j in range(64 * blocks)]
            ctr = counter + idx if increment else counter
            cv = table_cvs([inp], key, [ctr], flags, start, end, BitVecVal(64, 8), blocks)[0]
            offsets = [arm.key(32 * idx + j) for j in range(32)]
            info = lane_cache[lkey] = (offsets, cv)
        return info

    def check_range(m, first, n):
        first = arm.as_bv(first)
        out, expected = m.regions['out'], set()
        for lane in range(n):
            offsets, cv = lane_info(first + lane)
            expected.update(offsets)
            if any(o not in out.bytes for o in offsets):
                raise Unproved(f'output {lane} incomplete')
            got = spec.words([out.bytes[o] for o in offsets])
            for word in range(8):
                if not same(got[word], cv[word]):
                    raise Unproved(f'output {lane}, word {word}, differs')
        return expected

    def exactly(m, expected):
        actual = {o for o, value in m.regions['out'].bytes.items() if value is not None}
        if actual != expected:
            raise Unproved('writes outside its output range')

    def tail_count(m):
        n = ind.forced(m, rem)
        if n is None:
            raise Unproved('a tail path leaves its output count open')
        return n

    # Counts below one full pass, including zero. The induction's entry
    # covers one or more passes and every remainder.
    print('  base: no full pass, every remainder', flush=True)
    base = ind.snapshot(machine)
    base.assumptions += [group == 0]
    for m, how in ind.paths(base, setup.entry, set()):
        if how != 'return':
            raise Unproved('base case fails to return')
        import canon
        canon.set_context(m.assumptions, m.lengths)
        exactly(m, check_range(m, 0, tail_count(m)))
        p.check_abi(m)
    canon.set_context([], set())

    print('  base paths proved; constructing the group invariant', flush=True)
    start_machine = ind.snapshot(machine)
    start_machine.assumptions += [UGE(group, 1)]
    first = ind.run_to(start_machine, setup.entry, head)
    another = ind.snapshot(first)
    another.assumptions += [UGE(group, 2)]
    second = ind.run_to(another, head, head)
    exactly(second, check_range(second, 0, width))
    general = ind.Generalized(first, second, k)
    if increment:
        counter_invariant(general, first, second, counter, k, width)
    bad = ind.mismatches(first, general, BitVecVal(0, 64))
    if bad:
        raise Unproved(f'invariant differs at entry: {bad}')

    def check(m, how):
        expected = check_range(m, k * width, width)
        if how == 'return':
            if not arm.holds(m, group == k + 1):
                raise Unproved('returns before the last full group')
            expected |= check_range(m, (k + 1) * width, tail_count(m))
            p.check_abi(m)
        exactly(m, expected)
    # Fix the checked pass at A, with independent current counter C.
    # Replacing the original counter by C-W*A is a bijection for each A.
    # The invariant's binder K stays distinct: at A its counters simplify
    # to C+j, and at A+1 to C+W+j. This keeps large iteration arithmetic
    # out of the counter-carry lemmas, without restricting any counter.
    if increment:
        original_counter = counter
        current_counter = BitVec('counter_pass', 64)
        k = BitVec('len_at', 64)
        counter = current_counter - width * k
        def rebase(value):
            if is_bv(value):
                return substitute(value, (original_counter, counter))
            if isinstance(value, tuple):
                return tuple(rebase(v) for v in value)
            if isinstance(value, list):
                return [rebase(v) for v in value]
            if isinstance(value, dict):
                return {name: rebase(v) for name, v in value.items()}
            return value
        for attr in ('x', 'v', 'mem', 'masks'):
            setattr(general, attr, rebase(getattr(general, attr)))
        first.assumptions = rebase(first.assumptions)
        at = general.at
        def at_current(index):
            inst = at(index)
            def value(form, size=64):
                t = inst(form, size)
                return simplify(t, som=True, bv_sort_ac=True) if is_bv(t) else t
            return value
        general.at = at_current
        first.lengths |= {'len_at', 'counter_pass'}
        first.facts, first.fact_symbols = (), set()
        p.d_row_facts(first, current_counter, 2 * width, flag_words)
        first.fact_symbols.add('counter_pass')
    print('  initial invariant checked; proving its step and tails', flush=True)
    kinds = ind.loop_step(general, first, k,
                          list(first.assumptions) + [ULT(k, group), ULE(k, bound - 1)], head, check)
    return kinds


def cases():
    """The caller's fixed-counter messages/parents and incrementing chunks."""
    import prove_x86 as p
    return ([(isa, blocks, False) for isa in p.WIDTH for blocks in range(1, 17)]
            + [(isa, 16, True) for isa in p.WIDTH])


def prove_case(case):
    import prove_x86 as p
    isa, blocks, increment = case
    try:
        result = prove(p.assemble(isa), isa, blocks, increment)
    except Unproved as error:
        return case, False, str(error)
    return case, True, repr(result)


def main():
    """Default: all 51 cases. One optional Python literal selects a batch.

    Each tuple is (ISA, blocks, increment), for example:
      python3 tools/verify/prove_x86_loop.py "[('avx2', 1, False)]"
    Run under a bounded process-group supervisor. Fresh workers keep
    memory flat; set the caller's virtual-memory limit before launching.
    Choose --workers from the host's memory: each worker is a separate
    prover process under that same per-process limit.
    """
    import argparse
    import ast
    import sys
    import workers
    parser = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('cases', nargs='?', help='Python literal list of selected cases; default: the complete matrix')
    parser.add_argument('--workers', type=int, default=8, help='fresh prover processes at once (default: 8)')
    arguments = parser.parse_args()
    import prove_x86 as p
    declared = cases()
    todo = ast.literal_eval(arguments.cases) if arguments.cases is not None else declared
    for isa in {c[0] for c in todo}:
        so = p.assemble(isa)
        x86.symbols(so)
        x86.image(so)
    todo = sorted(todo, key=lambda c: (-c[2], -p.WIDTH[c[0]], -c[1]))
    assert todo and all(case in declared for case in todo), 'select nonempty cases from the declared 51-case matrix'
    assert arguments.workers >= 1, 'at least one worker'
    failures = 0
    for case, passed, details in workers.each(prove_case, todo, arguments.workers):
        print('PROVED' if passed else 'NOT PROVED', repr(case), details, flush=True)
        failures += not passed
    print(len(todo) - failures, 'of', len(todo), 'all-count cases proved', flush=True)
    sys.exit(bool(failures))


if __name__ == '__main__':
    main()
