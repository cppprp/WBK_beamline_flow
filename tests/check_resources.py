import _support  # noqa: F401 - sets sys.path and env
import math
import os

from wbk_publish.resources import (
    BATCH_Z,
    StackShape,
    available_cores,
    available_memory,
    parse_memory_size,
    plan_workers,
)

GiB = 2**30
NODE_CORES = 176
NODE_RAM = 512 * GiB

fails = []


def eq(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


def stack(slices, xy_side, bytes_px=2):
    return StackShape(n_slices=slices, xy_pixels=xy_side * xy_side, bytes_per_pixel=bytes_px)


def node(s, **kw):
    return plan_workers(s, cores=NODE_CORES, total_ram=NODE_RAM, **kw)


print(f"— constants (BATCH_Z read from webknossos = {BATCH_Z})")
eq("batch z is the shard z-size", BATCH_Z, 32)

print("— node profile table (176 cores, 512 GiB, 8000 slices)")
rows = []
for side in (2048, 4096, 8192):
    p = node(stack(8000, side))
    rows.append((side, p))
    print(
        f"  {side}^2: mem/worker={p.mem_per_worker/GiB:5.2f} GiB  "
        f"workers={p.workers:3d}  threads={p.threads_per_worker}  limited_by={p.limited_by}"
    )

# 250 tasks over 176 cores is two waves; two waves of 125 is the same wall time
# with 51 fewer processes holding memory, so the plan evens them out.
eq("2048^2 is wave-balanced", rows[0][1].limited_by, "waves")
eq("2048^2 keeps its wave count", math.ceil(rows[0][1].tasks / rows[0][1].workers), 2)
eq("2048^2 waves are even", rows[0][1].workers, math.ceil(rows[0][1].tasks / 2))
# With the measured 6x factor a 4096^2 uint16 slab is 6 GiB per worker: memory
# binds before the 176 cores do, and the wave split then trims one more worker.
eq("4096^2 is bounded by memory, then evened out", rows[1][1].limited_by, "waves")
eq("4096^2 fits its budget", rows[1][1].workers * rows[1][1].mem_per_worker <= rows[1][1].usable_ram, True)
eq("4096^2 still uses most of the node", rows[1][1].workers >= 48, True)
eq("8192^2 is memory-limited", rows[2][1].limited_by, "memory")

big = rows[2][1]
if not (big.workers < NODE_CORES // 2):
    fails.append("8192^2 should be capped well below the core count")
    print(f"  FAIL 8192^2 workers not capped: {big.workers}")
else:
    print(f"  ok   8192^2 capped to {big.workers} workers")

if not (big.workers * big.mem_per_worker <= big.usable_ram):
    fails.append("8192^2 plan exceeds usable RAM")
    print("  FAIL 8192^2 plan exceeds usable RAM")
else:
    print(f"  ok   8192^2 fits: {big.workers * big.mem_per_worker/GiB:.0f} of {big.usable_ram/GiB:.0f} GiB")

if not (big.threads_per_worker > 1):
    fails.append("memory-limited plan should hand spare cores to threads")
    print("  FAIL memory-limited plan left cores idle")
else:
    print(f"  ok   spare cores given as threads: {big.threads_per_worker}")

print("— every plan stays inside both budgets")
for slices in (10, 1500, 8000, 40000):
    for side in (1024, 2048, 4096, 8192):
        p = node(stack(slices, side))
        if p.workers * p.mem_per_worker > p.usable_ram and p.warning is None:
            fails.append(f"overcommit RAM at {slices}x{side}^2")
        if p.workers > NODE_CORES:
            fails.append(f"overcommit cores at {slices}x{side}^2")
        if p.workers * p.threads_per_worker > NODE_CORES * 2:
            fails.append(f"thread explosion at {slices}x{side}^2")
print("  ok   16 combinations checked for RAM, core and thread overcommit")

print("— small recon must not grab the whole node")
small = node(stack(1500, 2048))
eq("1500 slices -> 47 tasks", small.tasks, 47)
eq("workers capped at task count", small.workers, 47)
eq("limited by tasks", small.limited_by, "tasks")

print("— extra workers that do not save a wave are not taken")
even = node(stack(2038, 3584, bytes_px=4))   # the beamline stack: 64 tasks
eq("64 tasks", even.tasks, 64)
eq("two full waves, not one and a stub", even.workers, 32)
eq("limited by waves", even.limited_by, "waves")
if even.workers * even.mem_per_worker > even.usable_ram:
    fails.append("wave-balanced plan still exceeds the budget")
    print("  FAIL wave-balanced plan exceeds the budget")
else:
    print(f"  ok   {even.workers * even.mem_per_worker/GiB:.0f} GiB of {even.usable_ram/GiB:.0f} GiB usable")

print("— operator upper bound is respected")
capped = node(stack(8000, 2048), max_workers=8)
eq("workers", capped.workers, 8)
eq("limited by request", capped.limited_by, "requested")
eq("threads take up the slack, capped at 16", capped.threads_per_worker, 16)

print("— a request larger than the machine is ignored, not honoured")
greedy = node(stack(8000, 8192), max_workers=1000)
eq("still memory-limited", greedy.limited_by, "memory")

print("— tiny stack")
tiny = node(stack(10, 512))
eq("one task", tiny.tasks, 1)
eq("one worker", tiny.workers, 1)
eq("threads capped, not one-per-core", tiny.threads_per_worker, 16)

print("— a single worker that cannot fit warns instead of silently overcommitting")
huge = plan_workers(stack(8000, 32768), cores=NODE_CORES, total_ram=8 * GiB)
eq("still runs one worker", huge.workers, 1)
if huge.warning is None:
    fails.append("no warning when a worker exceeds available RAM")
    print("  FAIL no warning emitted")
else:
    print("  ok   warned about the OOM risk")

print("— tight-fit warning")
NODE = dict(cores=NODE_CORES, total_ram=NODE_RAM, hard_limit=NODE_RAM)
stitch = StackShape(10339, 10507*10512, 2)

quiet_small = plan_workers(stack(1500, 2048), **NODE)
eq("small recon: silent", quiet_small.warning, None)
quiet_med = plan_workers(StackShape(2048, 2000*2000, 4), **NODE)
eq("2000^2 float32: silent", quiet_med.warning, None)

# The per-worker factor is measured, so a plan that fills its 90% budget is the
# normal case, not a risk — a warning there would fire on every large run.
full = plan_workers(stitch, **NODE)
eq("big stitch filling its budget: silent", full.warning, None)
eq("...and it is memory-limited", full.limited_by, "memory")

# --max-memory well above the machine is the case the warning exists for: the
# plan then fills a budget the node cannot honour. Its advice must silence it.
over = plan_workers(stitch, cores=NODE_CORES, total_ram=2 * NODE_RAM, hard_limit=NODE_RAM)
if over.warning and "--max-memory" in over.warning:
    print("  ok   budget above the machine warns and suggests a budget")
else:
    fails.append("over-budget plan did not warn"); print(f"  FAIL over-budget warning: {over.warning!r}")
acted = plan_workers(stitch, cores=NODE_CORES, total_ram=384*GiB, hard_limit=NODE_RAM)
eq("following the advice silences it", acted.warning, None)
if acted.workers >= over.workers:
    fails.append("lowering the budget did not reduce workers")
    print("  FAIL lowering the budget did not reduce workers")
else:
    print(f"  ok   workers drop {over.workers} -> {acted.workers} with the lower budget")

print("— the conversion cap is deployment policy, on top of --cpus")
import wbk_publish.resources as res
from wbk_publish.logs import make_logger

log = make_logger("res-check", verbose=False)
saved_probe, saved_env = res.probe_stack, {
    k: os.environ.get(k) for k in ("SLURM_CPUS_ON_NODE", "SLURM_CPUS_PER_TASK", "SLURM_MEM_PER_NODE")
}
# A stack with far more tasks than the cap, on a machine with room to spare, so
# nothing but the cap can be the binding constraint.
res.probe_stack = lambda _files: stack(8000, 2048)
try:
    os.environ.pop("SLURM_CPUS_PER_TASK", None)
    os.environ["SLURM_CPUS_ON_NODE"] = str(NODE_CORES)
    os.environ["SLURM_MEM_PER_NODE"] = str(512 * 1024)

    eq("default is the cap", res.plan_for_stack(["x"], log).workers, res.MAX_CONVERSION_WORKERS)
    eq("--cpus can lower it", res.plan_for_stack(["x"], log, max_workers=4).workers, 4)
    eq("--cpus cannot raise it",
       res.plan_for_stack(["x"], log, max_workers=64).workers, res.MAX_CONVERSION_WORKERS)

    # The cap is a ceiling, not a target: memory and task count still bind first.
    res.probe_stack = lambda _files: stack(200, 8192)   # 7 tasks, 24 GiB/worker
    small = res.plan_for_stack(["x"], log)
    eq("a short stack is still limited by its task count", small.limited_by, "tasks")
    eq("...and stays below the cap", small.workers < res.MAX_CONVERSION_WORKERS, True)
finally:
    res.probe_stack = saved_probe
    for k, v in saved_env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

print("— memory size parsing")
eq("400G", parse_memory_size("400G"), 400 * GiB)
eq("512GB", parse_memory_size("512GB"), 512 * GiB)
eq("8000M", parse_memory_size("8000M"), 8000 * 2**20)
eq("plain bytes", parse_memory_size("1024"), 1024)
for bad in ("", "abc", "-5G", "0"):
    try:
        parse_memory_size(bad)
        fails.append(f"accepted bad size {bad!r}")
        print(f"  FAIL accepted {bad!r}")
    except ValueError:
        print(f"  ok   rejects {bad!r}")

print("— allocation detection reads SLURM, not the machine")
saved = {k: os.environ.get(k) for k in ("SLURM_CPUS_ON_NODE", "SLURM_CPUS_PER_TASK", "SLURM_MEM_PER_NODE")}
try:
    os.environ.pop("SLURM_CPUS_PER_TASK", None)
    os.environ["SLURM_CPUS_ON_NODE"] = "32"
    eq("cores from SLURM_CPUS_ON_NODE", available_cores(), 32)

    os.environ["SLURM_MEM_PER_NODE"] = str(400 * 1024)
    eq("mem from SLURM_MEM_PER_NODE", available_memory(), 400 * GiB)

    # --exclusive / --mem=0 reports 0, meaning "all of it", not "none".
    os.environ["SLURM_MEM_PER_NODE"] = "0"
    if available_memory() <= 0:
        fails.append("SLURM_MEM_PER_NODE=0 treated as zero bytes")
        print("  FAIL SLURM_MEM_PER_NODE=0 read as zero bytes")
    else:
        print(f"  ok   SLURM_MEM_PER_NODE=0 falls through to real total ({available_memory()/GiB:.0f} GiB)")

    eq("explicit override wins", available_memory(123 * GiB), 123 * GiB)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print()
print("FAILURES:" if fails else "ALL RESOURCE CHECKS PASSED")
for f in dict.fromkeys(fails):
    print(" -", f)
raise SystemExit(1 if fails else 0)
