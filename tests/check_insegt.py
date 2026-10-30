"""Sending insegt3d predictions back: reading, placing and thresholding.

No server. The upload itself is two library calls (delete_layer and
add_layer_as_copy with TransferMode.HTTP) whose signatures check_api pins;
everything that decides *what* gets uploaded is exercised here, against a
fixture laid out exactly like insegt3d's output:

    zarr.json   OME-Zarr 0.5 group, axes c,z,y,x, no omero labels
    0/          uint8 (C, Z, Y, X) with dimension_names
    1/          a coarser level (must be ignored)
    labels/     nested group of argmax labels (must be ignored)

Both the CLI and the minimal embed version build from the same fixture, and
their layers must be identical — that is what keeps the two in step.

Everything lives inside main(): the CLI's downsampling spawns workers, which
re-import this module.
"""

import _support  # noqa: F401 - sets sys.path and env
import json
import shutil
import sys

from _support import REPO_ROOT, WORK, Report

sys.path.insert(0, str(REPO_ROOT / "insegt"))

# Deliberately not shard-aligned in any axis, so edge tiles are partial.
SHAPE_XYZ = (70, 1100, 90)
N_CLASSES = 3
THRESHOLD = 128


def write_array(path, data, dimension_names):
    import tensorstore as ts

    store = ts.open(
        {
            "driver": "zarr3",
            "kvstore": {"driver": "file", "path": str(path)},
            "metadata": {
                "shape": list(data.shape),
                "data_type": "uint8",
                "dimension_names": list(dimension_names),
                "chunk_grid": {"name": "regular",
                               "configuration": {"chunk_shape": [1, 32, 32, 32][-data.ndim:]}},
            },
        },
        create=True,
        delete_existing=True,
    ).result()
    store.write(data).result()


def write_insegt_fixture(root, scores_czyx):
    import numpy as np

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    axes = [{"name": "c", "type": "channel"}] + [{"name": a, "type": "space"} for a in "zyx"]
    datasets = [
        {"path": str(i), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0] + [1300.0 * 2**i] * 3},
            {"type": "translation", "translation": [0.0] * 4}]}
        for i in range(2)
    ]
    (root / "zarr.json").write_text(json.dumps({
        "zarr_format": 3, "node_type": "group",
        "attributes": {"ome": {"version": "0.5", "multiscales": [
            {"axes": axes, "datasets": datasets, "name": root.stem}]}},
    }))
    write_array(root / "0", scores_czyx, "czyx")
    write_array(root / "1", scores_czyx[:, ::2, ::2, ::2].copy(), "czyx")
    labels = root / "labels"
    labels.mkdir()
    (labels / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "group"}))
    write_array(labels / "0", (scores_czyx.argmax(0) + 1).astype(np.uint8), "zyx")


def main() -> int:
    import numpy as np
    import webknossos as wk

    import insegt_to_wk as cli
    import wk_push

    r = Report()
    base = WORK / "insegt"
    if base.exists():
        shutil.rmtree(base)

    # The prediction is at mag 2-2-1 of a dataset whose mag-1 extent is twice
    # as large in x and y.
    rng = np.random.default_rng(0)
    x, y, z = SHAPE_XYZ
    scores_czyx = rng.integers(0, 256, size=(N_CLASSES, z, y, x), dtype=np.uint8)
    scores_czyx[2] = 0  # an empty class: uploads as an empty layer, not an error
    prediction = base / "predictions" / "xray__abc123"
    write_insegt_fixture(prediction, scores_czyx)

    # The dataset it came from, as a local stand-in for the remote one: only
    # bounding_box, mags, name and category are read from it.
    target = wk.Dataset(base / "target", voxel_size=(650, 650, 650))
    xray = target.add_layer("xray", "color", dtype_per_channel="uint8",
                            bounding_box=wk.BoundingBox((0, 0, 0), (2 * x, 2 * y, z)))
    for mag in ("1", "2-2-1", "4-4-2"):
        xray.add_mag(mag)

    print("— reading insegt3d output")
    src = cli.open_source(prediction)
    r.eq("finest level chosen", src.path.name, "0")
    r.eq("axes from the multiscales", src.axes, ["c", "z", "y", "x"])
    r.eq("shape in x,y,z", src.shape_xyz, SHAPE_XYZ)
    r.eq("channels", src.num_channels, N_CLASSES)
    r.eq("no labels in insegt output", src.labels, [])
    r.eq("names fall back to class_<n>, 1-based",
         cli.layer_names(src, [0, 2], None, "class"), ["class_1", "class_3"])
    r.eq("--channel-names wins", cli.layer_names(src, [1], "mito", "class"), ["mito"])
    block = src.read(1, (3, 5, 7), (9, 11, 13))
    r.ok("read returns x,y,z", np.array_equal(block, scores_czyx[1, 7:13, 5:11, 3:9].transpose(2, 1, 0)))

    print("— a bare array is read by its dimension_names")
    bare = cli.open_source(prediction / "0")
    r.eq("axes", bare.axes, ["c", "z", "y", "x"])

    print("— axes that cannot work are refused")
    for axes, why in (("czy", "too few names"), ("cyyx", "y twice, no z"),
                      ("qzyx", "non-channel axis of size 3")):
        try:
            cli.open_source(prediction, axes)
            r.ok(f"refused: {why}", False, "accepted")
        except cli.InsegtError:
            r.ok(f"refused: {why}", True)

    print("— matching the prediction to a mag")
    placement = cli.match_geometry(src.shape_xyz, xray.bounding_box, xray.mags)
    r.eq("mag 2-2-1 found by shape", placement.mag, wk.Mag("2-2-1"))
    r.eq("bbox is the reference layer's", placement.bbox, xray.bounding_box)
    forced = cli.match_geometry((2 * x, 2 * y, z), xray.bounding_box, xray.mags, "1")
    r.eq("--mag 1", forced.mag, wk.Mag(1))
    shifted = wk.BoundingBox((10, 10, 0), (2 * x - 10, 2 * y - 10, z))
    streamed = cli.match_geometry(SHAPE_XYZ, shifted, xray.mags)
    r.eq("streamed extent starts at the origin", tuple(streamed.bbox.topleft), (0, 0, 0))
    try:
        cli.match_geometry((5, 5, 5), xray.bounding_box, xray.mags)
        r.ok("no match refused", False, "accepted")
    except cli.InsegtError as exc:
        r.ok("no match refused, listing every mag", all(f"mag {m.to_layer_name()} " in str(exc) for m in xray.mags))
        r.ok("and suggesting --mag", "--mag" in str(exc))

    print("— tiles cover the box exactly once, one shard each")
    mag_bbox = wk.BoundingBox((5, 1000, 60), (2000, 1100, 70))
    tiles = cli.shard_tiles(mag_bbox)
    covered = sum(np.prod([h - l for l, h in zip(lo, hi)]) for lo, hi in tiles)
    r.eq("total volume", int(covered), int(np.prod(tuple(mag_bbox.size))))
    shards = {tuple(l // s for l, s in zip(lo, cli.SHARD_SHAPE)) for lo, _ in tiles}
    r.eq("distinct shards", len(shards), len(tiles))
    r.ok("no tile crosses a shard boundary", all(
        (h - 1) // s == l // s for lo, hi in tiles for l, h, s in zip(lo, hi, cli.SHARD_SHAPE)))

    print("— building the layers (CLI)")
    channels = list(range(N_CLASSES))
    names = cli.layer_names(src, channels, None, "class")
    built = cli.build_local(src, placement, channels, names, THRESHOLD,
                            (650, 650, 650), base / "cli", jobs=2, align_with=target)
    expected = (scores_czyx >= THRESHOLD).transpose(0, 3, 2, 1).astype(np.uint8)  # c,x,y,z
    for c, name in enumerate(names):
        layer = built.get_layer(name)
        got = layer.get_mag("2-2-1").read()[0]
        r.ok(f"{name} pixel-exact", np.array_equal(got, expected[c]))
        r.eq(f"{name} is segmentation", layer.category, "segmentation")
        r.eq(f"{name} largest_segment_id", layer.as_segmentation_layer().largest_segment_id, 1)
        r.eq(f"{name} pyramid follows the xray's mags", sorted(layer.mags)[:2], [wk.Mag("2-2-1"), wk.Mag("4-4-2")])
        r.ok(f"{name} pyramid stays binary",
             set(np.unique(layer.get_mag(max(layer.mags)).read())) <= {0, 1})

    print("— building the layers (minimal embed version)")
    minimal, minimal_names = wk_push.build_local(
        prediction, xray, (650, 650, 650), base / "minimal", THRESHOLD, log=lambda *_: None)
    r.eq("same names", minimal_names, names)
    for name in names:
        a, b = built.get_layer(name), minimal.get_layer(name)
        r.eq(f"{name} same mags", sorted(a.mags), sorted(b.mags))
        r.eq(f"{name} same bbox", a.bounding_box, b.bounding_box)
        r.ok(f"{name} identical at every mag",
             all(np.array_equal(a.get_mag(m).read(), b.get_mag(m).read()) for m in a.mags))

    return r.finish("INSEGT CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
