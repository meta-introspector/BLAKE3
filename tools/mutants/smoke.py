"""A short run of check.py, start to finish, for a change to it (CONTRIBUTING:
a tool that runs for hours lands after one on each kind of machine): two
shards on one small file, then the next run's start-of-run cleanup. About a
minute.

    python3 tools/mutants/smoke.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import check

check.remove_leftovers()
found, functions, timed = check.run(["src/io.rs"], shards=2, label="smoke")
print("found", dict(found), "timed", dict(timed), "functions", len(functions))
listed = subprocess.run(["git", "worktree", "list"], capture_output=True, text=True).stdout
left = listed.count(os.path.realpath(check.WORKTREES))
print("worktrees left by the run:", left)
assert left == 2, "the run leaves its two worktrees for the next run to remove"
check.remove_leftovers()
listed = subprocess.run(["git", "worktree", "list"], capture_output=True, text=True).stdout
assert listed.count(os.path.realpath(check.WORKTREES)) == 0 and not os.listdir(check.WORKTREES), "the next run's start removes them"
print("smoke: ok")
