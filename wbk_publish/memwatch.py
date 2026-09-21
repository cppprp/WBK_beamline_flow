"""Measure what the conversion actually uses, since nothing else will.

The worker count is derived from an estimate of memory per worker. The cluster
does not collect job accounting, so SLURM cannot report the real figure after the
fact, and a job that dies of memory pressure leaves no trace of how much it had.

This samples the worker processes while the conversion runs and logs the peak —
the largest single worker, and the total across all of them — against what the
plan assumed. Logged periodically, not only at the end, so a run that is killed
partway still leaves its last reading in the log. That is the number that
calibrates `resources.MEMORY_OVERHEAD_FACTOR`.

Sampling is a daemon thread that only ever reads `/proc`; it cannot affect the
conversion and never raises into it.
"""

import logging
import threading
import time

import psutil

from .resources import WorkerPlan, _fmt_bytes

# Below this, a worker's memory is mostly the interpreter and the libraries,
# not the data, and comparing it to the plan's per-slab figure says nothing.
_COMPARABLE_FROM = 256 * 2**20


class MemoryWatch:
    """Context manager around a parallel phase; reports peak worker memory."""

    def __init__(
        self,
        log: logging.Logger,
        plan: WorkerPlan,
        *,
        phase: str = "conversion",
        assumed_per_worker: int | None = None,
        sample_every: float = 5.0,
        report_every: float = 60.0,
    ) -> None:
        self.log = log
        self.plan = plan
        self.phase = phase
        # What this phase was sized on. Conversion is sized on the plan's
        # estimate; downsampling on a figure derived from the measured peak.
        self.assumed = assumed_per_worker or plan.mem_per_worker
        self.sample_every = sample_every
        self.report_every = report_every
        self.peak_total = 0
        self.peak_worker = 0
        self.workers_seen = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="memwatch", daemon=True)

    def __enter__(self) -> "MemoryWatch":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=self.sample_every * 2)
        self._sample()  # one last look, in case workers are still winding down
        self._report(final=True)

    # ── sampling ──────────────────────────────────────────────────────────────

    def _sample(self) -> None:
        try:
            me = psutil.Process()
            children = me.children(recursive=True)
        except psutil.Error:
            return

        worker_rss = []
        for child in children:
            try:
                worker_rss.append(child.memory_info().rss)
            except psutil.Error:  # gone between listing and reading; fine
                continue

        if not worker_rss:
            return

        self.workers_seen = max(self.workers_seen, len(worker_rss))
        self.peak_worker = max(self.peak_worker, max(worker_rss))
        self.peak_total = max(self.peak_total, sum(worker_rss))

    def _run(self) -> None:
        last_report = time.monotonic()
        while not self._stop.wait(self.sample_every):
            self._sample()
            if time.monotonic() - last_report >= self.report_every:
                self._report(final=False)
                last_report = time.monotonic()

    # ── reporting ─────────────────────────────────────────────────────────────

    def _report(self, *, final: bool) -> None:
        if self.workers_seen == 0:
            if final:
                self.log.info("Memory: no worker processes were observed")
            return

        assumed = self.assumed
        label = f"Peak memory ({self.phase})" if final else f"Memory so far ({self.phase})"
        line = (
            f"{label}: largest worker {_fmt_bytes(self.peak_worker)}, "
            f"total {_fmt_bytes(self.peak_total)} across up to {self.workers_seen} workers"
        )

        if assumed < _COMPARABLE_FROM:
            self.log.info(line + " (stack too small to check the plan against)")
            return

        ratio = self.peak_worker / assumed
        self.log.info(line + f" (plan assumed {_fmt_bytes(assumed)}/worker; actual is {ratio:.2f}x that)")

        if final and ratio > 1.15:
            # The safety margin in resources.ESTIMATE_MARGIN is 25%. Past that,
            # the sizing would have overcommitted the node on a bigger stack.
            knob = (
                "DOWNSAMPLE_PEAK_RATIO" if self.phase == "downsampling"
                else "MEMORY_OVERHEAD_FACTOR"
            )
            self.log.warning(
                f"Workers used {ratio:.2f}x the memory the {self.phase} phase was "
                f"sized on. On a larger stack this would exceed the node. Raise "
                f"{knob} in resources.py by about this ratio, or pass --max-memory "
                "to leave headroom until it is corrected."
            )
