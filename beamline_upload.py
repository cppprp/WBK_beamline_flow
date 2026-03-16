#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "webknossos",
#   "cluster_tools",
# ]
# ///
import os
os.environ["OMP_NUM_THREADS"] = "8"
os.environ["MKL_NUM_THREADS"] = "8"
os.environ["OPENBLAS_NUM_THREADS"] = "8"
os.environ["BLOSC_NTHREADS"] = "8"
"""
Beamline TIFF → WEBKNOSSOS upload script.

V20260226

Triggered automatically via SSH after reconstruction or stitching is complete.
Always uploads as the 'xray' layer. If the dataset already exists the xray
layer is replaced and the dataset ID is preserved.

The dataset name is constructed automatically from the input path

After upload: folder placement, team permissions, and MX-Cube metadata are
attached automatically.

Input path must be the recon or stitching directory:
    .../RECON/20250921/MA040/recon_111_1/    → tiff stack at recon_111_1/tomo/
    .../RECON/20250921/MA040/stitching_1/    → tiff stack at stitching_1/stack/

Dataset name constructed as:
    recon:     YYYYMMDD_p3l-user_SAMPLE_N          (e.g. 20250921_p3l-eduke_MA040_1)
    stitching: YYYYMMDD_p3l-user_SAMPLE_stitching_N (e.g. 20250921_p3l-eduke_MA040_stitching_1)

Usage:
    python beamline_upload.py \\
        /mnt/beegfs7/P14/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1/ \\
        --voxel-size 650
"""

import fcntl
import json
import logging

import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from webknossos import Dataset, RemoteFolder, Team
from webknossos.client import webknossos_context
from webknossos.dataset.remote_dataset import RemoteDataset
from webknossos.dataset.transfer_mode import TransferMode

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

WK_URL       = "https://webknossos.embl-hamburg.de"
VIRTUAL_DIR  = Path("/morningstar/webknossos/.published/EMBL_Hamburg/.virtual")
TEMP_BASE    = Path("/morningstar/tmp")
BEEGFS_MOUNT = Path("/mnt/beegfs7")
LAYER_NAME   = "xray"
XIMG_TEAM    = "ximg"

DATASET_NAME_PATTERN = re.compile(r"^\d{8}_p3l-[\w]+_.+$")

KEY_MAPPING = {
    'user':               'User',
    'operator':           'Operator',
    'proposal_id':        'Proposal ID',
    'timestamp':          'Timestamp',
    'sample_name':        'Sample Name',
    'sample_run':         'Sample Run',
    'magnification':      'Magnification',
    'pixelsize':          'Pixel Size (µm)',
    'energy_kev':         'Energy (keV)',
    'exposure_time_s':    'Exposure Time (s)',
    'number_of_images':   'Number of Projections',
    'angular_range_deg':  'Angular Range (°)',
    'distances_mm':       'Sample to Detector Distances (mm)',
    'betadelta':          'Beta/Delta Ratio',
    'mode':               'Acquisition Mode',
    'flux':               'Beam Intensity (A)',
    'tiling_x_steps':     'Tiling X Steps',
    'tiling_x_delta_mm':  'Tiling X Delta (mm)',
    'tiling_y_steps':     'Tiling Y Steps',
    'tiling_y_delta_mm':  'Tiling Y Delta (mm)',
    'tiling_z_steps':     'Tiling Z Steps',
    'tiling_z_delta_mm':  'Tiling Z Delta (mm)',
    'file_path':          'Path to Raw Data',
}


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def beamline_upload(
    input_dir: Path,
    voxel_size_nm: int,
    token: str,
    num_cpus: int = 8,
    dry_run: bool = False,
) -> RemoteDataset:

    dataset_name = _dataset_name_from_path(input_dir) # <-- Check passed
    log = _make_logger(dataset_name)
    log.info(f"Constructed dataset name: {dataset_name}")

    _validate_dataset_name(dataset_name) # <-- Check passed
    tiff_stack = _resolve_tiff_stack(input_dir) # <-- Check passed
    _check_beegfs(tiff_stack, log) # <-- Eye check test in prod

    voxel_size = (voxel_size_nm, voxel_size_nm, voxel_size_nm)  # → nm tuple
    TEMP_BASE.mkdir(parents=True, exist_ok=True)
    components = _parse_dataset_name(dataset_name) #<-- Check passed

    # Per-dataset lockfile — prevents two concurrent jobs on the same dataset.
    # fcntl.flock is released automatically if the process dies.
    lock_path = TEMP_BASE / f"{dataset_name}.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError(
            f"Another conversion is already running for '{dataset_name}'.\n"
            f"Lock file: {lock_path}"
        )

    try:
        existing_path = _find_dataset_on_disk(dataset_name) #<-- check passed
        if dry_run:
            action = "OVERWRITE xray layer" if existing_path else "FRESH UPLOAD"
            meta_file = input_dir.parent / f"meta_{components['run_number']}.json"
            log.info("=== DRY RUN — no files written, no API calls made ===")
            log.info(f"  Action:        {action}")
            log.info(f"  Dataset name:  {dataset_name}")
            log.info(f"  Readable name: {components['readable_name']}")
            log.info(f"  TIFF stack:    {tiff_stack}")
            log.info(f"  Voxel size:    {voxel_size_nm} nm")
            log.info(f"  Folder:        Datasets/{components['user']}/{components['imaging_date']}/")
            log.info(f"  Metadata file: {meta_file} ({'found' if meta_file.exists() else 'NOT FOUND'})")
            if existing_path:
                log.info(f"  Existing path: {existing_path}")
            log.info("=== DRY RUN complete — exiting===")
            raise SystemExit(0)

        if existing_path is None:
            remote_ds = _fresh_upload(tiff_stack, dataset_name, voxel_size, token, num_cpus, log) #<-- passed visual check
            _set_permissions_and_metadata(remote_ds, input_dir, components, token, log) #<-- visual check passed
        else:
            remote_ds = _overwrite_xray(tiff_stack, dataset_name, voxel_size, existing_path, token, num_cpus, log) #<-- visual check passed
            # Folder and teams already set on first upload — only refresh metadata.
            with webknossos_context(token=token, url=WK_URL):
                _attach_metadata(remote_ds, input_dir, components, log)
        final_path = _find_dataset_on_disk(dataset_name)
        if final_path:
            _write_metadata_sidecar(final_path, input_dir, components, log)
        else:
            log.warning("Could not locate dataset on disk — sidecar not written")

    except Exception as e:
        log.error(f"Pipeline failed: {type(e).__name__}: {e}")
        raise
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        lock_path.unlink(missing_ok=True)

    log.info(f"Done: {WK_URL}/datasets/{remote_ds.dataset_id}")
    return remote_ds


# ──────────────────────────────────────────────────────────────────────────────
# Fresh upload
# ──────────────────────────────────────────────────────────────────────────────

def _fresh_upload(
    tiff_stack: Path,
    dataset_name: str,
    voxel_size_um: tuple,
    token: str,
    num_cpus: int,
    log: logging.Logger,
) -> RemoteDataset:
    log.info("Fresh upload — no existing dataset found")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{dataset_name}") as tmp_dir:
        tmp_path = Path(tmp_dir)
        log.info(f"Staging in: {tmp_path}")

        local_ds = _build_wk_dataset(tiff_stack, tmp_path, dataset_name, voxel_size_um, num_cpus, log) #<-- passed visual check

        with webknossos_context(token=token, url=WK_URL):
            remote_ds = local_ds.upload(
                new_dataset_name=dataset_name,
                transfer_mode=TransferMode.MOVE_AND_SYMLINK,
            )

    log.info(f"Published: '{remote_ds.name}' (id={remote_ds.dataset_id})")
    return remote_ds


# ──────────────────────────────────────────────────────────────────────────────
# Overwrite xray layer
# ──────────────────────────────────────────────────────────────────────────────

def _overwrite_xray(
    tiff_stack: Path,
    dataset_name: str,
    voxel_size_um: tuple,
    storage_path: Path,
    token: str,
    num_cpus: int,
    log: logging.Logger,
) -> RemoteDataset:
    dataset_id = storage_path.name[len(f"{dataset_name}-"):]
    log.info(f"Overwriting xray layer — existing dataset id={dataset_id}")

    with TemporaryDirectory(dir=TEMP_BASE, prefix=f"{dataset_name}_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        log.info(f"Pre-writing new xray layer in: {tmp_path}")

        tmp_ds = _build_wk_dataset(tiff_stack, tmp_path, dataset_name, voxel_size_um, num_cpus, log)

        storage_ds = Dataset.open(storage_path) # call remote dataset.open
        # RemoteDataset.delete_layer()
        if LAYER_NAME in [l.name for l in storage_ds.layers.values()]:
            log.info(f"Removing old xray layer via API (updates datasource-properties.json)")
            storage_ds.delete_layer(LAYER_NAME)
        else:
            log.warning(f"No existing xray layer in dataset — adding fresh")

        log.info("Copying new xray layer into storage dataset …")
        storage_ds.add_layer_as_copy(  # add transfer mode for RD
            tmp_ds.get_layer(LAYER_NAME),
            new_layer_name=LAYER_NAME,
        )
        # Per conversation with Norman, no need to reload in datastore if RemoteDataset was edited
        log.info("Triggering datastore reload …")
        with webknossos_context(token=token, url=WK_URL):
            RemoteDataset.trigger_reload_in_datastore(dataset_id=dataset_id)
            remote_ds = RemoteDataset.open(dataset_id=dataset_id)

    log.info(f"Overwrite complete — id unchanged: {dataset_id}")
    return remote_ds


# ──────────────────────────────────────────────────────────────────────────────
# Dataset construction
# ──────────────────────────────────────────────────────────────────────────────

def _build_wk_dataset(
    tiff_stack: Path,
    out_path: Path,
    dataset_name: str,
    voxel_size_um: tuple,
    num_cpus: int,
    log: logging.Logger,
) -> Dataset:
    import cluster_tools
    import math
    tiff_files = sorted(
        list(tiff_stack.glob("*.tif")) + list(tiff_stack.glob("*.tiff"))
    )
    if not tiff_files:
        raise FileNotFoundError(f"No TIFF files found in {tiff_stack}")
    log.info(f"Found {len(tiff_files)} TIFF slices, voxel_size={voxel_size_um} µm")
    n_slices = len(tiff_files)

    # z: aim for at least num_cpus tasks, must be multiple of 32 (chunk_shape.z)
    #z_chunks = max(1, math.ceil(n_slices / num_cpus / 32))
    ds = Dataset(out_path, voxel_size=voxel_size_um, name=dataset_name)

    with cluster_tools.get_executor("multiprocessing", max_workers = num_cpus) as executor:
        layer = ds.add_layer_from_images(
            images=[str(f) for f in tiff_files],
            layer_name=LAYER_NAME,
            category="color",
            chunks_per_shard=(128, 128, 1),  # XYZ matches CLI default
            compress=True, executor=executor
        )
        layer.downsample(executor=executor)

    log.info(f"xray layer + mag pyramid written to {out_path}")
    return ds


# ──────────────────────────────────────────────────────────────────────────────
# Permissions, folder, and metadata (fresh upload only)
# ──────────────────────────────────────────────────────────────────────────────

def _set_permissions_and_metadata(
    remote_ds: RemoteDataset,
    input_dir: Path,
    components: dict,
    token: str,
    log: logging.Logger,
) -> None:
    log.info("Setting folder, permissions and metadata …")

    with webknossos_context(token=token, url=WK_URL):

        # Rename to human-readable form (e.g. MA040_1)
        remote_ds.name = components["readable_name"]
        log.debug(f"Dataset renamed to: {components['readable_name']}")

        # Folder: Datasets/<user>/<date>  — dataset appears as inside <date>
        folder_path = (
            f"{components['user']}/"
            f"{components['imaging_date']}/"
        )
        folder = _get_or_create_folder(folder_path, log)
        remote_ds.folder = folder
        log.debug(f"Placed in folder: Datasets/{folder_path}")

        # Teams: user team + ximg, both assigned simultaneously
        user_team = _get_or_create_team(components["user"], log)
        try:
            ximg_team = Team.get_by_name(XIMG_TEAM)
        except Exception:
            log.warning(f"'{XIMG_TEAM}' team not found — assigning user team only")
            ximg_team = None

        remote_ds.allowed_teams = [t for t in [user_team, ximg_team] if t is not None]
        log.debug(f"Teams assigned: {[t.name for t in remote_ds.allowed_teams]}")

        _attach_metadata(remote_ds, input_dir, components, log)

    log.info("✓ Permissions and metadata set")


def _attach_metadata(
    remote_ds: RemoteDataset,
    input_dir: Path,
    components: dict,
    log: logging.Logger,
) -> None:
    """
    Read MX-Cube meta_N.json from input_dir.parent (the sample directory)
    and attach remapped fields to the Webknossos dataset.
    """
    sample_dir = input_dir.parent
    meta_file = sample_dir / f"meta_{components['run_number']}.json"

    if not meta_file.exists():
        log.warning(f"Metadata file not found, skipping: {meta_file}")
        return

    with open(meta_file) as f:
        raw = json.load(f)

    # distances_mm must be strings for WK
    if "distances_mm" in raw and isinstance(raw["distances_mm"], list):
        raw["distances_mm"] = [str(d) for d in raw["distances_mm"]]

    # Remap in declared order; pass through any unmapped keys at the end
    readable = {}
    for raw_key, display_label in KEY_MAPPING.items():
        if raw_key in raw:
            readable[display_label] = raw[raw_key]
    for k, v in raw.items():
        if k not in KEY_MAPPING:
            readable[k] = v
            log.debug(f"Unmapped metadata key passed through: {k}")

    remote_ds.metadata = readable
    log.debug(f"Metadata attached from {meta_file}")



def _write_metadata_sidecar(
    storage_path: Path,
    input_dir: Path,
    components: dict,
    log: logging.Logger,
) -> None:
    """
        Read MX-Cube meta_N.json from input_dir.parent (the sample directory)
        and write a dataset-metadata.json to dataset on system storage
        """
    sample_dir = input_dir.parent
    meta_file = sample_dir / f"meta_{components['run_number']}.json"

    if not meta_file.exists():
        log.warning(f"Sidecar: metadata file not found, skipping: {meta_file}")
        return

    with open(meta_file) as f:
        dataset_block = json.load(f)

    sidecar = {
        "dataset": dataset_block,
        "dataset_display": {
            display_label: dataset_block[raw_key]
            for raw_key, display_label in KEY_MAPPING.items()
            if raw_key in dataset_block
        },
    }

    sidecar_path = storage_path / "dataset-metadata.json"
    with open(sidecar_path, "w") as f:
        json.dump(sidecar, f, indent=2)
    log.info(f"Sidecar written: {sidecar_path}")

# ──────────────────────────────────────────────────────────────────────────────
# WK helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_or_create_folder(folder_path: str, log: logging.Logger) -> RemoteFolder:
    current_full = "Datasets"
    current_folder = RemoteFolder.get_by_path(current_full)
    for part in folder_path.split("/"):
        current_full = f"{current_full}/{part}"
        try:
            current_folder = RemoteFolder.get_by_path(current_full)
            log.debug(f"Folder exists: {current_full}")
        except Exception:
            log.debug(f"Creating folder: {current_full}")
            current_folder = current_folder.add_subfolder(part)
    return current_folder


def _get_or_create_team(team_name: str, log: logging.Logger) -> Team:
    try:
        team = Team.get_by_name(team_name)
        log.debug(f"Found team: {team_name}")
    except Exception:
        log.debug(f"Creating team: {team_name}")
        team = Team.add(team_name)
    return team


# ──────────────────────────────────────────────────────────────────────────────
# Path parsing
# ──────────────────────────────────────────────────────────────────────────────

def _dataset_name_from_path(path: Path) -> str:
    """
    Construct dataset name from the beamline path structure.

    Expected structure:
        .../p3l-account/.../YYYYMMDD/sample_dir/run_dir/

    Examples:
        .../p3l-eduke/RECON/20250921/MA040/recon_111_1/
            → 20250921_p3l-eduke_MA040_1

        .../p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/
            → 20251130_p3l-eduke_tree_2x2x10_stitching_1
    """
    parts = Path(str(path).rstrip("/")).parts

    # p3l account
    p3l_account = next(
        (p for p in parts if re.match(r"^p3l-[^/]+$", p)), None
    )
    if not p3l_account:
        raise ValueError(f"Cannot find p3l-account in path: {path}")

    # 8-digit date
    date_dir = next(
        (p for p in parts if re.match(r"^\d{8}$", p)), None
    )
    if not date_dir:
        raise ValueError(f"Cannot find YYYYMMDD date component in path: {path}")

    # Run dir (recon_N_N or stitching_N) and the sample dir immediately before it
    run_dir = None
    sample_dir = None
    date_idx = parts.index(date_dir)
    for i, part in enumerate(parts):
        if re.match(r"^(stitching_\d+|recon_\d+_\d+)$", part) and i > date_idx:
            run_dir = part
            sample_dir = parts[i - 1]
            break

    if not run_dir or not sample_dir:
        raise ValueError(
            f"Cannot find run directory (recon_XYZ_N or stitching_N) after date in path: {path}"
        )

    # For recon_111_1 extract just the final number; for stitching_1 keep the whole token
    recon_match = re.match(r"^recon_.*_(\d+)$", run_dir)
    run_identifier = recon_match.group(1) if recon_match else run_dir

    return f"{date_dir}_{p3l_account}_{sample_dir}_{run_identifier}"


def _resolve_tiff_stack(input_dir: Path) -> Path:
    """
    Derive the tiff stack subdirectory from the recon/stitching directory name.
        recon_*     → input_dir/tomo/
        stitching_* → input_dir/stack/
    """
    name = input_dir.name
    if "recon" in name:
        return input_dir / "tomo"
    elif "stitching" in name:
        return input_dir / "stack"
    else:
        raise ValueError(
            f"Cannot determine tiff stack location from input directory: {input_dir}\n"
            "Directory name must contain 'recon' or 'stitching'."
        )


def _validate_dataset_name(dataset_name: str) -> None:
    if not DATASET_NAME_PATTERN.match(dataset_name):
        raise ValueError(
            f"Constructed dataset name '{dataset_name}' does not match expected convention.\n"
            "  Expected: YYYYMMDD_p3l-user_SAMPLE-NAME_RUN-IDENTIFIER\n"
            "  Check that the input path follows the beamline structure."
        )


def _parse_dataset_name(dataset_name: str) -> dict:
    """
    Break dataset name into components for folder placement and metadata lookup.

    20250921_p3l-eduke_MA040_1        → imaging_date=20250921, user=p3l-eduke,
                                         readable_name=MA040_1, run_number=1
    20251130_p3l-eduke_tree_stitching_1 → readable_name=tree_stitching_1, run_number=1
    """
    parts = dataset_name.split("_")
    return {
        "imaging_date":  parts[0],
        "user":          parts[1],
        "readable_name": "_".join(parts[2:]),
        "run_number":    parts[-1],
    }


def _check_beegfs(tiff_stack: Path, log: logging.Logger) -> None:
    """
    Fail fast before any conversion work if beegfs7 is not mounted or the
    tiff stack directory is not accessible.
    """
    if not BEEGFS_MOUNT.is_mount():
        raise RuntimeError(
            f"beegfs7 is not mounted at {BEEGFS_MOUNT}.\n"
            "Cannot access input data or metadata. Aborting."
        )
    if not tiff_stack.is_dir():
        raise FileNotFoundError(f"TIFF stack directory not found: {tiff_stack}")
    log.debug("beegfs7 mounted ✓, tiff stack directory accessible ✓")


def _find_dataset_on_disk(dataset_name: str) -> Optional[Path]:
    if not VIRTUAL_DIR.exists():
        raise FileNotFoundError(
            f"VIRTUAL_DIR does not exist: {VIRTUAL_DIR}\n"
            "Check that the path is correct and accessible."
        )
    prefix = f"{dataset_name}-"
    matches = [d for d in VIRTUAL_DIR.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple directories found for dataset '{dataset_name}':\n"
            + "\n".join(f"  {m}" for m in matches)
            + "\nManual inspection required."
        )
    return matches[0]


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def _make_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    return log


def _get_auth_token() -> str:
    """Fallback: read token from script-dir/.config/auth_token.txt"""
    token_file = Path(__file__).parent / ".config" / "auth_token.txt"
    return token_file.read_text().strip()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Beamline upload: TIFF stack → WEBKNOSSOS xray layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input_dir", type=Path,
        help=" Path up to Recon or stitching directory (e.g. .../recon_111_1/ or .../stitching_1/)",
    )
    parser.add_argument(
        "--voxel-size", type=int, choices=[325, 650], required=True,
        dest="voxel_size_nm",
        help="Voxel size in nm (325 or 650, depending on magnification)",
    )
    parser.add_argument(
        "--token", type=str, default=os.environ.get("WK_TOKEN"),
        help="OPTIONAL: WK API token (will otherwise read from .config/auth_token.txt)",
    )
    parser.add_argument(
        "--cpus", type=int, default=8,
        help="CPUs for conversion (default: 8)",
    )
    parser.add_argument(
        "--dry-run", default=False,
        help="Validate inputs and print what would happen without converting or uploading",
    )
    args = parser.parse_args()

    token = args.token
    if not token:
        try:
            token = _get_auth_token()
        except FileNotFoundError:
            parser.error(
                "No token provided. Use --token,"
                "or place token in .config/auth_token.txt"
            )

    beamline_upload(
        input_dir=args.input_dir,
        voxel_size_nm=args.voxel_size_nm,
        token=token,
        num_cpus=args.cpus,
        dry_run=args.dry_run
    )