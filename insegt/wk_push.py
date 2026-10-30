"""Minimal: push an insegt3d prediction into a WEBKNOSSOS dataset, one call.

The embeddable counterpart of insegt_to_wk.py — the same result, no CLI, no dry
run, no options beyond what a button needs. The dataset id is the address.

    from wk_push import push_predictions
    url = push_predictions("predictions/xray__1a2b3c", "68f0c0ffee...", token)

Hooked to a button in insegt3d (nicegui), after `predict_volume` has written
`prediction_file`; io_bound keeps the UI responsive during the upload:

    from nicegui import run
    url = await run.io_bound(push_predictions, prediction_file,
                             dataset_id_input.value, token_input.value)

Requires `webknossos>=4.0.0`; the plain package is enough (no `[all]`). Checked
to resolve together with insegt3d 0.2.1's pins on Python 3.13.
"""

import json
from pathlib import Path

import numpy as np

WK_URL = "https://webknossos.embl-hamburg.de"
SHARD = (1024, 1024, 64)  # x, y, z: one tile of work and one file on disk


def push_predictions(prediction_zarr, dataset_id, token, *, threshold=128,
                     channels=None, wk_url=WK_URL, log=print) -> str:
    """Threshold every class (score >= threshold -> 1) and add/replace layers class_<n>."""
    import tempfile

    import httpx
    from webknossos import RemoteDataset, TransferMode, webknossos_context

    # Legacy (non-virtual) datasets refuse mag uploads. Check before any work.
    info = httpx.get(f"{wk_url}/api/datasets/{dataset_id}",
                     headers={"X-Auth-Token": token}, timeout=30).raise_for_status().json()
    if info.get("isVirtual") is False:
        raise ValueError(f"Dataset {dataset_id} is a legacy dataset and cannot take uploaded "
                         "layers; it has to be re-published first.")

    with webknossos_context(url=wk_url, token=token):
        remote = RemoteDataset.open(dataset_id=dataset_id, read_only=False)
        # The xray: '<SAMPLE>_<RUN>_xray', or plain 'xray' on older datasets.
        ref = next((l for n, l in sorted(remote.layers.items()) if n == "xray" or n.endswith("_xray")),
                   None) or next(l for l in remote.layers.values() if l.category == "color")
        with tempfile.TemporaryDirectory() as tmp:
            local, names = build_local(prediction_zarr, ref, remote.voxel_size,
                                       Path(tmp), threshold, channels, log)
            for name in names:
                if name in remote.layers:
                    remote.delete_layer(name)  # a re-run replaces, never duplicates
                log(f"Uploading {name}")
                try:
                    remote.add_layer_as_copy(local.get_layer(name), transfer_mode=TransferMode.HTTP)
                except Exception:
                    # The layer is registered before its data; don't leave it empty.
                    if name in remote.layers:
                        remote.delete_layer(name)
                    raise
    return f"{wk_url}/datasets/{dataset_id}"


def build_local(prediction_zarr, ref, voxel_size, out_dir, threshold=128, channels=None, log=print):
    """Write the thresholded, downsampled layers locally. Returns (Dataset, layer names)."""
    import cluster_tools
    import tensorstore as ts
    from webknossos import Dataset, Mag, Vec3Int

    # insegt3d output: OME-Zarr group, finest level is (c, z, y, x) uint8.
    root = Path(prediction_zarr)
    level0 = json.loads((root / "zarr.json").read_text())["attributes"]["ome"]["multiscales"][0]["datasets"][0]["path"]
    scores = ts.open({"driver": "zarr3", "kvstore": {"driver": "file", "path": str(root / level0)}}).result()
    n_classes, *zyx = scores.shape
    shape_xyz = tuple(zyx[::-1])

    # The prediction is at whichever mag the model was trained on: find it by shape.
    for mag in sorted(Mag(m) for m in ref.mags):
        mag_bbox = ref.bounding_box.align_with_mag(mag, ceil=True).in_mag(mag)
        if tuple(mag_bbox.size) == shape_xyz:
            break
    else:
        raise ValueError(f"Prediction shape {shape_xyz} (xyz) matches no mag of layer '{ref.name}'")

    channels = list(range(n_classes)) if channels is None else list(channels)
    names = [f"class_{c + 1}" for c in channels]
    dataset = Dataset(out_dir / "dataset", voxel_size=tuple(voxel_size))
    views = [
        dataset.add_layer(name, "segmentation", dtype_per_channel="uint8", largest_segment_id=1,
                          bounding_box=ref.bounding_box)
        .add_mag(mag, chunk_shape=32, shard_shape=SHARD)
        for name in names
    ]

    lo, hi = tuple(mag_bbox.topleft), tuple(mag_bbox.bottomright)
    starts = [range(lo[a] // SHARD[a] * SHARD[a], hi[a], SHARD[a]) for a in range(3)]
    log(f"Thresholding {len(channels)} class(es) at mag {mag.to_layer_name()}")
    for x in starts[0]:
        for y in starts[1]:
            for z in starts[2]:
                t0 = (max(x, lo[0]), max(y, lo[1]), max(z, lo[2]))
                t1 = (min(x + SHARD[0], hi[0]), min(y + SHARD[1], hi[1]), min(z + SHARD[2], hi[2]))
                s0 = [a - b for a, b in zip(t0, lo)]
                s1 = [a - b for a, b in zip(t1, lo)]
                for channel, view in zip(channels, views):
                    zyx_block = scores[channel, s0[2]:s1[2], s0[1]:s1[1], s0[0]:s1[0]].read().result()
                    mask = zyx_block.transpose(2, 1, 0) >= threshold
                    if mask.any():  # untouched voxels already read as 0
                        view.write(mask.astype(np.uint8), absolute_offset=Vec3Int(t0) * mag.to_vec3_int(),
                                   allow_unaligned=True)

    log("Downsampling")
    # Sequential: never spawn processes from inside a GUI app.
    with cluster_tools.get_executor("sequential") as executor:
        for layer in dataset.layers.values():
            layer.downsample(from_mag=mag, shard_shape=SHARD, executor=executor,
                             align_with_other_layers=ref.dataset)  # same mags as the xray
    return dataset, names
