"""Conversion output: really zarr3, sharded, compressed, and pixel-exact."""

import json

from _support import SAMPLE, SIDE, SLICES, Report, ensure_sample_dataset


def main() -> int:
    import numpy as np
    import tifffile

    r = Report()
    dataset, plan, tiffs = ensure_sample_dataset(force=True)
    print(f"  plan: {plan.describe()}")

    out = SAMPLE / "out"
    layer = dataset.get_layer("xray")
    props = json.loads((out / "datasource-properties.json").read_text())
    layer_props = props["dataLayers"][0]

    print("— output")
    r.eq("data format is zarr3", layer_props["dataFormat"], "zarr3")
    r.eq("layer name", layer_props["name"], "xray")
    r.eq("category", layer_props["category"], "color")
    r.eq("voxel size nm", props["scale"]["factor"], [650.0, 650.0, 650.0])
    r.eq("dtype", str(layer.dtype_per_channel), "uint8")
    r.eq("channels", layer.num_channels, 1)
    r.eq("bbox size", tuple(layer.bounding_box.size), (SIDE, SIDE, SLICES))

    mags = sorted(str(m) for m in layer.mags)
    r.ok(f"downsampled pyramid: {mags}", len(mags) >= 2)

    # zarr3 keeps array metadata in zarr.json; zarr2 would use .zarray
    mag1 = out / "xray" / "1"
    r.eq("zarr.json present", (mag1 / "zarr.json").exists(), True)
    r.eq("no .zarray (would mean zarr2)", (mag1 / ".zarray").exists(), False)

    meta = json.loads((mag1 / "zarr.json").read_text())
    r.eq("zarr format 3", meta["zarr_format"], 3)

    # Compression sits inside the sharding_indexed codec, not at the top level.
    codecs = json.dumps(meta["codecs"]).lower()
    r.ok("sharded", "sharding_indexed" in codecs)
    r.ok("compressed", "zstd" in codecs or "blosc" in codecs)
    r.eq("shard shape", meta["chunk_grid"]["configuration"]["chunk_shape"], [1, 4096, 4096, 32])

    # The pyramid must keep mag 1's shard shape. webknossos 4.x would otherwise
    # give it 1024^3 shards, and since a downsample job is one target shard,
    # that collapses a big stack's pyramid onto a handful of workers.
    for mag_dir in sorted(p for p in mag1.parent.iterdir() if p.is_dir() and p.name != "1"):
        mag_meta = json.loads((mag_dir / "zarr.json").read_text())
        r.eq(f"mag {mag_dir.name} keeps the shard shape",
             mag_meta["chunk_grid"]["configuration"]["chunk_shape"], [1, 4096, 4096, 32])

    print("— roundtrip")
    # WEBKNOSSOS stores (x, y, z); tifffile returns (row=y, col=x), hence the transpose.
    view = layer.get_mag("1").read()[0]
    r.ok("first slice matches", np.array_equal(view[:, :, 0], tifffile.imread(tiffs[0]).T))
    r.ok("last slice matches", np.array_equal(view[:, :, -1], tifffile.imread(tiffs[-1]).T))

    return r.finish("CONVERSION CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
