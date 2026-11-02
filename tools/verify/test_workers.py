"""Proof-worker liveness controls, in bounded, owned process groups.

Run with unittest discovery. The subprocess deliberately leaves a sibling
blocked when another worker fails; a failure must reach the caller before
that sibling finishes. POSIX process-group cleanup belongs to this test:
it also removes workers when the old implementation fails the regression.
"""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import workers


def task(item):
    if item == "quick":
        return 1
    if item == "blocked":
        time.sleep(60)
    elif item == "exception":
        raise RuntimeError("deliberate worker failure")
    elif item == "empty":
        os._exit(0)
    elif item == "killed":
        os.kill(os.getpid(), signal.SIGKILL)
    return item * 2


@unittest.skipUnless(os.name == "posix", "owned process-group controls need POSIX")
class WorkerTests(unittest.TestCase):
    def child(self, mode, method):
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", mode, method],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            try:
                stdout, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail(f"worker {mode}: caller waited for the blocked sibling")
            return child.returncode, stdout, stderr
        finally:
            # This suite must also finish and reap its child on a broken build.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()

    def test_results(self):
        for method in ("fork", "spawn"):
            with self.subTest(method=method):
                code, stdout, stderr = self.child("success", method)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout), [2, 4, 6])

    def test_early_close_stops_the_blocked_sibling(self):
        for method in ("fork", "spawn"):
            with self.subTest(method=method):
                code, stdout, stderr = self.child("early", method)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout), [1])

    def test_failure_stops_the_blocked_sibling(self):
        for method in ("fork", "spawn"):
            for mode, status in (("exception", 1), ("empty", 0), ("killed", -9)):
                with self.subTest(mode=mode, method=method):
                    code, stdout, stderr = self.child(mode, method)
                    self.assertNotEqual(code, 0, stdout)
                    self.assertIn(f"the worker for '{mode}' ended without a result, exit status {status}", stderr)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        from unittest.mock import patch
        mode, method = sys.argv[2:]
        if mode == "success":
            items = [1, 2, 3]
        elif mode == "early":
            items = ["quick", "blocked"]
        else:
            items = ["blocked", mode]
        # Exercise two concurrent workers even when the test host has one CPU.
        with patch.object(workers.multiprocessing, "cpu_count", return_value=2):
            if mode == "early":
                results = workers.each(task, items, 2, multiprocessing.get_context(method))
                print(json.dumps([next(results)]))
                results.close()
            else:
                print(json.dumps(sorted(workers.each(task, items, 2, multiprocessing.get_context(method)))))
    else:
        unittest.main()
