"""The publishing pipeline: convert a stack and put it into WEBKNOSSOS.

Two flows, chosen automatically:

  fresh upload   — no dataset exists yet. Convert, then hand the whole dataset
                   over with Dataset.upload().

  layer overwrite— a dataset already exists (its id came from the sidecar).
                   Convert, then replace just that one layer on the existing
                   dataset. The dataset id, and therefore every URL and
                   annotation pointing at it, survives untouched.

Both hand data over with TransferMode.MOVE_AND_SYMLINK: the server reserves a
destination path, the converted directory is moved there with a plain rename,
and the server is told the write has landed. No large copy, and the datastore
is never reading a directory that is being swapped underneath it.
"""

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from webknossos import (
    DataFormat,
    RemoteAccessMode,
    RemoteDataset,
    TransferMode,
    webknossos_context,
)

from .config import TEMP_BASE, WK_URL
from .convert import build_layer, check_input_available, collect_tiffs
from .ngff import capture_layer_metadata, write_group_metadata
from .resources import WorkerPlan, plan_for_stack
from .remote import (
    attach_metadata,
    find_dataset_id_by_name,
    get_or_create_folder,
    resolve_teams,
)
from .sidecar import (
    load_mx_cube_metadata,
    read_dataset_id,
    write_sidecar,
)


@dataclass
class PublishRequest:
    """Everything the pipeline needs. Both CLIs build one of these."""

    input_dir: Path  # the recon/stitching directory
    tiff_stack: Path  # where the slices actually live
    dataset_name: str  # 20250921_p3l-eduke_MA040_1
    readable_name: str  # MA040_1 — shown in the WEBKNOSSOS UI
    folder_path: str  # relative to Datasets/, e.g. "p3l-eduke/20250921"
    user_team: str  # p3l-eduke
    metadata_file: Path  # meta_<run>.json
    voxel_size_nm: tuple[float, float, float]
    token: str
    layer_name: str = "xray"
    category: str = "color"
    # Upper bounds, not targets — the actual worker count is derived from the
    # stack geometry and the machine's allocation. See resources.py.
    max_workers: int | None = None
    max_memory: int | None = None
    sampling_mode: str = "anisotropic"
    dataset_id: str | None = None  # operator override, skips lookup
    dry_run: bool = False


def publish(request: PublishRequest, log: logging.Logger) -> RemoteDataset | None:
    """Run the pipeline. Returns None for a dry run."""
    check_input_available(request.tiff_stack, log)
    check_staging_available(log)

    # Cheap: a glob plus one TIFF header. Done before the lock so a dry run can
    # report how the machine would be used.
    tiff_files = collect_tiffs(request.tiff_stack)
    plan = plan_for_stack(
        tiff_files,
        log,
        max_workers=request.max_workers,
        max_memory=request.max_memory,
    )

    with _dataset_lock(request.dataset_name, log):
        dataset_id = _resolve_dataset_id(request, log)

        if request.dry_run:
            _report_dry_run(request, dataset_id, plan, log)
            return None

        raw_metadata = load_mx_cube_metadata(request.metadata_file, log)

        if dataset_id is None:
            remote_ds = _fresh_upload(request, tiff_files, plan, log)
        else:
            remote_ds = _overwrite_layer(request, tiff_files, plan, dataset_id, log)

        with webknossos_context(token=request.token, url=WK_URL):
            display_metadata = attach_metadata(remote_ds, raw_metadata, log)
            resolved_id = remote_ds.dataset_id

        write_sidecar(
            request.input_dir,
            dataset_id=resolved_id,
            dataset_name=request.dataset_name,
            layer_name=request.layer_name,
            raw_metadata=raw_metadata,
            display_metadata=display_metadata,
            log=log,
        )

    log.info(f"Done: {WK_URL}/datasets/{resolved_id}")
    return remote_ds


def check_staging_available(log: logging.Logger) -> None:
    """Refuse to run unless the staging directory already exists.

    Never create it. TEMP_BASE lives on the WEBKNOSSOS storage mount, and the
    conversion runs on a cluster node where that mount might be absent. A
    `mkdir -p` there would quietly create a local /morningstar/tmp on the node
    and stage a terabyte into it — and the hand-over rename would then fail
    across filesystems, or worse, land somewhere WEBKNOSSOS never looks. An
    absent directory is the signal that the mount is missing.
    """
    if not TEMP_BASE.is_dir():
        raise RuntimeError(
            f"Staging directory does not exist: {TEMP_BASE}\n"
            "It is not created automatically, because on a compute node its absence "
            "usually means the storage is not mounted. Check the mount, or point "
            "WK_TEMP_BASE at an existing directory on the same filesystem as the "
            "WEBKNOSSOS storage."
        )
    log.debug(f"Staging directory available: {TEMP_BASE}")


# ── Flow selection ────────────────────────────────────────────────────────────


def _resolve_dataset_id(request: PublishRequest, log: logging.Logger) -> str | None:
    """Find the dataset this run should update, if there is one.

    Order: explicit override, then the sidecar, then a lookup by name for
    datasets published before the sidecar recorded ids.
    """
    if request.dataset_id:
        log.info(f"Using dataset id from the command line: {request.dataset_id}")
        return request.dataset_id

    recorded = read_dataset_id(request.input_dir, log)
    if recorded:
        return recorded

    log.info("No dataset id in the sidecar — checking whether one exists by name")
    with webknossos_context(token=request.token, url=WK_URL):
        return find_dataset_id_by_name(
            [request.dataset_name, request.readable_name], log
        )


# ── Fresh upload ──────────────────────────────────────────────────────────────


def _fresh_upload(
    request: PublishRequest,
    tiff_files: list[Path],
    plan: WorkerPlan,
    log: logging.Logger,
) -> RemoteDataset:
    log.info("No existing dataset — publishing a new one")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{request.dataset_name}.") as tmp:
        local_ds = build_layer(
            tiff_files,
            Path(tmp),
            dataset_name=request.dataset_name,
            voxel_size_nm=request.voxel_size_nm,
            layer_name=request.layer_name,
            category=request.category,
            plan=plan,
            sampling_mode=request.sampling_mode,
            log=log,
        )

        # The upload transfers mags only; capture the group metadata before
        # staging is cleaned up so it can be restored afterwards.
        staged_metadata = capture_layer_metadata(Path(tmp), request.layer_name, log)

        with webknossos_context(token=request.token, url=WK_URL):
            # Folder and teams must exist before the upload so they can be set
            # as part of it, rather than patched on afterwards.
            folder = get_or_create_folder(request.folder_path, log)
            teams = resolve_teams(request.user_team, log)

            remote_ds = local_ds.upload(
                new_dataset_name=request.dataset_name,
                folder=folder,
                initial_team_ids=[team.id for team in teams],
                transfer_mode=TransferMode.MOVE_AND_SYMLINK,
            )
            remote_ds.name = request.readable_name

    write_group_metadata(
        remote_ds.dataset_id,
        request.layer_name,
        staged_metadata,
        request.token,
        WK_URL,
        log,
    )

    log.info(f"Published '{request.readable_name}' (id={remote_ds.dataset_id})")
    return remote_ds


# ── Layer overwrite ───────────────────────────────────────────────────────────


def _overwrite_layer(
    request: PublishRequest,
    tiff_files: list[Path],
    plan: WorkerPlan,
    dataset_id: str,
    log: logging.Logger,
) -> RemoteDataset:
    """Replace one layer on an existing dataset, preserving the dataset id.

    Conversion happens in full before anything on the server is touched, so a
    failure during conversion leaves the published dataset exactly as it was.
    """
    log.info(f"Dataset exists (id={dataset_id}) — replacing layer '{request.layer_name}'")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{request.dataset_name}.") as tmp:
        local_ds = build_layer(
            tiff_files,
            Path(tmp),
            dataset_name=request.dataset_name,
            voxel_size_nm=request.voxel_size_nm,
            layer_name=request.layer_name,
            category=request.category,
            plan=plan,
            sampling_mode=request.sampling_mode,
            log=log,
        )
        local_layer = local_ds.get_layer(request.layer_name)

        # Only mags are transferred; capture the group metadata before staging
        # is cleaned up so it can be restored afterwards.
        staged_metadata = capture_layer_metadata(Path(tmp), request.layer_name, log)

        with webknossos_context(token=request.token, url=WK_URL):
            # DIRECT_PATH rather than the default zarr streaming: streamed
            # datasets are read-only and cannot have layers added or removed.
            remote_ds = RemoteDataset.open(
                dataset_id=dataset_id,
                access_mode=RemoteAccessMode.DIRECT_PATH,
                read_only=False,
            )

            if request.layer_name in remote_ds.layers:
                _delete_layer(remote_ds, request.layer_name, dataset_id, log)
            else:
                log.warning(
                    f"Dataset has no '{request.layer_name}' layer yet — adding it. "
                    f"Existing layers: {sorted(remote_ds.layers)}"
                )

            extra = {}
            if request.category == "segmentation":
                extra["largest_segment_id"] = (
                    local_layer.as_segmentation_layer().largest_segment_id
                )

            new_layer = remote_ds.add_layer(
                request.layer_name,
                request.category,
                dtype=local_layer.dtype_per_channel,
                num_channels=local_layer.num_channels,
                data_format=DataFormat.Zarr3,
                bounding_box=local_layer.bounding_box,
                **extra,
            )

            # Finest magnification first, so the dataset is usable in the
            # viewer as early as possible if a later mag transfer fails.
            for mag in sorted(local_layer.mags):
                log.info(f"Transferring mag {mag}")
                new_layer.add_mag_as_copy(
                    local_layer.get_mag(mag),
                    transfer_mode=TransferMode.MOVE_AND_SYMLINK,
                    # Lets a retry reuse a path reserved by an earlier attempt
                    # that failed partway through.
                    overwrite_pending=True,
                )

    write_group_metadata(
        dataset_id,
        request.layer_name,
        staged_metadata,
        request.token,
        WK_URL,
        log,
    )

    log.info(f"Layer replaced — dataset id unchanged: {dataset_id}")
    return remote_ds


# ── Removing the old layer ────────────────────────────────────────────────────

# Statuses a reverse proxy returns when it gives up waiting for the backend.
_GATEWAY_STATUSES = ("502", "503", "504")


class LayerDeletionTimeout(RuntimeError):
    """`delete_layer` did not return in time. The server may still be working."""


def _delete_layer(
    remote_ds: RemoteDataset, layer_name: str, dataset_id: str, log: logging.Logger
) -> None:
    """Remove the layer, reporting clearly if the call times out.

    Removing a layer is not just a metadata edit. The server diffs the old and
    new datasource, finds the mag paths that are no longer referenced, and
    deletes them from disk — all synchronously, inside this one HTTP request. On
    a large stitch that is a recursive delete of hundreds of gigabytes, so this
    call can take minutes, and either the client or a reverse proxy in front of
    WEBKNOSSOS may give up before it finishes.

    A timeout here is ambiguous rather than fatal: the server has very likely
    completed, or will. What matters is that the operator can tell that apart
    from a real failure, so the message says what state things are in.
    """
    import httpx

    log.info(f"Removing the current '{layer_name}' layer")
    log.debug(
        "The server deletes the old mag data as part of this call; for a large "
        "layer it may take several minutes."
    )

    started = time.monotonic()
    try:
        remote_ds.delete_layer(layer_name)
    except httpx.TimeoutException as exc:
        raise LayerDeletionTimeout(
            _timeout_message(layer_name, dataset_id, started, "the client timed out", exc)
        ) from exc
    except Exception as exc:
        text = str(exc)
        if any(f"status {status}" in text for status in _GATEWAY_STATUSES):
            raise LayerDeletionTimeout(
                _timeout_message(
                    layer_name, dataset_id, started,
                    "a proxy in front of WEBKNOSSOS returned a gateway error",
                    exc,
                )
            ) from exc
        raise

    log.info(f"Layer removed in {time.monotonic() - started:.0f}s")


def _timeout_message(
    layer_name: str, dataset_id: str, started: float, cause: str, exc: Exception
) -> str:
    elapsed = time.monotonic() - started
    return (
        f"Removing layer '{layer_name}' did not complete: {cause} after "
        f"{elapsed:.0f}s.\n"
        f"  {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}\n"
        "\n"
        "This is most likely a slow delete, not a failure. WEBKNOSSOS removes the\n"
        "old mag data from disk inside this request, which on a large layer takes\n"
        "minutes; the request was abandoned while the server was still working.\n"
        "\n"
        "The dataset is NOT corrupt. Its data was fully converted in staging before\n"
        "this step, and nothing else has been changed. Depending on how far the\n"
        "server got, the layer is either still present or already removed.\n"
        "\n"
        "What to do:\n"
        f"  1. Check the dataset in the browser: {WK_URL}/datasets/{dataset_id}\n"
        "  2. Wait for the delete to finish (watch the dataset directory shrink).\n"
        "  3. Re-run the same command. It is safe: the sidecar records the dataset\n"
        "     id, so the re-run overwrites the same dataset rather than duplicating\n"
        f"     it. To be explicit, pass --dataset-id {dataset_id}\n"
        "\n"
        "To stop it happening again, raise both timeouts — the client's and the\n"
        "proxy's, since either can be the one that gives up:\n"
        "  export WK_TIMEOUT=7200                    # client, default 1800s\n"
        "  proxy_read_timeout in the nginx config    # often only 60s by default\n"
    )


# ── Support ───────────────────────────────────────────────────────────────────


@contextmanager
def _dataset_lock(dataset_name: str, log: logging.Logger) -> Iterator[None]:
    """Prevent two runs from publishing the same dataset at once.

    The lock is advisory and held on an open file descriptor, so the kernel
    releases it if the process is killed.
    """
    lock_path = TEMP_BASE / f"{dataset_name}.lock"
    lock_file = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # With jobs running on different cluster nodes, "another run" is
            # not actionable on its own. The holder wrote down who it is. Read
            # it through a fresh open: over NFS the descriptor we already hold
            # may serve a cached, empty view of a file another host just wrote,
            # and close-to-open consistency only revalidates on open.
            try:
                holder = lock_path.read_text().strip() or "unknown"
            except OSError:
                holder = "unknown"
            raise RuntimeError(
                f"Another run is already publishing '{dataset_name}'.\n"
                f"  held by: {holder}\n"
                f"  lock:    {lock_path}\n"
                "If that job is no longer running, the lock is stale — remove the "
                "file and re-run."
            ) from None

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(_lock_holder())
        lock_file.flush()
        os.fsync(lock_file.fileno())  # on the server before anyone else can look

        log.debug(f"Acquired lock: {lock_path}")
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        lock_path.unlink(missing_ok=True)


def _lock_holder() -> str:
    """Enough to find the process holding a lock from another machine."""
    import os
    import socket
    from datetime import datetime, timezone

    job = os.environ.get("SLURM_JOB_ID")
    where = f"{socket.gethostname()} pid {os.getpid()}" + (f" slurm job {job}" if job else "")
    return f"{where} since {datetime.now(timezone.utc).isoformat(timespec='seconds')}"


def _report_dry_run(
    request: PublishRequest,
    dataset_id: str | None,
    plan: WorkerPlan,
    log: logging.Logger,
) -> None:
    action = (
        f"OVERWRITE layer '{request.layer_name}' on {dataset_id}"
        if dataset_id
        else "FRESH UPLOAD"
    )
    found = "found" if request.metadata_file.exists() else "NOT FOUND"

    log.info("=== DRY RUN — nothing written, no data transferred ===")
    log.info(f"  Action:         {action}")
    log.info(f"  Dataset name:   {request.dataset_name}")
    log.info(f"  Readable name:  {request.readable_name}")
    log.info(f"  Layer:          {request.layer_name} ({request.category})")
    log.info(f"  Image stack:    {request.tiff_stack}")
    log.info(f"  Voxel size:     {request.voxel_size_nm} nm")
    log.info(f"  Folder:         Datasets/{request.folder_path}")
    log.info(f"  Teams:          {request.user_team} + ximg")
    log.info(f"  Metadata file:  {request.metadata_file} ({found})")
    log.info(f"  Parallelism:    {plan.describe()}")
    log.info("=== DRY RUN complete ===")
