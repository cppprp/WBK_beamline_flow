"""Sizing the conversion to the machine it is running on.

Two numbers decide how a conversion should be parallelised, and they pull in
opposite directions:

  * How many tasks exist. `add_layer_from_images` splits work along z only —
    each task takes the full xy extent for one shard's worth of slices — so the
    task count is `ceil(n_slices / 32)` and nothing else. Slice size adds no
    parallelism.

  * How much memory each task needs. That *is* set by slice size: a worker holds
    the full xy plane for those 32 slices. This cannot be tuned down; the batch
    size may not go below the shard z-size for compressed layers.

Sizing workers from the core count is what produces the failures this module
exists to prevent — 176 workers on a large stitch will ask for more RAM than the
node has, and an OOM there takes the whole run with it. So: derive the worker
count from memory first, then hand the leftover cores to each worker as threads.
"""

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

# The z-extent of one conversion task. Read from webknossos rather than hardcoded
# so this tracks the library if its shard defaults ever change.
from webknossos.dataset.defaults import DEFAULT_SHARD_SHAPE_FROM_IMAGES

BATCH_Z = DEFAULT_SHARD_SHAPE_FROM_IMAGES.z

# Peak worker memory during conversion. Two terms, because measurement says one
# is not enough:
#
#   slab = full xy extent x 32 slices x bytes per pixel
#   mem  = max(WORKER_BASE + SLAB_FACTOR * slab,  MEMORY_OVERHEAD_FACTOR * slab)
#
# The proportional part is the buffer work — decoded slices, the assembled
# array, the transposed copy for the zarr axis order. The fixed part is a cost
# each worker carries whatever the image size, and its cause is NOT established.
# Two plausible explanations were tested and both failed:
#
#   * padding to the 4096x4096x32 shard grid — no: the write buffer is clamped
#     to the real slice size (buffered_slice_writer.py takes min(shard, width)),
#     and zarr3 sharding omits chunks outside the array domain entirely.
#   * tensorstore's unbounded chunk cache — no: converting 96 slices of 1024^2
#     peaked at 4.65 GiB with the default cache and 5.2-5.4 GiB with the pool
#     capped at 256 MB and 64 MB. Capping it changes nothing.
#
# So the term is empirical. It is included because leaving it out underestimated
# a real run by 2.5x, not because we know what it is.
#
# Measured, both on webknossos 4.0:
#   3584^2 float32, slab 1.53 GiB -> 9.8 GiB per worker
#   1994^2 float32, slab 0.47 GiB -> 7.2 GiB per worker
#
# Those two solve to 6.0 GiB fixed + 2.46 x slab, which is a two-parameter fit
# to two points — enough to trust where it raises the estimate, not enough to
# trust where it lowers one. Hence the max(): the old pure-multiple formula
# still wins for large slabs, so big stitches keep their existing (conservative)
# sizing until a run measures one. Re-check both against the "Peak memory" line.
WORKER_BASE = 6 * 2**30
SLAB_FACTOR = 2.5
MEMORY_OVERHEAD_FACTOR = 6.0

# Downsampling, as a multiple of the conversion peak measured in the same run.
# Once the pyramid keeps mag 1's flat shard shape (see convert.py) a downsample
# worker is the smaller of the two: 8.4 GiB against 9.8 on the 3584^2 float32
# stack, i.e. 0.86x. 1.2 leaves headroom above that without throwing away
# workers — and workers matter here, because a downsample job is one target
# shard, so mag 2 of that stack is 32 jobs and anything under 32 workers runs
# it in multiple waves. The earlier 2.5 was measured against the 1024^3 shards
# 4.x used before, where one job assembled a 4 GiB buffer.
DOWNSAMPLE_PEAK_RATIO = 1.1

# The share of RAM the workers may hold. The rest is not spare — it is the page
# cache the TIFF reads come through and the dirty pages the writes go out
# through, and on this node the latter now moves at over a gigabyte a second.
# A run planned at 90% (49 workers x 9.2 GiB of 503 GiB) died with a MemoryError
# on a 1.5 GiB buffer while the workers held 420 GiB: the memory was there on
# paper and not in practice.
RAM_SAFETY_FRACTION = 0.75

# How wrong the per-worker figure could be and still have to fit. A plan that
# would exceed the machine if the real figure were this much higher is flagged.
# MEMORY_OVERHEAD_FACTOR is measured (a 3584^2 float32 run came in at 0.91x it),
# so 10% covers shape and dtype variation; it must stay below the headroom
# RAM_SAFETY_FRACTION leaves (10%), or a plan that merely fills its budget would
# trip the warning on every memory-limited run and it would be ignored.
#
# The test is against the hard limit the OOM killer enforces, not against the
# budget: when memory is the binding constraint the plan always fills its budget.
ESTIMATE_MARGIN = 0.10

# Conversion writes faster than the staging filesystem accepts, so past a point
# more workers only queue for a full pipe — and then start costing throughput.
# Measured on morningstar (389 MB/s for 8 concurrent incompressible streams),
# converting a 2038 x 3584^2 float32 stack:
#
#     8 workers   3:17   392 MB/s    66 GiB
#    16 workers   3:04   420 MB/s   135 GiB   <- at the ceiling
#    32 workers   3:43   347 MB/s   266 GiB   <- past it, and slower
#
# 16 is the default: the fastest measured, and it leaves three quarters of the
# node free for other work. This tracks the *storage*, not the data — a bigger
# stack does not want more workers, it wants the same ones for longer. Raise it
# when the storage gets faster (WK_MAX_CONVERSION_WORKERS), or per run with
# --cpus / --jobs, which is applied on top of this and can only lower it.
MAX_CONVERSION_WORKERS = int(os.environ.get("WK_MAX_CONVERSION_WORKERS", "16"))

# Spare cores are handed to each worker as threads, but only up to a point: BLAS
# and zstd stop scaling long before a whole node's worth of threads, and past that
# the extra threads just contend. A job with few tasks leaves the node partly idle
# rather than pretending 88 threads will help.
MAX_THREADS_PER_WORKER = 16


@dataclass(frozen=True)
class StackShape:
    """What one TIFF header tells us about the whole stack."""

    n_slices: int
    xy_pixels: int
    bytes_per_pixel: int

    @property
    def slice_bytes(self) -> int:
        return self.xy_pixels * self.bytes_per_pixel


@dataclass(frozen=True)
class WorkerPlan:
    workers: int
    threads_per_worker: int
    tasks: int
    mem_per_worker: int
    cores: int
    usable_ram: int
    limited_by: str  # cores | memory | tasks | requested | waves
    warning: str | None = None

    def describe(self) -> str:
        return (
            f"{self.workers} workers x {self.threads_per_worker} threads "
            f"({self.tasks} tasks, {_fmt_bytes(self.mem_per_worker)}/worker, "
            f"limited by {self.limited_by})"
        )


def _fmt_bytes(num_bytes: float) -> str:
    """Human-readable size, switching unit so small values stay informative."""
    if num_bytes >= 2**30:
        return f"{num_bytes / 2**30:.1f} GiB"
    if num_bytes >= 2**20:
        return f"{num_bytes / 2**20:.0f} MiB"
    return f"{num_bytes / 2**10:.0f} KiB"


# ── Reading what the machine actually gave us ─────────────────────────────────


def available_cores() -> int:
    """Cores this process may actually use.

    `os.cpu_count()` reports the machine, not the allocation — inside a SLURM
    cpuset on a 176-core node it still says 176, which is how thread pools end up
    oversubscribed by an order of magnitude. Prefer what SLURM and the scheduler
    affinity mask say.
    """
    slurm_cpus = os.environ.get("SLURM_CPUS_PER_TASK") or os.environ.get(
        "SLURM_CPUS_ON_NODE"
    )
    if slurm_cpus and slurm_cpus.isdigit():
        return max(1, int(slurm_cpus))

    # Linux only, and the authority when cgroup CPU confinement is in use.
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))

    return max(1, os.cpu_count() or 1)


def available_memory(explicit: int | None = None) -> int:
    """Bytes of RAM this run may use, before the safety margin."""
    if explicit:
        return explicit

    # SLURM reports whole-node memory in MB. A zero means "all of it" (what
    # --mem=0 or --exclusive gives), not "none" — fall through to the real total.
    per_node = os.environ.get("SLURM_MEM_PER_NODE")
    if per_node and per_node.isdigit() and int(per_node) > 0:
        return int(per_node) * 2**20

    per_cpu = os.environ.get("SLURM_MEM_PER_CPU")
    if per_cpu and per_cpu.isdigit() and int(per_cpu) > 0:
        return int(per_cpu) * 2**20 * available_cores()

    import psutil  # a hard dependency of webknossos

    return int(psutil.virtual_memory().total)


_SIZE_SUFFIXES = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}


def parse_memory_size(value: str) -> int:
    """Parse '400G', '512GB', '8000M' or a plain byte count into bytes."""
    text = str(value).strip().upper().rstrip("B")
    if not text:
        raise ValueError("Empty memory size")

    multiplier = 1
    if text[-1] in _SIZE_SUFFIXES:
        multiplier = _SIZE_SUFFIXES[text[-1]]
        text = text[:-1]

    try:
        amount = float(text)
    except ValueError:
        raise ValueError(
            f"Cannot parse memory size {value!r}. Use e.g. '400G' or '8000M'."
        ) from None

    if amount <= 0:
        raise ValueError(f"Memory size must be positive, got {value!r}")

    return int(amount * multiplier)


def probe_stack(tiff_files: list[Path]) -> StackShape:
    """Derive the stack geometry from the file count and one TIFF header.

    Only the first file's header is read — no image data is decoded, so this is
    cheap even for a twenty-thousand slice stitch.
    """
    import tifffile

    if not tiff_files:
        raise ValueError("Cannot probe an empty stack")

    # A truncated or non-TIFF first slice would fail the conversion anyway, an hour
    # in. Catching it here costs milliseconds and names the offending file.
    try:
        with tifffile.TiffFile(tiff_files[0]) as handle:
            page = handle.pages[0]
            shape = tuple(page.shape)
            bytes_per_pixel = page.dtype.itemsize if page.dtype is not None else 2
    except Exception as exc:
        size = tiff_files[0].stat().st_size if tiff_files[0].exists() else 0
        raise RuntimeError(
            f"Cannot read the first slice: {tiff_files[0]} ({size} bytes)\n"
            f"  {type(exc).__name__}: {exc}\n"
            "The file is truncated or not a TIFF. Check the stack before publishing."
        ) from exc

    # Shape is (y, x) for plain greyscale, with a trailing sample axis for RGB.
    xy_pixels = shape[0] * shape[1] if len(shape) >= 2 else shape[0]
    samples = shape[2] if len(shape) >= 3 else 1

    return StackShape(
        n_slices=len(tiff_files),
        xy_pixels=xy_pixels,
        bytes_per_pixel=bytes_per_pixel * samples,
    )


# ── The sizing decision ───────────────────────────────────────────────────────


def plan_workers(
    stack: StackShape,
    *,
    cores: int,
    total_ram: int,
    hard_limit: int | None = None,
    max_workers: int | None = None,
) -> WorkerPlan:
    """Choose a worker count and a per-worker thread cap.

    `total_ram` is the budget this run may spend. `hard_limit` is what the machine
    will actually enforce — the same as the budget unless the operator lowered it
    with --max-memory, in which case the gap between them is the safety margin.

    `max_workers` is an upper bound from the operator, not a target: asking for
    more workers than the data has tasks, or than memory allows, is silently
    reduced rather than honoured.
    """
    if hard_limit is None:
        hard_limit = total_ram
    tasks = max(1, math.ceil(stack.n_slices / BATCH_Z))
    slab = stack.slice_bytes * BATCH_Z
    mem_per_worker = max(
        1,
        int(WORKER_BASE + slab * SLAB_FACTOR),
        int(slab * MEMORY_OVERHEAD_FACTOR),
    )
    usable_ram = int(total_ram * RAM_SAFETY_FRACTION)

    limits = {
        "cores": cores,
        "tasks": tasks,
        "memory": usable_ram // mem_per_worker,
    }
    if max_workers is not None:
        limits["requested"] = max_workers

    limited_by = min(limits, key=lambda key: limits[key])
    workers = max(1, min(limits.values()))

    # Tasks run in waves of `workers`. Once the wave count is fixed, extra
    # workers only make the last wave emptier — 64 tasks take two waves whether
    # there are 32 workers or 49, and the 49 cost 50% more memory for the same
    # wall time. Spread the tasks evenly over the waves instead.
    waves = math.ceil(tasks / workers)
    balanced = math.ceil(tasks / waves)
    if balanced < workers:
        workers = balanced
        limited_by = "waves"

    peak_ram = workers * mem_per_worker

    warning = None
    if limits["memory"] < 1:
        warning = (
            f"A single worker needs {_fmt_bytes(mem_per_worker)} but only "
            f"{_fmt_bytes(usable_ram)} is available. Running one worker anyway — "
            "this may be killed by the OOM handler."
        )
    elif peak_ram * (1 + ESTIMATE_MARGIN) > hard_limit:
        suggested = max(1, int(hard_limit * 0.75) // 2**30)
        warning = (
            f"Tight memory fit: {_fmt_bytes(peak_ram)} planned against a "
            f"{_fmt_bytes(hard_limit)} limit. Per-worker memory is an estimate "
            f"({_fmt_bytes(WORKER_BASE)} fixed plus a multiple of the slab), not a "
            f"measurement — if the real figure is more than {ESTIMATE_MARGIN:.0%} "
            f"higher this run will be OOM-killed. For a first run at this size use "
            f"--max-memory {suggested}G, then relax it once you have seen the actual usage."
        )

    # Hand the cores that memory left unused to each worker as threads, within
    # the range where extra threads still buy anything.
    threads_per_worker = max(1, min(cores // workers, MAX_THREADS_PER_WORKER))

    return WorkerPlan(
        workers=workers,
        threads_per_worker=threads_per_worker,
        tasks=tasks,
        mem_per_worker=mem_per_worker,
        cores=cores,
        usable_ram=usable_ram,
        limited_by=limited_by,
        warning=warning,
    )


def downsample_workers(
    plan: WorkerPlan, measured_peak: int, log: logging.Logger
) -> tuple[int, int]:
    """Size the downsample pool from what conversion actually used.

    Deliberately not bounded by the conversion worker count. The two phases
    want opposite things and are measured to disagree: conversion is slower
    with more workers (3:43 at 32 against 3:17 at 8 on the reference stack,
    because the per-task memory shuffling contends), while downsampling is
    faster (1:27 against 2:07, because it is job-count-limited and barely
    touches memory). Tying them together gives up one or the other.

    `measured_peak` is the largest worker seen during conversion; the larger
    of that and the plan's estimate is the baseline, since a measurement below
    the estimate is not a reason to be less careful.

    Returns (workers, assumed bytes per worker).
    """
    baseline = max(measured_peak, plan.mem_per_worker)
    per_worker = int(baseline * DOWNSAMPLE_PEAK_RATIO)

    # One downsample job is one target shard. Mag 2 halves z, so its job count
    # is about half the conversion task count — the coarser levels have fewer
    # still, and sizing for the first (much the largest) level is what matters.
    # A big stitch also splits in xy, which only adds jobs; ignoring that keeps
    # the estimate on the conservative side.
    jobs = max(1, math.ceil(plan.tasks / 2))

    workers = max(1, min(plan.cores, plan.usable_ram // per_worker, jobs))
    waves = math.ceil(jobs / workers)
    workers = max(1, math.ceil(jobs / waves))

    log.info(
        f"Downsampling with {workers} workers "
        f"({_fmt_bytes(per_worker)}/worker assumed: conversion peaked at "
        f"{_fmt_bytes(baseline)}, and downsampling has been seen at "
        f"{DOWNSAMPLE_PEAK_RATIO}x that; ~{jobs} jobs in the first level)"
    )
    return workers, per_worker


def plan_for_stack(
    tiff_files: list[Path],
    log: logging.Logger,
    *,
    max_workers: int | None = None,
    max_memory: int | None = None,
) -> WorkerPlan:
    """Probe the stack, size the run, and report the reasoning."""
    stack = probe_stack(tiff_files)
    cores = available_cores()
    total_ram = available_memory(max_memory)

    # Deployment policy, applied on top of whatever the operator asked for: the
    # sizing arithmetic below knows about cores, memory and task count, but not
    # about how fast the staging filesystem accepts bytes.
    capped = min(max_workers or MAX_CONVERSION_WORKERS, MAX_CONVERSION_WORKERS)

    # The budget may be lowered by --max-memory; the hard limit is what the
    # machine enforces regardless, and is what the safety check measures against.
    plan = plan_workers(
        stack,
        cores=cores,
        total_ram=total_ram,
        hard_limit=available_memory(),
        max_workers=capped,
    )

    log.info(
        f"Stack: {stack.n_slices} slices, {stack.xy_pixels:,} px/slice, "
        f"{stack.bytes_per_pixel} B/px"
    )
    log.info(
        f"Machine: {cores} cores, {_fmt_bytes(total_ram)} RAM "
        f"({_fmt_bytes(plan.usable_ram)} usable)"
    )
    if plan.workers == capped == MAX_CONVERSION_WORKERS:
        log.info(
            f"Conversion capped at {MAX_CONVERSION_WORKERS} workers — more than the "
            "staging filesystem can absorb. Raise WK_MAX_CONVERSION_WORKERS if it "
            "has been made faster."
        )
    log.info(f"Plan: {plan.describe()}")
    if plan.warning:
        log.warning(plan.warning)

    return plan
