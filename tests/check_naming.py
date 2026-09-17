"""Naming, sidecar and metadata mapping — the pure logic, no server needed."""

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
from wbk_publish.sidecar import (
    read_dataset_id,
    read_sidecar,
    to_display_metadata,
    write_sidecar,
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

print("— sidecar roundtrip")
write_sidecar(
    recon,
    dataset_id="abc123",
    dataset_name="20250921_p3l-eduke_MA040_1",
    layer_name="xray",
    raw_metadata=raw,
    display_metadata=d,
    log=log,
)
eq("id read back", read_dataset_id(recon, log), "abc123")
sc = read_sidecar(recon)
eq("keeps raw block", sc["dataset"]["energy_kev"], 18)
eq("keeps display block", sc["dataset_display"]["User"], "eduke")
eq("records layer", sc["layer"], "xray")
eq("no tmp left", (recon / "dataset-metadata.json.tmp").exists(), False)

(recon / "dataset-metadata.json").write_text("{ broken")
eq("corrupt sidecar tolerated", read_dataset_id(recon, log), None)
(recon / "dataset-metadata.json").unlink()
eq("missing sidecar tolerated", read_dataset_id(recon, log), None)

print()
print("FAILURES:" if fails else "ALL CHECKS PASSED")
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
