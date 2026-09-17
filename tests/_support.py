"""Shared setup for the check scripts.

Importing this puts the repository root on `sys.path`, points the pipeline at a
scratch work directory instead of the real beamline and storage paths, and
provides the tiny sample dataset that the conversion and NGFF checks share.

Import it first, before anything from `wbk_publish` — the environment it sets is
read at import time by `wbk_publish.config`.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Scratch space for fixtures. Override with WBK_TEST_WORK to run somewhere else.
WORK = Path(os.environ.get("WBK_TEST_WORK", REPO_ROOT / "tests" / ".work"))
(WORK / "tmp").mkdir(parents=True, exist_ok=True)

# "/" is always a mount point, so the beegfs guard passes without a real mount.
os.environ.setdefault("WK_BEEGFS_MOUNT", "/")
os.environ.setdefault("WK_TEMP_BASE", str(WORK / "tmp"))

SAMPLE = WORK / "conv"
SLICES, SIDE = 64, 128


class Report:
    """Minimal pass/fail reporting, so the checks need no test framework."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: str = "") -> bool:
        print(("  ok   " if condition else "  FAIL ") + label + (f": {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)
        return condition

    def eq(self, label: str, got, want) -> bool:
        return self.ok(label, got == want, f"{got!r}" if got == want else f"{got!r} (want {want!r})")

    def finish(self, banner: str) -> int:
        print()
        print("FAILURES:" if self.failures else banner)
        for f in dict.fromkeys(self.failures):
            print(" -", f)
        return 1 if self.failures else 0


def write_stack(directory: Path, value: int) -> list[Path]:
    """A tiny uniform TIFF stack. The fill value identifies which run wrote it."""
    import numpy as np
    import tifffile

    directory.mkdir(parents=True, exist_ok=True)
    plane = np.full((SIDE, SIDE), value, dtype=np.uint8)
    files = []
    for i in range(SLICES):
        f = directory / f"s{i:04d}.tif"
        tifffile.imwrite(f, plane)
        files.append(f)
    return files


def ensure_sample_dataset(force: bool = False):
    """Build (or reuse) a small zarr3 dataset and return (dataset, plan, tiffs).

    Must be called from inside a `if __name__ == "__main__"` guard: cluster_tools
    spawns its workers, which re-import the calling module.
    """
    import shutil

    from wbk_publish.convert import build_layer, collect_tiffs
    from wbk_publish.logs import make_logger
    from wbk_publish.resources import plan_for_stack
    from webknossos import Dataset

    log = make_logger("fixture", verbose=False)
    stack, out = SAMPLE / "stack", SAMPLE / "out"

    if out.exists() and not force:
        tiffs = collect_tiffs(stack)
        plan = plan_for_stack(tiffs, log, max_workers=4)
        return Dataset.open(out), plan, tiffs

    shutil.rmtree(stack, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    tiffs = write_stack(stack, value=11)
    plan = plan_for_stack(tiffs, log, max_workers=4)

    dataset = build_layer(
        tiffs,
        out,
        dataset_name="20250921_p3l-test_SAMPLE_1",
        voxel_size_nm=(650.0, 650.0, 650.0),
        layer_name="xray",
        category="color",
        plan=plan,
        sampling_mode="anisotropic",
        log=log,
    )
    return dataset, plan, tiffs
