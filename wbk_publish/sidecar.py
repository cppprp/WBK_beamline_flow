"""The `dataset-metadata.json` sidecar: MX-Cube provenance plus dataset identity.

The sidecar lives next to the input data, in the recon/stitching directory. It
serves two purposes:

  1. Provenance — the MX-Cube acquisition metadata that was published, and when.
  2. Identity — the WEBKNOSSOS dataset id.

The identity half is what makes re-runs idempotent. A reconstruction that is
redone must replace the layer in the dataset it produced last time rather than
publishing a second copy, and the dataset id is the only stable handle for that
(names can be edited in the WEBKNOSSOS UI). It has to be recorded somewhere
derivable from the input path, because we need to look it up *before* we know
the id — hence: next to the input data.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import KEY_MAPPING, SIDECAR_NAME, WK_URL


def sidecar_path(input_dir: Path) -> Path:
    return input_dir / SIDECAR_NAME


def read_sidecar(input_dir: Path) -> dict | None:
    """Return the parsed sidecar, or None if it is missing or unreadable.

    A corrupt sidecar is not fatal: the caller falls back to a lookup by name,
    and the file is rewritten on the next successful publish.
    """
    path = sidecar_path(input_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def read_dataset_id(input_dir: Path, log: logging.Logger) -> str | None:
    """Return the recorded dataset id, if this sidecar refers to our WK instance."""
    sidecar = read_sidecar(input_dir)
    if not sidecar:
        return None

    dataset_id = sidecar.get("dataset_id")
    if not dataset_id:
        return None

    recorded_url = sidecar.get("webknossos_url")
    if recorded_url and recorded_url.rstrip("/") != WK_URL.rstrip("/"):
        log.warning(
            f"Sidecar refers to a different WEBKNOSSOS instance ({recorded_url}), ignoring its id"
        )
        return None

    log.info(f"Found dataset id in sidecar: {dataset_id}")
    return dataset_id


def find_mx_cube_metadata(
    input_dir: Path, run_number: str, explicit: Path | None = None
) -> Path:
    """Locate meta_<run>.json in the sample directory, unless given explicitly."""
    if explicit is not None:
        return explicit
    return input_dir.parent / f"meta_{run_number}.json"


def load_mx_cube_metadata(meta_file: Path, log: logging.Logger) -> dict:
    """Read the MX-Cube acquisition metadata. Returns {} when absent."""
    if not meta_file.exists():
        log.warning(f"Metadata file not found, continuing without it: {meta_file}")
        return {}
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read metadata file {meta_file}: {exc}")
        return {}


def to_display_metadata(raw: dict, log: logging.Logger) -> dict:
    """Remap raw MX-Cube keys to the labels shown in the WEBKNOSSOS UI.

    Mapped keys come first, in the order declared in KEY_MAPPING; anything
    unrecognised is passed through unchanged so new acquisition fields still
    reach the UI without a code change.
    """
    if not raw:
        return {}

    raw = dict(raw)

    # WEBKNOSSOS metadata values must be strings, numbers, or lists of strings.
    if isinstance(raw.get("distances_mm"), list):
        raw["distances_mm"] = [str(d) for d in raw["distances_mm"]]

    display = {
        label: raw[key] for key, label in KEY_MAPPING.items() if key in raw
    }
    for key, value in raw.items():
        if key not in KEY_MAPPING:
            display[key] = value
            log.debug(f"Unmapped metadata key passed through: {key}")

    return display


def write_sidecar(
    input_dir: Path,
    *,
    dataset_id: str,
    dataset_name: str,
    layer_name: str,
    raw_metadata: dict,
    display_metadata: dict,
    log: logging.Logger,
) -> Path:
    """Write the sidecar atomically, recording identity and published metadata."""
    payload = {
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "webknossos_url": WK_URL,
        "layer": layer_name,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": raw_metadata,
        "dataset_display": display_metadata,
    }

    path = sidecar_path(input_dir)
    # Write-then-rename: a crash mid-write must not destroy a recorded id.
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)

    log.info(f"Sidecar written: {path}")
    return path
