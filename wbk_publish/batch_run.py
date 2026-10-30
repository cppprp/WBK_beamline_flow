"""Publish a whole batch inside one allocation, one line after another.

`wbk_batch.sh <list> --sequential` submits this instead of one job per line.
That is what lines which must not run at the same time need — several
channels or layers of one dataset, which the per-dataset lock would otherwise
let only one of through — and it keeps a batch on a single node.

The list is not parsed here; `wbk_batch.sh` writes the same expanded plan it
writes for a batch dry run (see batch_check.py), one submission per line:

    wbk_publish.manual<TAB>--voxel-size<TAB>650<TAB>--zarr<TAB>/mnt/.../ch0.zarr ...

Each line runs in a fresh interpreter, exactly as it would as its own job. A
publish sets thread caps and builds worker pools sized for the node, and a new
process gives every line the same clean start; it also means a line that dies —
killed for memory, say — ends that line, not the batch.

A failed line does not stop the ones after it. Lines that depend on it fail on
their own, safely: a layer meant for a dataset that was never created is refused
before any conversion (see publish._orphan_layer_message). The summary at the
end lists what failed, with the time each line took.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .logs import make_logger


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.batch_run",
        description="Run every line of an expanded batch plan in turn, for real.",
    )
    parser.add_argument("--plan", required=True, help="Expanded plan written by wbk_batch.sh")
    args = parser.parse_args(argv)

    log = make_logger("batch-run", verbose=False)
    plan = Path(args.plan)
    if not plan.is_file():
        log.error(f"Plan file not found: {plan}")
        return 2

    entries = [
        line.split("\t")
        for line in plan.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    log.info(f"Publishing {len(entries)} submission(s), one after another\n")

    results: list[tuple[str, int, float]] = []
    started_all = time.monotonic()
    for index, (module_name, *cli_args) in enumerate(entries, start=1):
        target = next((a for a in cli_args if a.startswith("/")), "?")
        log.info("=" * 78)
        log.info(f"[{index}/{len(entries)}] {target}")
        log.info("=" * 78)
        sys.stdout.flush()  # keep our headers ahead of the child's output

        started = time.monotonic()
        code = subprocess.run([sys.executable, "-m", module_name, *cli_args]).returncode
        elapsed = time.monotonic() - started
        results.append((target, code, elapsed))
        log.info(f"[{index}/{len(entries)}] {'done' if code == 0 else f'FAILED (exit {code})'} "
                 f"in {_fmt(elapsed)}\n")

    log.info("=" * 78)
    for target, code, elapsed in results:
        log.info(f"  {'ok    ' if code == 0 else 'FAILED'}  {_fmt(elapsed)}  {target}")
    failed = [r for r in results if r[1] != 0]
    log.info(f"{len(results) - len(failed)} of {len(results)} succeeded "
             f"in {_fmt(time.monotonic() - started_all)}")
    if failed:
        log.error("Each line's own error is in its section above. Fix those lines and "
                  "re-submit only them; the ones that succeeded keep their dataset ids.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
