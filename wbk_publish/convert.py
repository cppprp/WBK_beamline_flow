"""Local conversion: TIFF stack → zarr3 layer with a magnification pyramid.

Conversion always writes to a staging directory first and only afterwards is the
result handed to WEBKNOSSOS. Nothing on the server is touched until the stack has
converted successfully, so a failed conversion can never leave a published
dataset without its layer.

How the work is parallelised is decided in `resources.py`, not here.
"""

import logging
import os
from pathlib import Path

from webknossos import (
    DataFormat,
    Dataset,
    ImageConversionError,
    SamplingModes,
    UnsupportedImageDataError,
    UnsupportedImageFormatError,
)

from .memwatch import MemoryWatch
from .resources import WorkerPlan, downsample_workers
from .threads import apply_thread_limit

# webknossos 3.x defaults to Zarr3 already; naming it here keeps the intent
# explicit and immune to the WK_DEFAULT_DATA_FORMAT environment override.
DATA_FORMAT = DataFormat.Zarr3


class ConversionFailed(RuntimeError):
    """The images could not be converted. Nothing has been published."""


def mount_point(path: Path) -> Path:
    """The filesystem `path` lives on: its nearest existing ancestor that is a mount."""
    current = path.absolute()
    while not current.is_mount() and current != current.parent:
        current = current.parent
    return current


def check_input_available(tiff_stack: Path, log: logging.Logger) -> None:
    """Fail fast, before any conversion work, if the input is not reachable.

    The interesting failure is a network filesystem that is not mounted on this
    node: the path then either does not exist, or exists as an empty stub under
    a mount point that never got mounted. Both are caught here — the first by
    the directory check, the second by collect_tiffs finding nothing — and the
    message names the filesystem so it is obvious which mount is missing.

    Deliberately not tied to one filesystem. Inputs arrive from beegfs, ximg and
    the big JBODs, and an earlier version checked that beegfs specifically was
    mounted, which refused perfectly good runs whose data lived elsewhere.
    """
    if not tiff_stack.is_dir():
        mount = mount_point(tiff_stack)
        raise FileNotFoundError(
            f"Image stack directory not found: {tiff_stack}\n"
            f"  The nearest mounted filesystem on {os.uname().nodename} is {mount}.\n"
            "  If the data is on a network filesystem, check that it is mounted here —\n"
            "  an unmounted path looks exactly like a missing directory."
        )
    log.debug(f"Input readable on {mount_point(tiff_stack)}: {tiff_stack}")


def collect_tiffs(tiff_stack: Path) -> list[Path]:
    """Return the slices in Z order.

    An empty directory is the one input failure the existence check above
    cannot distinguish from a storage problem: a network filesystem that
    mounted but has nothing under it looks exactly like a stack that was never
    written. Say both, rather than only the one that is less likely.
    """
    tiffs = sorted(
        list(tiff_stack.glob("*.tif")) + list(tiff_stack.glob("*.tiff"))
    )
    if not tiffs:
        entries = sum(1 for _ in tiff_stack.iterdir())
        raise FileNotFoundError(
            f"No TIFF files found in {tiff_stack}\n"
            f"  The directory exists on {mount_point(tiff_stack)} and holds "
            f"{entries} other entries.\n"
            "  Either the reconstruction has not written its slices yet, or that\n"
            "  filesystem is mounted but empty on this host."
        )
    return tiffs


def build_layer(
    tiff_files: list[Path],
    out_path: Path,
    *,
    dataset_name: str,
    voxel_size_nm: tuple[float, float, float],
    layer_name: str,
    category: str,
    plan: WorkerPlan,
    sampling_mode: str,
    log: logging.Logger,
) -> Dataset:
    """Convert the stack into a zarr3 dataset at `out_path` and downsample it."""
    import cluster_tools

    log.info(
        f"Converting {len(tiff_files)} slices → zarr3 "
        f"(layer '{layer_name}', {category}, voxel size {voxel_size_nm} nm)"
    )

    # Must precede executor creation: workers are spawned, so they read the
    # environment as it stands when the pool is built.
    apply_thread_limit(plan.threads_per_worker, log)

    dataset = Dataset(out_path, voxel_size=voxel_size_nm, name=dataset_name)

    # Watch the workers' memory: the plan's per-worker figure is an estimate,
    # and this is the only place it ever gets measured.
    with MemoryWatch(log, plan, phase="conversion") as watch, cluster_tools.get_executor(
        "multiprocessing", max_workers=plan.workers
    ) as executor:
        try:
            layer = dataset.add_layer_from_images(
                images=[str(t) for t in tiff_files],
                layer_name=layer_name,
                category=category,
                data_format=DATA_FORMAT,
                compress=True,
                executor=executor,
            )
        except ImageConversionError as exc:
            raise ConversionFailed(_conversion_message(exc, tiff_files)) from exc
        except Exception as exc:
            # Not every reader failure is typed. webknossos raises
            # ImageConversionError when picking a reader or reading the first
            # slice, but a slice that goes bad partway through a stack fails
            # inside a worker and surfaces as the reader's own error — precisely
            # the case worth reporting well on a long conversion. If the message
            # names one of our slices we can still place it; if not, this is
            # something else and must not be disguised as a data problem.
            if _locate(exc, tiff_files)[0] is None:
                raise
            raise ConversionFailed(_conversion_message(exc, tiff_files)) from exc

    # Downsampling runs in its own, usually smaller, pool. A downsample worker
    # has been seen at more than twice the conversion peak, and the conversion
    # peak was just measured — so size this phase from the measurement rather
    # than from the estimate the first pool was built on.
    workers, assumed = downsample_workers(plan, watch.peak_worker, log)
    with MemoryWatch(
        log, plan, phase="downsampling", assumed_per_worker=assumed
    ), cluster_tools.get_executor("multiprocessing", max_workers=workers) as executor:
        layer.downsample(
            sampling_mode=SamplingModes.parse(sampling_mode),
            # Keep mag 1's shard shape (4096 x 4096 x 32) for the whole pyramid.
            # Left unset, webknossos 4.x gives the coarser mags its generic
            # 1024^3 default, and since one downsample job is one target shard
            # that turns a 2000-slice stack's mag 2 into 4 jobs and every
            # coarser level into a single job — most of the pool sits idle and
            # the pyramid takes longer than the conversion. 3.x copied the
            # source mag's shard shape; this restores that.
            shard_shape=layer.get_mag(1).info.shard_shape,
            executor=executor,
        )

    mags = ", ".join(str(m) for m in sorted(layer.mags))
    log.info(f"Conversion complete — mags: {mags}")
    return dataset


# ── Reporting a conversion failure ────────────────────────────────────────────


def _locate(exc: Exception, tiff_files: list[Path]) -> tuple[object | None, str | None]:
    """Find the offending slice, and where it sits in the stack.

    Typed errors carry the path directly. Untyped reader errors do not, but they
    almost always name the file in their message, and we know every filename we
    handed in — so matching against that list places the failure without parsing
    anyone's error format.

    Returns (path, position); either may be None.
    """
    path = getattr(exc, "path", None)
    if path is not None:
        name = Path(str(path)).name
        for index, candidate in enumerate(tiff_files, start=1):
            if candidate.name == name:
                return path, f"slice {index} of {len(tiff_files)}"
        return path, None

    text = str(exc)
    for index, candidate in enumerate(tiff_files, start=1):
        if candidate.name in text:
            return candidate, f"slice {index} of {len(tiff_files)}"
    return None, None


def _conversion_message(exc: Exception, tiff_files: list[Path]) -> str:
    """Turn a conversion failure into something an operator can act on."""
    path, where = _locate(exc, tiff_files)

    lines = [f"Conversion failed: {type(exc).__name__}"]
    if where:
        lines.append(f"  {where}")
    if path is not None:
        lines.append(f"  {path}")
    lines.append(f"  {str(exc).splitlines()[0] if str(exc) else ''}")
    lines += [
        "",
        "Nothing was published. The dataset in WEBKNOSSOS is unchanged, and the",
        "partial conversion in staging is discarded.",
        "",
    ]

    if isinstance(exc, UnsupportedImageFormatError):
        missing = getattr(exc, "missing_extras", ()) or ()
        if missing:
            extras = ", ".join(f"webknossos[{name}]" for name in missing)
            lines += [
                f"The format needs an optional dependency that is not installed: {extras}.",
                "This project declares webknossos[all], which includes it — so the",
                "environment running the conversion is not the project environment.",
                "Check that it was started with `uv run` from the checkout.",
            ]
        else:
            supported = getattr(exc, "supported_file_extensions", ()) or ()
            lines.append("No reader handles this format.")
            if supported:
                lines.append(f"Supported here: {', '.join(sorted(supported))}")
    elif isinstance(exc, UnsupportedImageDataError):
        lines += [
            "The images were read, but cannot be stored as requested. Common causes:",
            "  - float data being written into a segmentation layer (use --layer_type color)",
            "  - a multi-channel image, which webknossos 4.x converts into one layer",
            "    per channel and so refuses to add as a single layer",
            "This pipeline expects single-channel grayscale slices.",
        ]
    else:
        # CorruptImageError, and untyped reader failures on a named slice.
        lines += [
            "The slice could not be read — damaged, or written incompletely. A",
            "truncated file is usually smaller than its neighbours, so comparing",
            "sizes around it is the quickest check. Fix or replace it and re-run:",
            "the metadata file records the dataset id, so a re-run overwrites rather",
            "than duplicates.",
        ]

    return "\n".join(lines)
