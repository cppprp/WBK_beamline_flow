"""Naming, the metadata file and its display mapping — pure logic, no server needed."""

import os
from pathlib import Path

from _support import WORK as SC  # noqa: E402 - sets sys.path and env

from wbk_publish.logs import make_logger
from wbk_publish.manual import parse_voxel_size
from wbk_publish.naming import (
    dataset_name_from_path,
    parse_dataset_name,
    resolve_tiff_stack,
    validate_dataset_name,
)
from wbk_publish.metadata import (
    find_mx_cube_metadata,
    load_mx_cube_metadata,
    read_dataset_id,
    record_publication,
    report_staleness,
    to_display_metadata,
)

log = make_logger("check", verbose=False)
fails = []


def eq(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


recon = SC / "data/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1"
stitch = SC / "data/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1"
recon.mkdir(parents=True, exist_ok=True)
stitch.mkdir(parents=True, exist_ok=True)

print("— naming")
eq("recon name", dataset_name_from_path(recon), "20250921_p3l-eduke_MA040_1")
eq("stitching name", dataset_name_from_path(stitch), "20251130_p3l-eduke_tree_2x2x10_stitching_1")
eq("trailing slash", dataset_name_from_path(Path(str(recon) + "/")), "20250921_p3l-eduke_MA040_1")

p = parse_dataset_name("20250921_p3l-eduke_MA040_1")
eq("readable", p.readable_name, "MA040_1")
eq("run", p.run_number, "1")
eq("user", p.user, "p3l-eduke")
eq("date", p.imaging_date, "20250921")

p2 = parse_dataset_name("20251130_p3l-eduke_tree_2x2x10_stitching_1")
eq("stitch readable", p2.readable_name, "tree_2x2x10_stitching_1")
eq("stitch run", p2.run_number, "1")

eq("recon stack", resolve_tiff_stack(recon).name, "tomo")
eq("stitch stack", resolve_tiff_stack(stitch).name, "stack")

for bad, why in [
    (Path("/no/account/20250921/s/recon_1_1"), "no p3l"),
    (Path("/x/p3l-a/RECON/notadate/s/recon_1_1"), "no date"),
    (Path("/x/p3l-a/RECON/20250921/s/whatever"), "no run dir"),
]:
    try:
        dataset_name_from_path(bad)
        fails.append(f"rejects {why}")
        print(f"  FAIL rejects {why}: accepted")
    except ValueError:
        print(f"  ok   rejects {why}")

try:
    validate_dataset_name("nonsense")
    fails.append("validate accepted nonsense")
    print("  FAIL validate accepted nonsense")
except ValueError:
    print("  ok   validate rejects nonsense")

print("— voxel size")
eq("single", parse_voxel_size("650"), (650.0, 650.0, 650.0))
eq("triple", parse_voxel_size("325,325,650"), (325.0, 325.0, 650.0))
try:
    parse_voxel_size("650,650")
    fails.append("voxel accepted 2 parts")
    print("  FAIL accepted 2 components")
except Exception:
    print("  ok   rejects 2 components")

print("— metadata mapping")
raw = {"user": "eduke", "energy_kev": 18, "distances_mm": [1.5, 2.5], "unmapped_field": "keep me"}
d = to_display_metadata(raw, log)
eq("mapped label", d["User"], "eduke")
eq("distances stringified", d["Sample to Detector Distances (mm)"], ["1.5", "2.5"])
eq("unmapped passthrough", d["unmapped_field"], "keep me")
eq("order: mapped first", list(d)[-1], "unmapped_field")
eq("raw not mutated", raw["distances_mm"], [1.5, 2.5])
eq("empty in, empty out", to_display_metadata({}, log), {})

print("— publication record in meta_<run>.json")
import json

meta = find_mx_cube_metadata(recon, "1")
eq("meta file sits in the sample directory", meta, recon.parent / "meta_1.json")
mx_cube = {
    "betadelta": 0.05, "distances_mm": [73, 77], "sample_run": 1,
    "operator": " Anzhelika ASVETLOVE", "energy_kev": 18.000267327900946,
}
meta.write_text(json.dumps(mx_cube, indent=2))
for stale in (recon / "dataset-metadata.json", recon.parent / "dataset-metadata.json"):
    stale.unlink(missing_ok=True)

eq("nothing recorded yet", read_dataset_id(meta, recon, log), None)
record_publication(meta, recon, dataset_id="abc123", dataset_name="20250921_p3l-eduke_MA040_1",
                   layer_name="xray", log=log)
data = json.loads(meta.read_text())
eq("id read back", read_dataset_id(meta, recon, log), "abc123")
eq("keyed by the input directory name", list(data["webknossos"]), ["recon_111_1"])
eq("records the layer", data["webknossos"]["recon_111_1"]["layer"], "xray")
eq("MX-Cube keys untouched", {k: v for k, v in data.items() if k != "webknossos"}, mx_cube)
eq("MX-Cube key order kept", list(data)[:-1], list(mx_cube))
eq("non-ascii stays readable", "\\u" in meta.read_text(), False)
eq("no tmp left", (meta.parent / "meta_1.json.tmp").exists(), False)

loaded = load_mx_cube_metadata(meta, log)
eq("load drops our block", "webknossos" in loaded, False)
eq("display never carries our block", "webknossos" in to_display_metadata(data, log), False)

# A tiled scan: recon and stitching share the file, each with its own entry.
tile_stitch = recon.parent / "stitching_1"
tile_stitch.mkdir(exist_ok=True)
record_publication(meta, tile_stitch, dataset_id="stitch9", dataset_name="x_stitching_1",
                   layer_name="xray", log=log)
data = json.loads(meta.read_text())
eq("second dataset keyed alongside", sorted(data["webknossos"]), ["recon_111_1", "stitching_1"])
eq("first entry survives", read_dataset_id(meta, recon, log), "abc123")
eq("second entry read by its own dir", read_dataset_id(meta, tile_stitch, log), "stitch9")

# Re-publish: same id, entry replaced not duplicated.
record_publication(meta, recon, dataset_id="abc123", dataset_name="20250921_p3l-eduke_MA040_1",
                   layer_name="xray", log=log)
eq("re-publish keeps one entry", len(json.loads(meta.read_text())["webknossos"]), 2)

# The recon tool edited the file after the publish: our block survives and we notice.
data = json.loads(meta.read_text())
data["betadelta"] = 0.02
data["last_updated"] = "2099-01-01T00:00:00Z"
meta.write_text(json.dumps(data, indent=2))
eq("id survives an edit", read_dataset_id(meta, recon, log), "abc123")
import io, logging
buf = io.StringIO(); h = logging.StreamHandler(buf); log.addHandler(h)
report_staleness(meta, recon, log)
log.removeHandler(h)
eq("staleness reported", "after the last publish" in buf.getvalue(), True)

# A different instance's record is ignored.
data["webknossos"]["recon_111_1"]["webknossos_url"] = "https://elsewhere.example"
meta.write_text(json.dumps(data))
eq("other instance ignored", read_dataset_id(meta, recon, log), None)

# Legacy sidecar in the input directory is still honoured, never written.
meta.write_text(json.dumps(mx_cube))
(recon / "dataset-metadata.json").write_text(json.dumps({"dataset_id": "old777", "webknossos_url": "https://webknossos.embl-hamburg.de"}))
eq("legacy sidecar read", read_dataset_id(meta, recon, log), "old777")
record_publication(meta, recon, dataset_id="old777", dataset_name="n", layer_name="xray", log=log)
eq("meta file now preferred", "old777" in meta.read_text(), True)
(recon / "dataset-metadata.json").unlink()

# Corrupt meta file: tolerated on read, never overwritten on write.
meta.write_text("{ broken")
eq("corrupt meta tolerated on read", read_dataset_id(meta, recon, log), None)
eq("corrupt meta not overwritten", record_publication(meta, recon, dataset_id="x", dataset_name="n", layer_name="xray", log=log), None)
eq("corrupt content intact", meta.read_text(), "{ broken")

# No meta file at all (archive publish): created with only our block.
meta.unlink()
eq("missing meta tolerated", read_dataset_id(meta, recon, log), None)
record_publication(meta, recon, dataset_id="new1", dataset_name="n", layer_name="xray", log=log)
eq("created with only our block", list(json.loads(meta.read_text())), ["webknossos"])
meta.unlink()

print()
print("FAILURES:" if fails else "ALL CHECKS PASSED")
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
