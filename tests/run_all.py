#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos[all]>=4.0.0",
#     "numpy",
# ]
# ///
"""Run every check. No test framework — each check is a script that exits non-zero.

    uv run tests/run_all.py
    uv run tests/run_all.py check_api        # just one

Order matters only in that check_convert builds the sample dataset the NGFF
check reuses; check_ngff builds it itself if it is missing.

None of these need a WEBKNOSSOS server. check_api is the one to run after any
dependency upgrade — it verifies every API the pipeline calls still exists with
the arguments it passes.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHECKS = [
    ("check_naming", "path parsing, the meta_<run>.json record, MX-Cube metadata mapping"),
    ("check_resources", "worker/thread sizing and the tight-memory guard"),
    ("check_threads", "thread caps reach spawned workers"),
    ("check_convert", "TIFF stack to zarr3, sharded and pixel-exact"),
    ("check_ngff", "zarr group metadata rebuilt over suffixed mag paths"),
    ("check_conversion_errors", "a bad slice is located and explained"),
    ("check_delete_errors", "delete_layer timeout diagnostics"),
    ("check_batch", "folder discovery and the batch submitter's list parsing"),
    ("check_layers", "adding a layer to an existing dataset, and refusing to orphan one"),
    ("check_legacy", "legacy datasets refused before conversion; --replace-legacy"),
    ("check_insegt", "insegt3d predictions: reading, mag matching, thresholded layers"),
    ("check_api", "webknossos API surface the pipeline depends on"),
]


def main(argv: list[str]) -> int:
    selected = argv or [name for name, _ in CHECKS]
    known = {name for name, _ in CHECKS}

    unknown = [s for s in selected if s not in known]
    if unknown:
        print(f"Unknown check(s): {', '.join(unknown)}")
        print("Available: " + ", ".join(name for name, _ in CHECKS))
        return 2

    results: list[tuple[str, bool]] = []
    for name, description in CHECKS:
        if name not in selected:
            continue
        # Flush before handing stdout to the child, or the headers buffer up
        # and print after all the child output.
        print(f"\n{'=' * 78}\n{name} — {description}\n{'=' * 78}", flush=True)
        proc = subprocess.run([sys.executable, str(HERE / f"{name}.py")])
        results.append((name, proc.returncode == 0))

    print(f"\n{'=' * 78}")
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    failed = [name for name, passed in results if not passed]
    print(f"{'=' * 78}")
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f" — failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
