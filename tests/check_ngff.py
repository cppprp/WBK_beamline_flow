"""Rebuilding zarr3 group metadata over a server-suffixed published layout.

Reproduces what the probe found in production: the server renamed each mag
directory with a random suffix, the stale directories from the previous run are
still present, and both group-level zarr.json files are missing because the
upload transfers mags only.
"""

import json
import shutil

from _support import SAMPLE, WORK, Report, ensure_sample_dataset

SUFFIXES = {
    "1": "1__bhgirKI86DNZGmBv",
    "2": "2__FsLDmkoFJiFZjLo-",
    "4": "4__Aqf4aGUYjOSz-vCr",
}


def main() -> int:
    from wbk_publish.logs import make_logger
    from wbk_publish.ngff import (
        ROOT_GROUP,
        ZARR_JSON,
        _remap_mag_paths,
        _write_json,
        capture_layer_metadata,
    )

    log = make_logger("ngff-check", verbose=False)
    r = Report()

    ensure_sample_dataset()
    staging = SAMPLE / "out"

    print("— capture from staging")
    meta = capture_layer_metadata(staging, "xray", log)
    r.eq("captured", meta is not None, True)
    staged = [d["path"] for d in meta["attributes"]["ome"]["multiscales"][0]["datasets"]]
    r.eq("staged paths are plain mag names", staged, ["1", "2", "4"])

    print("— build a published layout the way the server does")
    published = WORK / "published_sim/probe-1234-6a95"
    shutil.rmtree(published.parent, ignore_errors=True)
    (published / "xray").mkdir(parents=True)

    for mag, suffixed in SUFFIXES.items():
        # stale directory left by the previous run, as seen in production
        (published / "xray" / mag).mkdir()
        (published / "xray" / mag / ZARR_JSON).write_text('{"stale": true}')
        # the live one the server actually reserved
        shutil.copytree(staging / "xray" / mag, published / "xray" / suffixed)

    present = sorted(p.name for p in (published / "xray").iterdir())
    r.eq("both generations present", present, sorted(list(SUFFIXES) + list(SUFFIXES.values())))
    r.eq("no root group yet", (published / ZARR_JSON).exists(), False)
    r.eq("no layer group yet", (published / "xray" / ZARR_JSON).exists(), False)

    print("— remap and write")
    remapped = _remap_mag_paths(meta, dict(SUFFIXES), log)
    _write_json(published / "xray" / ZARR_JSON, remapped)
    _write_json(published / ZARR_JSON, ROOT_GROUP)

    print("— validate the hierarchy")
    root = json.loads((published / ZARR_JSON).read_text())
    r.eq("root is a zarr3 group", (root["zarr_format"], root["node_type"]), (3, "group"))

    layer = json.loads((published / "xray" / ZARR_JSON).read_text())
    r.eq("layer is a zarr3 group", (layer["zarr_format"], layer["node_type"]), (3, "group"))

    scales = layer["attributes"]["ome"]["multiscales"][0]
    r.eq("ome version preserved", layer["attributes"]["ome"]["version"], "0.5")
    r.eq("axes preserved", [a["name"] for a in scales["axes"]], ["c", "x", "y", "z"])

    entries = scales["datasets"]
    r.eq("paths point at the reserved dirs", [d["path"] for d in entries], list(SUFFIXES.values()))

    print("— every referenced path must resolve to a real zarr3 array")
    for entry in entries:
        target = published / "xray" / entry["path"]
        if not r.ok(f"{entry['path']} exists", target.is_dir()):
            continue
        arr = json.loads((target / ZARR_JSON).read_text())
        r.ok(
            f"{entry['path']} is a zarr3 array",
            arr.get("node_type") == "array" and arr.get("zarr_format") == 3,
        )

    print("— must not point at the stale directories")
    r.eq("no stale dirs referenced", {d["path"] for d in entries} & set(SUFFIXES), set())

    print("— scale metadata survives the rewrite")
    r.eq("mag 1 scale", entries[0]["coordinateTransformations"][0]["scale"],
         [1.0, 650.0, 650.0, 650.0])
    r.eq("mag 4 scale", entries[2]["coordinateTransformations"][0]["scale"],
         [1.0, 2600.0, 2600.0, 2600.0])

    print("— the staged metadata must not be mutated in place")
    r.eq("original untouched", staged,
         [d["path"] for d in meta["attributes"]["ome"]["multiscales"][0]["datasets"]])
    r.eq("no tmp files left", list(published.rglob("*.json.tmp")), [])

    return r.finish("NGFF CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
