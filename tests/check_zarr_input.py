"""A zarr store as the input instead of a TIFF stack.

No server. Covers what is new for zarr input: reading the geometry and voxel
size from the store, refusing a multi-channel store without --channel, the
manual CLI's --zarr wiring up to a full dry run, and a conversion that is
pixel-exact — including one channel picked out of several, and a store whose
axes are stored z,y,x.

Everything lives inside main(): the conversion spawns workers, which re-import
this module.
"""

import _support  # noqa: F401 - sets sys.path and env
import json
import logging
import shutil

from _support import WORK, Report

X, Y, Z = 48, 40, 70  # deliberately unequal, so a transposed read cannot pass


def write_array(path, data, dimension_names=None, v2=False):
    import tensorstore as ts

    if v2:
        spec = {"driver": "zarr", "kvstore": {"driver": "file", "path": str(path)},
                "metadata": {"shape": list(data.shape), "dtype": data.dtype.str,
                             "chunks": [32] * data.ndim}}
    else:
        metadata = {"shape": list(data.shape), "data_type": str(data.dtype),
                    "chunk_grid": {"name": "regular",
                                   "configuration": {"chunk_shape": [32] * data.ndim}}}
        if dimension_names:
            metadata["dimension_names"] = list(dimension_names)
        spec = {"driver": "zarr3", "kvstore": {"driver": "file", "path": str(path)},
                "metadata": metadata}
    store = ts.open(spec, create=True, delete_existing=True).result()
    store.write(data).result()


def write_ome(root, data, axes, unit="micrometer", scale_xyz=(0.65, 0.65, 0.65)):
    """OME-Zarr 0.5 group with one level, axes in the given order."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    by_axis = dict(zip("xyz", scale_xyz))
    axis_meta = [{"name": a, "type": "channel"} if a == "c" else
                 {"name": a, "type": "space", **({"unit": unit} if unit else {})} for a in axes]
    scale = [1.0 if a == "c" else by_axis[a] for a in axes]
    (root / "zarr.json").write_text(json.dumps({
        "zarr_format": 3, "node_type": "group",
        "attributes": {"ome": {"version": "0.5", "multiscales": [{
            "axes": axis_meta,
            "datasets": [{"path": "0", "coordinateTransformations": [
                {"type": "scale", "scale": scale}]}]}]}},
    }))
    write_array(root / "0", data, axes)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def main() -> int:
    import numpy as np

    import importlib

    from wbk_publish import manual

    # The package re-exports the publish *function* under the module's name, so
    # the module itself has to be fetched explicitly to stub its lookups.
    pub = importlib.import_module("wbk_publish.publish")
    from wbk_publish.convert import build_layer
    from wbk_publish.logs import make_logger
    from wbk_publish.resources import plan_for_shape
    from wbk_publish.zarr_input import ZarrInputError, check_zarr_input, is_zarr, probe_zarr

    r = Report()
    log = make_logger("zarr-check", verbose=False)
    captured = Capture()
    log.addHandler(captured)
    base = WORK / "zarr_input"
    if base.exists():
        shutil.rmtree(base)

    rng = np.random.default_rng(1)
    volume_xyz = rng.integers(0, 60000, size=(X, Y, Z), dtype=np.uint16)
    zyx = volume_xyz.transpose(2, 1, 0).copy()
    single = base / "exports" / "vol.zarr"
    write_ome(single, zyx, "zyx")
    channels_xyz = rng.integers(0, 255, size=(3, X, Y, Z), dtype=np.uint8)
    multi = base / "exports" / "multi.zarr"
    write_ome(multi, channels_xyz.transpose(0, 3, 2, 1).copy(), "czyx")
    bare = base / "exports" / "bare.zarr"
    write_array(bare, zyx, "zyx")
    v2 = base / "exports" / "v2.zarr"
    write_array(v2, volume_xyz, v2=True)

    print("— reading the store's geometry")
    info = probe_zarr(single)
    r.ok("detected as zarr", is_zarr(single))
    r.ok("a TIFF directory is not", not is_zarr(base / "exports"))
    r.eq("finest level opened", info.array_path.name, "0")
    r.eq("axes", info.axes, ["z", "y", "x"])
    r.eq("dtype", info.dtype, "uint16")
    r.eq("voxel size from OME, in nm", info.scale_nm, (650.0, 650.0, 650.0))
    shape = info.stack_shape()
    r.eq("plan geometry", (shape.n_slices, shape.xy_pixels, shape.bytes_per_pixel), (Z, X * Y, 2))
    r.eq("bare v3 array: axes from dimension_names", probe_zarr(bare).axes, ["z", "y", "x"])
    r.eq("zarr v2 array: webknossos' x,y,z default", probe_zarr(v2).axes, ["x", "y", "z"])
    r.eq("multi-channel count", probe_zarr(multi).num_channels, 3)
    write_ome(base / "nounit.zarr", zyx, "zyx", unit=None)
    r.eq("no unit -> no voxel size claimed", probe_zarr(base / "nounit.zarr").scale_nm, None)

    print("— refused before any work")
    try:
        probe_zarr(base / "exports")
        r.ok("non-zarr refused", False, "accepted")
    except ZarrInputError:
        r.ok("non-zarr refused", True)
    for channel, why in ((None, "several channels, none chosen"), (3, "channel out of range")):
        try:
            check_zarr_input(probe_zarr(multi), channel, (650, 650, 650), log)
            r.ok(f"refused: {why}", False, "accepted")
        except ZarrInputError as exc:
            r.ok(f"refused: {why}", "--channel" in str(exc))
    check_zarr_input(probe_zarr(multi), 1, (650, 650, 650), log)
    r.ok("channel 1 of 3 accepted", True)

    print("— voxel size disagreeing with the store is a warning")
    captured.lines.clear()
    check_zarr_input(info, None, (325, 325, 325), log)
    warned = [l for l in captured.lines if "OME metadata says" in l]
    r.ok("warned", bool(warned))
    r.ok("and suggests the store's value", any("--voxel_size 650,650,650" in l for l in warned))
    captured.lines.clear()
    check_zarr_input(info, None, (650, 650, 650), log)
    r.ok("no warning when they agree", not any("OME metadata says" in l for l in captured.lines))

    print("— manual CLI: --zarr up to a full dry run")
    pub.find_dataset_id_by_directory = lambda name, log: None
    pub.find_dataset_id_by_name = lambda names, log: None
    pub.is_virtual = lambda dataset_id, token, log: True
    args = ["--zarr", str(single), "--sample_name", "20260115_p3l-test_ZARR_1",
            "--voxel_size", "650", "--token", "dummy", "--dry-run"]
    parser_args = manual_args(manual, args)
    request = manual.build_request(parser_args)
    r.eq("input is the store", request.tiff_stack, single)
    r.eq("record keyed by the store", request.input_dir, single)
    r.eq("metadata file beside the store", request.metadata_file, single.parent / "meta_1.json")
    r.eq("default layer name", request.layer_name, "ZARR_1_xray")
    r.eq("dry run exits 0", manual.main(args), 0)
    r.eq("multi-channel without --channel fails",
         manual.main(["--zarr", str(multi), "--sample_name", "20260115_p3l-test_MULTI_1",
                      "--token", "dummy", "--dry-run"]), 1)
    r.eq("...and with --channel succeeds",
         manual.main(["--zarr", str(multi), "--channel", "2", "--sample_name",
                      "20260115_p3l-test_MULTI_1", "--token", "dummy", "--dry-run"]), 0)
    for bad, why in ((["--zarr", str(single), "--stack_path", str(single)], "--zarr with --stack_path"),
                     ([], "neither --input_dir nor --zarr")):
        try:
            manual.build_request(manual_args(manual, bad + ["--sample_name",
                                 "20260115_p3l-test_ZARR_1", "--token", "dummy"]))
            r.ok(f"refused: {why}", False, "accepted")
        except ValueError:
            r.ok(f"refused: {why}", True)

    print("— conversion is pixel-exact")
    plan = plan_for_shape(info.stack_shape(), log, max_workers=2)
    out = build_layer(single, base / "out_single", dataset_name="zarr_single",
                      voxel_size_nm=(650, 650, 650), layer_name="vol", category="color",
                      plan=plan, sampling_mode="anisotropic", log=log)
    layer = out.get_layer("vol")
    r.eq("bbox", tuple(layer.bounding_box.size), (X, Y, Z))
    r.ok("z,y,x store lands as x,y,z", np.array_equal(layer.get_mag(1).read()[0], volume_xyz))
    r.ok("pyramid built", len(layer.mags) > 1, str(sorted(layer.mags)))

    multi_info = probe_zarr(multi)
    out = build_layer(multi, base / "out_multi", dataset_name="zarr_multi",
                      voxel_size_nm=(650, 650, 650), layer_name="c2", category="color",
                      plan=plan_for_shape(multi_info.stack_shape(), log, max_workers=2),
                      sampling_mode="anisotropic", log=log, channel=2)
    got = out.get_layer("c2")
    r.eq("one channel in the layer", got.num_channels, 1)
    r.ok("it is channel 2", np.array_equal(got.get_mag(1).read()[0], channels_xyz[2]))

    print("— binary masks as segmentation layers")
    from wbk_publish.zarr_input import conversion_dtype

    mask_zyx = rng.random((Z, Y, X)) > 0.7
    bool_store = base / "exports" / "mask_bool.zarr"
    write_ome(bool_store, mask_zyx, "zyx")
    float_store = base / "exports" / "mask_float.zarr"
    write_ome(float_store, mask_zyx.astype(np.float32), "zyx")
    bool_info = probe_zarr(bool_store)
    r.eq("bool is converted to uint8 while reading",
         conversion_dtype(bool_info, "segmentation", log), "uint8")
    r.eq("uint8 is left alone", conversion_dtype(info, "segmentation", log), None)
    r.eq("float is left alone as color", conversion_dtype(probe_zarr(float_store), "color", log), None)
    try:
        conversion_dtype(probe_zarr(float_store), "segmentation", log)
        r.ok("float segmentation refused up front", False, "accepted")
    except ZarrInputError as exc:
        r.ok("float segmentation refused up front", "uint8" in str(exc))
    r.eq("...also through the CLI, before any conversion",
         manual.main(["--zarr", str(float_store), "--sample_name", "20260115_p3l-test_FLOAT_1",
                      "--layer_type", "segmentation", "--layer_name", "FLOAT_1_mask",
                      "--new-dataset", "--token", "dummy", "--dry-run"]), 1)
    out = build_layer(bool_store, base / "out_bool", dataset_name="zarr_bool",
                      voxel_size_nm=(650, 650, 650), layer_name="mask", category="segmentation",
                      plan=plan_for_shape(bool_info.stack_shape(), log, max_workers=2),
                      sampling_mode="anisotropic", log=log, dtype="uint8")
    seg = out.get_layer("mask").as_segmentation_layer()
    r.eq("bool mask stored as uint8", str(seg.dtype_per_channel), "uint8")
    r.ok("bool mask is exact 0/1", np.array_equal(seg.get_mag(1).read()[0],
                                                  mask_zyx.transpose(2, 1, 0).astype(np.uint8)))
    r.eq("segment id 1", seg.largest_segment_id, 1)
    r.ok("pyramid stays binary",
         set(np.unique(seg.get_mag(max(seg.mags)).read()).tolist()) <= {0, 1})

    return r.finish("ZARR INPUT CHECKS PASSED")


def manual_args(manual, argv):
    """Parse argv exactly as manual.main does, without running the pipeline."""
    import argparse

    captured = {}
    original = manual.build_request

    def grab(args: argparse.Namespace):
        captured["args"] = args
        raise SystemExit(0)

    manual.build_request = grab
    try:
        try:
            manual.main(argv)
        except SystemExit:
            pass
    finally:
        manual.build_request = original
    return captured["args"]


if __name__ == "__main__":
    raise SystemExit(main())
