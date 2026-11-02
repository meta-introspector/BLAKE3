"""Controls for reach.py's rule, on fixed maps and diffs (no cargo): what a
change selects, and in particular what it must never leave out. Run with
    python3 tools/tests/test_reach.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reach  # noqa: E402


def base(texts):
    """A base tree held in memory: {path: text}."""
    reach._BASE.clear()
    reach._BASE["commit"] = "fixed"
    reach._BASE["paths"] = set(texts)
    reach._BASE["texts"] = {p: t.splitlines() for p, t in texts.items()}


def hunk(old_start, old_count, new_start, new_count, new_lines=()):
    return (old_start, old_count, new_start, new_count, list(new_lines))


class Rule(unittest.TestCase):
    LIB = "pub const ANSWER: u32 = 7;\n\npub fn twice(x: u32) -> u32 {\n    let y = x * 2;\n    y\n}\n\npub fn thrice(x: u32) -> u32 {\n    x * 3\n}\n"
    # Tests: `two` ran twice's body, `three` ran thrice's.
    TESTS = {"two": {"src/lib.rs": {3, 4, 5}}, "three": {"src/lib.rs": {8, 9}}}
    INSTRUMENTED = {"src/lib.rs": {3, 4, 5, 8, 9}}

    def setUp(self):
        base({"src/lib.rs": self.LIB})

    def test_a_comment_selects_nothing(self):
        diff = {"src/lib.rs": [hunk(4, 1, 4, 1, ["    let y = x * 2; // doubled"])]}
        # The old line 4 is code that `two` ran: the rewrite selects `two`.
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
        self.assertEqual(set(chosen), {"two"})
        self.assertIsNone(everything)
        # A blank line inserted between functions selects nothing.
        diff = {"src/lib.rs": [hunk(6, 0, 7, 1, [""])]}
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
        self.assertEqual(chosen, {})
        self.assertIsNone(everything)

    def test_a_body_line_selects_the_tests_that_ran_it(self):
        diff = {"src/lib.rs": [hunk(9, 1, 9, 1, ["    x * 4"])]}
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
        self.assertEqual(set(chosen), {"three"})
        self.assertIsNone(everything)

    def test_a_constant_runs_every_test(self):
        # Sam Shackleton's control (October 23): a constant's users are the
        # compiler's to know; names in the text are no closure.
        diff = {"src/lib.rs": [hunk(1, 1, 1, 1, ["pub const ANSWER: u32 = 9;"])]}
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
        self.assertIsNotNone(everything)

    def test_a_file_coverage_never_saw_runs_every_test(self):
        # Sam Shackleton's control: a constant-only module has no instrumented
        # line and is compiled all the same.
        base({"src/lib.rs": self.LIB, "src/consts.rs": "pub const K: u16 = 3;\npub const L: u16 = K;\n"})
        diff = {"src/consts.rs": [hunk(1, 1, 1, 1, ["pub const K: u16 = 259;"])]}
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
        self.assertIsNotNone(everything)

    def test_a_const_fn_body_runs_every_test(self):
        # Sam Shackleton's third control: a const fn's body may be evaluated
        # at compile time into a constant another test reads, so coverage of
        # its lines names only part of its reach.
        lib = "const fn base() -> u16 {\n    3\n}\nconst B: u16 = base();\npub fn direct() -> u8 {\n    base() as u8\n}\npub fn indirect() -> u16 {\n    B\n}\n"
        base({"src/lib.rs": lib})
        tests = {"direct": {"src/lib.rs": {1, 2, 3, 5, 6, 7}}, "indirect": {"src/lib.rs": {8, 9, 10}}}
        diff = {"src/lib.rs": [hunk(2, 1, 2, 1, ["    259"])]}
        chosen, everything = reach.select(tests, {"src/lib.rs": set(range(1, 11))}, ["direct", "indirect"], diff)
        self.assertIsNotNone(everything)

    def test_a_const_fn_in_any_form_runs_every_test(self):
        # Sam Shackleton's controls, October 24: `const extern "C" fn`, and
        # `const` on one line with `fn` on the next, are const fns too.
        for header in ['#[inline(never)]\nconst extern "C" fn base() -> u16 {', "#[inline(never)]\nconst\nfn base() -> u16 {", "pub const unsafe fn base() -> u16 {"]:
            lib = header + "\n    3\n}\nconst B: u16 = base();\npub fn direct() -> u8 {\n    base() as u8\n}\npub fn indirect() -> u16 {\n    B\n}\n"
            base({"src/lib.rs": lib})
            body = header.count("\n") + 2
            lines = lib.count("\n")
            tests = {"direct": {"src/lib.rs": set(range(1, body + 5))}, "indirect": {"src/lib.rs": set(range(body + 5, lines + 1))}}
            diff = {"src/lib.rs": [hunk(body, 1, body, 1, ["    259"])]}
            chosen, everything = reach.select(tests, {"src/lib.rs": set(range(1, lines + 1))}, ["direct", "indirect"], diff)
            self.assertIsNotNone(everything, header)
        # And a plain fn after a const item stays a plain fn: its body
        # line selects its own tests.
        base({"src/lib.rs": self.LIB})
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], {"src/lib.rs": [hunk(4, 1, 4, 1, ["    let y = x + x;"])]})
        self.assertIsNone(everything)
        self.assertEqual(set(chosen), {"two"})

    def test_what_coverage_cannot_see_runs_every_test(self):
        for path in ["c/blake3_avx2_x86-64_unix.S", "build.rs", "Cargo.toml", ".config/nextest.toml"]:
            diff = {path: [hunk(1, 1, 1, 1, ["x"])]}
            chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three"], diff)
            self.assertIsNotNone(everything, path)

    def test_a_new_test_runs(self):
        chosen, everything = reach.select(self.TESTS, self.INSTRUMENTED, ["two", "three", "four"], {})
        self.assertEqual(set(chosen), {"four"})

    def test_an_attribute_on_a_function_selects_its_tests(self):
        lib = "#[inline]\npub fn twice(x: u32) -> u32 {\n    x * 2\n}\n"
        base({"src/lib.rs": lib})
        tests = {"two": {"src/lib.rs": {2, 3}}}
        diff = {"src/lib.rs": [hunk(1, 1, 1, 1, ["#[inline(never)]"])]}
        chosen, everything = reach.select(tests, {"src/lib.rs": {2, 3}}, ["two"], diff)
        self.assertEqual(set(chosen), {"two"})
        self.assertIsNone(everything)

    def test_lines_carry_through_a_diff(self):
        # Two lines inserted before line 5 move it to 7; a removed line goes.
        self.assertEqual(reach.translate({3, 5, 8}, [hunk(5, 0, 5, 2)]), {3, 7, 10})
        self.assertEqual(reach.translate({3, 5, 8}, [hunk(5, 1, 5, 0)]), {3, 7})


if __name__ == "__main__":
    unittest.main()
