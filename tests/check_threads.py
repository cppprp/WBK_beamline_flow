"""Verify the thread cap actually reaches worker processes.

This is the assumption the whole sizing design rests on: cluster_tools spawns its
workers, so environment set before the pool is built is read by the fresh
interpreters when they import numpy. If this ever regresses to fork, the cap set
after numpy is imported would silently stop working.
"""

import _support  # noqa: F401 - sets sys.path and env
import os


def _report(_ignored):
    import multiprocessing

    return {
        "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "BLOSC_NTHREADS": os.environ.get("BLOSC_NTHREADS"),
        "start_method": multiprocessing.get_start_method(allow_none=True),
    }


def main():
    import cluster_tools

    from wbk_publish.logs import make_logger
    from wbk_publish.threads import apply_thread_limit

    log = make_logger("threads-check", verbose=False)
    fails = []

    def eq(label, got, want):
        ok = got == want
        print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}"
              + ("" if ok else f"  (want {want!r})"))
        if not ok:
            fails.append(label)

    print("— start method must be spawn for the env-var approach to work")
    ctx_method = cluster_tools.get_executor("multiprocessing", max_workers=1)
    method = ctx_method._mp_context.get_start_method()
    ctx_method.shutdown()
    eq("cluster_tools start method", method, "spawn")

    print("— cap applied before pool creation reaches the workers")
    apply_thread_limit(3, log)
    eq("parent env set", os.environ["OMP_NUM_THREADS"], "3")

    with cluster_tools.get_executor("multiprocessing", max_workers=2) as ex:
        results = [f.result() for f in ex.map_to_futures(_report, [0, 1])]

    for i, r in enumerate(results):
        eq(f"worker {i} OMP_NUM_THREADS", r["OMP_NUM_THREADS"], "3")
        eq(f"worker {i} BLOSC_NTHREADS", r["BLOSC_NTHREADS"], "3")

    print("— changing the cap for a second run also propagates")
    apply_thread_limit(7, log)
    with cluster_tools.get_executor("multiprocessing", max_workers=2) as ex:
        again = [f.result() for f in ex.map_to_futures(_report, [0])]
    eq("worker sees updated cap", again[0]["OMP_NUM_THREADS"], "7")

    print("— an operator-set value is never overwritten")
    from wbk_publish import threads as th

    saved_op, saved_env = th._operator_set, os.environ.get("OMP_NUM_THREADS")
    try:
        th._operator_set = frozenset({"OMP_NUM_THREADS"})
        os.environ["OMP_NUM_THREADS"] = "99"
        apply_thread_limit(4, log)
        eq("operator value kept", os.environ["OMP_NUM_THREADS"], "99")
        eq("others still updated", os.environ["MKL_NUM_THREADS"], "4")
    finally:
        th._operator_set = saved_op
        if saved_env is not None:
            os.environ["OMP_NUM_THREADS"] = saved_env

    print()
    print("FAILURES:" if fails else "ALL THREAD CHECKS PASSED")
    for f in fails:
        print(" -", f)
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
