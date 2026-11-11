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
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh <run_dir> --voxel-size 650 --dry-run
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
| account | the `p3l-` directory in the path |

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

**The layer is named after the dataset**, then what it holds: `_xray` for a
tomogram, `_segmentation` for labels. A layer that carries the sample name says
what it is on its own — in a dataset that has several layers, in a link someone
sends you, in a figure panel — where a layer called plainly `xray` only says
which modality it was. The type has to be in there too, or a segmentation
published without an explicit name inherits the tomogram's word.

**The account may be a bare username.** `p3l-` marks a proposal account and is
matched wherever it sits in the path. A path without one — staff data under
`/mnt/ximg/2025/jdoe/...` — takes the nearest directory before the date that is
not a structural name like `RECON`, and says so in the log:

```
Account taken by position (no p3l- component in the path): jdoe
```

Read that line when you see it. An account name becomes the team name, and the
team decides who can open the dataset.

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
WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh <run_dir> --voxel-size <325|650>
```

`<run_dir>` is the **run** directory — the one containing `tomo/` or `stack/`,
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

Set `WBK_MODULE=wbk_publish.manual`. Every flag is hyphenated, here and on the
beamline CLI, so one set of common arguments serves a batch of either kind.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack <dir> --dataset-name <name> [options]
```

**Two arguments carry it.** `--stack` says where the TIFFs are — the directory
holding the `.tif` files themselves, not a run directory above them. And
`--dataset-name` says which dataset they belong to. There is no second path to
give and nothing is derived from the path.

The name is required because this CLI exists for the trees where nothing
reliable *can* be derived — archive layouts, postprocessing output, stitchings
whose directory carries no run number. It is also what finds an existing
dataset: an exact lookup on the dataset's directory name, which is unique by
construction. That is why adding a layer needs neither a recorded id nor
`--dataset-id`.

### Options

| flag | default | what it does |
|---|---|---|
| `--stack DIR` | **required** (or `--zarr`) | the directory holding the TIFF slices |
| `--dataset-name NAME` | **required** | `YYYYMMDD_account_SAMPLE_RUN`; also finds an existing dataset |
| `--zarr PATH` | — | publish a zarr store instead of TIFFs (example 10) |
| `--channel N` | — | which channel of a multi-channel zarr to publish, 0-based |
| `--voxel-size` | `650,650,650` | `650`, or `x,y,z` in nm |
| `--project NAME` | none — straight under the account | subfolder under the account |
| `--layer-name NAME` | `<readable name>_<type>` | which layer to write |
| `--layer-type` | `color` | `color` or `segmentation` |
| `--sampling-mode` | `anisotropic` | `anisotropic`, `isotropic`, `constant_z` |
| `--metadata-file PATH` | `meta_<run>.json` beside the stack | read metadata from, and record the publication in |
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
    --stack /mnt/ximg/2026/p3l-schmidt/RECON/20260830/LN_2x2x4/stitching_1/stack \
    --dataset-name 20260830_p3l-schmidt_LN_2x2x4_stitching_1 \
    --voxel-size 650 \
    --project lymph_node_atlas
```

*Change:* `--stack` to your slice directory, `--dataset-name` to match it,
`--project` to your folder name.
*Result:* published as `LN_2x2x4_stitching_1` into
`Datasets/p3l-schmidt/lymph_node_atlas` instead of `.../20260830`.

---

**2. Archive data that predates the naming convention**

Use when the path has no account directory or no `YYYYMMDD`, so nothing can be
derived. You supply the name yourself, in the convention's format.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/bigjbod6/archive/2019/batch_5/tiffs \
    --dataset-name 20190501_p3l-doejw_batch5_1 \
    --voxel-size 650
```

*Change:* the path, and `--dataset-name` — it must look like
`YYYYMMDD_account_SAMPLE_RUN` or it will be rejected. The date is the one part
you cannot leave out; invent one if the real one is lost, and keep it consistent.
*Result:* published as `batch5_1` into `Datasets/p3l-doejw`, with layer
`batch5_1_xray`. Unlike the beamline CLI, the manual one does **not** file by
date — pass `--project` if you want a subfolder.
*Note:* the account may be a bare username. `20190501_jdoe_batch5_1` is accepted
and files under `Datasets/jdoe`, which also becomes the team name.

---

**3. The TIFFs are not in `tomo/` or `stack/`**

Use when the reconstruction wrote them somewhere else. The beamline CLI looks
only in `tomo/` and `stack/`; here you point straight at them.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1/slices_16bit \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650
```

*Change:* `--stack` to the directory that actually holds the `.tif` files.
*Note:* the name is the one the beamline CLI would have derived — `recon_111_1`
contributes only its trailing `1`. §4 has the rule.

---

**4. Adding a segmentation alongside an existing tomogram**

Use for model predictions or manual labels. This adds a second layer to the
**same dataset** — it does not touch the tomogram layer.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/SEGMENTATION/20260615/MB012/labels \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650 \
    --layer-type segmentation
```

*Change:* `--stack` to your label TIFFs, and `--dataset-name` to the dataset
they belong to — the same name its tomogram was published under.
*Result:* the log says `Dataset has no 'MB012_1_segmentation' layer yet — adding
it. Existing layers: ['MB012_1_xray']`, and the dataset gains the layer.
*Note:* segmentation stacks must be integer-typed, not float.

**The name is what pairs them.** `--dataset-name` is looked up against the
dataset's directory name — an exact match, unique by construction — so the layer
lands on the right dataset without a recorded id, without `--dataset-id`, and
without the label stack having to sit anywhere in particular. That is the whole
mechanism: get the name right and the pairing follows.

**The layer name follows from the type.** Omitting `--layer-name` gives
`<readable name>_segmentation`, so `MB012_1_segmentation` above. Pass it
explicitly when a dataset has more than one segmentation and the names have to
tell them apart:

```bash
    --layer-name MB012_1_mitochondria \
    --layer-type segmentation
```

**When the name is wrong, the run refuses rather than guessing.** A layer name
other than the dataset's own default, with no dataset found, would create a
*new* dataset holding only the segmentation and no tomogram:

```
No existing dataset was found, so publishing layer 'MB012_1_mitochondria' now
would create a NEW dataset containing only that layer — no 'MB012_1_xray'
beside it.
Nothing has been changed.
To add this layer to a dataset that already exists, give its id:
    --dataset-id <id>
```

Almost always that means a typo in `--dataset-name`. Check it against the
dataset's directory name in WEBKNOSSOS before reaching for `--dataset-id`.

**Where the record goes.** `meta_<run>.json` beside the stack, so a label stack
under `SEGMENTATION/` records itself there rather than in the recon's file. Pass
`--metadata-file` to put it with the tomogram's record instead, and both layers
end up described in one file:

```bash
    --metadata-file /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/meta_1.json
```

If that write fails — a directory that is not yours, most often — the publish
still stands and the log carries the dataset id. Nothing about the dataset is
wrong; you have only lost the note.

**5. A second image modality as its own layer**

Same idea, but a colour layer — e.g. a fluorescence or absorption channel
reconstructed separately.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_121_1/tomo \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650 \
    --layer-name phase_contrast
```

*Result:* the dataset `MB012_1` now has both `MB012_1_xray` and the new layer.

---

**6. Non-cubic voxels**

Use when the z spacing differs from the pixel size — common for sparse z
sampling.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1/tomo \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650,650,1300
```

*Change:* the three numbers are x, y, z in nanometres.
*Note:* with strongly anisotropic voxels, `--sampling-mode anisotropic` (the
default) is right — it downsamples x and y before z until the voxels are roughly
cubic.

---

**7. Overwriting one specific dataset**

Use when the automatic lookup found more than one candidate, or when you want to
be certain which dataset is replaced.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1/tomo \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650 \
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
    --stack /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/recon_111_1/tomo \
    --dataset-name 20260615_p3l-doejw_MB012_1 \
    --voxel-size 650 \
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
    --stack /mnt/bigjbod6/archive/2019/batch_5/tiffs \
    --dataset-name 20190501_p3l-doejw_batch5_1 \
    --dataset-name 20190501_p3l-doejw_batch5_1 \
    --voxel-size 650 \
    --dry-run
```

**10. A zarr store instead of a TIFF stack**

A zarr anywhere on the ximg mount (a plain array or OME-Zarr, zarr v2 or v3)
publishes like a TIFF stack: same conversion, same pyramid, and re-running
replaces the layer and keeps the dataset id.

```bash
WBK_MODULE=wbk_publish.manual WBK_NODE=bcrunch-n08 ./wbk_publish_job.sh \
    --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040.zarr \
    --dataset-name 20260115_p3l-eduke_MA040_1 \
    --voxel-size 650 \
    --dry-run
```

*Change:* the path, `--dataset-name` (a zarr path has no beamline layout to derive
the name from), and `--voxel-size`.
*Result:* published as `MA040_1` with layer `MA040_1_xray` into `Datasets/p3l-eduke`.
The dataset id is recorded in `meta_1.json` **beside the store** (created if
missing), which is how the next run finds the same dataset.
*Note:*
- **One channel per run.** A store with several channels is refused until you pick
  one with `--channel N`. To publish more channels, run once per channel with
  `--layer-name` and `--dataset-id`, as in example 4.
- **Check the voxel size.** If the store's OME metadata states one (with units),
  the run compares it with `--voxel-size` and warns if they differ. It still uses
  `--voxel-size`, because OME units are often wrong. The default is 650 nm, so
  leaving the flag out is the easy mistake; the dry run shows the warning.
- Axes are taken from the store's own metadata (OME axes, or zarr v3
  `dimension_names`). An array with neither is read as x, y, z.
- **Binary masks** go in as `--layer-type segmentation`. A uint8 mask is stored as
  is, and its foreground value is the segment id: 0/1 gives id 1, 0/255 gives 255.
  A bool mask is converted to uint8 0/1 while reading. A float store is refused as a
  segmentation before anything is converted: re-save a float mask as uint8, and
  threshold probabilities first.

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
manual: --stack /mnt/bigjbod6/2026/p3l-doejw/SEG/MB012/labels \
        --dataset-name 20260615_p3l-doejw_MB012_stitching_1 \
        --layer-name MB012_stitching_1_labels --layer-type segmentation
```

Blank lines and `#` comments are ignored; a trailing `\` continues a line. Every
flag is hyphenated on both CLIs, so one set of common arguments serves both kinds
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

**The stack directory becomes part of the layer name.** `labels/` on dataset
`MB012_1` publishes as the layer `MB012_1_labels`; adding `instances/` later
gives `MB012_1_instances` with no change to the convention. The dataset's name
comes first so a layer still says which sample it is when seen on its own, and
the directory name keeps two segmentations of one dataset apart.

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
manual: --stack .../MB012/recon_111_1 --stack .../MB012/recon_111_1/labels \
        --layer-name labels --layer-type segmentation
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

### A batch of zarr stores

There is no scan for zarr stores: they can sit anywhere, so nothing in their path
names the dataset. Write the list by hand, one `manual:` line per store, each with
its own `--dataset-name`:

```
# zarr stores — one line each; the name cannot be derived from the path
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040.zarr --dataset-name 20260115_p3l-eduke_MA040_1
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/MA041.zarr --dataset-name 20260115_p3l-eduke_MA041_1 \
        --voxel-size 325
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/two_ch.zarr --channel 1 --dataset-name 20260115_p3l-eduke_TWO_1
```

Then the same three steps as any batch:

```bash
./wbk_batch.sh zarrs.batch --voxel-size 650 --check     # print the commands
./wbk_batch.sh zarrs.batch --voxel-size 650 --dry-run   # one job checks every line
./wbk_batch.sh zarrs.batch --voxel-size 650             # one job per store
```

The batch dry run is worth it here more than anywhere. It reports per store a
multi-channel store without `--channel`, and a `--voxel-size` that disagrees
with the store's own metadata, before anything is converted. Lines can mix
zarr stores and TIFF runs. Each store's dataset id is recorded in `meta_<run>.json`
beside it, so re-submitting the same list replaces layers rather than duplicating.

### One after another on one node: `--sequential`

Normally every line is its own job and they run in parallel. Some lines must not:
**several layers of one dataset**, such as three zarr volumes that are channels of
the same sample. Each run holds a lock on its dataset, so in parallel only one would
get through and the others would stop with "Another run is already publishing".

`--sequential` submits **one** job instead, on one exclusive node, that publishes
the lines in list order, each starting when the one before it has finished:

```
# three channels of one dataset, in the order they must run
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040_ch0.zarr --dataset-name 20260115_p3l-eduke_MA040_1 \
        --layer-name MA040_1_ch0 --new-dataset
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040_ch1.zarr --dataset-name 20260115_p3l-eduke_MA040_1 \
        --layer-name MA040_1_ch1
manual: --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040_ch2.zarr --dataset-name 20260115_p3l-eduke_MA040_1 \
        --layer-name MA040_1_ch2
```

```bash
./wbk_batch.sh channels.batch --voxel-size 650 --check --sequential   # the order, nothing submitted
./wbk_batch.sh channels.batch --voxel-size 650 --sequential           # one job, three publishes
```

- **Same `--dataset-name` on every line** puts the layers in one dataset.
- **A different `--layer-name` per line.** Otherwise each line would replace the one
  before it.
- **`--new-dataset` on the first line only.** It creates the dataset; the later lines
  find it by name. If the first line is the tomogram itself, leave out both its
  `--layer-name` and `--new-dataset`.
- **Order matters.** The line that creates the dataset comes first.

A failed line does not stop the rest. A line that depended on it then fails at
once, before converting anything, because its dataset does not exist. The end of
the log lists every line with ok/FAILED and how long it took.

**The time limit covers the whole batch**, not each line. The default is 8 hours.
For long conversions raise it:

```bash
SBATCH_ARGS="--time=24:00:00" ./wbk_batch.sh channels.batch --voxel-size 650 --sequential
```

Check first with `--dry-run` as usual (without `--sequential`). A dry run already runs
as one job, line by line. On a dry run, lines after the first report that no dataset
exists yet, because the first line is not really run: that is expected here.

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
| `WK_PUBLISHED_BASE` | `/morningstar/webknossos/.published/EMBL_Hamburg/.virtual` | checked for write access before converting; `''` turns the check off |
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
Memory so far (conversion): largest worker 9.8 GiB, total 135.1 GiB, 16 workers (1.06x plan)
```
One line every five minutes while a phase runs. It exists so a run that gets
killed still leaves a reading behind — that is the number the sizing model is
calibrated from.

```
Peak memory (conversion): largest worker 9.8 GiB … actual is 1.06x that
```
Once per phase, with the full breakdown beside it: CPU, worker states, threads.
A ratio near 1.0 means the sizing was right. Well above it triggers a warning —
report it, the model is calibrated from these.

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
| `Dataset name '…' does not match the expected convention` | The path isn't beamline-shaped. Use the manual CLI with `--dataset-name`. |
| `Cannot find p3l-account in path` / `Cannot find YYYYMMDD` | Same — the path lacks the structure the name is derived from. |
| `Image stack directory not found` | Check the path, and that its filesystem is mounted on the node. The message names the nearest mount. |
| `No TIFF files found in …` | The reconstruction hasn't written its slices, or the filesystem is mounted but empty. |
| `Cannot read the first slice: … (N bytes)` | The first TIFF is truncated or not a TIFF. Fix it and re-run. |
| `Conversion failed: … slice N of M` | A slice is damaged. The message names the file; compare its size with its neighbours. |
| `The published directory is not writable by you` | Your account cannot create the dataset directory the finished layer is renamed into. Ask its owner for group write (`chmod g+rwxs`) and check `id -nG`. Refused up front, so nothing is converted. |
| `Cannot check the published directory before converting` | Advisory. The configured path is not present on this node, so the check was skipped — set `WK_PUBLISHED_BASE` if the storage is mounted elsewhere. |
| `Staging directory does not exist: /morningstar/tmp` | morningstar is not mounted on this node. Use `WBK_NODE=bcrunch-n08`. |
| `Found N datasets sharing a display name` | §5 — pick one and pass `--dataset-id`. |
| `was published by the old system` | §5 — add `--replace-legacy`. |
| `would create a NEW dataset containing only that layer` | You are adding a layer but no dataset was found. Pass `--dataset-id`, or `--new-dataset` if a standalone dataset really is intended. |
| `Another run is already publishing '…'` | Someone is converting that dataset right now. The message names the host and SLURM job — check it with `squeue`. If that job is gone, the lock is stale: `rm` the file it names and re-run. |
| `the lock file exists and is not writable by you` | A killed job left its lock behind and it predates the 0666 change. It names the owner and the `rm` command; if the staging directory is sticky, only that user or root can remove it. |
| `Could not remove the lock file …` | Advisory, after a successful publish. The file is harmless and the next run takes it over. |
| `Could not determine whether this dataset already exists` | The server was unreachable. It refuses rather than risk a duplicate. Check connectivity and re-run. |
| `Tight memory fit: … planned against a … limit` | Advisory. Follow the `--max-memory` suggestion for a first run at that size. |
| `Layer deletion timed out` | The server is still deleting; the data is not corrupt. Wait, then re-run with the `--dataset-id` it prints. |
| Job stuck in `PD … (Resources)` | The node is taken — most often by your own forgotten interactive session. `squeue -w bcrunch-n08` shows who; close the session and the job starts. |
| `warning: WBK_LOCAL is set` | You are running outside SLURM. Don't — see §1. |
| `FAILED TO SUBMIT (line N)` | That one line of a batch was rejected by sbatch; the others were still submitted. The error follows it. |

If a run dies mid-conversion, a directory is left under `/morningstar/tmp`;
it is safe to delete. A killed job can also leave a `<dataset name>.lock` file
there. That one is harmless — it is created writable by everyone, so the next
run takes it over whoever submitted the job that died. Only a lock left by a job
that is *still running* matters, and the file says which job that is:

```bash
cat /morningstar/tmp/<dataset name>.lock
```

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
