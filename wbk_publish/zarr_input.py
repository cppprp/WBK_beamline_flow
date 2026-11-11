"""A zarr store as the input, instead of a TIFF stack.

webknossos reads zarr through the same `add_layer_from_images` call the TIFF
path uses — plain arrays and OME-Zarr groups, zarr v2 and v3, taking the finest
level of a multiscale group and ordering axes by name — so the conversion, the
pyramid, the hand-over and re-runs are all unchanged. What differs is only what
happens before: there is no slice list to glob and no TIFF header to probe, so
the geometry the worker plan needs is read from the store's metadata here.

Two things are checked up front because they would otherwise fail, or quietly
go wrong, an hour into a conversion:

  * a store with several channels must name one (`--channel`). The pipeline
    publishes one layer per run, and webknossos refuses to add a multi-channel
    source as a single layer.
  * a bool store is converted to uint8 (0/1) while it is read — webknossos
    cannot store bool, and binary masks are often saved that way — and a float
    store is refused as a segmentation, which must be integers. Floats are not
    converted silently: they may be probabilities, and casting would truncate.
  * the voxel size given on the command line is compared with the store's own
    OME scale when it has one and says what unit it is in. A disagreement is a
    warning, not an error — OME units are often missing or wrong — but it is the
    one place a wrong `--voxel-size` (the default is 650 nm) would show up.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .resources import StackShape

ZARR_JSON = "zarr.json"
ZARRAY = ".zarray"
ZATTRS = ".zattrs"

# OME unit names → nanometres. Anything else is reported and not compared.
_UNIT_TO_NM = {
    "nanometer": 1.0,
    "micrometer": 1e3,
    "millimeter": 1e6,
    "angstrom": 0.1,
}


class ZarrInputError(RuntimeError):
    """The store cannot be published as given. Nothing has been changed."""


@dataclass(frozen=True)
class ZarrInfo:
    array_path: Path  # the array actually read: the finest level of a group
    axes: list[str]
    shape: tuple[int, ...]
    dtype: str
    scale_nm: tuple[float, float, float] | None  # x, y, z, when the store says

    def size(self, axis: str) -> int:
        return self.shape[self.axes.index(axis)] if axis in self.axes else 1

    @property
    def num_channels(self) -> int:
        return self.size("c")

    def stack_shape(self) -> StackShape:
        """What the worker plan needs, in the TIFF stack's terms.

        Conversion splits along z and every worker holds full xy planes, exactly
        as for a TIFF stack, so the same memory model applies. One channel is
        converted, whatever the store holds.
        """
        import numpy as np

        return StackShape(
            n_slices=self.size("z"),
            xy_pixels=self.size("x") * self.size("y"),
            bytes_per_pixel=np.dtype(self.dtype).itemsize,
        )


def is_zarr(path: Path) -> bool:
    return any((path / name).is_file() for name in (ZARR_JSON, ZARRAY, ZATTRS))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ZarrInputError(f"Cannot read {path}: {exc}") from exc


def probe_zarr(path: Path) -> ZarrInfo:
    """Read the geometry from metadata alone; no chunk is decoded."""
    import tensorstore as ts

    if not is_zarr(path):
        raise ZarrInputError(
            f"Not a zarr store: {path}\n"
            f"  Expected {ZARR_JSON} (zarr v3) or {ZARRAY} / {ZATTRS} (zarr v2) in it."
        )

    v3 = (path / ZARR_JSON).is_file()
    meta = _read_json(path / ZARR_JSON) if v3 else _read_json(path / ZATTRS)
    attrs = meta.get("attributes", {}) if v3 else meta
    ome = attrs.get("ome", attrs)  # NGFF 0.5 nests under "ome"; 0.4 did not
    multiscales = ome.get("multiscales")

    axes: list[str] | None = None
    scale_nm = None
    array_path = path
    if multiscales:
        finest = multiscales[0]["datasets"][0]
        array_path = path / finest["path"]
        axis_meta = multiscales[0].get("axes", [])
        axes = [str(a["name"] if isinstance(a, dict) else a).lower() for a in axis_meta]
        scale_nm = _scale_nm(axis_meta, finest)

    driver = "zarr3" if v3 else "zarr"
    try:
        store = ts.open(
            {"driver": driver, "kvstore": {"driver": "file", "path": str(array_path)}},
            read=True,
        ).result()
    except Exception as exc:
        raise ZarrInputError(f"Cannot open the array at {array_path}: {exc}") from exc

    if axes is None and v3:
        names = _read_json(array_path / ZARR_JSON).get("dimension_names")
        if names and all(names):
            axes = [str(n).lower() for n in names]
    if axes is None:
        # Same assumption webknossos makes for an unlabelled array.
        axes = ["c", "x", "y", "z"][-len(store.shape):] if len(store.shape) <= 4 else None
    if axes is None or len(axes) != len(store.shape):
        raise ZarrInputError(
            f"Cannot tell the axes of {array_path} (shape {tuple(store.shape)}). "
            "Add OME multiscales metadata or zarr v3 dimension_names to the store."
        )
    for axis in "xyz":
        if axis not in axes:
            raise ZarrInputError(f"{array_path} has no '{axis}' axis (axes {axes})")

    return ZarrInfo(
        array_path=array_path,
        axes=axes,
        shape=tuple(int(s) for s in store.shape),
        dtype=str(store.dtype.numpy_dtype),
        scale_nm=scale_nm,
    )


def _scale_nm(axis_meta: list, finest: dict) -> tuple[float, float, float] | None:
    """The finest level's x, y, z scale in nm — only when every unit is known."""
    scale = next(
        (t["scale"] for t in finest.get("coordinateTransformations", []) if t.get("type") == "scale"),
        None,
    )
    if scale is None or not all(isinstance(a, dict) for a in axis_meta):
        return None
    by_name = {str(a["name"]).lower(): (a.get("unit"), s) for a, s in zip(axis_meta, scale)}
    out = []
    for axis in "xyz":
        unit, value = by_name.get(axis, (None, None))
        factor = _UNIT_TO_NM.get(str(unit).lower()) if unit else None
        if factor is None or value is None:
            return None
        out.append(float(value) * factor)
    return tuple(out)


def conversion_dtype(info: ZarrInfo, category: str, log: logging.Logger) -> str | None:
    """The dtype to convert to while reading, or None to keep the store's."""
    import numpy as np

    kind = np.dtype(info.dtype).kind
    if kind == "b":
        log.info("  Store is bool: converting to uint8 (0/1) while reading.")
        return "uint8"
    if kind == "f" and category == "segmentation":
        raise ZarrInputError(
            f"{info.array_path} holds {info.dtype}, but a segmentation layer must be "
            "integers.\n"
            "  Binary masks saved as float (0.0/1.0) should be re-saved as uint8.\n"
            "  Probabilities should be thresholded first, or published as a color "
            "layer (--layer-type color)."
        )
    return None


def check_zarr_input(
    info: ZarrInfo,
    channel: int | None,
    voxel_size_nm: tuple[float, float, float],
    log: logging.Logger,
) -> None:
    """Refuse what cannot work; warn about what looks wrong."""
    n = info.num_channels
    if n > 1 and channel is None:
        raise ZarrInputError(
            f"{info.array_path} has {n} channels. One run publishes one layer, so "
            f"name the channel to publish: --channel 0 ... --channel {n - 1}.\n"
            "To publish several, run once per channel with a different --layer-name."
        )
    if channel is not None and not 0 <= channel < n:
        raise ZarrInputError(f"--channel {channel} does not exist; the store has {n} channel(s).")

    log.info(
        f"Zarr input: {info.array_path} — axes {','.join(info.axes)}, shape {info.shape}, "
        f"{info.dtype}" + (f", channel {channel} of {n}" if n > 1 else "")
    )
    if info.scale_nm is None:
        log.info("  The store states no usable voxel size; using --voxel-size as given.")
    elif not all(math.isclose(a, b, rel_tol=0.01) for a, b in zip(info.scale_nm, voxel_size_nm)):
        log.warning(
            f"--voxel-size is {voxel_size_nm} nm but the store's OME metadata says "
            f"{tuple(round(s, 3) for s in info.scale_nm)} nm (x, y, z). Publishing with "
            "--voxel-size. If the store is right, re-run with --voxel-size "
            + ",".join(f"{s:g}" for s in info.scale_nm)
        )
