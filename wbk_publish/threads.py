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

tensorstore is the exception to the environment-variable approach. webknossos
does all zarr3 I/O through it, and it sizes its data-copy and file-I/O pools
from the machine's core count with no variable to say otherwise: ~180 threads
per worker on a 176-core node, so 50 workers ask for ~9,000. That is not a
resource limit on this cluster (`ulimit -u` is 513,072) and raising the cap to
32 changed conversion time by three seconds — the pool crashes it was first
introduced for turned out to be memory exhaustion, not thread exhaustion, and
are fixed in resources.py. It stays because a worker sized for 176 cores when
it has been given 5 is simply wrong, and keeping the count near the plan makes
the "Threads" log line mean something. The cap is passed through
WBK_TENSORSTORE_THREADS and installed by `limit_tensorstore_threads`
— in the parent when the limit is applied, and in every worker when it imports
this package (spawned workers re-import the entry-point module, which imports
`wbk_publish`).
"""

import logging
import os
import sys

# Read by tensorstore-using code in this package, not by tensorstore itself.
TENSORSTORE_VAR = "WBK_TENSORSTORE_THREADS"

_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "BLOSC_NTHREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    TENSORSTORE_VAR,
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

    limit_tensorstore_threads(int(os.environ[TENSORSTORE_VAR]))


def limit_tensorstore_threads(threads: int) -> None:
    """Bound tensorstore's thread pools in this process.

    webknossos opens every array with one module-level `ts.Context`, imported by
    name into several modules, so the limited context has to replace each of
    those bindings (and the default argument that captured it). New arrays pick
    it up from then on; nothing already open is affected.
    """
    import tensorstore as ts
    import webknossos.dataset  # noqa: F401 - loads every module holding TS_CONTEXT

    threads = max(1, threads)
    limited = ts.Context(
        ts.Context.Spec(
            {
                "data_copy_concurrency": {"limit": threads},
                "file_io_concurrency": {"limit": threads},
            }
        )
    )
    for name, module in list(sys.modules.items()):
        if name.startswith("webknossos") and getattr(module, "TS_CONTEXT", None) is not None:
            module.TS_CONTEXT = limited

    helpers = sys.modules["webknossos.dataset._utils.tensorstore_helpers"]
    open_array = helpers.open_zarr3_array
    if open_array.__defaults__:
        open_array.__defaults__ = tuple(
            limited if isinstance(default, ts.Context) else default
            for default in open_array.__defaults__
        )


def install_tensorstore_limit_from_env() -> None:
    """In a worker: apply the cap the parent put in the environment, if any."""
    value = os.environ.get(TENSORSTORE_VAR)
    if value and value.isdigit():
        limit_tensorstore_threads(int(value))
