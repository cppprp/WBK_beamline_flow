"""The MX-Cube metadata file, `meta_<run>.json`: provenance plus dataset identity.

MX-Cube writes one per reconstruction into the sample directory, and the recon
tool edits it in place when a reconstruction is redone. It is where the
acquisition and reconstruction parameters live, so it is also where a publish
records what it did — under one top-level key:

    "webknossos": {
      "recon_111_1": {
        "dataset_id": "6710f...",
        "dataset_name": "20250921_p3l-eduke_MA040_1",
        "webknossos_url": "https://webknossos.embl-hamburg.de",
        "layers": {
          "MA040_1_xray": "2026-09-22T09:14:03+00:00",
          "predictions": "2026-09-30T09:02:11+00:00"
        }
      }
    }

The dataset id is what makes re-runs idempotent: a reconstruction that is redone
must replace the layer in the dataset it produced last time rather than publish
a second copy, and the id is the only stable handle for that (names can be
edited in the WEBKNOSSOS UI). It has to be recorded somewhere derivable from the
input path, because we need it *before* we know the id — hence this file.

A dataset can carry several layers — a tomogram and a segmentation of it — so
each is recorded with its own publication time under `layers`. The block is
keyed by the published directory's name because every directory in
a sample shares the one meta file: a tiled scan has `recon_111_1`, `recon_121_1`
and `stitching_1` beside a single `meta_1.json`. Nothing else in the file is ever
touched by a publish.

Publishes made before this scheme wrote a `dataset-metadata.json` sidecar into
the input directory. That file is still read, never written.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import KEY_MAPPING, LEGACY_SIDECAR_NAME, WK_URL

# The top-level key this pipeline owns in meta_<run>.json.
BLOCK_KEY = "webknossos"


def find_mx_cube_metadata(
    base_dir: Path, run_number: str, explicit: Path | None = None
) -> Path:
    """Locate meta_<run>.json one level above `base_dir`, unless given explicitly.

    The beamline CLI passes the run directory, so the file is looked for in the
    sample directory beside it, which is where MX-Cube writes it. The manual CLI
    passes the stack directory, so the file lands beside the stack — there is no
    run directory there, and the publication record has to go somewhere the data
    can be found from.

    The path is returned whether or not it exists; callers treat an absent file
    as "no acquisition metadata" and `record_publication` creates it.
    """
    if explicit is not None:
        return explicit
    return base_dir.parent / f"meta_{run_number}.json"


def _read_json(path: Path) -> dict | None:
    """Parse a JSON object file; None if missing, unreadable, or not an object."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def load_mx_cube_metadata(meta_file: Path, log: logging.Logger) -> dict:
    """Read the acquisition metadata, without our own block. {} when absent."""
    if not meta_file.exists():
        log.warning(f"Metadata file not found, continuing without it: {meta_file}")
        return {}
    data = _read_json(meta_file)
    if data is None:
        log.warning(f"Could not read metadata file {meta_file}; continuing without it")
        return {}
    data.pop(BLOCK_KEY, None)
    return data


# ── Identity: which dataset does this directory belong to? ────────────────────


def _id_for_this_instance(record: dict, source: str, log: logging.Logger) -> str | None:
    dataset_id = record.get("dataset_id")
    if not dataset_id:
        return None
    recorded_url = record.get("webknossos_url")
    if recorded_url and recorded_url.rstrip("/") != WK_URL.rstrip("/"):
        log.warning(
            f"{source} refers to a different WEBKNOSSOS instance ({recorded_url}), ignoring its id"
        )
        return None
    log.info(f"Found dataset id in {source}: {dataset_id}")
    return dataset_id


def read_dataset_id(
    meta_file: Path, key: str, log: logging.Logger, sidecar_dir: Path | None = None
) -> str | None:
    """The dataset previously published under `key`, if recorded.

    `key` identifies the entry inside the file's `webknossos` block. The
    beamline CLI uses the run directory's name, because one meta_<run>.json can
    describe two runs — a recon and a stitching with the same run number both
    resolve to meta_1.json. The manual CLI has no run directory to name and uses
    the dataset name, which is unique by construction.

    Looks in meta_<run>.json first, then in the legacy sidecar when a directory
    to look in was given. A missing or corrupt file is not fatal: the caller
    falls back to a lookup by name.
    """
    data = _read_json(meta_file)
    record = (data or {}).get(BLOCK_KEY, {}).get(key)
    if isinstance(record, dict):
        found = _id_for_this_instance(record, meta_file.name, log)
        if found:
            return found

    if sidecar_dir is not None:
        legacy = _read_json(sidecar_dir / LEGACY_SIDECAR_NAME)
        if legacy:
            return _id_for_this_instance(legacy, "legacy sidecar", log)
    return None


def _layers_of(record: dict) -> dict[str, str]:
    """A record's layers, migrating the single-layer form this replaced.

    Records written before datasets could carry a second layer have `layer` and
    `published_at` instead. Reading them as a one-entry mapping means the next
    publish rewrites them in the new shape without losing when the first layer
    went out.
    """
    layers = record.get("layers")
    if isinstance(layers, dict):
        return dict(layers)
    if record.get("layer"):
        return {record["layer"]: record.get("published_at", "")}
    return {}


def _last_published(record: dict) -> str | None:
    """The most recent publication time across this record's layers."""
    times = [t for t in _layers_of(record).values() if t]
    return max(times) if times else None


def report_staleness(meta_file: Path, key: str, log: logging.Logger) -> None:
    """Say whether the metadata changed after the last publish.

    The recon tool stamps `last_updated` when it edits the file. Later than the
    most recent publish, it means WEBKNOSSOS still shows the previous
    reconstruction. "Most recent" across all layers, since any of them being
    older than the metadata is the thing worth saying.
    """
    data = _read_json(meta_file) or {}
    record = data.get(BLOCK_KEY, {}).get(key) or {}
    updated, published = data.get("last_updated"), _last_published(record)
    if not (updated and published):
        return
    try:
        stale = _parse_time(updated) > _parse_time(published)
    except ValueError:
        return
    if stale:
        log.info(
            f"Metadata was updated on {updated}, after the last publish on {published} "
            "— this run refreshes it"
        )
    else:
        log.info(f"Metadata unchanged since the last publish on {published}")


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── The display mapping ───────────────────────────────────────────────────────


def to_display_metadata(raw: dict, log: logging.Logger) -> dict:
    """Remap raw MX-Cube keys to the labels shown in the WEBKNOSSOS UI.

    Mapped keys come first, in the order declared in KEY_MAPPING; anything
    unrecognised is passed through unchanged so new acquisition fields still
    reach the UI without a code change. Our own block never does.
    """
    if not raw:
        return {}

    raw = {key: value for key, value in raw.items() if key != BLOCK_KEY}

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


# ── Recording a publish ───────────────────────────────────────────────────────


def record_publication(
    meta_file: Path,
    key: str,
    *,
    dataset_id: str,
    dataset_name: str,
    layer_name: str,
    log: logging.Logger,
) -> Path | None:
    """Set `key`'s entry in the file's `webknossos` block.

    The file is re-read here rather than reusing what was loaded before the
    conversion: on a long run the recon tool may have edited it in between,
    and those edits must not be written over. Returns the path written, or
    None if the file exists but is not valid JSON — overwriting an unreadable
    file could destroy acquisition data, so the id is logged instead and the
    next run finds the dataset by name.
    """
    if meta_file.exists():
        data = _read_json(meta_file)
        if data is None:
            log.error(
                f"{meta_file} is not a JSON object; not recording the publication in it.\n"
                f"  Published: dataset_id={dataset_id} for {key}\n"
                "  Fix the file, then add the id under its \"webknossos\" key, or pass\n"
                "  --dataset-id on the next run."
            )
            return None
    else:
        log.info(f"No metadata file at {meta_file}; creating one with the publication record")
        data = {}

    block = data.setdefault(BLOCK_KEY, {})
    if not isinstance(block, dict):
        block = data[BLOCK_KEY] = {}
    # Keep the other layers' entries: a dataset may hold a tomogram and a
    # segmentation of it, published at different times by different commands.
    record = block.get(key)
    layers = _layers_of(record) if isinstance(record, dict) else {}
    layers[layer_name] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    block[key] = {
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "webknossos_url": WK_URL,
        "layers": layers,
    }

    # Write-then-rename: a crash mid-write must not destroy the file.
    tmp_path = meta_file.with_name(meta_file.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(meta_file)

    log.info(f"Publication recorded in {meta_file} under '{key}'")
    return meta_file
