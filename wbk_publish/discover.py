"""Find what is publishable under a directory, and write it as a batch list.

This exists because the login node has no mounts: nothing can look at the data
except a job, so discovery has to be one. Running it as a module means it goes
through the same wrapper as a publish and inherits its venv and mount checks.

It writes a list rather than submitting anything. Publishing forty datasets is
hard to undo, and a file you can read and edit first costs one command.

The selection rule, for each sample directory:

  * a `stitching_N` present  → the stitching, and not the tile recons, which
    are intermediate products of it
  * otherwise                → every `recon_XYZ_N` found

Each line carries the voxel size read from that run's own MX-Cube metadata,
because magnification varies between samples within a beamtime and getting it
wrong is silent — the data looks correct and is scaled wrong.

Candidates that cannot produce a legal dataset name, or whose stack directory
holds no TIFFs, are written as comments with the reason rather than dropped —
a list that silently omits things is worse than one that explains itself.
"""

import argparse
import fnmatch
import logging
import os
import sys
from pathlib import Path

from .logs import make_logger
from .metadata import find_mx_cube_metadata, load_mx_cube_metadata
from .naming import _RUN_DIR, dataset_name_from_path, parse_dataset_name, resolve_tiff_stack

# What the beamline CLI will accept. Anything else has to go through the manual
# CLI, so the scan reports it rather than writing a line that would be rejected.
BEAMLINE_VOXEL_SIZES = (325, 650)


def find_run_dirs(root: Path, only: str | None = None, supersede: bool = True) -> list[Path]:
    """Run directories under `root`, one sample's worth at a time.

    Prunes rather than walking everything. A run directory holds the slices —
    twenty thousand of them on a large stitch — and nothing below it is ever a
    run directory, so descending into one costs a directory listing per dataset
    and answers nothing. Over beegfs that is the difference between reading a
    few hundred entries and the better part of a million.

    Sorted, so the list is stable between scans and diffs cleanly.
    """
    samples: dict[Path, list[Path]] = {}

    for dirpath, dirnames, _files in os.walk(root):
        here = Path(dirpath)
        runs_here = [d for d in dirnames if _RUN_DIR.match(d)]
        for name in runs_here:
            samples.setdefault(here, []).append(here / name)
        # Do not descend into run directories, nor into anything that cannot
        # contain one.
        dirnames[:] = [d for d in dirnames if d not in runs_here and d not in ("tomo", "stack")]
        dirnames.sort()

    chosen_runs: list[Path] = []
    for _sample, found in sorted(samples.items()):
        # A stitching supersedes the tile recons it was made from — but only
        # when looking for tomograms to publish. A prediction tree may hold
        # output for a tile recon and for the stitching, and those are layers
        # on two different datasets, so neither replaces the other.
        chosen = found
        if supersede:
            stitchings = [p for p in found if p.name.startswith("stitching_")]
            chosen = stitchings or found
        if only:
            chosen = [p for p in chosen if fnmatch.fnmatch(p.name, only)]
        chosen_runs.extend(sorted(chosen))

    return chosen_runs


def voxel_size_nm(run_dir: Path, log: logging.Logger) -> int | None:
    """The voxel size this run was acquired at, from its MX-Cube metadata.

    Magnification varies between samples in one beamtime, and getting it wrong
    is silent — the data looks right and is scaled wrong. `pixelsize` is in
    micrometres, so 0.65 means 650 nm.

    None when there is no metadata to read, or the value is not one the
    beamline CLI accepts; the caller then leaves the line for the operator.
    """
    try:
        run_number = parse_dataset_name(dataset_name_from_path(run_dir)).run_number
        meta = find_mx_cube_metadata(run_dir, run_number)
        pixel_um = load_mx_cube_metadata(meta, log).get("pixelsize")
    except (ValueError, KeyError):
        return None

    if not isinstance(pixel_um, (int, float)) or pixel_um <= 0:
        return None

    nm = round(pixel_um * 1000)
    return nm if nm in BEAMLINE_VOXEL_SIZES else None


def describe(run_dir: Path) -> tuple[bool, str]:
    """Can this be published, and if not, why not? Returns (ok, note)."""
    try:
        name = dataset_name_from_path(run_dir)
    except ValueError as exc:
        return False, str(exc).splitlines()[0]

    try:
        stack = resolve_tiff_stack(run_dir)
    except ValueError as exc:
        return False, str(exc).splitlines()[0]

    if not stack.is_dir():
        return False, f"no stack directory at {stack}"

    # One glob, not a count: a 20,000-file listing over beegfs is not free, and
    # the question is only whether the reconstruction has written anything.
    if not any(stack.glob("*.tif")) and not any(stack.glob("*.tiff")):
        return False, f"no TIFFs in {stack}"

    return True, name


def build_list(root: Path, only: str | None, log: logging.Logger) -> list[str]:
    runs = find_run_dirs(root, only)
    log.info(f"Found {len(runs)} run directories under {root}")

    if not runs:
        # An empty file that does not say why is the least useful outcome. The
        # usual cause is pointing the scan at a tree that is not laid out like
        # RECON — a postprocessing or segmentation tree, say, where the stacks
        # sit in directories named after the sample rather than the run.
        log.warning(f"Nothing to publish was found under {root}")
        return [
            f"# Nothing found under {root}",
            "#",
            "# The scan looks for run directories named recon_<XYZ>_<N> or",
            "# stitching_<N>, inside the beamline layout:",
            "#",
            "#     .../p3l-account/.../YYYYMMDD/<sample>/<run>/",
            "#",
            "# If the stacks here sit somewhere else — a segmentation or",
            "# postprocessing tree, where directories are named after the sample",
            "# rather than the run — this scan cannot name the datasets, and the",
            "# lines have to say which dataset each stack belongs to:",
            "#",
            "#   manual: --stack <this stack> \\",
            "#           --dataset-name <the tomogram's dataset name> \\",
            "#           --layer-type segmentation",
            "#",
            "# See docs/USAGE.md section 6.",
        ]

    lines = [
        f"# Generated by wbk_publish.discover from {root}",
        "# Edit freely: each line is the arguments for one submission.",
        "# Submit with:  ./wbk_batch.sh <this file> --voxel-size 650",
        "",
    ]
    publishable = 0
    sizes: dict[int | None, int] = {}
    for run in runs:
        ok, note = describe(run)
        if not ok:
            lines.append(f"# SKIPPED  {run}")
            lines.append(f"#          {note}")
            continue

        # Magnification varies between samples, so read it per run rather than
        # letting one --voxel-size cover a beamtime it does not fit.
        voxel = voxel_size_nm(run, log)
        sizes[voxel] = sizes.get(voxel, 0) + 1
        if voxel is None:
            lines.append(f"# no voxel size in the metadata for "
                         f"{run.parent.name}/{run.name}; the value passed to "
                         "wbk_batch.sh will be used")
            lines.append(str(run))
        else:
            lines.append(f"{run}  --voxel-size {voxel}")
        publishable += 1

    log.info(f"{publishable} publishable, {len(runs) - publishable} skipped with a reason")
    known = {k: v for k, v in sizes.items() if k is not None}
    if len(known) > 1:
        log.warning(
            "This beamtime mixes voxel sizes: "
            + ", ".join(f"{n} nm x{c}" for n, c in sorted(known.items()))
            + ". Each line carries its own, so a --voxel-size given to "
              "wbk_batch.sh would be overridden by it."
        )
    elif known:
        log.info(f"All runs are {next(iter(known))} nm")
    if None in sizes:
        log.warning(f"{sizes[None]} run(s) had no usable pixelsize in their metadata")

    return lines


def layer_dirs(run_dir: Path) -> list[Path]:
    """Subdirectories of a prediction run directory that hold slices.

    Each becomes a layer named after the directory, so `labels/` publishes as
    `labels` and adding `instances/` later needs no convention change.
    """
    found = []
    for sub in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        if any(sub.glob("*.tif")) or any(sub.glob("*.tiff")):
            found.append(sub)
    return found


def build_segmentation_list(
    root: Path, layer_type: str, only: str | None, log: logging.Logger
) -> list[str]:
    """Pair prediction stacks with the datasets they were computed from.

    The pairing is the path itself: a prediction tree mirrors the account, date,
    sample and run of the tomogram, so the same derivation that names a dataset
    from a RECON path names it from here. Nothing else links the two, which is
    why the rule is worth enforcing — a prediction directory that does not carry
    those four components cannot be attributed to a dataset by any means.
    """
    runs = find_run_dirs(root, only, supersede=False)
    log.info(f"Found {len(runs)} run directories under {root}")

    lines = [
        f"# Generated by wbk_publish.discover --segmentations from {root}",
        "# Each line adds a layer to the dataset the prediction was computed from.",
        "# Submit with:  ./wbk_batch.sh <this file>",
        "",
    ]
    if not runs:
        lines += [
            "# Nothing found.",
            "#",
            "# A prediction tree must mirror the tomogram's path components:",
            "#",
            "#     <anything>/p3l-account/YYYYMMDD/<sample>/<run>/<layer>/*.tif",
            "#",
            "# Those four are what names the dataset; without them a prediction",
            "# cannot be attributed to one. See docs/USAGE.md section 6.",
        ]
        log.warning(f"Nothing found under {root} — does the tree mirror the RECON components?")
        return lines

    paired = 0
    for run in runs:
        try:
            name = dataset_name_from_path(run)
        except ValueError as exc:
            lines.append(f"# SKIPPED  {run}")
            lines.append(f"#          {str(exc).splitlines()[0]}")
            continue

        layers = layer_dirs(run)
        if not layers:
            lines.append(f"# SKIPPED  {run}")
            lines.append("#          no subdirectory here holds TIFFs")
            continue

        # The layer is named after the dataset and then the directory it came
        # from, so `labels/` and `instances/` on the same dataset stay apart —
        # which the type-derived default could not do.
        readable = parse_dataset_name(name).readable_name
        lines.append(f"# {name}")
        for layer in layers:
            lines.append(
                f"manual: --stack {layer} --dataset-name {name} "
                f"--layer-name {readable}_{layer.name} --layer-type {layer_type}"
            )
            paired += 1

    log.info(f"{paired} layer(s) across {len(runs)} run directories")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.discover",
        description="List the publishable run directories under a folder.",
    )
    parser.add_argument("--root", required=True, help="Directory to scan")
    parser.add_argument(
        "--segmentations",
        action="store_true",
        help="Scan a prediction tree rather than a RECON tree. Each run directory's "
             "subdirectories become layers on the dataset the path names.",
    )
    parser.add_argument(
        "--layer-type",
        default="segmentation",
        choices=("segmentation", "color"),
        help="Category for the layers found with --segmentations (default: segmentation)",
    )
    parser.add_argument(
        "--only",
        help="Only run directories matching this pattern, e.g. 'stitching_*' or 'recon_111_*'",
    )
    parser.add_argument(
        "--out",
        help="Where to write the list. Printed to stdout when omitted, which in a "
             "job means the job log.",
    )
    args = parser.parse_args(argv)

    log = make_logger("discover", verbose=False)
    root = Path(args.root)
    if not root.is_dir():
        log.error(f"Not a directory: {root}")
        return 2

    if args.segmentations:
        lines = build_segmentation_list(root, args.layer_type, args.only, log)
    else:
        lines = build_list(root, args.only, log)

    if args.out:
        out = Path(args.out)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info(f"Wrote {out}")
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
