# Publishing data to WEBKNOSSOS — user guide

This tool converts a TIFF stack into a zarr3 dataset and publishes it to
WEBKNOSSOS at <https://webknossos.embl-hamburg.de>.

**The one thing to understand:** re-running on the same input **replaces the
layer on the dataset you published before and keeps the dataset id**. Links you
have shared, and annotations people have made, stay valid. You do not get a
second copy. Everything else in this guide follows from that.

---

## 1. Before your first run

**Get an API token.** In WEBKNOSSOS, click your name (top right) →
*Auth Token* → *Copy*. Then either:

```bash
export WK_TOKEN=<your token>
```

or put it in `.config/auth_token.txt` inside the checkout (git-ignored). A
`--token` flag overrides both. The token is yours — datasets are created under
your account, so use your own rather than sharing one.

**Everything goes through sbatch.** Submit from the login node:

```bash
cd /mx-beta/ximg-beta/wbk-publish
```

Currently only **bcrunch-n08** has `/morningstar` mounted, so pin to it with
`WBK_NODE=bcrunch-n08` until that changes.

> ### Do not run conversions in an interactive session
>
> Not `srun --pty bash`, not directly on the login node. Every command in this
> guide is an `sbatch` submission, including dry runs, and there is no situation
> in normal use that needs anything else.
>
> This is not bureaucracy. A conversion needs a whole node, and the pipeline
> decides how many worker processes fit by **reading the SLURM allocation** —
> outside a job it has no allocation to read and will size itself wrongly. An
> interactive session also holds the node exclusively for as long as your shell
> is open, which is how the queue ends up blocked by a terminal somebody forgot
> about. And the login node has neither the input filesystems nor morningstar
> mounted, so nothing would run there anyway.
>
> Submit it, then `tail -f` the log. If you need an interactive session to look
> at files, keep it separate from publishing and close it when you are done —
> a job cannot start while your session holds the node.

---

## 2. Quick start

**A normal reconstruction or stitching** — this is the command you will use 95%
of the time:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-eduke/RECON/20260828/cmoluccenis_1_scout/recon_111_1 \
    --voxel-size 650
```

It submits itself as an sbatch job and prints where the log will be. Follow it:

```bash
tail -f logs/wbk_<jobid>.log
```

**Check before committing an allocation** — this converts nothing:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh <input_dir> --voxel-size 650 --dry-run
```

It is submitted like any other job, but with a 15-minute limit instead of eight
hours, so it cannot sit on a node. Do this for any path or size you have not
published before. It reports the
dataset name it derived, whether it found an existing dataset, the folder and
teams, whether the metadata file is there, and how the machine will be used.

---

## 3. Which one do you need?

**Beamline CLI** — your data sits in the standard beamline tree
(`.../p3l-account/RECON/YYYYMMDD/<sample>/<run>/`) and you want it published as
a normal tomogram. This is what the automatic trigger runs. → [§4](#4-the-beamline-cli)

**Manual CLI** — anything else: archive data, a different layer name or a
segmentation, a project folder, an unusual voxel size, or a path that doesn't
follow the convention. → [§5](#5-the-manual-cli)

Both run the same pipeline and both keep the dataset id on re-runs. The beamline
one simply refuses to let you change things the beamline should never change.

---

## 4. The beamline CLI

For data in the standard tree. This is the default, so you don't name a module.

### What it decides for you

| | how |
|---|---|
| dataset name | derived from the path, then validated |
| displayed name | the short readable form |
| folder | `Datasets/<account>/<date>` |
| teams | your p3l account team, plus `ximg` |
| layer | always `<readable name>_xray`, always colour |

None of these can be overridden here — that's the point. A trigger firing with a
wrong argument must not be able to publish a mislabelled dataset. If you need to
change any of them, use the manual CLI.

### How the path becomes a name

```
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1
                      └ account ┘        └date┘ └sam┘ └─ run ─┘

  dataset name   20260615_p3l-doejw_MB012_1        (the directory name)
  shown in UI    MB012_1
  layer          MB012_1_xray
  folder         Datasets/p3l-doejw/20260615
```

**The layer is named after the dataset**, with `_xray` after it. A layer that
carries the sample name says what it is on its own — in a dataset that has
several layers, in a link someone sends you, in a figure panel — where a layer
called plainly `xray` only says which modality it was.

A `recon_XYZ_N` run contributes only its trailing number. A `stitching_N` is kept
whole:

```
.../20260615/MB012/stitching_1     →  20260615_p3l-doejw_MB012_stitching_1
                                      layer: MB012_stitching_1_xray
.../20261102/tooth_A7/recon_121_2  →  20261102_p3l-doejw_tooth_A7_2
                                      layer: tooth_A7_2_xray
```

### The command

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh <input_dir> --voxel-size <325|650>
```

`<input_dir>` is the **run** directory — the one containing `tomo/` or `stack/`,
not the `tomo/` directory itself.

### Options

| flag | when you need it |
|---|---|
| `--voxel-size 325` or `650` | **always** — set by the magnification used |
| `--dry-run` | before any path or size you haven't published before |
| `--metadata-file PATH` | the metadata isn't at `<sample>/meta_<run>.json` |
| `--dataset-id ID` | the name lookup found more than one candidate |
| `--replace-legacy` | the match was published by the old system |
| `--cpus N` | almost never — see [§9](#9-how-long-will-it-take-and-how-much-of-the-node) |
| `--max-memory 400G` | almost never |
| `--token TOKEN` | your token isn't in `WK_TOKEN` or `.config/auth_token.txt` |

### Examples

**Publish a reconstruction.** The everyday case:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 650
```

Publishes `MB012_1` into `Datasets/p3l-doejw/20260615`.

**Publish a stitching.** Identical — the tool works out which it is from the
directory name:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1 \
    --voxel-size 650
```

**Check first without converting anything.** Prints the name it derived, whether
a dataset already exists, the folder, the teams, whether the metadata file is
there, and how the node will be used:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 650 --dry-run
```

**Re-publish after a better reconstruction.** Exactly the same command as the
first time. The log will say `Dataset exists (id=…) — replacing layer 'MB012_1_xray'`,
and the id will not change, so shared links and existing annotations survive:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 650
```

> **Datasets published before October 2026** have a layer called plainly `xray`,
> from before layers were named after their dataset. Re-publishing one adds
> `MB012_1_xray` beside it rather than replacing it, and the run says so. The old
> layer is left alone — it is published data, and nothing here deletes that on
> the strength of a name. Delete it in WEBKNOSSOS once the new layer looks right,
> or the dataset will show the same tomogram twice and take up twice the space.

**At 20x magnification** — the voxel size is the one thing you must get right,
because nothing else knows the magnification you used:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 325
```

**The metadata lives somewhere else** — or this stitching has no `meta_1.json`
of its own:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1 \
    --voxel-size 650 \
    --metadata-file /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/meta_1.json
```

That file is read **and** written — the publication record goes into it.

**The run stopped because two datasets share a name.** Open the URLs it printed,
find the one whose directory name carries this run's date, and name it:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 650 \
    --dataset-id 6a9069a12358dcbef5d8bcab
```

Only needed once — the id is recorded in the metadata file afterwards.

**The dataset was published by the old system.** It cannot be updated in place,
so this publishes a new one beside it. The old one is left alone; delete it in
the WEBKNOSSOS UI when you're happy:

```bash
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel-size 650 --replace-legacy
```

**A very large stitch.** The default limit is 8 hours, which covers everything
run so far; give a multi-terabyte one headroom rather than losing it near the end:

```bash
SBATCH_ARGS="--time=24:00:00" WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1 \
    --voxel-size 650
```

---

## 5. The manual CLI

For everything the beamline path can't express. Same pipeline, same id-preserving
behaviour — but you state what the beamline CLI would have derived.

Set `WBK_MODULE=wbk_publish.manual` and note the **underscores**: this CLI uses
`--voxel_size`, `--input_dir`, `--metadata_file`, where the beamline one uses
hyphens.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir <dir> [options]
```

Only `--input_dir` is required. Everything else falls back to the same defaults
the beamline CLI uses.

### Options

| flag | default | what it does |
|---|---|---|
| `--input_dir DIR` | **required** | the run directory; its name keys the record in the metadata file |
| `--stack_path DIR` | `<input_dir>/tomo` or `/stack` | where the TIFFs actually are |
| `--sample_name NAME` | derived from the path | the dataset name, `YYYYMMDD_p3l-account_SAMPLE_RUN` |
| `--voxel_size` | `650,650,650` | `650`, or `x,y,z` in nm |
| `--project NAME` | filed by date | subfolder under the account |
| `--layer_name NAME` | `<readable name>_xray` | which layer to write |
| `--layer_type` | `color` | `color` or `segmentation` |
| `--sampling_mode` | `anisotropic` | `anisotropic`, `isotropic`, `constant_z` |
| `--metadata_file PATH` | `<sample>/meta_<run>.json` | read metadata from, and record the publication in |
| `--jobs N` | automatic | upper bound on workers |
| `--max-memory 400G` | from SLURM | memory this run may use |
| `--dataset-id ID` | looked up | overwrite exactly this dataset |
| `--replace-legacy` | off | publish fresh if the match is an old-system dataset |
| `--new-dataset` | off | confirm that a non-default layer should start a *new* dataset rather than join an existing one |
| `--dry-run` | off | report and stop |
| `--token TOKEN` | `WK_TOKEN` | API token |

### Examples

Each of these is complete. Copy it, change the marked lines, submit.

---

**1. A stitching, filed under a project instead of by date**

Use when a study spans several beamtimes and you want the datasets together.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/ximg/2026/p3l-schmidt/RECON/20260830/LN_2x2x4/stitching_1 \
    --voxel_size 650 \
    --project lymph_node_atlas
```

*Change:* `--input_dir` to your directory, `--project` to your folder name.
*Result:* published as `LN_2x2x4_stitching_1` into
`Datasets/p3l-schmidt/lymph_node_atlas` instead of `.../20260830`.

---

**2. Archive data that predates the naming convention**

Use when the path has no `p3l-account` or no `YYYYMMDD`, so nothing can be
derived. You supply the name yourself, in the convention's format.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/bigjbod6/archive/2019/batch_5 \
    --stack_path /mnt/bigjbod6/archive/2019/batch_5/tiffs \
    --sample_name 20190501_p3l-doejw_batch5_1 \
    --voxel_size 650
```

*Change:* both paths, and `--sample_name` — it must look like
`YYYYMMDD_p3l-account_SAMPLE_RUN` or it will be rejected.
*Result:* published as `batch5_1` into `Datasets/p3l-doejw/20190501`.
*Note:* `--stack_path` is needed because the TIFFs are in `tiffs/`, not
`tomo/` or `stack/`.

---

**3. The TIFFs are not in `tomo/` or `stack/`**

Use when the reconstruction wrote them somewhere else. Everything else is still
derived from the path.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --stack_path /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1/slices_16bit \
    --voxel_size 650
```

*Change:* `--stack_path` to the directory that actually holds the `.tif` files.

---

**4. Adding a segmentation alongside an existing tomogram**

Use for model predictions or manual labels. This adds a second layer to the
**same dataset** — it does not touch the tomogram layer.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --stack_path /mnt/beegfs7/P14/2026/p3l-doejw/SEGMENTATION/20260615/MB012/labels \
    --voxel_size 650 \
    --layer_name predictions \
    --layer_type segmentation
```

*Change:* `--stack_path` to your label TIFFs, `--layer_name` to whatever you want
it called in the viewer.
*Result:* the log says `Dataset has no 'predictions' layer yet — adding it.
Existing layers: ['MB012_1_xray']`, and the dataset gains the layer.
*Note:* segmentation stacks must be integer-typed, not float.

**Adding `--dataset-id` is the safer habit.** If the lookup does not find the
dataset, publishing a non-default layer would create a *new* dataset holding
only the segmentation, with no tomogram — which is why the run refuses rather than
doing it:

```
No existing dataset was found, so publishing layer 'predictions' now would
create a NEW dataset containing only that layer — no 'MB012_1_xray' beside it.
Nothing has been changed.
To add this layer to a dataset that already exists, give its id:
    --dataset-id <id>
```

Passing `--dataset-id` up front removes any dependence on the lookup. Copy the
id from the dataset's URL in WEBKNOSSOS.

**Why `--input_dir` still points at the recon.** It is where the publication
record is written — so both layers end up described in the same `meta_1.json`,
under the `recon_111_1` key.

**If the recon directory is gone**, name the dataset by id instead. You still
have to give `--input_dir` — the record has to live somewhere — but it can now
point at the segmentation, and then you must also supply `--sample_name`,
because a directory like `labels/` is not a run directory and no name can be
derived from it:

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/SEGMENTATION/20260615/MB012/labels \
    --sample_name 20260615_p3l-doejw_MB012_1 \
    --dataset-id 6aad4234b6d36ce98fb28bd6 \
    --voxel_size 650 \
    --layer_name predictions \
    --layer_type segmentation
```

*Trade-off:* the record is then filed under `labels` in a metadata file beside
the segmentation, so the dataset's provenance is split across two files. Prefer
the first form when the recon directory still exists.

**5. A second image modality as its own layer**

Same idea, but a colour layer — e.g. a fluorescence or absorption channel
reconstructed separately.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --stack_path /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_121_1/tomo \
    --voxel_size 650 \
    --layer_name phase_contrast
```

*Result:* the dataset `MB012_1` now has both `MB012_1_xray` and `phase_contrast`.

---

**6. Non-cubic voxels**

Use when the z spacing differs from the pixel size — common for sparse z
sampling.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel_size 650,650,1300
```

*Change:* the three numbers are x, y, z in nanometres.
*Note:* with strongly anisotropic voxels, `--sampling_mode anisotropic` (the
default) is right — it downsamples x and y before z until the voxels are roughly
cubic.

---

**7. Overwriting one specific dataset**

Use when the automatic lookup found more than one candidate, or when you want to
be certain which dataset is replaced.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel_size 650 \
    --dataset-id 6aad4234b6d36ce98fb28bd6
```

*Change:* the id — copy it from the dataset's URL in WEBKNOSSOS.
*Result:* the `MB012_1_xray` layer on that dataset is replaced; the id and all links stay.

---

**8. Replacing a dataset from the old system**

Use when a run stopped with *"was published by the old system and cannot be
updated in place"*.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1 \
    --voxel_size 650 \
    --replace-legacy
```

*Result:* a **new** dataset with a new id, beside the old one. The old one is not
touched — delete it yourself in WEBKNOSSOS once the new one looks right. Every
later re-run updates the new one in place.

---

**9. Check anything before running it**

Add `--dry-run` to any of the above. It converts nothing and holds the node for
seconds, not hours:

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --input_dir /mnt/bigjbod6/archive/2019/batch_5 \
    --stack_path /mnt/bigjbod6/archive/2019/batch_5/tiffs \
    --sample_name 20190501_p3l-doejw_batch5_1 \
    --voxel_size 650 \
    --dry-run
```

---

## 6. Publishing many at once

`wbk_batch.sh` submits one job per dataset, so a failure affects only its own
dataset and each gets its own log. Run it from the login node; it does no work
itself.

### Step 1 — find what is there

```bash
./wbk_batch.sh --scan /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615
```

This submits a short job (it needs the mounts, which the login node lacks) that
writes `20260615.batch` beside the checkout and **submits nothing else**. It is
quick — it reads one directory per sample and never looks inside the slice
folders. For each
sample it takes the `stitching_N` if there is one — the tile recons are
intermediate — and otherwise every `recon_XYZ_N`. Add `--only 'stitching_*'` or
`--only 'recon_111_*'` to narrow it, `--out FILE` to put the list elsewhere.

**Each line carries its own voxel size**, read from that run's `meta_<run>.json`.
Magnification varies between samples in one beamtime, and a wrong voxel size is
silent — the data looks right and is scaled wrong — so the scan takes it from the
metadata rather than expecting you to remember. The scan log says when a beamtime
mixes them.

Anything it cannot publish is written as a comment with the reason rather than
left out:

```
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1  --voxel-size 650
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB013/recon_111_1  --voxel-size 325
# no voxel size in the metadata for MB014/recon_111_1; the value passed to wbk_batch.sh will be used
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB014/recon_111_1
# SKIPPED  /mnt/beegfs7/.../MB015/recon_111_1
#          no TIFFs in /mnt/beegfs7/.../MB015/recon_111_1/tomo
```

A run whose metadata has no usable `pixelsize` gets no flag, so the value you
pass to `wbk_batch.sh` applies — and a comment says which ones those are.

### Step 2 — read the list, delete what you do not want

It is a plain text file. Publishing is hard to undo, which is why the scan does
not submit: forty unwanted datasets are forty things to delete.

### Step 3 — submit it

```bash
./wbk_batch.sh 20260615.batch --voxel-size 650 --check
```

`--check` prints the commands and submits nothing. The `--voxel-size` here is a
fallback: lines that carry their own from the metadata keep it, because per-line
arguments come last.

To check the whole beamtime against the server — names, existing datasets,
metadata files, parallelism — add `--dry-run` instead:

```bash
./wbk_batch.sh 20260615.batch --voxel-size 650 --dry-run
```

That submits **one** job which dry-runs every line in turn, not one per dataset:
the checks take seconds each and it is the exclusive node that is expensive. The
log reports each in sequence and ends with a summary of anything that would
fail. Nothing is converted. When they look right, drop it:

```bash
./wbk_batch.sh 20260615.batch --voxel-size 650
```

Each line becomes a job. The job ids, log paths and targets are written to
`20260615.batch.submitted` so you can find the results afterwards. Watch progress
with `squeue -u $USER`.

To re-run only the failures, delete the lines that worked and submit again.

### The list format

Each non-comment line is **the arguments for one submission** — exactly what you
would type after `wbk_publish_job.sh`. Arguments given to `wbk_batch.sh` are
applied to every line, and per-line arguments come after them, so they win.

```
# Tomograms — the beamline CLI, which is the default
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB013/recon_111_1
/mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB014/recon_111_1  --voxel-size 325

# Segmentations — prefix the line with manual: to use the manual CLI
manual: --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1 \
        --stack_path /mnt/bigjbod6/2026/p3l-doejw/SEG/MB012/labels \
        --layer_name predictions --layer_type segmentation
```

Blank lines and `#` comments are ignored; a trailing `\` continues a line. Both
flag spellings work everywhere, so one set of common arguments serves both kinds
of line.

### A batch of segmentations

#### The convention predictions must follow

A prediction has to say which dataset it was computed from, and the only thing
that can carry that is its path. So a prediction tree **mirrors the tomogram's
four naming components**:

```
<anything you like>/p3l-account/YYYYMMDD/<sample>/<run>/<layer>/*.tif
                    └──────── exactly as they appear under RECON ────────┘
```

For example:

```
/mnt/ximg/2026/asalomon/postprocessed/v20260915/p3l-doejw/20260615/MB012/recon_111_1/labels/*.tif
└──────────── free: yours to organise ────────────┘└──── must mirror RECON ────┘
```

Everything above the account is yours — a model version, a person, a project.
Below it, the account, the date, the sample and the run directory must match the
tomogram exactly, because that is what names the dataset.

**The stack directory becomes the layer name.** `labels/` publishes as a layer
called `labels`; adding `instances/` later gives a second layer with no change
to the convention.

Get one of the four components wrong and the prediction is attributed to a
different dataset without complaint — a mistyped run number names another real
reconstruction. That is the cost of pairing by path, and the reason the rule is
worth enforcing in whatever writes the predictions.

#### Scanning a prediction tree

```bash
./wbk_batch.sh --scan /mnt/ximg/2026/asalomon/postprocessed/v20260915 --segmentations
```

produces one line per layer, with the dataset it belongs to named above it:

```
# 20260615_p3l-doejw_MB012_1
manual: --input_dir .../MB012/recon_111_1 --stack_path .../MB012/recon_111_1/labels \
        --layer_name labels --layer_type segmentation
# SKIPPED  .../MB015/recon_111_1
#          no subdirectory here holds TIFFs
```

Unlike a tomogram scan, a stitching does **not** supersede the tile recons:
predictions for a tile recon and for the stitching are layers on two different
datasets, so both are kept.

```bash
./wbk_batch.sh predictions.txt
```

Each line adds its layer to the existing dataset without touching the tomogram, and
the dataset's `meta_<run>.json` lists every layer with its own publication time.
If a tomogram has not been published yet, that line fails on its own with a
message saying so — the rest continue.

#### If the tree does not follow the convention

The scan says so rather than returning an empty file, and names what the path
must contain. Predictions that cannot be attributed to a dataset by their path
cannot be attributed by any other means either — nothing else links the two
trees — so the fix is to move or rename them, not to work around it.

### Checking afterwards

```bash
cat 20260615.batch.submitted          # job id, log path, target
squeue -u $USER                       # what is still queued or running
grep -l "Pipeline failed" logs/*.log  # which ones went wrong
```

---

## 7. Cluster settings

| variable | default | purpose |
|---|---|---|
| `WBK_NODE` | SLURM chooses | pin to a node — `bcrunch-n08` for now |
| `WBK_MODULE` | `wbk_publish.beamline` | `wbk_publish.manual` for the manual CLI |
| `WBK_LOGS` | `<checkout>/logs` | where job logs go |
| `WBK_HOME` | the script's directory | the checkout to run from |
| `SBATCH_ARGS` | — | extra sbatch flags, e.g. `--time=24:00:00` |
| `WBK_LOCAL` | unset | run outside SLURM — debugging only; prints a warning |
| `UV` | `/mx-beta/ximg-beta/uv/uv` | the uv binary |

Jobs are `--exclusive`: one dataset per node. Anything else in your environment
(`WK_TOKEN`, and the settings in [§8](#8-settings)) is carried into the job.

## 8. Settings

| variable | default | purpose |
|---|---|---|
| `WK_TOKEN` | — | API token (or `.config/auth_token.txt`) |
| `WK_URL` | `https://webknossos.embl-hamburg.de` | server |
| `WK_TEMP_BASE` | `/morningstar/tmp` | conversion staging — must be on the WEBKNOSSOS storage |
| `WK_XIMG_TEAM` | `ximg` | facility-wide team added to every dataset |
| `WK_MAX_CONVERSION_WORKERS` | `16` | worker ceiling; raise only if the storage gets faster |
| `WK_TIMEOUT` | 1800 | client timeout in seconds; raise for very large layers |

---

## 9. How long will it take, and how much of the node?

Worker count is chosen for you from the stack, the machine and the storage. You
rarely need `--cpus`; it is an upper bound, never a target.

Measured on bcrunch-n08, writing to morningstar:

| dataset | conversion | pyramid | total | memory |
|---|---|---|---|---|
| 1994² × 2021 slices (32 GB) | 1:11 | 1:00 | **2:15** | 82 GiB |
| 3584² × 2038 slices (105 GB) | 3:04 | 2:06 | **5:17** | 135 GiB |
| 4774² × 21338 slices (0.97 TB) | 27:51 | 35:33 | **1:04** | 160 GiB |
| 10507 × 10512 × 10339 (2.3 TB) | ~65 min | ~85 min | **~2.5 h** | ~316 GiB |

The last row is an estimate; the rest are measured.

**The pyramid overtakes the conversion on large datasets.** On a 105 GB stack it
is two-thirds of the conversion time; at 1 TB it is longer than the conversion.
The reason is that downsampling reads mag 1 back, and on a small dataset most of
it is still in the node's page cache while on a large one none of it is — a cold
read from morningstar runs at about 376 MB/s, much like writing. Budget roughly
**1.3 × the conversion time** for the pyramid on anything above a few hundred GB.

Conversion is limited by how fast morningstar accepts data (~420 MB/s), not by
the node — which is why the worker count is capped at 16 and why more workers
make it *slower*, not faster.

**Publish stitches as uint16 where the dynamic range allows.** Per-worker memory
scales with the full stitched xy plane, so float32 stitches lose workers fast; a
5×5 tile grid takes 27 minutes in uint16 and 1 h 45 in float32.

**Check disk before a big one.** Staging needs the full converted size, and on a
re-publish the old copy survives until the new one is ready — so the transient
peak is about double.

---

## 10. Reading the log

```
Plan: 16 workers x 11 threads (667 tasks, 8.2 GiB/worker, limited by requested)
```
How the work was split, and what bound it: `cores`, `memory`, `tasks`, `waves`
or `requested` (your `--cpus`, or the default ceiling).

```
Memory so far (conversion): largest worker 9.8 GiB, total 135.1 GiB ... actual is 1.06x that
```
Measured against the plan. A final ratio near 1.0 means the sizing was right.
Well above it triggers a warning — report it, the model is calibrated from these.

```
CPU (conversion): 7% of 176 cores busy on average
```
Low is expected: conversion waits on storage, not on the CPU.

```
Layer replaced — dataset id unchanged: 6aad4234b6d36ce98fb28bd6
```
The point of the whole exercise.

---

## 11. When something goes wrong

Nothing is published until the conversion has finished successfully, so a failed
run never leaves a dataset without its layer. Re-running is always safe.

| message | what to do |
|---|---|
| `Dataset name '…' does not match the expected convention` | The path isn't beamline-shaped. Use the manual CLI with `--sample_name`. |
| `Cannot find p3l-account in path` / `Cannot find YYYYMMDD` | Same — the path lacks the structure the name is derived from. |
| `Image stack directory not found` | Check the path, and that its filesystem is mounted on the node. The message names the nearest mount. |
| `No TIFF files found in …` | The reconstruction hasn't written its slices, or the filesystem is mounted but empty. |
| `Cannot read the first slice: … (N bytes)` | The first TIFF is truncated or not a TIFF. Fix it and re-run. |
| `Conversion failed: … slice N of M` | A slice is damaged. The message names the file; compare its size with its neighbours. |
| `Staging directory does not exist: /morningstar/tmp` | morningstar is not mounted on this node. Use `WBK_NODE=bcrunch-n08`. |
| `Found N datasets sharing a display name` | §5 — pick one and pass `--dataset-id`. |
| `was published by the old system` | §5 — add `--replace-legacy`. |
| `would create a NEW dataset containing only that layer` | You are adding a layer but no dataset was found. Pass `--dataset-id`, or `--new-dataset` if a standalone dataset really is intended. |
| `Could not determine whether this dataset already exists` | The server was unreachable. It refuses rather than risk a duplicate. Check connectivity and re-run. |
| `Tight memory fit: … planned against a … limit` | Advisory. Follow the `--max-memory` suggestion for a first run at that size. |
| `Layer deletion timed out` | The server is still deleting; the data is not corrupt. Wait, then re-run with the `--dataset-id` it prints. |
| Job stuck in `PD … (Resources)` | The node is taken — most often by your own forgotten interactive session. `squeue -w bcrunch-n08` shows who; close the session and the job starts. |
| `warning: WBK_LOCAL is set` | You are running outside SLURM. Don't — see §1. |
| `FAILED TO SUBMIT (line N)` | That one line of a batch was rejected by sbatch; the others were still submitted. The error follows it. |

If a run dies mid-conversion, a directory is left under `/morningstar/tmp`;
it is safe to delete.

---

## 12. Other tools

| script | purpose |
|---|---|
| `bench_convert.sh` | time the plain `webknossos convert` CLI against a target and library version — for diagnosing storage slowness |
| `bench_phases.py` | split one worker's decode / convert / write apart |
| `wbk_batch.sh --scan` | list the publishable runs under a folder (§6) |
| `wbk_batch.sh … --dry-run` | check a whole list against the server in one job (§6) |
| `scan_orphans.py` | find orphaned dataset directories and stale mags on the storage |
| `probe_overwrite.py` | verify against the live server that layer replacement frees the old data |
| `tests/run_all.py` | the test suite; run it after any change |
| `insegt/insegt_to_wk.py` | send insegt3d predictions back as segmentation layers — runs on your own computer, see below |

### Sending insegt3d predictions back to WEBKNOSSOS

insegt3d writes a probability per class (0–255). `insegt/insegt_to_wk.py` thresholds
each class into a mask and adds it to the dataset as a segmentation layer
`class_1`, `class_2`, … — the same numbers insegt3d's own `labels` use.

**This one runs on your own computer, not the cluster.** It talks to WEBKNOSSOS over
HTTPS only. You need [uv](https://docs.astral.sh/uv/), your own token, and the
dataset id (the last part of the dataset's URL):

```bash
export WK_TOKEN=<your token>
```

Check first. This reads the prediction, finds which magnification it was made at and
lists what would be added or replaced, but uploads nothing:

```bash
uv run insegt/insegt_to_wk.py <output>/predictions/xray__1a2b3c --dataset-id <id> --dry-run
```

Then run it for real, usually skipping the background class. `--channels` is 0-based:
channel 0 becomes `class_1`.

```bash
uv run insegt/insegt_to_wk.py <output>/predictions/xray__1a2b3c --dataset-id <id> --channels 1,2
```

| flag | when you need it |
|---|---|
| `--threshold N` | default 128, i.e. probability ≥ 0.5 |
| `--channels 1,2` | upload only these classes |
| `--channel-names mito,nuclei` | your own layer names instead of `class_N` |
| `--mag 4-4-2` | the prediction is at a mag the dataset does not have |
| `--work-dir DIR` | where to build the layers before upload (default: system temp) |
| `--jobs N` | parallel tiles and downsampling workers |

Running it again **replaces** layers with the same name; nothing is duplicated and the
dataset id stays the same. The layers are built and downsampled on your computer
first and uploaded afterwards, so a failure while building changes nothing on the
server. Only datasets published by this pipeline work (they are "virtual"). An
old-system dataset is refused before any work is done, because the server rejects
uploads to it. To use one, re-publish it first with `--replace-legacy` (§5); that
gives it a new id, so run this against the new one.

`insegt/wk_push.py` is the same thing as one function, `push_predictions(prediction,
dataset_id, token)`, for wiring into insegt3d as a button.

---

## 13. Where things are

| | |
|---|---|
| checkout | `/mx-beta/ximg-beta/wbk-publish` |
| logs | `<checkout>/logs/wbk_<jobid>.log` |
| staging | `/morningstar/tmp` |
| published data | `/morningstar/webknossos/.published/EMBL_Hamburg/.virtual/` |
| dataset id + provenance | `meta_<run>.json` beside your data |

For the design of the pipeline — how parallelism is chosen, why the storage
behaves as it does, what the metadata file contains — see the
[README](../README.md).
