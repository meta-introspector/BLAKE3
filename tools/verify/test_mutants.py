"""Mutation rejection is a proof result; checker crashes fail the run."""

import unittest

from aarch64 import Unproved
from mutants import run


class MutationResults(unittest.TestCase):
    def test_accepted_mutant_fails(self):
        _, failed = run(("control", None, lambda: None))
        self.assertTrue(failed)

    def test_rejected_mutant_passes(self):
        def reject():
            raise Unproved("control rejection")
        line, failed = run(("control", None, reject))
        self.assertFalse(failed)
        self.assertIn("control rejection", line)

    def test_checker_error_propagates(self):
        def broken():
            raise KeyError("checker defect")
        with self.assertRaises(KeyError):
            run(("control", None, broken))


if __name__ == "__main__":
    unittest.main()
