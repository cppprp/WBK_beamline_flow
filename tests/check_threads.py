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


def _tensorstore_in_worker(_ignored):
    """What a real worker does: import the package, then write a zarr3 array."""
    import sys, tempfile
    import numpy as np
    import tensorstore as ts
    import wbk_publish  # noqa: F401 - installs the cap from the environment

    array_mod = sys.modules["webknossos.dataset.layer.view._array"]
    spec = array_mod.TS_CONTEXT.spec.to_json()

    # Exercise the pools so the threads exist, then count them.
    arr = ts.open({
        "driver": "zarr3", "kvstore": {"driver": "file", "path": tempfile.mkdtemp()},
        "metadata": {"shape": [512, 512, 64], "data_type": "float32",
                     "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [512, 512, 32]}},
                     "codecs": [{"name": "sharding_indexed", "configuration": {
                         "chunk_shape": [32, 32, 32],
                         "codecs": [{"name": "bytes"}, {"name": "zstd", "configuration": {"level": 1}}]}}]},
        "create": True}, context=array_mod.TS_CONTEXT).result()
    arr[...] = np.zeros((512, 512, 64), "float32")
    return {"limit": spec.get("data_copy_concurrency", {}).get("limit"),
            "os_threads": _thread_count()}


def _thread_count():
    import subprocess, sys
    pid = os.getpid()
    if sys.platform == "darwin":
        out = subprocess.check_output(["sh", "-c", f"ps -M {pid} | wc -l"])
        return int(out) - 1
    return len(os.listdir(f"/proc/{pid}/task"))


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

    print("— tensorstore's pools are capped inside the workers")
    with cluster_tools.get_executor("multiprocessing", max_workers=1) as ex:
        ts_report = ex.submit(_tensorstore_in_worker, 0).result()
    eq("worker context carries the cap", ts_report["limit"], 3)
    # Uncapped, tensorstore starts ~1.5x the core count; capped at 3 a worker
    # stays in the single digits plus the interpreter's own threads.
    eq("worker OS threads bounded", ts_report["os_threads"] <= 12, True)
    print(f"       ({ts_report['os_threads']} OS threads in the worker, {os.cpu_count()} cores)")

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
