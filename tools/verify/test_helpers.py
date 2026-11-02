"""Regression tests for the proofs' helpers: the normalizer's proof
context (canon.py), the induction's generalization (induction.py), and the
timing proof's leak detector (aarch64.py).

    python3 -m unittest discover -s tools/verify -p test_helpers.py
"""

import unittest
from z3 import BitVec, BitVecs, Concat, Extract
import canon
import induction


class ContextTests(unittest.TestCase):
    def tearDown(self):
        canon.set_context([], set())

    def test_array_fingerprint_distinguishes_chunk_offsets(self):
        from z3 import Array, BitVecSort, BitVecVal, Select, simplify
        name = "stride_test_input"
        array = Array(name, BitVecSort(64), BitVecSort(8))
        canon._values[(name, 0, 0)] = 0x0123456789ABCDEF
        canon._values[(name, 0, 1)] = 0
        sampled = canon.sample(name, array, 0)
        self.assertEqual(simplify(Select(sampled, BitVecVal(0, 64))).as_long(), 0)
        self.assertEqual(simplify(Select(sampled, BitVecVal(256, 64))).as_long(), 0x23)

    def test_iteration_counter_is_a_parameter(self):
        counter, iteration = BitVecs("counter len_k", 64)
        canon.set_context([], {"len_k"})
        whole = Extract(31, 0, counter + 16 * iteration)
        halves = Extract(31, 0, counter) + 16 * Extract(31, 0, iteration)
        self.assertEqual(canon.canon(whole), canon.canon(halves))
        self.assertNotEqual(canon.canon(whole), canon.canon(halves + 1))

    def test_length_parts_of_a_sum_combine(self):
        # A word plus a group number the kernel computes from an address,
        # against the same word plus K + 1: one normal form; K + 2 differs.
        from z3 import BitVec, LShR, ULE
        word, k = BitVec("pair0", 32), BitVec("len_k", 64)
        canon.set_context([ULE(k, 1 << 39)], {"len_k"})
        computed = word + Extract(31, 0, LShR(1024 * k + 1024 + 5, 10))
        self.assertEqual(canon.canon(computed), canon.canon(word + Extract(31, 0, k) + 1))
        self.assertNotEqual(canon.canon(computed), canon.canon(word + Extract(31, 0, k) + 2))

    def test_context_resets_symbol_classification(self):
        _, iteration = BitVecs("counter len_k", 64)
        self.assertTrue(canon.has_data(iteration))
        canon.set_context([], {"len_k"})
        self.assertFalse(canon.has_data(iteration))
        canon.set_context([], set())
        self.assertTrue(canon.has_data(iteration))


class ModelFactsTests(unittest.TestCase):
    def test_counter_high_facts_separate_at_carry_boundaries(self):
        from unittest.mock import patch
        import x86
        counter, k = BitVecs("counter len_k", 64)
        m = x86.Machine({}, {})
        m.facts = tuple(Extract(63, 32, counter + 8 * k + i) for i in range(5))
        m.fact_symbols = {"counter", "len_k"}
        wanted = m.facts[2]
        loaded = Concat(*(Extract(8 * i + 7, 8 * i, wanted) for i in reversed(range(4))))
        # The high halves agree at the random points; each declared
        # offset's carry boundary separates all the wrong candidates.
        with patch.object(x86, "Solver", wraps=x86.Solver) as solver:
            self.assertTrue(m.normal(loaded).eq(wanted))
        self.assertEqual(solver.call_count, 1)


class X86EntryTests(unittest.TestCase):
    def test_incoming_masks_are_arbitrary(self):
        from unittest.mock import patch
        from z3 import is_bv_value, is_const
        import prove_x86 as p
        with patch.object(p.x86, "symbols", return_value={"entry": 0}), \
             patch.object(p.x86, "disassemble", return_value={}), \
             patch.object(p.x86, "text_end", return_value=1), \
             patch.object(p.x86, "image", return_value={0: 0}):
            setup = p.Setup("unused", "entry", {}, [], p.ENTRY_STACKS[0])
        self.assertEqual(set(setup.m.k), set(range(8)))
        self.assertEqual(len({value.get_id() for value in setup.m.k.values()}), 8)
        for value in setup.m.k.values():
            self.assertTrue(is_const(value))
            self.assertFalse(is_bv_value(value))
            self.assertEqual(value.size(), 16)


class GeneralizationTests(unittest.TestCase):
    def test_iteration_rank_is_a_symbolic_variable(self):
        with self.assertRaisesRegex(AssertionError, "symbolic 64-bit"):
            induction.loop_step(None, None, BitVec("len_k", 64) + 1, [], 0, None)

    def test_constant_difference_through_byte_shuffles(self):
        # A word loaded byte by byte: its bytes' Concat hides the difference
        # from Z3's simplifier.
        x = BitVec("pair0", 32)
        def loaded(t):
            return Concat(Extract(31, 24, t), Extract(23, 16, t), Extract(15, 8, t), Extract(7, 0, t))
        self.assertEqual(induction.constant_difference(loaded(x), loaded(x + 1)), 1)
        self.assertIsNone(induction.constant_difference(x, x * x))

    def test_backedge_requires_successor_domain_and_no_wrap(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from z3 import ULT, UGE
        from aarch64 import Unproved
        k, groups = BitVecs("len_k len_groups", 64)
        state = SimpleNamespace(x={}, v={}, regions={}, lengths={"len_k", "len_groups"}, assumptions=[])
        g = induction.Generalized(state, state, k)
        # A back edge at the last legal iteration must exit instead.
        # Even an unrestricted domain must keep the unsigned rank growing.
        for assumptions in ([UGE(groups, 1), ULT(k, groups)], []):
            with self.subTest(assumptions=assumptions):
                with patch.object(induction, "paths", side_effect=lambda m, *_: [(m, "head")]):
                    with self.assertRaisesRegex(Unproved, "domain or wraps"):
                        induction.loop_step(g, state, k, assumptions, 0, lambda *_: None)

    def test_public_successor_uses_direct_equality(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        counter, k = BitVecs("counter len_k", 64)
        state = SimpleNamespace(x={"r": counter}, v={}, regions={})
        g = induction.Generalized(state, state, k)
        g.x["r"] = counter + 8 * k
        end = SimpleNamespace(x={"r": counter + 8 * k + 8}, v={}, regions={},
                              lengths={"counter", "len_k"}, assumptions=[])
        with patch("prove_hybrid.same", side_effect=AssertionError("public arithmetic uses the solver")):
            self.assertEqual(induction.mismatches(end, g, k + 1), [])
            end.x["r"] += 1
            self.assertEqual(induction.mismatches(end, g, k + 1), [("x", "r")])

    def test_unwritten_first_word_skips_second_reconstruction(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from aarch64 import Region
        first = Region("out", 8, True, None)
        second = Region("out", 8, True, None)
        second.bytes = {i: BitVec(f"result{i}", 8) for i in range(8)}
        states = [SimpleNamespace(x={}, v={}, regions={"out": r}) for r in (first, second)]
        word = induction.word
        def reconstruct(region, offset):
            self.assertIs(region, first, "an absent first word cannot yield an invariant")
            return word(region, offset)
        with patch.object(induction, "word", side_effect=reconstruct):
            g = induction.Generalized(*states, BitVec("len_k", 64))
        self.assertEqual(g.mem["out"], {i: None for i in range(8)})

    def test_x86_snapshot_keeps_masks_independent(self):
        from z3 import BitVecVal
        import x86
        original = x86.Machine({}, {})
        fork = induction.snapshot(original)
        fork.k[1] = BitVecVal(7, 16)
        self.assertEqual(original.k[1].as_long(), 0)

    def test_x86_masks_participate_in_the_invariant(self):
        from z3 import BitVecVal, simplify
        import x86
        first = x86.Machine({}, {})
        second = induction.snapshot(first)
        second.k[1] = BitVecVal(1, 16)
        k = BitVec("len_k", 64)
        g = induction.Generalized(first, second, k)
        current = g.machine(first, BitVecVal(2, 64))
        self.assertEqual(simplify(current.k[1]).as_long(), 2)
        self.assertEqual(induction.mismatches(current, g, BitVecVal(1, 64)), [("mask", 1)])
        g.forget(("mask", 1))
        self.assertIsNone(g.at(k)(g.masks[1]))

    def test_explicit_counter_halves_at_the_next_iteration(self):
        from types import SimpleNamespace
        from z3 import BitVecVal, simplify, substitute
        state = SimpleNamespace(x={}, v={}, regions={})
        counter, k = BitVecs("counter len_k", 64)
        g = induction.Generalized(state, state, k)
        values = [counter + 8 * k + lane for lane in range(2)]
        inst = g.at(BitVecVal(1, 64))
        # The low word carries while each packed 32-bit lane advances
        # independently. An affine 64-bit packed-word step gets this wrong.
        for high, expected in [(False, 1 << 32), (True, (1 << 32) + 1)]:
            lo = 32 if high else 0
            packed = Concat(Extract(lo + 31, lo, values[1]), Extract(lo + 31, lo, values[0]))
            got = simplify(substitute(inst(packed), (counter, BitVecVal((1 << 32) - 8, 64))))
            self.assertEqual(got.as_long(), expected)


class ConcreteCasesTests(unittest.TestCase):
    def test_representative_cases_are_the_declared_promotion_set(self):
        import prove_x86
        got = prove_x86.representative_cases()
        self.assertEqual(len(got), 203)
        self.assertEqual(len(set(got)), 203)
        self.assertTrue(all(case in prove_x86.cases(set()) for case in got))
        # pair.rs's shape: two inputs at consecutive counters, 1 to 15
        # blocks, AVX2 and AVX-512 (#163).
        for isa in ("avx2", "avx512"):
            for blocks in range(1, 16):
                self.assertIn((isa, "hash_many", (2, blocks, True)), got)


class EveryCountCasesTests(unittest.TestCase):
    def test_declared_matrix_has_every_caller_shape_once(self):
        import prove_x86_loop
        expected = {(isa, blocks, False) for isa in ('sse41', 'avx2', 'avx512') for blocks in range(1, 17)}
        expected |= {(isa, 16, True) for isa in ('sse41', 'avx2', 'avx512')}
        got = prove_x86_loop.cases()
        self.assertEqual(len(got), 51)
        self.assertEqual(set(got), expected)


if __name__ == "__main__":
    unittest.main()


class LeakTests(unittest.TestCase):
    """The timing proof's detector: a run stops where the message steers
    a branch or an address, and runs on where only lengths do."""

    def run_code(self, code):
        import aarch64
        from aarch64 import Machine, Region, Ptr
        msg = [BitVec(f"msg{i}", 8) for i in range(16)]
        m = Machine(code, {"input": Region("input", 16, False, lambda o: msg[o]),
                           "table": Region("table", 256, False, lambda o: BitVec(f"t{o}", 8))})
        m.x = {i: BitVec(f"x{i}_in", 64) for i in range(31)}
        m.x[0], m.x[1], m.x[30] = Ptr("input", 0), Ptr("table", 0), Ptr("return", 0)
        m.x["sp"] = Ptr("stack", 0)
        return aarch64.run(m, 0)

    def test_branch_on_a_message_byte_stops(self):
        from aarch64 import Unproved
        code = {0: ("ldrb", ["w8", "[x0, #3]"]), 4: ("cbz", ["w8", "c <x>"]), 8: ("nop", []), 12: ("ret", [])}
        with self.assertRaisesRegex(Unproved, "depends on data"):
            self.run_code(code)

    def test_table_index_from_a_message_byte_stops(self):
        from aarch64 import Unproved
        code = {0: ("ldrb", ["w8", "[x0, #3]"]), 4: ("ldrb", ["w9", "[x1, x8]"]), 8: ("ret", [])}
        with self.assertRaisesRegex(Unproved, "address depends on data"):
            self.run_code(code)

    def test_message_data_without_steering_runs(self):
        code = {0: ("ldrb", ["w8", "[x0, #3]"]), 4: ("eor", ["w8", "w8", "#0x5a"]), 8: ("ret", [])}
        self.run_code(code)

