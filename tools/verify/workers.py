"""Runs a function on each item, each in a fresh process, a few at once.

A fresh process per item keeps the provers' memory flat (the normal forms'
caches in canon.py keep their terms alive). A worker that ends without a
result (the kernel's out-of-memory killer, a crash, an exception, whose
traceback it prints) stops the whole run with its item named. Whenever
the run stops—on that failure, or when its caller stops taking results—
pending siblings are killed and reaped: Python otherwise joins them at
interpreter exit. Descendants still require the caller's process-group
supervisor. multiprocessing.Pool would wait forever for the missing result.
"""
import multiprocessing
from multiprocessing.connection import wait


def _run(fn, item, conn):
    conn.send(fn(item))


def each(fn, items, workers, context=multiprocessing):
    """Yields fn(item) for every item, in the order they finish, with at
    most `workers` (and at most one per CPU) running at once. fn and its
    items are picklable when `context` spawns."""
    todo, running = list(items), {}
    try:
        workers = min(workers, multiprocessing.cpu_count())
        while todo or running:
            while todo and len(running) < workers:
                item = todo.pop(0)
                here, there = context.Pipe(duplex=False)
                p = context.Process(target=_run, args=(fn, item, there))
                p.start()
                there.close()
                running[here] = (p, item)
            for conn in wait(list(running)):
                p, item = running.pop(conn)
                try:
                    result = conn.recv()
                except EOFError:
                    p.join()
                    raise SystemExit(f"the worker for {item!r} ended without a result, exit status {p.exitcode} "
                                     "(-9 is a kill, most often the kernel's out-of-memory killer: run fewer at once)")
                p.join()
                yield result
    finally:
        # One stop path for a worker failure and an early-closing caller.
        for sibling, _ in running.values():
            sibling.kill()
        for sibling, _ in running.values():
            sibling.join()
