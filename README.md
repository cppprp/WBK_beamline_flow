# wbk_publish

Publishes beamline tomography data to WEBKNOSSOS as zarr3 datasets.

Re-running on the same input **replaces the layer on the dataset published last
time and keeps the dataset id**, so URLs and existing annotations stay valid.
This is the difference from the old `wbki_acp.py` flow, used throughout OCT 2025 - OCT 2026
## Install

```bash
uv sync
```

`pyproject.toml` requires `webknossos[all]>=4.0.0`. The `[all]` extra matters:
since 4.0 it is what carries `tifffile` and `imagecodecs` (TIFF reading) and
`numba` (fast downsampling) — none of them are in the base package.

## Run

Automatic (the SSH trigger after recon/stitching):

```bash
uv run python -m wbk_publish.beamline /mnt/beegfs7/P14/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1/ --voxel-size 650
```

Manual or batch:

```bash
uv run python -m wbk_publish.manual --input_dir /mnt/ximg/2025/p3l-eduke/RECON/20250921/MA040/stitching_1 --voxel_size 325 --project cardiac_study
```

Both take `--dry-run`, which reports what would happen — fresh upload or layer
overwrite, target folder, teams, metadata file, and how the machine would be used —
without converting or transferring anything. Run it first on any path you have not
published before.

If the trigger cannot `cd` into the checkout, use `uv run --directory /path/to/WBK_convert ...`.

## Queueing on the cluster

The conversion runs on the SLURM cluster; morningstar is only the storage it
writes to. `wbk_publish_job.sh` submits itself as an sbatch job and, inside the
allocation, runs the conversion from the project's own venv. One command:

```bash
./wbk_publish_job.sh /mnt/beegfs7/P14/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1/ --voxel-size 650
```

**The node must reach morningstar at `/morningstar`** — mounted there, or via a
symlink to wherever it is mounted. The WEBKNOSSOS server hands the client
`/morningstar/...` paths to write into and the client library has no way to remap
them, so a mount at any other path cannot work. The wrapper checks this, and
`/mnt/beegfs7`, before starting Python; the pipeline also refuses to create its
staging directory if missing — on a node without the mount, `mkdir -p` would
otherwise create a local `/morningstar/tmp` and stage a conversion into it.

| Variable | Default | Purpose |
|---|---|---|
| `WBK_HOME` | the directory the script is in | the checkout |
| `WBK_NODE` | let SLURM choose | pin to a specific node (`--nodelist`) |
| `WBK_MODULE` | `wbk_publish.beamline` | or `wbk_publish.manual` |
| `WBK_LOGS` | `$WBK_HOME/logs` | job logs, `wbk_<jobid>.log` |
| `WBK_LOCAL` | unset | set to run in place without queueing |
| `UV` | `/mx-beta/ximg-beta/uv/uv` | the uv binary |
| `SBATCH_ARGS` | — | extra sbatch flags, e.g. `--time=24:00:00` |

At submit time the wrapper runs `uv sync --frozen` on the login node, so the
compute node finds a ready venv and two jobs starting together never race to build
it. Inside the job it runs `.venv/bin/python` directly — uv is not needed on the
node. uv-managed interpreters are placed next to uv on shared storage
(`UV_PYTHON_INSTALL_DIR`), not under `$HOME`, so the venv resolves on every node.

`WBK_HOME` and `WBK_LOGS` are resolved at submit time — inside the job, sbatch has
copied the script to its spool directory and its own path is meaningless — and
exported into the job. Anything else in the submitting environment, including
`WK_TOKEN` and `WK_TIMEOUT`, reaches the job the same way.

`WBK_LOCAL=1` with `--dry-run` is the way to check a path from the login node
before spending an allocation.

## The storage path

Everything the node writes to morningstar goes through NFS over InfiniBand. Two
server-side settings decide how fast that is, and both were wrong once:

- **The export must be `async`.** With `sync`, every NFS write is committed to
  disk before it is acknowledged, and on ZFS without a separate log device that
  means ~30–50 MB/s per stream, ~280 MB/s across the server's 8 threads —
  regardless of how the client writes (measured: conversion, plain `cp`, `dd`
  all hit the same ceiling; `fio` went from 29.7 to 610 MiB/s when IT switched
  the export for 10.40.15.108 to `async`). The trade is that a morningstar crash
  can lose the last few seconds of NFS writes; for this pipeline that is a
  conversion to re-run, and the old local-disk system carried the same exposure.
- **nfsd threads**: the distribution default is 8. Less critical with `async`,
  but 49 parallel writers through 8 server threads still queue; `threads=64`
  under `[nfsd]` in `/etc/nfs.conf` is the usual setting for a 192-core server.

`bench_convert.sh` times the plain `webknossos convert` CLI against a chosen target
and library version, with network and NFS counters — it is how the above was found,
and the thing to reach for if conversions get slow again.

## How parallelism is chosen

`--cpus` (beamline) and `--jobs` (manual) are **upper bounds, not targets**. The actual
worker count comes from `resources.py`, which probes one TIFF header and reads the
machine's allocation:

```
tasks        = ceil(n_slices / 32)          # z-split; slice size adds no parallelism
mem/worker   = xy_pixels * 32 * bytes_px * 6.0   # measured, see below
workers      = min(cores, tasks, usable_ram // mem_per_worker, --cpus)
threads/worker = min(cores // workers, 16)
```

Two things this prevents. Sizing workers from the core count would put 176 workers on
a large stitch asking for more RAM than the node has, and an OOM there kills the whole
run — so memory wins. And each worker's thread pools are capped to the derived thread
count rather than the machine's core count — numpy/BLAS/blosc/numba through their
environment variables, and tensorstore (all zarr3 I/O) by replacing the context
webknossos opens arrays with, since it has no variable. This is what stops N workers
times a machine-sized pool from swamping the node: uncapped, tensorstore alone starts
~180 threads per worker on a 176-core node, and 50 workers of that exceed the process
limit and kill the pool.

### When it warns

Per-worker memory is a multiplier on the raw slab. The 6.0 was calibrated on the
cluster from the `Peak memory` log line: a 3584² float32 stack (1.5 GiB slab) peaked
at 9.4 GiB per worker during conversion. Every run prints that line for its own
phase, so the figure can be re-checked whenever a new shape or dtype goes through.
Downsampling is sized separately, from the peak the conversion phase just measured
(`DOWNSAMPLE_PEAK_RATIO`, observed 2.2x, planned 2.5x). On most datasets there is
ample slack. The run warns when the plan would not survive the figure being 10%
higher than planned — which a plan filling its 90% budget does not trip, but one
pushed past that by `--cpus` or a lowered `--max-memory` can.

The run says so when that happens, and suggests a budget:

```
Tight memory fit: 444.4 GiB planned against a 512.0 GiB limit. Per-worker memory
is an estimate (6.0x the raw slab), not a measurement — if the real figure is more
than 10% higher this run will be OOM-killed. For a first run at this size use
--max-memory 384G, then relax it once you have seen the actual usage.
```

The check measures against the limit the machine enforces, not against the budget.
When memory is the binding constraint the plan always fills whatever budget it is
given, so comparing the two would warn on every large run — including after you
lowered the budget on this warning's own advice.

`--max-memory 400G` overrides the detected allocation. Cores and memory are read from
`SLURM_CPUS_ON_NODE` / `SLURM_MEM_PER_NODE` when present, falling back to the scheduler
affinity mask — never `cpu_count()`, which reports the machine rather than the
allocation.

A thread limit you set yourself in the environment is always respected; the run only
manages variables it set itself.

### The pyramid

Downsampling is parallelised per target shard. webknossos 4.x gives coarser mags a
generic 1024³ shard unless told otherwise, and on a 2000-slice stack that leaves mag 2
with 4 jobs and every coarser level with one — the pyramid then takes longer than the
conversion (measured: 4:27 for mag 2 alone, 1:42 for mag 4, on a 3584² float32 stack).
`convert.py` passes mag 1's 4096 × 4096 × 32 shard shape through, as webknossos 3.x did
implicitly, which gives 16 and 8 jobs for the same levels. The pyramid uses the same
shard shape as mag 1 in every dataset this publishes.

## Reading the datasets with external tools

WEBKNOSSOS transfers **mags only** when publishing — each mag directory is renamed
into place carrying its own array-level `zarr.json`, while the two group-level files
stay behind in staging:

```
<dataset>/zarr.json              root group marker
<dataset>/<layer>/zarr.json      OME-NGFF multiscales: axes, voxel sizes, pyramid
```

WEBKNOSSOS does not need them — it resolves mags from the datasource, which for
virtual datasets lives in the database. But without them the published directory is
not a readable zarr3 hierarchy, so napari, zarr-python, tensorstore and neuroglancer
see nothing when pointed at it.

`ngff.py` restores both after every publish. It is not a straight copy: the server
reserves mag paths with a random suffix (`1__bhgirKI86DNZGmBv`) while the staged
metadata calls them `1`, so the multiscales paths are remapped to what the server
actually allocated. Copying verbatim would produce a hierarchy that looks valid and
points at directories that do not exist.

Failures here are logged, not raised. The dataset is correct in WEBKNOSSOS either
way, and a metadata file is not worth failing a six-hour conversion over.

## Sharing a dataset as a zarr link


Annotation private links are the shareable form. The access token sits in the path, so
the URL authenticates itself — no header, no login, no query string — and the route
serves the underlying dataset's layers, not just the annotation's:

```bash
./wbk_publish/streaming.py <datasetId>
```

prints, for each layer:

```
https://webknossos.embl-hamburg.de/data/annotations/zarr3_experimental/<accessToken>/<layer>
```

which is [OME-NGFF 0.5](https://ngff.openmicroscopy.org/0.5/#metadata) over zarr v3 and
opens in napari, zarr-python, tensorstore or the OME-NGFF validator as-is.

which is what an external segmentation tool needs: one HTTPS URL it can open directly,
with no header to configure and no login.

**Run it with your own token.** A link is owned by the account that created it, and only
that account can revoke it — so the token comes from `--token` or `$WK_TOKEN` and nowhere
else. There is deliberately no fallback to the pipeline's `.config/auth_token.txt`, which
would mint links belonging to the service account. Every run prints the account it is
acting as. Get your token from Account Settings → Auth Token.

**Reuse rather than re-mint.** Before creating anything the script looks through the
links your account already owns and returns the one already pointing at this dataset, so
running it twice hands out the same URL. `--new` overrides that; `--expires-in-days N`
sets an expiry (default: never). Private links belong to an annotation, so a dataset with
none gets a throwaway skeleton annotation to hang the link on — it holds no data, and a
skeleton contributes no layers of its own, so the link exposes exactly the dataset's
layers.

**The printed URL is a credential.** Anyone holding it can read every layer of that
dataset, regardless of team permissions, until the link is deleted — in the annotation's
share dialog or via `DELETE /api/zarrPrivateLinks/<id>`. Hand out as few as possible and
prefer an expiry for anything leaving the facility.

The script is fully standalone: an inline dependency block (httpx only) like
`probe_overwrite.py` and `scan_orphans.py`, no import of `config.py`, and the instance URL
hardcoded — copy it anywhere and it runs. It is the one module here that does not share
the package's deployment settings, because a link only means anything on the server that
issued it.

```bash
WK_TOKEN=<your-token> ./wbk_publish/streaming.py <datasetId>
```

Two things HTTP streaming cannot give you, because it re-encodes WEBKNOSSOS' internal
buckets rather than reading our files: chunks arrive as uncompressed 32³ blocks instead
of our shards, and the axes are ordered `c,x,y,z` where the spec recommends `z,y,x`
(readers that honour `axes` are fine; readers that assume trailing zyx will transpose).
On the cluster, where the files are on the same filesystem, skip HTTP entirely and read
the published directory directly — that is what `ngff.py` restores the group metadata
for.

## How a run decides what to do

1. `--dataset-id`, if given.
2. The `dataset_id` recorded for this directory in `meta_<run>.json` (or, for
   datasets published before the record moved there, in the old
   `dataset-metadata.json` sidecar beside the input data — read, never written).
3. A lookup by name, for datasets published before ids were recorded.
4. Otherwise: fresh upload.

If step 3 finds several candidates, or cannot reach the server, the run stops
rather than guessing — a wrong guess either overwrites the wrong dataset or
publishes a duplicate. Pass `--dataset-id` to resolve it.

## The metadata file

MX-Cube writes `meta_<run>.json` into the sample directory, one per
reconstruction, and the recon tool edits it in place when a reconstruction is
redone. It is the provenance of the data, so it is also where a publish records
what it did — under a single top-level key, keyed by the directory that was
published:

```json
{
  "betadelta": 0.02,
  "distances_mm": [73, 77, 83, 92],
  "sample_run": 1,
  "...": "...the rest of the MX-Cube keys, untouched...",
  "energy_kev": 18.000267327900946,
  "webknossos": {
    "recon_111_1": {
      "dataset_id": "68d2a1f04c3b9e0017a3c5d1",
      "dataset_name": "20260111_p3l-faberjw_123-10-T4_1",
      "webknossos_url": "https://webknossos.embl-hamburg.de",
      "layer": "xray",
      "published_at": "2026-07-09T08:02:51+00:00"
    }
  },
  "last_updated": "2026-07-08T17:38:05Z"
}
```

Everything outside `webknossos` is attached to the dataset as display metadata,
exactly as before. Re-publishing the same directory keeps its `dataset_id` — that
is what makes the run replace the layer instead of creating a second dataset —
and only moves `published_at`. A tiled scan has `recon_111_1`, `recon_121_1` and
`stitching_1` beside one `meta_1.json`; each published directory gets its own
entry, so they never overwrite each other.

`last_updated` is set by the recon tool. When it is later than `published_at`, the
run (and `--dry-run`) logs that the metadata was updated after the last publish and
WEBKNOSSOS is behind.

If the file is missing (archive data published by hand) it is created with only
the `webknossos` block. If it exists but is not valid JSON, it is left alone: the
publish succeeds, the id is printed in the log, and the next run finds the dataset
by name.

## Configuration

Defaults live in `config.py` and can be overridden per environment:

| Variable | Default | Purpose |
|---|---|---|
| `WK_URL` | `https://webknossos.embl-hamburg.de` | Server |
| `WK_TOKEN` | — | API token; else `.config/auth_token.txt` |
| `WK_TEMP_BASE` | `/morningstar/tmp` | Conversion staging |
| `WK_BEEGFS_MOUNT` | `/mnt/beegfs7` | Checked before work starts |
| `WK_XIMG_TEAM` | `ximg` | Facility-wide team |

**`WK_TEMP_BASE` must be on the same filesystem as the WEBKNOSSOS storage.**
Converted layers are handed over with `TransferMode.MOVE_AND_SYMLINK`, which is
an `os.rename()` and cannot cross a mount point.

## Layout

| Module | Responsibility |
|---|---|
| `beamline.py` / `manual.py` | CLIs; build a `PublishRequest` |
| `publish.py` | Pipeline: locking, flow selection, fresh upload, layer overwrite |
| `convert.py` | TIFF stack → zarr3 layer + mag pyramid, in staging |
| `remote.py` | Folders, teams, metadata, dataset lookup |
| `metadata.py` | `meta_<run>.json`: the publication record, MX-Cube key mapping |
| `ngff.py` | Restores zarr3 group metadata so external tools can read the output |
| `streaming.py` | Mints shareable annotation private links to a dataset's layers |
| `naming.py` | Path → dataset name, and the convention it must match |
| `resources.py` | Probes the stack, sizes workers and threads to the allocation |
| `threads.py` | Caps per-worker numeric-library threads |
| `config.py` | Deployment settings and the metadata key mapping |

## Differences from `wbki_acp.py`

- Conversion is in-process (`add_layer_from_images`) rather than a
  `uvx webknossos convert` subprocess, so the webknossos version is the one
  pinned in the lockfile instead of whatever `uvx` resolves at run time.
- Datasets get the user team **and** `ximg`; the old script assigned only the
  user team.
- `--skip_conversion` / `--skip_permissions` / `--skip_announce` are gone.
  They existed to work around the old announce-then-convert-into-place
  sequence, which no longer happens: conversion completes in staging before
  anything is announced.
