"""The publishing pipeline: convert a stack and put it into WEBKNOSSOS.

Two flows, chosen automatically:

  fresh upload   — no dataset exists yet. Convert, then hand the whole dataset
                   over with Dataset.upload().

  layer overwrite— a dataset already exists (its id is in meta_<run>.json).
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

from .config import LEGACY_LAYER_NAME, PUBLISHED_BASE, TEMP_BASE, WK_URL
from .convert import build_layer, check_input_available, collect_tiffs
from .naming import default_layer_name
from .ngff import capture_layer_metadata, write_group_metadata
from .resources import WorkerPlan, plan_for_shape, plan_for_stack
from .zarr_input import check_zarr_input, conversion_dtype, is_zarr, probe_zarr
from .remote import (
    attach_metadata,
    find_dataset_id_by_directory,
    find_dataset_id_by_name,
    get_or_create_folder,
    is_virtual,
    resolve_teams,
)
from .metadata import (
    load_mx_cube_metadata,
    read_dataset_id,
    record_publication,
    report_staleness,
)


@dataclass
class PublishRequest:
    """Everything the pipeline needs. Both CLIs build one of these."""

    input_dir: Path  # the recon/stitching directory
    tiff_stack: Path  # where the slices live — or a zarr store (zarr_input.py)
    dataset_name: str  # 20250921_p3l-eduke_MA040_1
    readable_name: str  # MA040_1 — shown in the WEBKNOSSOS UI
    folder_path: str  # relative to Datasets/, e.g. "p3l-eduke/20250921"
    user_team: str  # p3l-eduke
    metadata_file: Path  # meta_<run>.json
    voxel_size_nm: tuple[float, float, float]
    token: str
    # None means "the default for this dataset": the readable name with the
    # xray suffix, filled in below once readable_name is known. Both CLIs leave
    # it unset for a tomogram and pass a name only for a further layer.
    layer_name: str | None = None
    category: str = "color"
    # Upper bounds, not targets — the actual worker count is derived from the
    # stack geometry and the machine's allocation. See resources.py.
    max_workers: int | None = None
    max_memory: int | None = None
    sampling_mode: str = "anisotropic"
    dataset_id: str | None = None  # operator override, skips lookup
    # A dataset from the old system cannot be updated in place. With this set,
    # publish a new one alongside it instead of refusing.
    replace_legacy: bool = False
    # Confirms that a layer named something other than the dataset's own default
    # really is meant to start a new dataset, rather than be added to one that
    # already exists.
    new_dataset: bool = False
    # Which channel of a multi-channel zarr input to publish. TIFF input ignores it.
    channel: int | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.layer_name is None:
            self.layer_name = default_layer_name(self.readable_name)


def publish(request: PublishRequest, log: logging.Logger) -> RemoteDataset | None:
    """Run the pipeline. Returns None for a dry run."""
    check_input_available(request.tiff_stack, log)
    check_staging_available(log)
    check_published_writable(log)

    # Cheap: a glob plus one TIFF header, or a zarr store's metadata. Done
    # before the lock so a dry run can report how the machine would be used.
    source, plan, dtype = _resolve_source(request, log)

    with _dataset_lock(request.dataset_name, log):
        dataset_id = _resolve_dataset_id(request, log)

        # A dataset published by the old convert-in-place system cannot be
        # updated through this API: the mag hand-over is virtual-only, and
        # removing its single layer leaves a datasource the datastore rejects.
        # Decide that now, not after the conversion.
        if dataset_id is not None and _is_legacy(dataset_id, request.token, log):
            if not request.replace_legacy:
                raise LegacyDatasetError(_legacy_message(dataset_id, request))
            log.warning(
                f"Dataset {dataset_id} was published by the old system and cannot be "
                "updated in place. Publishing a NEW dataset instead (--replace-legacy). "
                "The old one stays until you delete it in WEBKNOSSOS."
            )
            dataset_id = None

        # Adding a second layer is the one flow where a failed lookup is worse
        # than an error: it would publish a NEW dataset containing only the
        # segmentation, with no tomogram beside it, and nothing would say so. A
        # layer name other than this dataset's own default almost always means
        # "add to an existing dataset", so make the operator say which one.
        if (
            dataset_id is None
            and request.layer_name != default_layer_name(request.readable_name)
            and not request.new_dataset
        ):
            raise RuntimeError(_orphan_layer_message(request))

        if request.dry_run:
            _report_dry_run(request, dataset_id, plan, log)
            return None

        raw_metadata = load_mx_cube_metadata(request.metadata_file, log)

        if dataset_id is None:
            remote_ds = _fresh_upload(request, source, plan, log, dtype)
        else:
            remote_ds = _overwrite_layer(request, source, plan, dataset_id, log, dtype)

        with webknossos_context(token=request.token, url=WK_URL):
            attach_metadata(remote_ds, raw_metadata, log)
            resolved_id = remote_ds.dataset_id

        record_publication(
            request.metadata_file,
            request.input_dir,
            dataset_id=resolved_id,
            dataset_name=request.dataset_name,
            layer_name=request.layer_name,
            log=log,
        )

    log.info(f"Done: {WK_URL}/datasets/{resolved_id}")
    return remote_ds


def _resolve_source(request: PublishRequest, log: logging.Logger):
    """The input to convert, the worker plan, and any dtype to convert to while reading."""
    dtype = None
    if is_zarr(request.tiff_stack):
        info = probe_zarr(request.tiff_stack)
        check_zarr_input(info, request.channel, request.voxel_size_nm, log)
        dtype = conversion_dtype(info, request.category, log)
        shape = info.stack_shape()
        source = request.tiff_stack
    else:
        if request.channel is not None:
            raise ValueError("--channel applies to zarr input only; this is a TIFF stack.")
        source = collect_tiffs(request.tiff_stack)
        shape = None
    plan = (
        plan_for_shape(shape, log, max_workers=request.max_workers, max_memory=request.max_memory)
        if shape is not None
        else plan_for_stack(source, log, max_workers=request.max_workers, max_memory=request.max_memory)
    )
    return source, plan, dtype


def check_published_writable(log: logging.Logger) -> None:
    """Refuse early if we could not hand a dataset over at the end.

    The hand-over is the last step of a publish: the client creates the dataset
    directory under the published root and renames the staged layer into it. A
    missing permission there fails *after* the conversion and the pyramid, and
    the staged copy is discarded with the exception — so an hour of work is lost
    to something that was knowable in a millisecond.

    Deliberately asymmetric, because a false refusal is worse than a late
    failure:

      * not writable  → refuse now. That is a definite failure later.
      * absent        → warn only. The server decides the real destination and
                        does not say where until reserve time, so a path that
                        does not exist here may just mean this node mounts the
                        storage somewhere else. Refusing would block a run that
                        would have worked.

    Advisory either way: os.access cannot see NFS ACLs or a root-squashed
    export, so a pass is not a guarantee — it only rules out the common case of
    the published directory not being group-writable for this account.
    """
    if PUBLISHED_BASE is None:
        return

    if not PUBLISHED_BASE.is_dir():
        log.warning(
            f"Cannot check the published directory before converting: {PUBLISHED_BASE} "
            "is not there. If this node mounts the WEBKNOSSOS storage somewhere else, "
            "set WK_PUBLISHED_BASE to that path (or to '' to silence this). A "
            "permission problem will then only surface at hand-over, after the "
            "conversion."
        )
        return

    if os.access(PUBLISHED_BASE, os.W_OK | os.X_OK):
        log.debug(f"Published directory is writable: {PUBLISHED_BASE}")
        return

    raise RuntimeError(
        f"The published directory is not writable by you: {PUBLISHED_BASE}\n"
        "\n"
        "Nothing has been changed. Refusing now rather than at hand-over, which is "
        "after the conversion and would discard it.\n"
        "\n"
        "A publish creates the dataset directory here and renames the converted "
        "layer into it, so write and execute are both needed. Ask the owner of that "
        "directory to give your group write access:\n"
        f"    chmod g+rwxs {PUBLISHED_BASE}\n"
        "and check you are in that group:  id -nG\n"
        "\n"
        "The setgid bit matters: without it the directories you create keep your own "
        "group and the next person hits this too. Note it only affects new "
        "directories — replacing a layer on a dataset somebody else published needs "
        "write inside that dataset's own directory as well."
    )


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


# ── Legacy datasets ───────────────────────────────────────────────────────────


class LegacyDatasetError(RuntimeError):
    """The matched dataset predates this pipeline and cannot be updated in place."""


def _is_legacy(dataset_id: str, token: str, log: logging.Logger) -> bool:
    """True when the dataset is not virtual, i.e. was converted in place.

    Virtual datasets keep their datasource in the WEBKNOSSOS database and
    support the mag hand-over this pipeline relies on. Non-virtual ones have a
    datasource-properties.json on disk that the server rewrites on every change
    — and the datastore refuses to write one with no layers, which is exactly
    what removing the only layer produces.
    """
    virtual = is_virtual(dataset_id, token, log)
    if virtual is None:
        # Cannot tell; let the operation proceed and the error translation in
        # _delete_layer explain it if it turns out to be legacy after all.
        return False
    return not virtual


def _orphan_layer_message(request: PublishRequest) -> str:
    return (
        f"No existing dataset was found, so publishing layer '{request.layer_name}' "
        f"now would create a NEW dataset containing only that layer —\n"
        f"no '{default_layer_name(request.readable_name)}' beside it. That is almost "
        "never what is wanted when the layer name is not this dataset's default.\n"
        "\n"
        "Nothing has been changed.\n"
        "\n"
        "To add this layer to a dataset that already exists, give its id:\n"
        f"    --dataset-id <id>          ({WK_URL}/datasets/<id> — copy it from the URL)\n"
        "\n"
        "If a new dataset really is intended, say so explicitly:\n"
        "    --new-dataset\n"
    )


def _legacy_message(dataset_id: str, request: PublishRequest) -> str:
    return (
        f"Dataset {dataset_id} ('{request.dataset_name}') was published by the old\n"
        "convert-in-place system and cannot be updated in place: WEBKNOSSOS only\n"
        "supports adding mags to virtual datasets, and this one is not.\n"
        "\n"
        "Nothing has been changed.\n"
        "\n"
        "To publish this reconstruction, re-run with --replace-legacy. That creates a\n"
        "NEW dataset (new id) alongside the old one and records the new id in the\n"
        "metadata file, so every later re-run updates it in place. Delete the old dataset\n"
        f"in WEBKNOSSOS afterwards: {WK_URL}/datasets/{dataset_id}"
    )


# ── Flow selection ────────────────────────────────────────────────────────────


def _resolve_dataset_id(request: PublishRequest, log: logging.Logger) -> str | None:
    """Find the dataset this run should update, if there is one.

    Order: explicit override, then the record in meta_<run>.json (or the legacy
    sidecar), then a lookup by name for datasets published before ids were
    recorded.
    """
    if request.dataset_id:
        log.info(f"Using dataset id from the command line: {request.dataset_id}")
        return request.dataset_id

    recorded = read_dataset_id(request.metadata_file, request.input_dir, log)
    if recorded:
        report_staleness(request.metadata_file, request.input_dir, log)
        return recorded

    log.info("No recorded dataset id — checking whether one exists by name")
    with webknossos_context(token=request.token, url=WK_URL):
        # The directory name carries the date and is unique; the display name
        # does not and is not. Ask the unambiguous question first.
        by_directory = find_dataset_id_by_directory(request.dataset_name, log)
        if by_directory:
            return by_directory
        return find_dataset_id_by_name(
            [request.dataset_name, request.readable_name], log
        )


# ── Fresh upload ──────────────────────────────────────────────────────────────


def _fresh_upload(
    request: PublishRequest,
    source: list[Path] | Path,
    plan: WorkerPlan,
    log: logging.Logger,
    dtype: str | None = None,
) -> RemoteDataset:
    log.info("No existing dataset — publishing a new one")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{request.dataset_name}.") as tmp:
        local_ds = build_layer(
            source,
            Path(tmp),
            dataset_name=request.dataset_name,
            voxel_size_nm=request.voxel_size_nm,
            layer_name=request.layer_name,
            category=request.category,
            plan=plan,
            sampling_mode=request.sampling_mode,
            log=log,
            channel=request.channel,
            dtype=dtype,
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
    source: list[Path] | Path,
    plan: WorkerPlan,
    dataset_id: str,
    log: logging.Logger,
    dtype: str | None = None,
) -> RemoteDataset:
    """Replace one layer on an existing dataset, preserving the dataset id.

    Conversion happens in full before anything on the server is touched, so a
    failure during conversion leaves the published dataset exactly as it was.
    """
    log.info(f"Dataset exists (id={dataset_id}) — replacing layer '{request.layer_name}'")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{request.dataset_name}.") as tmp:
        local_ds = build_layer(
            source,
            Path(tmp),
            dataset_name=request.dataset_name,
            voxel_size_nm=request.voxel_size_nm,
            layer_name=request.layer_name,
            category=request.category,
            plan=plan,
            sampling_mode=request.sampling_mode,
            log=log,
            channel=request.channel,
            dtype=dtype,
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
                _warn_about_legacy_layer(request, remote_ds, log)

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


def _warn_about_legacy_layer(
    request: PublishRequest, remote_ds: RemoteDataset, log: logging.Logger
) -> None:
    """Point out a plain 'xray' layer left by a publish from before the rename.

    Layers used to be called 'xray' regardless of which dataset they were on.
    Re-publishing such a dataset now writes MA040_1_xray instead, which the
    overwrite path treats as a new layer — so the old one stays, and the dataset
    ends up showing the same tomogram twice.

    This is not deleted automatically. The old layer is real published data, and
    removing it on the strength of its name is the operator's call, not a side
    effect of a re-publish.
    """
    if request.layer_name == LEGACY_LAYER_NAME:
        return
    if LEGACY_LAYER_NAME not in remote_ds.layers:
        return
    if request.layer_name != default_layer_name(request.readable_name):
        return

    log.warning(
        f"This dataset still has a layer called '{LEGACY_LAYER_NAME}', from a publish "
        f"made before layers were named after their dataset. It holds the same "
        f"tomogram as the '{request.layer_name}' layer being written now, and it is "
        f"left in place — nothing here deletes published data.\n"
        f"    Remove it in WEBKNOSSOS once the new layer looks right:\n"
        f"    {WK_URL}/datasets/{remote_ds.dataset_id}"
    )


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
        # Backstop for a legacy dataset that slipped past the up-front check —
        # the datastore refuses to write an on-disk datasource with no layers.
        if "No layers" in text or "notVirtual" in text:
            raise LegacyDatasetError(
                f"Removing layer '{layer_name}' was rejected by the server, which "
                "means this dataset is not virtual: it was published by the old "
                "convert-in-place system.\n"
                f"  {text.splitlines()[0]}\n\n"
                "Nothing has been changed. Re-run with --replace-legacy to publish a "
                "new dataset alongside it."
            ) from exc
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
        "  3. Re-run the same command. It is safe: the metadata file records the dataset\n"
        "     id, so the re-run overwrites the same dataset rather than duplicating\n"
        f"     it. To be explicit, pass --dataset-id {dataset_id}\n"
        "\n"
        "To stop it happening again, raise both timeouts — the client's and the\n"
        "proxy's, since either can be the one that gives up:\n"
        "  export WK_TIMEOUT=7200                    # client, default 1800s\n"
        "  proxy_read_timeout in the nginx config    # often only 60s by default\n"
    )


# ── Support ───────────────────────────────────────────────────────────────────


def _open_lock(lock_path: Path):
    """Open the lock file, writable by anyone who comes next.

    `open(path, "a+")` needs write permission on an existing file, so a lock
    left behind by a killed job would stop every *other* user from even opening
    it — and not with "the lock is held" but with a PermissionError. The staging
    directory is shared and jobs get killed, so that is the common case, not the
    rare one.

    A lock file holds nothing worth protecting and its whole purpose is to be
    contended, so it is created 0666 and the next run takes it over whoever left
    it. The mode is set explicitly because O_CREAT's mode is masked by the
    umask, and only the owner may chmod — so for a file somebody else created
    that is best-effort, and the caller explains the failure if it is still
    unwritable.

    Not truncated on open: the holder's identity has to survive for a contending
    run to read, and truncating before the lock is taken would destroy it.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if os.fstat(fd).st_mode & 0o777 != 0o666:
            os.fchmod(fd, 0o666)
    except OSError:
        pass  # someone else's file; we hold it open, which is what matters
    return os.fdopen(fd, "r+")


def _unwritable_lock_message(dataset_name: str, lock_path: Path) -> str:
    owner = "unknown"
    holder = "unknown"
    try:
        import pwd

        owner = pwd.getpwuid(lock_path.stat().st_uid).pw_name
    except (OSError, KeyError, ImportError):
        pass
    try:
        holder = lock_path.read_text().strip() or "unknown"
    except OSError:
        pass

    return (
        f"Cannot take the publication lock for '{dataset_name}': the lock file "
        f"exists and is not writable by you.\n"
        f"  lock:     {lock_path}\n"
        f"  owner:    {owner}\n"
        f"  held by:  {holder}\n"
        "\n"
        "Nothing has been changed.\n"
        "\n"
        "A run that fails normally removes its own lock, so this is almost always "
        "one left behind by a job that was killed. Check whether that job is still "
        "running (the line above names its host and SLURM id). If it is not, remove "
        "the file and re-run:\n"
        f"    rm {lock_path}\n"
        "\n"
        f"If {owner} cannot be reached and the staging directory is sticky, only they "
        "or root can remove it."
    )


@contextmanager
def _dataset_lock(dataset_name: str, log: logging.Logger) -> Iterator[None]:
    """Prevent two runs from publishing the same dataset at once.

    The lock is advisory and held on an open file descriptor, so the kernel
    releases it if the process is killed.
    """
    lock_path = TEMP_BASE / f"{dataset_name}.lock"
    try:
        lock_file = _open_lock(lock_path)
    except PermissionError:
        raise RuntimeError(_unwritable_lock_message(dataset_name, lock_path)) from None

    # Taking the lock is kept out of the try/finally below on purpose. That
    # finally unlinks the file, and a run that failed to acquire the lock must
    # not delete the file the holder is still using: flock lives on the inode,
    # so the holder would keep working while a third run created a fresh file
    # at the same path and locked that — two runs, both believing they are
    # alone, publishing the same dataset.
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        # With jobs running on different cluster nodes, "another run" is not
        # actionable on its own. The holder wrote down who it is. Read it
        # through a fresh open: over NFS the descriptor we held may serve a
        # cached, empty view of a file another host just wrote, and
        # close-to-open consistency only revalidates on open.
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

    try:
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
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:
            # Removing a file needs write permission on the directory, and a
            # sticky staging directory also requires owning the file. Failing
            # here would turn a finished publish into a failed job over a file
            # the next run can take over anyway.
            log.warning(f"Could not remove the lock file {lock_path}: {exc}")


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
    kind = "zarr store" if is_zarr(request.tiff_stack) else "TIFF stack"
    channel = f", channel {request.channel}" if request.channel is not None else ""
    log.info(f"  Input:          {request.tiff_stack} ({kind}{channel})")
    log.info(f"  Voxel size:     {request.voxel_size_nm} nm")
    log.info(f"  Folder:         Datasets/{request.folder_path}")
    log.info(f"  Teams:          {request.user_team} + ximg")
    log.info(f"  Metadata file:  {request.metadata_file} ({found})")
    log.info(f"  Parallelism:    {plan.describe()}")
    log.info("=== DRY RUN complete ===")
