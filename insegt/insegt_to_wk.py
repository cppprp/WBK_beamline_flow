#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos>=4.0.0",
#     "numba",  # fast downsampling; without it webknossos falls back to slow numpy
#     "tensorstore",
#     "numpy",
# ]
# ///
"""Send insegt3d predictions back to WEBKNOSSOS as segmentation layers.

insegt3d writes per-class scores as an OME-Zarr 0.5 store: a uint8 array of
shape (C, Z, Y, X) where each value is 255 x the class probability. This script
thresholds every class into a binary mask (voxel >= threshold -> segment 1) and
adds one segmentation layer per class to an existing dataset, named after the
class (`class_1`, `class_2`, ... — the same numbering insegt3d's own `labels`
array uses). Running it again replaces those layers; the dataset id never
changes.

    uv run insegt/insegt_to_wk.py <prediction.zarr> --dataset-id <id> --dry-run
    uv run insegt/insegt_to_wk.py <prediction.zarr> --dataset-id <id> --channels 2,3

It runs on your own computer and talks to WEBKNOSSOS over HTTPS only — no
access to the storage behind the server is needed. The layers are built and
downsampled locally first, in a temporary directory, and uploaded afterwards,
so nothing on the server changes until the local result is complete.

Which magnification the prediction belongs to is worked out from its shape:
insegt3d predicts at whichever pyramid level the model was trained on, and the
level-0 array of its output has that level's shape. It is matched against the
dataset's xray layer at each of its mags.

The token comes from --token or $WK_TOKEN — your own, from Account Settings ->
Auth Token. Layers are added under that account.

Only virtual datasets accept uploaded mags. Everything wbk_publish has
published is virtual; datasets from the old convert-in-place system are not and
are refused before any work is done.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

WK_URL = "https://webknossos.embl-hamburg.de"

# The layer whose geometry the prediction is matched against. insegt3d is
# normally run on a streamed xray layer: named <SAMPLE>_<RUN>_xray since
# wbk_publish adopted that convention, plain 'xray' before. Any color layer is
# the fallback.
REFERENCE_SUFFIX = "xray"

# insegt3d stores 255 x p, so 128 is p >= 0.5 — for two classes, the argmax.
DEFAULT_THRESHOLD = 128

# Output storage, in voxels of the target mag, (x, y, z). One shard is one unit
# of work: 64 MiB of uint8, small enough for a laptop, and since every tile is a
# whole shard, tiles can be written in parallel without two writers ever
# touching the same file.
CHUNK_SHAPE = 32
SHARD_SHAPE = (1024, 1024, 64)


class InsegtError(RuntimeError):
    """A problem with the input or the target, reported before anything is uploaded."""


def log(message: str = "") -> None:
    print(message, flush=True)


# ── Reading the prediction ────────────────────────────────────────────────────


@dataclass
class Source:
    """The finest level of the prediction, readable as (x, y, z) per channel."""

    path: Path  # the array directory
    store: object  # tensorstore.TensorStore, in the order it is stored
    axes: list[str]  # one name per stored dimension
    labels: list[str | None]  # OME channel labels, if any
    scale_xyz: tuple[float, float, float] | None  # NGFF scale, for a sanity check

    @property
    def shape_xyz(self) -> tuple[int, int, int]:
        shape = self.store.shape
        return tuple(int(shape[self.axes.index(a)]) for a in "xyz")

    @property
    def num_channels(self) -> int:
        return int(self.store.shape[self.axes.index("c")]) if "c" in self.axes else 1

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self.store.dtype.numpy_dtype)

    def read(self, channel: int, lo: tuple[int, int, int], hi: tuple[int, int, int]) -> np.ndarray:
        """One channel of the region [lo, hi), given in (x, y, z), returned as (x, y, z)."""
        index = []
        for axis in self.axes:
            if axis in "xyz":
                i = "xyz".index(axis)
                index.append(slice(lo[i], hi[i]))
            elif axis == "c":
                index.append(channel)
            else:
                index.append(0)  # validated to have size 1
        data = self.store[tuple(index)].read().result()
        spatial = [a for a in self.axes if a in "xyz"]
        return np.transpose(data, [spatial.index(a) for a in "xyz"])


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InsegtError(f"Not a zarr3 store: {path} does not exist") from None


def open_source(path: Path, axes: str | None = None) -> Source:
    """Open an OME-Zarr group (insegt3d's output) or a bare zarr3 array.

    Axis names, never positions, decide the order: insegt3d writes c,z,y,x,
    while WEBKNOSSOS streams c,x,y,z, and either may arrive here.
    """
    import tensorstore as ts

    path = Path(str(path).rstrip("/"))
    meta = _read_json(path / "zarr.json")

    group_axes: list[str] | None = None
    labels: list[str | None] = []
    scale: list[float] | None = None

    if meta.get("node_type") == "group":
        attrs = meta.get("attributes", {})
        ome = attrs.get("ome", attrs)  # 0.5 nests under "ome"; 0.4 did not
        multiscales = ome.get("multiscales")
        if not multiscales:
            raise InsegtError(
                f"{path} is a zarr group but has no OME multiscales metadata, so the "
                "array to read cannot be found. Point at the array directory instead "
                "(usually <store>/0)."
            )
        finest = multiscales[0]["datasets"][0]
        array_path = path / finest["path"]
        group_axes = [a["name"] if isinstance(a, dict) else a for a in multiscales[0].get("axes", [])]
        labels = [c.get("label") for c in ome.get("omero", {}).get("channels", [])]
        for transform in finest.get("coordinateTransformations", []):
            if transform.get("type") == "scale":
                scale = transform["scale"]
    else:
        array_path = path

    array_meta = _read_json(array_path / "zarr.json")
    if array_meta.get("zarr_format") != 3 or array_meta.get("node_type") != "array":
        raise InsegtError(f"{array_path} is not a zarr v3 array")

    if axes:
        names = list(axes.lower())
    else:
        names = group_axes or array_meta.get("dimension_names")
        if not names or any(n is None for n in names):
            raise InsegtError(
                f"{array_path} does not name its axes. Pass --axes, e.g. --axes czyx."
            )
        names = [str(n).lower() for n in names]

    store = ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": str(array_path)}},
        read=True,
    ).result()

    _validate_axes(names, store.shape, array_path)

    scale_xyz = None
    if scale is not None and len(scale) == len(names):
        scale_xyz = tuple(float(scale[names.index(a)]) for a in "xyz")

    return Source(array_path, store, names, labels, scale_xyz)


def _validate_axes(names: list[str], shape: tuple[int, ...], where: Path) -> None:
    if len(names) != len(shape):
        raise InsegtError(
            f"{len(names)} axis names {names} for a {len(shape)}-dimensional array at {where}"
        )
    for axis in "xyz":
        if names.count(axis) != 1:
            raise InsegtError(f"Axes {names} must contain '{axis}' exactly once ({where})")
    if names.count("c") > 1:
        raise InsegtError(f"Axes {names} contain more than one channel axis ({where})")
    for name, size in zip(names, shape):
        if name not in "cxyz" and size != 1:
            raise InsegtError(
                f"Axis '{name}' has size {size}; only x, y, z and one channel axis may be "
                f"larger than 1 ({where})"
            )


# ── Where the prediction goes in the dataset ──────────────────────────────────


@dataclass
class Placement:
    mag: object  # webknossos.Mag
    bbox: object  # webknossos.BoundingBox, in mag-1 voxels
    how: str


def expected_shape(bbox, mag) -> tuple[int, int, int]:
    """The shape of `bbox` stored at `mag`, rounded the way WEBKNOSSOS rounds it."""
    return tuple(bbox.align_with_mag(mag, ceil=True).in_mag(mag).size)


def match_geometry(shape_xyz, ref_bbox, ref_mags, forced_mag=None) -> Placement:
    """Find the mag, and the extent, the prediction corresponds to.

    Two extents are tried at each mag: the reference layer's bounding box, and
    the same box extended back to the origin — a streamed layer starts at 0, so
    a prediction made from a streaming link has that shape. For the datasets
    wbk_publish creates the two are identical.
    """
    from webknossos import BoundingBox, Mag

    candidates = [(ref_bbox, "the reference layer's bounding box")]
    if ref_bbox.topleft != (0, 0, 0):
        candidates.append(
            (BoundingBox((0, 0, 0), ref_bbox.bottomright), "the streamed extent (from the origin)")
        )

    mags = [Mag(forced_mag)] if forced_mag else sorted(Mag(m) for m in ref_mags)
    tried = []
    for mag in mags:
        for bbox, how in candidates:
            want = expected_shape(bbox, mag)
            if tuple(shape_xyz) == want:
                return Placement(mag, bbox, f"mag {mag.to_layer_name()}, {how}")
            tried.append(f"    mag {mag.to_layer_name():<8} {want}   ({how})")

    raise InsegtError(
        f"The prediction's shape (x, y, z) = {tuple(shape_xyz)} matches no mag of the dataset.\n"
        "Expected one of:\n" + "\n".join(tried) + "\n"
        "Was the prediction made from this dataset? If it was made at a mag the dataset\n"
        "does not have, name it with --mag, e.g. --mag 4 or --mag 4-4-2."
    )


# ── Building the layers locally ───────────────────────────────────────────────


def shard_tiles(mag_bbox) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """[lo, hi) tiles in mag coordinates, one per shard the bbox touches.

    Shards sit on a grid anchored at the origin, not at the bbox, so tiles at
    the edges are partial — but no two tiles ever share a shard.
    """
    lo, hi = tuple(mag_bbox.topleft), tuple(mag_bbox.bottomright)
    ranges = []
    for axis in range(3):
        step = SHARD_SHAPE[axis]
        first, last = lo[axis] // step, math.ceil(hi[axis] / step)
        ranges.append(
            [(max(lo[axis], g * step), min(hi[axis], (g + 1) * step)) for g in range(first, last)]
        )
    return [
        ((x0, y0, z0), (x1, y1, z1))
        for (x0, x1) in ranges[0]
        for (y0, y1) in ranges[1]
        for (z0, z1) in ranges[2]
    ]


def build_local(
    source: Source,
    placement: Placement,
    channels: list[int],
    names: list[str],
    threshold: float,
    voxel_size,
    out_dir: Path,
    jobs: int = 4,
    align_with=None,
):
    """Write one thresholded, downsampled segmentation layer per channel. Returns the Dataset.

    `align_with` is the target dataset: the pyramid then gets the same mags as
    its xray layer (e.g. 4-4-2 rather than 4), so the layers zoom together.
    """
    import cluster_tools
    from webknossos import DataFormat, Dataset, SamplingModes, Vec3Int

    mag = placement.mag
    dataset = Dataset(out_dir, voxel_size=tuple(voxel_size))
    mag_views = []
    for name in names:
        layer = dataset.add_layer(
            name,
            "segmentation",
            dtype_per_channel="uint8",
            data_format=DataFormat.Zarr3,
            largest_segment_id=1,
            bounding_box=placement.bbox,
        )
        mag_views.append(
            layer.add_mag(mag, chunk_shape=CHUNK_SHAPE, shard_shape=SHARD_SHAPE, compress=True)
        )

    mag_bbox = placement.bbox.align_with_mag(mag, ceil=True).in_mag(mag)
    origin = tuple(mag_bbox.topleft)
    tiles = shard_tiles(mag_bbox)
    mag_vec = mag.to_vec3_int()
    written = [0] * len(names)

    def process(tile) -> None:
        lo, hi = tile
        src_lo = tuple(a - o for a, o in zip(lo, origin))
        src_hi = tuple(a - o for a, o in zip(hi, origin))
        for i, (channel, mag_view) in enumerate(zip(channels, mag_views)):
            mask = source.read(channel, src_lo, src_hi) >= threshold
            # A fresh layer reads as 0 everywhere, so empty tiles need not be
            # written — and are then not uploaded either. Sparse classes are the
            # common case, and this is most of their size.
            if not mask.any():
                continue
            mag_view.write(
                mask.astype(np.uint8),
                absolute_offset=Vec3Int(lo) * mag_vec,
                allow_unaligned=True,  # partial edge tiles; each shard has one writer
            )
            written[i] += 1

    log(f"Thresholding {len(channels)} class(es) at >= {threshold:g}: {len(tiles)} tiles")
    started = time.monotonic()
    # The first tile alone, so every array is opened before threads share it.
    process(tiles[0])
    done, step = 1, max(1, len(tiles) // 20)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for _ in pool.map(process, tiles[1:]):
            done += 1
            if done % step == 0 or done == len(tiles):
                log(f"  {done}/{len(tiles)} tiles ({time.monotonic() - started:.0f}s)")

    for name, count in zip(names, written):
        log(f"  {name}: {count} of {len(tiles)} tiles non-empty")

    log("Downsampling")
    with cluster_tools.get_executor("multiprocessing", max_workers=max(1, jobs)) as executor:
        for layer in dataset.layers.values():
            layer.downsample(
                from_mag=mag,
                sampling_mode=SamplingModes.ANISOTROPIC,
                shard_shape=SHARD_SHAPE,
                align_with_other_layers=align_with if align_with is not None else True,
                executor=executor,
            )
    return dataset


# ── WEBKNOSSOS ────────────────────────────────────────────────────────────────


def resolve_token(explicit: str | None) -> str:
    token = explicit or os.environ.get("WK_TOKEN", "").strip()
    if not token:
        raise InsegtError(
            "No token. Pass --token or set $WK_TOKEN — your own, from WEBKNOSSOS "
            "Account Settings -> Auth Token."
        )
    return token


def is_virtual(dataset_id: str, token: str, wk_url: str) -> bool | None:
    """Whether the dataset accepts uploaded mags. None if the server does not say."""
    import httpx

    response = httpx.get(
        f"{wk_url}/api/datasets/{dataset_id}", headers={"X-Auth-Token": token}, timeout=30
    )
    if response.status_code in (400, 403, 404):
        raise InsegtError(
            f"Dataset {dataset_id} not found, or this account cannot access it "
            f"(HTTP {response.status_code})."
        )
    response.raise_for_status()
    flag = response.json().get("isVirtual")
    return None if flag is None else bool(flag)


def reference_layer(remote_ds):
    for name in sorted(remote_ds.layers):
        if name == REFERENCE_SUFFIX or name.endswith(f"_{REFERENCE_SUFFIX}"):
            return remote_ds.get_layer(name)
    for layer in remote_ds.layers.values():
        if layer.category == "color":
            return layer
    raise InsegtError(f"Dataset has no color layer to match against: {sorted(remote_ds.layers)}")


def upload(remote_ds, local_dataset, names: list[str]) -> None:
    """Replace or add each layer. The local copy is complete before this runs."""
    from webknossos import TransferMode

    for name in names:
        if name in remote_ds.layers:
            log(f"Removing the existing '{name}' layer")
            remote_ds.delete_layer(name)
        log(f"Uploading '{name}'")
        try:
            remote_ds.add_layer_as_copy(
                local_dataset.get_layer(name), transfer_mode=TransferMode.HTTP
            )
        except Exception as exc:
            # add_layer_as_copy registers the layer before uploading its mags,
            # so a refused upload leaves an empty layer in the datasource.
            # Remove it rather than leave the dataset with a layer that has no data.
            try:
                if name in remote_ds.layers:
                    remote_ds.delete_layer(name)
            except Exception:  # noqa: BLE001 - the upload error is the one to report
                pass
            raise RuntimeError(
                f"Uploading '{name}' failed: {type(exc).__name__}: {exc}\n"
                "Layers before it were uploaded; this one was removed again. "
                "Re-running the same command uploads it."
            ) from exc


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_ints(value: str) -> list[int]:
    return [int(v) for v in value.split(",") if v.strip()]


def layer_names(source: Source, channels: list[int], explicit: str | None, prefix: str) -> list[str]:
    if explicit:
        names = [n.strip() for n in explicit.split(",") if n.strip()]
        if len(names) != len(channels):
            raise InsegtError(
                f"{len(names)} names given for {len(channels)} channels: {names}"
            )
        return names
    return [
        (source.labels[c] if c < len(source.labels) and source.labels[c] else f"{prefix}_{c + 1}")
        for c in channels
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Threshold insegt3d predictions and add them to a WEBKNOSSOS dataset "
        "as segmentation layers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("prediction", type=Path, help="insegt3d prediction store (or a zarr3 array)")
    parser.add_argument("--dataset-id", required=True, help="Target dataset, from its URL")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"Voxels >= this become 1 (default {DEFAULT_THRESHOLD}, i.e. p >= 0.5)",
    )
    parser.add_argument(
        "--channels", type=parse_ints,
        help="Channels to upload, 0-based, e.g. 1,2 to skip a background class 0 (default: all)",
    )
    parser.add_argument("--channel-names", help="Layer names, one per uploaded channel")
    parser.add_argument("--layer-prefix", default="class", help="Name prefix (default: class)")
    parser.add_argument("--mag", help="The mag the prediction is at, e.g. 2 or 2-2-1 (default: detected)")
    parser.add_argument("--axes", help="Axis order if the store does not name it, e.g. czyx")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="Parallel tiles and downsampling workers")
    parser.add_argument("--work-dir", type=Path, help="Where to build the layers (default: system temp)")
    parser.add_argument("--wk-url", default=WK_URL, help=f"WEBKNOSSOS instance (default {WK_URL})")
    parser.add_argument("--token", help="Your API token. Defaults to $WK_TOKEN")
    parser.add_argument("--dry-run", action="store_true", help="Check and report; build and upload nothing")
    args = parser.parse_args(argv)

    try:
        return run(args)
    except InsegtError as exc:
        log(f"\nError: {exc}\nNothing was uploaded.")
        return 1


def run(args: argparse.Namespace) -> int:
    from webknossos import RemoteDataset, User, webknossos_context

    source = open_source(args.prediction, args.axes)
    channels = args.channels if args.channels is not None else list(range(source.num_channels))
    bad = [c for c in channels if not 0 <= c < source.num_channels]
    if bad:
        raise InsegtError(f"Channels {bad} do not exist; the store has {source.num_channels}")
    names = layer_names(source, channels, args.channel_names, args.layer_prefix)
    if source.dtype.kind == "f" and args.threshold > 1:
        log(f"Warning: float data with threshold {args.threshold:g} — insegt3d scores are uint8 "
            "0-255, but probabilities in [0, 1] would need e.g. --threshold 0.5")

    token = resolve_token(args.token)
    with webknossos_context(url=args.wk_url, token=token):
        user = User.get_current_user()
        log(f"Acting as {user.first_name} {user.last_name} <{user.email}>")

        virtual = is_virtual(args.dataset_id, token, args.wk_url)
        if virtual is False:
            raise InsegtError(
                f"Dataset {args.dataset_id} is not virtual (published by the old "
                "convert-in-place system), so it cannot take uploaded layers — the "
                "server refuses the mag upload with 403 (tested 2026-10-07 with "
                "insegt/probe_legacy_upload.py). Re-publish it with wbk_publish "
                "--replace-legacy first, then run this against the new dataset id."
            )

        remote_ds = RemoteDataset.open(dataset_id=args.dataset_id, read_only=False)
        ref = reference_layer(remote_ds)
        placement = match_geometry(source.shape_xyz, ref.bounding_box, ref.mags.keys(), args.mag)

        voxel_size = tuple(remote_ds.voxel_size)
        if source.scale_xyz:
            expected = tuple(v * m for v, m in zip(voxel_size, placement.mag.to_list()))
            if not all(math.isclose(s, e, rel_tol=0.01) for s, e in zip(source.scale_xyz, expected)):
                log(f"Warning: the store's scale {source.scale_xyz} is not the dataset's voxel "
                    f"size at {placement.mag.to_layer_name()} {expected}. Units may differ; "
                    "the shape match is what is used.")

        log("")
        log(f"  Prediction:   {source.path}")
        log(f"  Axes / dtype: {','.join(source.axes)} / {source.dtype}")
        log(f"  Shape (xyz):  {source.shape_xyz}, {source.num_channels} channel(s)")
        log(f"  Dataset:      {args.wk_url}/datasets/{args.dataset_id} ('{remote_ds.name}')")
        log(f"  Matched:      {placement.how} (against '{ref.name}')")
        log(f"  Bounding box: {placement.bbox}")
        log(f"  Threshold:    >= {args.threshold:g}")
        for channel, name in zip(channels, names):
            action = "REPLACE" if name in remote_ds.layers else "ADD"
            log(f"  channel {channel} -> {name:<20} {action}")
        log("")

        if args.dry_run:
            log("Dry run: nothing built, nothing uploaded.")
            return 0

        with tempfile.TemporaryDirectory(dir=args.work_dir, prefix="insegt_to_wk.") as tmp:
            local = build_local(
                source, placement, channels, names, args.threshold, voxel_size,
                Path(tmp) / "dataset", jobs=args.jobs, align_with=remote_ds,
            )
            upload(remote_ds, local, names)

    log(f"\nDone: {args.wk_url}/datasets/{args.dataset_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
