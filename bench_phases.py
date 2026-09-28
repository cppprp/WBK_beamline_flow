"""Where does a conversion task's time actually go?

The outside numbers stopped being informative: a 2038-slice stack converts at
327 MB/s of output while the node reads its TIFFs at 1890 MB/s, writes 32
concurrent streams to the same NFS mount at 2.9 GB/s, and leaves 90% of the
cores idle. Something inside one worker is slow, and only splitting the work
apart says what.

This runs ONE worker over a handful of slices and times:

  decode        tifffile, no webknossos — the floor
  convert       add_layer_from_images, per target directory, compressed
  convert raw   the same with compress=False — the difference is zstd

Comparing a local target (tmpfs) with the NFS target separates the write path
from everything else. Comparing compressed with raw separates the codec.

    ./bench_phases.py <tiff_stack> [n_slices] [target_dir ...]

Defaults to 64 slices (two tasks' worth) into /dev/shm and /morningstar/tmp.
Run it on the node, inside an allocation.
"""

import os
import shutil
import sys
import time
from pathlib import Path


def fmt(nbytes: float, seconds: float) -> str:
    return f"{seconds:7.1f}s  {nbytes / seconds / 1e6:7.0f} MB/s"


def main() -> int:
    import wbk_publish  # noqa: F401 - must precede numpy for the caps to take
    import cluster_tools
    import tifffile
    from webknossos import DataFormat, Dataset

    from wbk_publish.logs import make_logger
    from wbk_publish.threads import apply_thread_limit

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    stack = Path(args[0])
    n_slices = int(args[1]) if len(args) > 1 else 64
    targets = [Path(t) for t in (args[2:] or ["/dev/shm", "/morningstar/tmp"])]

    # Match a real worker. Without this the benchmark would inherit the
    # import-time default of 2 and its rates would not compare with a run whose
    # plan gave each worker 5. WBK_BENCH_THREADS overrides for a sweep.
    apply_thread_limit(int(os.environ.get("WBK_BENCH_THREADS", "5")),
                       make_logger("bench", verbose=False))

    files = sorted(list(stack.glob("*.tif")) + list(stack.glob("*.tiff")))[:n_slices]
    if not files:
        print(f"no TIFFs in {stack}")
        return 2

    on_disk = sum(f.stat().st_size for f in files)
    print(f"{len(files)} slices, {on_disk / 1e9:.2f} GB on disk")
    print(f"threads: OMP={os.environ.get('OMP_NUM_THREADS')} "
          f"tensorstore={os.environ.get('WBK_TENSORSTORE_THREADS')}\n")

    start = time.perf_counter()
    decoded = sum(tifffile.imread(f).nbytes for f in files)
    print(f"  decode only          {fmt(decoded, time.perf_counter() - start)}")

    for target in targets:
        if not target.is_dir():
            print(f"  -- skipping {target}, not a directory")
            continue
        for compress in (True, False):
            out = target / f"phasebench_{os.getpid()}"
            shutil.rmtree(out, ignore_errors=True)
            try:
                dataset = Dataset(out, voxel_size=(650.0, 650.0, 650.0))
                with cluster_tools.get_executor("multiprocessing", max_workers=1) as ex:
                    start = time.perf_counter()
                    dataset.add_layer_from_images(
                        images=[str(f) for f in files],
                        layer_name="xray",
                        category="color",
                        data_format=DataFormat.Zarr3,
                        compress=compress,
                        executor=ex,
                    )
                    elapsed = time.perf_counter() - start
                written = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
                label = "compressed" if compress else "raw       "
                print(f"  convert {label} -> {str(target):<18} {fmt(decoded, elapsed)}"
                      f"   (wrote {written / 1e9:.2f} GB)")
            finally:
                shutil.rmtree(out, ignore_errors=True)

    print("\nRates are per input byte, so they compare directly with the 477 MB/s")
    print("the full 32-worker run achieves. One worker reaching a similar rate means")
    print("the pipeline does not scale; one worker being much faster means it does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
