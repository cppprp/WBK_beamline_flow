"""Batch discovery and submission.

Two halves. `discover` decides what is publishable under a directory, and its
rule has a case that is easy to get backwards: a tiled sample's stitching
replaces its tile recons, but a sample without a stitching keeps all of them.

`wbk_batch.sh` turns a list into submissions. Its parsing is exercised through
`--check`, which prints what it would run and submits nothing — so this needs
no SLURM and no server, and still covers the things that would be silently
wrong: which CLI a line is routed to, that per-line arguments come after the
common ones (and therefore win), and that comments, blanks and continuations
are handled.
"""

import subprocess
import sys

import _support  # noqa: F401
from _support import REPO_ROOT, WORK, Report


def stack(path, empty=False):
    """A directory of slices, or an empty one standing in for work in progress."""
    import numpy as np
    import tifffile

    path.mkdir(parents=True, exist_ok=True)
    if not empty:
        for i in range(2):
            tifffile.imwrite(path / f"s{i}.tif", np.zeros((8, 8), "uint16"))


def build_tree():
    """A sample directory of each shape the rule has to tell apart."""
    root = WORK / "batch/P14/2026/p3l-doejw/RECON/20260615"

    def meta(sample, pixel_um):
        import json
        (root / sample / "meta_1.json").write_text(
            json.dumps({"sample_name": sample, "pixelsize": pixel_um}))

    # Tiled: tile recons plus the stitching made from them.
    for run in ("recon_111_1", "recon_121_1"):
        stack(root / "MB012" / run / "tomo")
    stack(root / "MB012/stitching_1/stack")
    # Plain: two independent reconstructions, no stitching.
    for run in ("recon_111_1", "recon_121_1"):
        stack(root / "MB013" / run / "tomo")
    # Reconstruction has not written its slices yet.
    stack(root / "MB014/recon_111_1/tomo", empty=True)

    # Magnification differs between samples in one beamtime: 10x and 20x.
    meta("MB012", 0.65)
    meta("MB013", 0.325)
    # MB014 has no metadata at all.
    return root


def main() -> int:
    from wbk_publish.discover import build_list, describe, find_run_dirs
    from wbk_publish.logs import make_logger

    r = Report()
    log = make_logger("batch-check", verbose=False)
    root = build_tree()

    print("— a stitching replaces the tile recons it was made from")
    runs = [p.relative_to(root).as_posix() for p in find_run_dirs(root)]
    r.eq("tiled sample yields only the stitching",
         [p for p in runs if p.startswith("MB012")], ["MB012/stitching_1"])

    print("— a sample without a stitching keeps every reconstruction")
    r.eq("both recons kept", [p for p in runs if p.startswith("MB013")],
         ["MB013/recon_111_1", "MB013/recon_121_1"])

    print("— the walk prunes; it never lists the slices")
    # A run directory holds the slices and cannot contain another run
    # directory, so descending into one reads thousands of entries to learn
    # nothing. Over beegfs that is the difference between a scan taking
    # seconds and taking minutes, and it would regress silently.
    import os
    entries = sum(1 for _ in root.rglob("*"))
    reads = {"n": 0}
    real_scandir = os.scandir

    def counting(path="."):
        reads["n"] += 1
        return real_scandir(path)

    os.scandir = counting
    try:
        find_run_dirs(root)
    finally:
        os.scandir = real_scandir
    r.ok(f"reads {reads['n']} directories for a tree of {entries} entries",
         reads["n"] < 20)

    print("— each line carries the voxel size from its own metadata")
    # Magnification varies within a beamtime, and a wrong voxel size is silent:
    # the data looks right and is scaled wrong.
    from wbk_publish.discover import voxel_size_nm
    r.eq("10x sample -> 650 nm", voxel_size_nm(root / "MB012/stitching_1", log), 650)
    r.eq("20x sample -> 325 nm", voxel_size_nm(root / "MB013/recon_111_1", log), 325)
    r.eq("no metadata -> unknown", voxel_size_nm(root / "MB014/recon_111_1", log), None)

    listing = "\n".join(build_list(root, None, log))
    r.ok("the 10x line carries 650", "MB012/stitching_1  --voxel-size 650" in listing)
    r.ok("the 20x line carries 325", "MB013/recon_111_1  --voxel-size 325" in listing)

    print("— --only narrows further")
    only = [p.relative_to(root).as_posix() for p in find_run_dirs(root, "stitching_*")]
    r.eq("only stitchings", only, ["MB012/stitching_1"])

    print("— a run with no slices is reported, not silently dropped")
    ok, note = describe(root / "MB014/recon_111_1")
    r.ok("not publishable", not ok)
    r.ok("says why", "no TIFFs" in note)

    lines = build_list(root, None, log)
    body = "\n".join(lines)
    r.ok("skipped runs appear as comments", "# SKIPPED" in body)
    # The path does appear — inside the comment. What matters is that no line
    # the submitter would act on mentions it.
    live = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    r.ok("and no live line refers to it", all("MB014" not in ln for ln in live))
    r.eq("live lines are exactly the publishable runs", len(live), 3)
    r.ok("the list says how to submit it", "wbk_batch.sh" in body)

    print("— a prediction tree pairs with its datasets through the mirrored path")
    # The path is the only link between a prediction and the dataset it was
    # computed from: mirroring the account, date, sample and run is what makes
    # the same derivation name both. Nothing else connects the two trees.
    from wbk_publish.discover import build_segmentation_list, layer_dirs

    preds = WORK / "batch/preds/v20260915/p3l-doejw/20260615"
    for sample, run, layers in (
        ("MB012", "recon_111_1", ("labels",)),
        ("MB013", "recon_111_1", ("labels", "instances")),
        ("MB014", "stitching_1", ("labels",)),
    ):
        for layer in layers:
            stack(preds / sample / run / layer)
    (preds / "MB015/recon_111_1").mkdir(parents=True, exist_ok=True)

    seg = build_segmentation_list(preds, "segmentation", None, log)
    body = "\n".join(seg)
    lines = [ln for ln in seg if ln.startswith("manual:")]
    r.eq("one line per layer, not per dataset", len(lines), 4)
    r.ok("the dataset each belongs to is named",
         "# 20260615_p3l-doejw_MB012_1" in body)
    r.ok("a stitching's prediction names the stitching dataset",
         "# 20260615_p3l-doejw_MB014_stitching_1" in body)
    r.ok("every line points --input_dir at the prediction run directory",
         all("--input_dir" in ln and "/recon_" in ln or "/stitching_" in ln for ln in lines))
    r.ok("the layer is named after its directory",
         any("--layer_name instances" in ln for ln in lines))
    r.ok("and typed", all("--layer_type segmentation" in ln for ln in lines))
    r.ok("a run with nothing written is skipped with a reason",
         "no subdirectory here holds TIFFs" in body)

    print("— a stitching does not supersede a recon in a prediction tree")
    # Unlike a RECON scan: predictions for a tile recon and for the stitching
    # are layers on two different datasets, so neither replaces the other.
    both = WORK / "batch/preds2/p3l-doejw/20260615/MB020"
    for run in ("recon_111_1", "stitching_1"):
        stack(both / run / "labels")
    kept = build_segmentation_list(both.parent.parent.parent, "segmentation", None, log)
    r.eq("both kept", len([ln for ln in kept if ln.startswith("manual:")]), 2)

    print("— an unmirrored tree says so instead of returning nothing")
    flat = WORK / "batch/flat/burtii767M_01"
    stack(flat / "labels")
    empty = "\n".join(build_segmentation_list(WORK / "batch/flat", "segmentation", None, log))
    r.ok("explains what the path must contain", "p3l-account/YYYYMMDD" in empty)
    r.ok("and produces no submittable line", "manual:" not in empty)

    print("— wbk_batch.sh --check routes and orders arguments")
    list_file = WORK / "batch/list.txt"
    list_file.write_text(
        "# a comment\n"
        "\n"
        "/data/MB012/stitching_1\n"
        "/data/MB014/recon_111_1  --voxel-size 325\n"
        "manual: --input_dir /data/MB012/stitching_1 \\\n"
        "        --layer_name predictions --layer_type segmentation\n"
        "manual: --zarr /data/exports/MA040.zarr --sample_name 20260115_p3l-test_MA040_1\n"
    )
    out = subprocess.run(
        [str(REPO_ROOT / "wbk_batch.sh"), str(list_file), "--voxel-size", "650", "--check"],
        capture_output=True, text=True,
    )
    r.eq("exits cleanly", out.returncode, 0)
    submissions = [ln.strip() for ln in out.stdout.splitlines() if "wbk_publish_job.sh" in ln]
    r.eq("one submission per real line", len(submissions), 4)
    r.ok("comments and blanks skipped", all("# a comment" not in s for s in submissions))
    r.ok("plain lines use the beamline CLI",
         submissions[0].startswith("WBK_MODULE=wbk_publish.beamline"))
    r.ok("a manual: line uses the manual CLI",
         submissions[2].startswith("WBK_MODULE=wbk_publish.manual"))
    r.ok("the manual: prefix is not passed on", "manual:" not in submissions[2].split("job.sh")[1])
    r.ok("continuation joined into one submission",
         "--layer_name predictions" in submissions[2] and "--input_dir" in submissions[2])
    r.ok("a --zarr line goes to the manual CLI as written",
         submissions[3].startswith("WBK_MODULE=wbk_publish.manual")
         and "--zarr /data/exports/MA040.zarr" in submissions[3])
    r.ok("common arguments applied to every line",
         all("--voxel-size 650" in s for s in submissions))
    # argparse takes the last occurrence, so a per-line value must come after.
    tail = submissions[1].split("--voxel-size")
    r.ok("per-line arguments come last, so they win", tail[-1].strip().startswith("325"))
    r.ok("nothing was submitted", "--check: nothing was submitted" in out.stdout)

    print("— a batch dry run becomes one job, not one per dataset")
    # Forty exclusive allocations to do forty seconds of checking would hold the
    # node against everyone else. The list is expanded here, in the one place
    # that parses it, and handed to a single job.
    import os
    import stat

    stub = WORK / "batch/bin"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "sbatch").write_text("#!/bin/sh\necho 'Submitted batch job 7777'\n")
    (stub / "sbatch").chmod(stat.S_IRWXU)

    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}",
               UV="/usr/bin/true", WBK_LOGS=str(WORK / "batch/logs"))
    out = subprocess.run(
        [str(REPO_ROOT / "wbk_batch.sh"), str(list_file), "--voxel-size", "650", "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    r.eq("exits cleanly", out.returncode, 0)
    r.ok("says it is submitting one job", "one job to dry-run all 4" in out.stdout)
    r.ok("a --zarr line is labelled by its store",
         "will check    /data/exports/MA040.zarr" in out.stdout)
    r.eq("and only one sbatch happened", out.stdout.count("Submitted batch job"), 1)

    plan = list_file.with_suffix(".txt.plan")
    r.ok("an expanded plan was written", plan.is_file())
    rows = [ln.split("\t") for ln in plan.read_text().splitlines() if ln.strip()]
    r.eq("one row per submission", len(rows), 4)
    r.ok("each names its module", all(row[0].startswith("wbk_publish.") for row in rows))
    r.ok("each carries --dry-run", all("--dry-run" in row for row in rows))
    r.ok("the manual row is routed to the manual CLI",
         rows[2][0] == "wbk_publish.manual")
    r.ok("arguments are separate fields, so paths with spaces survive",
         all(" " not in field for row in rows for field in row))

    print("— --sequential: one job publishes the lines in order")
    # Several channels of one dataset must not run at once: the per-dataset
    # lock would refuse all but one. This puts the whole list in one job.
    out = subprocess.run(
        [str(REPO_ROOT / "wbk_batch.sh"), str(list_file), "--voxel-size", "650", "--sequential"],
        capture_output=True, text=True, env=env,
    )
    r.eq("exits cleanly", out.returncode, 0)
    r.ok("says it is submitting one job", "one job to publish all 4" in out.stdout)
    r.eq("and only one sbatch happened", out.stdout.count("Submitted batch job"), 1)
    r.ok("it runs the batch runner", "submitting: wbk_publish.batch_run" in out.stdout)
    r.ok("warns that the time limit covers all lines", "--time=" in out.stdout)
    r.ok("lines are labelled as publishes", "will publish  /data/exports/MA040.zarr" in out.stdout)
    rows = [ln.split("\t") for ln in plan.read_text().splitlines() if ln.strip()]
    r.eq("one row per line, in list order", [row[-1] for row in rows][:1], ["/data/MB012/stitching_1"])
    r.eq("four rows", len(rows), 4)
    r.ok("no row is a dry run", all("--dry-run" not in row for row in rows))
    r.ok("--sequential itself is not passed to the lines",
         all("--sequential" not in row for row in rows))
    out = subprocess.run(
        [str(REPO_ROOT / "wbk_batch.sh"), str(list_file), "--sequential", "--check"],
        capture_output=True, text=True,
    )
    r.ok("--check says they would run as one job", "would run as ONE job" in out.stdout)

    print("— the batch runner: in order, each in its own process, failures do not stop it")
    from wbk_publish import batch_run
    order = WORK / "batch/order.txt"
    order.unlink(missing_ok=True)
    seq_plan = WORK / "batch/seq.plan"
    # Real interpreters, stand-in modules: `-m timeit` runs its setup once (with
    # -r 1) and records that it ran; `-m json.tool` on a missing file fails.
    seq_plan.write_text(
        "timeit\t-n\t1\t-r\t1\t-s\timport pathlib\tpathlib.Path('" + str(order) + "').open('a').write('first\\n')\n"
        "json.tool\t/nonexistent/input.json\n"
        "timeit\t-n\t1\t-r\t1\t-s\timport pathlib\tpathlib.Path('" + str(order) + "').open('a').write('third\\n')\n"
    )
    code = batch_run.main(["--plan", str(seq_plan)])
    r.eq("reports the failure", code, 1)
    lines = order.read_text().split() if order.exists() else []
    r.eq("ran before and after the failing line, in order", lines, ["first", "third"])

    print("— the manual CLI accepts both flag spellings, so common args work for it")
    import argparse

    import wbk_publish.manual as mn
    seen = {}
    orig = argparse.ArgumentParser.parse_args

    def spy(self, args=None, namespace=None, _orig=orig):
        seen.update(vars(_orig(self, args, namespace)))
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = spy
    try:
        mn.main(["--input-dir", "/x", "--voxel-size", "650", "--token", "t"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig
    r.eq("hyphenated --input-dir", str(seen.get("input_dir")), "/x")
    r.eq("hyphenated --voxel-size", seen.get("voxel_size"), (650.0, 650.0, 650.0))

    return r.finish("BATCH CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())
