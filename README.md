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

## How parallelism is chosen

`--cpus` (beamline) and `--jobs` (manual) are **upper bounds, not targets**. The actual
worker count comes from `resources.py`, which probes one TIFF header and reads the
machine's allocation:

```
tasks        = ceil(n_slices / 32)          # z-split; slice size adds no parallelism
mem/worker   = xy_pixels * 32 * bytes_px * 2.5
workers      = min(cores, tasks, usable_ram // mem_per_worker, --cpus)
threads/worker = min(cores // workers, 16)
```

Two things this prevents. Sizing workers from the core count would put 176 workers on
a large stitch asking for more RAM than the node has, and an OOM there kills the whole
run — so memory wins. And each worker's numeric libraries are capped to the derived
thread count rather than the machine's core count, which is what stops N workers times
a machine-sized thread pool from swamping the node.

### When it warns

Per-worker memory is an estimate — 2.5x the raw slab — not a measurement. On most
datasets there is ample slack. On a very large stitch there is not: a plan can sit at
87% of the node, where a 25% underestimate is the difference between fitting and
being OOM-killed hours in.

The run says so when that happens, and suggests a budget:

```
Tight memory fit: 444.4 GiB planned against a 512.0 GiB limit. Per-worker memory
is an estimate (2.5x the raw slab), not a measurement — if the real figure is more
than 25% higher this run will be OOM-killed. For a first run at this size use
--max-memory 384G, then relax it once you have seen the actual usage.
```

Acting on it costs throughput and buys margin — on a 10507 x 10512 x 10339 uint16
stitch, 27 workers drops to 20 and the run takes 17 waves instead of 12. Worth it
once, to measure; not worth it forever.

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
2. The `dataset_id` recorded in `dataset-metadata.json` beside the input data.
3. A lookup by name, for datasets published before the sidecar carried an id.
4. Otherwise: fresh upload.

If step 3 finds several candidates, or cannot reach the server, the run stops
rather than guessing — a wrong guess either overwrites the wrong dataset or
publishes a duplicate. Pass `--dataset-id` to resolve it.

## The sidecar

`dataset-metadata.json`, written into the recon/stitching directory after every
successful publish. It carries both the MX-Cube acquisition metadata that was
published and the dataset identity:

```json
{
  "dataset_id": "6710f...",
  "dataset_name": "20250921_p3l-eduke_MA040_1",
  "webknossos_url": "https://webknossos.embl-hamburg.de",
  "layer": "xray",
  "published_at": "2026-08-28T09:14:03+00:00",
  "dataset": { "...raw meta_N.json..." },
  "dataset_display": { "...remapped for the WEBKNOSSOS UI..." }
}
```

It lives next to the *input* data rather than in the dataset's storage
directory, because the id has to be findable before the id is known. Deleting
it is recoverable — the next run falls back to a lookup by name — but it is the
authoritative record, so keep it with the data.

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
| `sidecar.py` | `dataset-metadata.json` read/write, MX-Cube key mapping |
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
