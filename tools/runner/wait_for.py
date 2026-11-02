#!/usr/bin/env python3
"""Wait for a runner job to finish, then print its verdict.

    python3 tools/runner/wait_for.py JOB

JOB is the job file's name in runner/jobs/ (with or without `.json`).
Checks every 30 s for runner/results/<JOB>.*/verdict.json and prints it.
It holds no deadline of its own over the job: the runner's is the job's one
clock (time_limit_seconds), and the runner writes a verdict when it passes
("timed out"). Two cases have no verdict to wait for, each its own outcome:
the runner is not running (the job has not started, and no job has run for
IDLE_SECONDS: the Mac asleep, or the runner not started), or it stopped
inside the job (the job's folder is older than its time limit plus GRACE,
with no verdict: the runner was killed or the Mac shut down).

Exit status, one outcome each, never merged:
  0  the job passed ("ok", "passed", "no regression")
  1  it failed ("regression", "error", or any other status)
  2  it reached no verdict: "timed out" (its time limit ended it) or
     "no verdict" (other programs kept the machine busy)
  3  the runner is not running, or stopped without writing a verdict
The VM stays idle meanwhile, as a Mac measurement needs.
"""
import json
import os
import sys
import time
from pathlib import Path

EXCHANGE = Path(__file__).resolve().parents[2] / "runner"
# A runner takes its next job within seconds of finishing one.
IDLE_SECONDS = 180
# What a runner does around a job's time limit: power readings and the
# verdict file, seconds at most; a folder this far past its limit without a
# verdict belongs to a runner that stopped.
GRACE = 600
# runner.py's default and largest time limits.
DEFAULT_LIMIT, MAX_LIMIT = 1800, 7200
PASSED = ("ok", "passed", "no regression")
NO_VERDICT = ("timed out", "no verdict")


def main():
    assert len(sys.argv) == 2, __doc__
    stem = sys.argv[1].removesuffix(".json")
    job = json.loads((EXCHANGE / "jobs" / f"{stem}.json").read_text())
    limit = job.get("time_limit_seconds", DEFAULT_LIMIT)
    idle_since = time.monotonic()
    results = EXCHANGE / "results"
    while True:
        # One listing of the folder and at most two files' status a poll:
        # the folder is on the Mac, and each operation through the mount is
        # work on the Mac while it measures (a poll that read every result
        # folder, every 10 s, kept half a CPU busy there: October 22, 2026).
        names = os.listdir(results)
        mine = sorted(n for n in names if n.startswith(stem + "."))
        if mine and (results / mine[-1] / "verdict.json").exists():
            verdict = json.loads((results / mine[-1] / "verdict.json").read_text())
            print(f"{results / mine[-1]}\n{json.dumps(verdict, indent=2)}")
            status = verdict["status"]
            return 0 if status in PASSED else 2 if status in NO_VERDICT else 1
        if mine and time.time() - (results / mine[-1]).stat().st_mtime > limit + GRACE:
            print(f"wait_for: {mine[-1]} started more than {limit + GRACE} s ago (its limit, {limit} s, and {GRACE} s) "
                  "and has no verdict: the runner stopped inside the job; restart it on the Mac (tools/runner/setup-mac.sh)")
            return 3
        # A job running: the newest folder without a verdict yet, made
        # within the longest a job may take (older ones are jobs whose
        # runner stopped).
        newest = max(names, key=lambda n: n.rsplit(".", 1)[-1], default=None)
        running = newest is not None and not (results / newest / "verdict.json").exists() \
            and time.time() - (results / newest).stat().st_mtime < MAX_LIMIT + GRACE
        if mine or running:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > IDLE_SECONDS:
            print(f"wait_for: {stem} has not started and no job has run for {IDLE_SECONDS} s: the runner looks stopped; start it on the Mac (tools/runner/setup-mac.sh)")
            return 3
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
