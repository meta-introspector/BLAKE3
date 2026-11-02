#!/usr/bin/env python3
"""Start, watch, and stop long jobs, by name, safely.

    python3 tools/jobs.py start NAME -- COMMAND...   run COMMAND as job NAME
    python3 tools/jobs.py status [NAME]              running or ended, and the log's tail
    python3 tools/jobs.py stop NAME|PID              stop it and every process it started

A job runs in a session of its own (its own process group), with its
output in tmp/jobs/NAME.log and its process ID in tmp/jobs/NAME.pid, in
the checkout this tool sits in; `start` refuses a NAME still running.
`stop` sends TERM to the job's process group and to every descendant of
its leader, waits up to 10 s, then sends KILL to what remains. It never
matches processes by name (as pkill -f does, which can match the shell
that runs it), and it refuses to stop itself or any process it descends
from. A raw PID stops that process's tree the same way. An ended process
whose parent is gone can linger as a zombie (ps state Z: the VM's PID 1
reaps none); it holds nothing, and this tool counts it as ended.

Why and when to use it: AGENTS.md, "The VM" (the fork's AGENTS.md).
"""

import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = os.path.join(ROOT, "tmp", "jobs")
GRACE_S = 10


def processes():
    """{pid: (ppid, pgid)} for every process, from ps (the same on Linux and macOS)."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, check=True).stdout
    table = {}
    for line in out.split("\n"):
        fields = line.split()
        if len(fields) == 3:
            pid, ppid, pgid = map(int, fields)
            table[pid] = (ppid, pgid)
    return table


def ancestors(table):
    """This process and every process it descends from."""
    seen, pid = set(), os.getpid()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        pid = table[pid][0]
    return seen


def tree(table, root):
    """`root`, the processes of its group when it leads one, and their descendants."""
    found = {root} if root in table else set()
    found |= {pid for pid, (_, pgid) in table.items() if pgid == root}
    grew = True
    while grew:
        children = {pid for pid, (ppid, _) in table.items() if ppid in found} - found
        found |= children
        grew = bool(children)
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie has ended; only its reaping is left.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def pid_of(name):
    path = os.path.join(JOBS, f"{name}.pid")
    if not os.path.exists(path):
        sys.exit(f"jobs: no job named {name} (no {path})")
    return int(open(path).read())


def start(name, command):
    assert name.replace("-", "").replace("_", "").isalnum(), "a job's NAME: letters, digits, - and _"
    assert command, "start NAME -- COMMAND..."
    os.makedirs(JOBS, exist_ok=True)
    pid_path, log_path = os.path.join(JOBS, f"{name}.pid"), os.path.join(JOBS, f"{name}.log")
    if os.path.exists(pid_path) and alive(int(open(pid_path).read())):
        sys.exit(f"jobs: {name} is still running (PID {open(pid_path).read().strip()}); stop it first")
    log = open(log_path, "w")
    proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(pid_path, "w") as f:
        f.write(f"{proc.pid}\n")
    print(f"jobs: {name} started, PID {proc.pid}, log {log_path}")


def status(name=None):
    names = [name] if name else sorted(f[:-4] for f in os.listdir(JOBS) if f.endswith(".pid")) if os.path.isdir(JOBS) else []
    if not names:
        print("jobs: none")
    for n in names:
        pid = pid_of(n)
        log = os.path.join(JOBS, f"{n}.log")
        print(f"{n}: PID {pid}, {'running' if alive(pid) else 'ended'}, log {log}")
        if name and os.path.exists(log):
            print("".join(open(log, errors="replace").readlines()[-5:]), end="")


def stop(target):
    root = int(target) if target.isdigit() else pid_of(target)
    table = processes()
    victims = tree(table, root)
    mine = ancestors(table)
    assert not (victims & mine), f"jobs: PID {root}'s tree holds this tool or a process it descends from; refusing"
    victims = {p for p in victims if alive(p)}
    if not victims:
        print(f"jobs: nothing of {target} is running")
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + GRACE_S
        while time.monotonic() < deadline and any(alive(p) for p in victims):
            time.sleep(0.1)
        left = {p for p in victims if alive(p)}
        if not left:
            print(f"jobs: stopped {target}: {len(victims)} processes ({'TERM' if sig == signal.SIGTERM else 'KILL'})")
            return
        victims = left
    sys.exit(f"jobs: still alive after KILL: {sorted(victims)}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    command, rest = args[0], args[1:]
    if command == "start":
        assert len(rest) >= 2 and "--" in rest, "start NAME -- COMMAND..."
        split = rest.index("--")
        assert split == 1, "start NAME -- COMMAND..."
        start(rest[0], rest[2:])
    elif command == "status":
        status(*rest[:1])
    elif command == "stop":
        assert len(rest) == 1, "stop NAME|PID"
        stop(rest[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
