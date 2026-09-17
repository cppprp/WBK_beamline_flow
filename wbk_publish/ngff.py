"""Restore the zarr3 group metadata that the upload leaves behind.

`Dataset.upload` and `RemoteLayer.add_mag_as_copy` transfer *mags only* — each
mag directory is renamed into place, carrying its own array-level `zarr.json`
inside it. The two group-level files stay in staging and are lost:

    <dataset>/zarr.json          the root group marker
    <dataset>/<layer>/zarr.json  the layer group, holding the OME-NGFF
                                 multiscales block: axes, voxel sizes,
                                 coordinate transforms, mag pyramid

WEBKNOSSOS does not need them — it resolves mags from the datasource, which for
virtual datasets lives in the database. But without them the published directory
is not a readable zarr3 hierarchy, so napari, zarr-python, tensorstore and
neuroglancer see nothing when pointed at it. The old convert-in-place flow left
complete datasets on disk, so this restores that.

The rewrite is not a straight copy. The server reserves mag paths with a random
suffix (`1__bhgirKI86DNZGmBv`), while the staged metadata refers to them as
plain `1`. Copying it verbatim would produce a hierarchy that looks valid and
points at directories that do not exist — worse than none. So the paths are
remapped to what the server actually allocated.

Failures here are logged and swallowed. The dataset is complete and correct in
WEBKNOSSOS either way, and a metadata file is not worth failing a six-hour
conversion over.
"""

import json
import logging
from copy import deepcopy
from pathlib import Path

from webknossos import RemoteAccessMode, RemoteDataset, webknossos_context

ZARR_JSON = "zarr.json"
ROOT_GROUP = {"zarr_format": 3, "node_type": "group"}


def capture_layer_metadata(
    staging: Path, layer_name: str, log: logging.Logger
) -> dict | None:
    """Read the staged layer's group metadata before staging is cleaned up."""
    path = staging / layer_name / ZARR_JSON
    if not path.exists():
        log.debug(f"No layer group metadata in staging at {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"Could not read staged layer metadata: {exc}")
        return None


def _remap_mag_paths(metadata: dict, mag_dirs: dict[str, str], log: logging.Logger) -> dict:
    """Point the multiscales entries at the directory names the server chose."""
    out = deepcopy(metadata)
    multiscales = out.get("attributes", {}).get("ome", {}).get("multiscales", [])

    remapped = 0
    for scale in multiscales:
        for entry in scale.get("datasets", []):
            original = entry.get("path")
            if original in mag_dirs:
                entry["path"] = mag_dirs[original]
                remapped += 1
            else:
                log.warning(
                    f"Mag '{original}' in the staged metadata has no published "
                    "counterpart; leaving its path unchanged"
                )

    log.debug(f"Remapped {remapped} multiscale paths")
    return out


def write_group_metadata(
    dataset_id: str,
    layer_name: str,
    staged_metadata: dict | None,
    token: str,
    url: str,
    log: logging.Logger,
) -> bool:
    """Write the root and layer group files into the published dataset.

    Returns True when both were written. Never raises — a failure leaves the
    dataset usable in WEBKNOSSOS, just not readable by external zarr tools.
    """
    if staged_metadata is None:
        log.warning("No staged layer metadata captured — skipping group metadata")
        return False

    try:
        with webknossos_context(token=token, url=url):
            remote = RemoteDataset.open(
                dataset_id=dataset_id,
                access_mode=RemoteAccessMode.DIRECT_PATH,
                read_only=True,
            )
            layer = remote.get_layer(layer_name)
            mag_paths = {str(mag): Path(str(layer.get_mag(mag).path)) for mag in layer.mags}

        if not mag_paths:
            log.warning("Published layer reports no mags — skipping group metadata")
            return False

        # A published mag lives at <dataset>/<layer>/<mag-dir>.
        any_path = next(iter(mag_paths.values()))
        layer_dir = any_path.parent
        dataset_dir = layer_dir.parent

        if not dataset_dir.is_dir():
            log.warning(
                f"Published dataset directory is not a local path ({dataset_dir}) — "
                "skipping group metadata"
            )
            return False

        mag_dirs = {mag: path.name for mag, path in mag_paths.items()}
        layer_metadata = _remap_mag_paths(staged_metadata, mag_dirs, log)

        _write_json(layer_dir / ZARR_JSON, layer_metadata)
        log.info(f"Wrote layer group metadata: {layer_dir / ZARR_JSON}")

        # Shared by every layer, so only create it if it is not already there.
        root = dataset_dir / ZARR_JSON
        if root.exists():
            log.debug(f"Root group metadata already present: {root}")
        else:
            _write_json(root, ROOT_GROUP)
            log.info(f"Wrote root group metadata: {root}")

        return True

    except Exception as exc:  # noqa: BLE001 - never fail a publish over metadata
        log.warning(
            f"Could not write zarr group metadata ({type(exc).__name__}: {exc}). "
            "The dataset is fine in WEBKNOSSOS but will not open in external "
            "zarr tools."
        )
        return False


def _write_json(path: Path, payload: dict) -> None:
    """Write atomically so a reader never sees a half-written group file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4), encoding="utf-8")
    tmp.replace(path)
