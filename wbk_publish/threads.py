"""Keep the numeric libraries from oversubscribing the machine.

Conversion runs N worker processes, and each would otherwise let numpy/BLAS/zstd
start a full complement of threads sized to the *machine's* core count — which is
not the same as this job's allocation. N workers times a machine-sized pool is how
a 176-core node ends up with thousands of runnable threads and runs slower than it
would with a tenth of them.

Timing matters. These variables are read by the numeric libraries when they are
first imported, so a value set afterwards has no effect on an already-imported
library. Two things make this workable:

  * `limit_thread_oversubscription()` runs at package import, before anything
    numeric is loaded, and installs a conservative default.
  * `apply_thread_limit()` sets the real value once the stack has been probed and
    the worker count is known. cluster_tools starts its pool with the "spawn"
    method, so each worker is a fresh interpreter that imports numpy from scratch
    and reads the environment as it stands at pool creation. Call it before
    building the executor and the workers get the right value.

A value the operator set themselves is never overwritten.
"""

import logging
import os

_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "BLOSC_NTHREADS",
    "NUMEXPR_NUM_THREADS",
)

# Conservative until the real worker count is known. Low rather than high: an
# under-threaded parent costs a little speed, an over-threaded one costs a lot.
DEFAULT_THREADS_PER_WORKER = 2

_operator_set: frozenset[str] = frozenset()
_initialised = False


def limit_thread_oversubscription(threads: int = DEFAULT_THREADS_PER_WORKER) -> None:
    """Install a default cap, remembering which values the operator had already set.

    Must run before numpy is imported — it is called from the package __init__
    ahead of every other import.
    """
    global _operator_set, _initialised

    if not _initialised:
        _operator_set = frozenset(var for var in _THREAD_VARS if var in os.environ)
        _initialised = True

    for var in _THREAD_VARS:
        os.environ.setdefault(var, str(threads))


def apply_thread_limit(threads: int, log: logging.Logger) -> None:
    """Set the per-worker thread cap now that the worker count is known.

    Overwrites the defaults installed at import, but leaves alone anything the
    operator set in the environment. Call before creating the executor.
    """
    value = str(max(1, threads))
    respected = sorted(_operator_set)
    changed = []

    for var in _THREAD_VARS:
        if var in _operator_set:
            continue
        if os.environ.get(var) != value:
            os.environ[var] = value
            changed.append(var)

    if changed:
        log.debug(f"Thread cap set to {value} for: {', '.join(changed)}")
    if respected:
        log.info(
            f"Leaving operator-set thread limits unchanged: "
            + ", ".join(f"{var}={os.environ[var]}" for var in respected)
        )
