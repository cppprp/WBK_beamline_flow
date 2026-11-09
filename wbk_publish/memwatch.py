"""Measure what the conversion actually uses, since nothing else will.

The worker count is derived from an estimate of memory per worker. The cluster
does not collect job accounting, so SLURM cannot report the real figure after the
fact, and a job that dies of memory pressure leaves no trace of how much it had.

This samples the worker processes while the conversion runs and logs the peak —
the largest single worker, and the total across all of them — against what the
plan assumed. Threads are counted the same way, because a thread cap that is
set but not honoured (tensorstore's pools, before they were bounded) shows up
here and nowhere else: memory looked fine on the run that died of it.

The full breakdown — CPU, worker states, threads, the plan comparison — is
logged once, when the phase ends. In between there is a single line every few
minutes, because a run that is killed partway never reaches the final report and
its last reading is then the only number there is; that is what calibrates
`resources.MEMORY_OVERHEAD_FACTOR`. Four near-identical lines a minute, which is
what this used to print, buried everything else in the log.

Sampling is a daemon thread that only ever reads `/proc`; it cannot affect the
conversion and never raises into it. It runs in the idle parent process, not in
the workers. Every two seconds, because the figure that matters is the peak and
a run has died between samples: ~100 reads from a virtual filesystem, well under
a millisecond, against a node-minute of real work.
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
        sample_every: float = 2.0,
        report_every: float = 300.0,
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
        self.peak_threads_worker = 0
        self.peak_threads_total = 0
        self.workers_seen = 0
        self._cpu_samples: list[float] = []
        self._states: dict[str, int] = {}
        self._warned = False
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
        # Whole-node utilisation since the previous sample. It answers the
        # question the timings alone cannot: whether a phase is waiting on the
        # cores or on the storage. Only meaningful under --exclusive, which is
        # how these jobs run.
        try:
            self._cpu_samples.append(psutil.cpu_percent(interval=None))
        except psutil.Error:
            pass

        try:
            me = psutil.Process()
            children = me.children(recursive=True)
        except psutil.Error:
            return

        worker_rss, worker_threads = [], []
        for child in children:
            try:
                worker_rss.append(child.memory_info().rss)
                worker_threads.append(child.num_threads())
                # Running, or blocked? This is the *main* thread's state, which
                # for a worker doing its I/O through tensorstore's own thread
                # pool reads "sleeping" while those threads work. Useful for
                # spotting disk-sleep (uninterruptible I/O waits); not a measure
                # of whether the worker as a whole is busy — CPU percentage is.
                state = child.status()
                self._states[state] = self._states.get(state, 0) + 1
            except psutil.Error:  # gone between listing and reading; fine
                continue

        if not worker_rss:
            return

        self.workers_seen = max(self.workers_seen, len(worker_rss))
        self.peak_worker = max(self.peak_worker, max(worker_rss))
        self.peak_total = max(self.peak_total, sum(worker_rss))
        self.peak_threads_worker = max(self.peak_threads_worker, max(worker_threads))
        self.peak_threads_total = max(self.peak_threads_total, sum(worker_threads))

    def _run(self) -> None:
        last_report = time.monotonic()
        while not self._stop.wait(self.sample_every):
            self._sample()
            if time.monotonic() - last_report >= self.report_every:
                self._report(final=False)
                last_report = time.monotonic()

    # ── reporting ─────────────────────────────────────────────────────────────

    def _report_progress(self) -> None:
        """One line, so the log stays readable while the phase runs.

        Enough to calibrate from if the run is killed before the final report,
        which is the only reason to print anything mid-phase at all. The CPU,
        worker-state and thread breakdowns do not change meaningfully minute to
        minute, so they wait for the end.
        """
        if self.workers_seen == 0:
            return

        line = (
            f"Memory so far ({self.phase}): largest worker {_fmt_bytes(self.peak_worker)}, "
            f"total {_fmt_bytes(self.peak_total)}, {self.workers_seen} workers"
        )
        ratio = self.peak_worker / self.assumed if self.assumed else 0.0
        if self.assumed >= _COMPARABLE_FROM:
            line += f" ({ratio:.2f}x plan)"
        self.log.info(line)

        # Once, and now rather than in a final report an OOM-killed run never
        # reaches. The detailed advice stays at the end.
        if ratio > 1.15 and self.assumed >= _COMPARABLE_FROM and not self._warned:
            self._warned = True
            self.log.warning(
                f"Workers are using {ratio:.2f}x the memory this phase was sized on. "
                "If the job dies, re-run with --max-memory below the node's RAM to "
                "force fewer workers."
            )

    def _report(self, *, final: bool) -> None:
        if not final:
            self._report_progress()
            return

        # The first cpu_percent() call only starts the clock; it reads 0.0.
        busy = [c for c in self._cpu_samples[1:]]
        if busy:
            mean = sum(busy) / len(busy)
            self.log.info(
                f"CPU ({self.phase}): {mean:.0f}% of {psutil.cpu_count()} cores busy on "
                f"average, peak {max(busy):.0f}% — "
                + ("cores are the limit" if mean >= 80 else
                   "not CPU-bound; the limit is I/O or too few workers")
            )

        if self.workers_seen == 0:
            self.log.info("Memory: no worker processes were observed")
            return

        assumed = self.assumed
        line = (
            f"Peak memory ({self.phase}): largest worker {_fmt_bytes(self.peak_worker)}, "
            f"total {_fmt_bytes(self.peak_total)} across up to {self.workers_seen} workers"
        )

        if self._states:
            total = sum(self._states.values())
            breakdown = ", ".join(
                f"{name} {count / total:.0%}"
                for name, count in sorted(self._states.items(), key=lambda kv: -kv[1])
            )
            self.log.info(f"Worker state ({self.phase}, main thread): {breakdown}")

        threads = (
            f"Threads ({self.phase}): up to {self.peak_threads_worker} per worker, "
            f"{self.peak_threads_total} across all workers "
            f"(plan: {self.plan.threads_per_worker} per worker; a few more is normal — "
            "the interpreter and the pool's own threads — a multiple is not)"
        )

        if assumed < _COMPARABLE_FROM:
            self.log.info(line + " (stack too small to check the plan against)")
            self.log.info(threads)
            return

        ratio = self.peak_worker / assumed
        self.log.info(line + f" (plan assumed {_fmt_bytes(assumed)}/worker; actual is {ratio:.2f}x that)")
        self.log.info(threads)

        if ratio > 1.15:
            # Past the margin in resources.ESTIMATE_MARGIN, the sizing would have
            # overcommitted the node on a bigger stack.
            if self.phase == "downsampling":
                advice = ("Raise DOWNSAMPLE_PEAK_RATIO in resources.py by about "
                          "this ratio.")
            else:
                # Not a single knob: the estimate is a fixed term plus a multiple
                # of the slab, and which one is wrong depends on the stack. A
                # small stack that overshoots means WORKER_BASE is too low; a
                # large one means SLAB_FACTOR is.
                advice = (
                    f"The estimate is {_fmt_bytes(self.plan.mem_per_worker)}/worker = "
                    "SHARD_FACTOR x shard + SLAB_FACTOR x slab (resources.py). On a "
                    "small stack an overshoot means SHARD_FACTOR is too low; on a "
                    "large one, SLAB_FACTOR. Record this run's slab, dtype and peak "
                    "before changing either — one number cannot fit both ends."
                )
            self.log.warning(
                f"Workers used {ratio:.2f}x the memory the {self.phase} phase was "
                f"sized on. On a larger stack this could exceed the node. {advice} "
                "Pass --max-memory to leave headroom until it is corrected."
            )
