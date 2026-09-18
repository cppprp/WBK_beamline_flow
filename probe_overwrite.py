#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos[all]>=4.0.0",
#     "numpy",
# ]
# ///
"""Probe: does the layer-overwrite path actually work, and does it leak disk?

Two open questions that cannot be answered from the client source alone, because
both depend on what the WEBKNOSSOS backend does:

  1. `RemoteDataset.delete_layer` removes the layer from the dataset properties and
     does no filesystem work (unlike the local `Dataset.delete_layer`, which calls
     rmtree). Does the server free the old zarr3 data, or is it orphaned?

  2. When we then re-add the layer and ask for a mag upload path, does the server
     hand back the *same* deterministic path the old data still occupies? If so,
     `TransferMode.MOVE_AND_SYMLINK` asserts `not dst_path.exists()` and the
     overwrite fails on the first mag — the exact case the pipeline exists for.

This script publishes a tiny throwaway dataset, then re-publishes it through the
same sequence `wbk_publish` uses, instrumenting the transfer so the reserved path
is reported whether or not the transfer succeeds.

Run it on a host that can reach the WEBKNOSSOS server and write to the staging
directory, with staging on the same filesystem as the WEBKNOSSOS storage.

    ./probe_overwrite.py --yes
    ./probe_overwrite.py --yes --temp-base /morningstar/tmp --keep

It creates a real dataset named `probe-overwrite-<timestamp>`. Delete it from the
WEBKNOSSOS UI when you are done; the script never deletes datasets itself.
"""

import argparse
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import tifffile
from webknossos import (
    DataFormat,
    Dataset,
    RemoteAccessMode,
    RemoteDataset,
    TransferMode,
    webknossos_context,
)

LAYER = "xray"
SLICES, SIDE = 64, 128


# ── reporting ─────────────────────────────────────────────────────────────────

results: dict[str, object] = {}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def head(title: str) -> None:
    say()
    say(f"── {title} " + "─" * max(0, 74 - len(title)))


def dir_bytes(path: Path) -> tuple[int, int]:
    """(allocated, apparent) size of a directory tree, not following symlinks.

    Allocated is what `du` reports and what actually costs disk. Apparent can be
    far larger: zarr3 shard files carry a fixed-size index with one slot per
    chunk, so a sparsely populated shard is mostly zeros that the filesystem
    never allocates. Reporting only the apparent size overstates the cost.
    """
    allocated = apparent = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                st = (Path(root) / name).lstat()
            except OSError:
                continue
            apparent += st.st_size
            allocated += st.st_blocks * 512  # POSIX: st_blocks is in 512B units
    return allocated, apparent


def gib(n: float) -> str:
    return f"{n / 2**20:.1f} MiB" if n < 2**30 else f"{n / 2**30:.2f} GiB"


# ── the instrument ────────────────────────────────────────────────────────────

transfers: list[dict] = []
_original_move = TransferMode.move_and_symlink


def _spy(src, dst):
    """Record what path the server reserved, and whether it was already occupied."""
    record = {"src": str(src), "dst": str(dst), "dst_existed": Path(str(dst)).exists()}
    transfers.append(record)
    say(f"    reserved: {dst}")
    say(f"    already occupied: {record['dst_existed']}")
    return _original_move(src, dst)


# ── building throwaway data ───────────────────────────────────────────────────

def write_stack(directory: Path, value: int) -> list[Path]:
    """A tiny uniform stack. The fill value distinguishes run 1 from run 2."""
    directory.mkdir(parents=True, exist_ok=True)
    plane = np.full((SIDE, SIDE), value, dtype=np.uint8)
    files = []
    for i in range(SLICES):
        f = directory / f"s{i:04d}.tif"
        tifffile.imwrite(f, plane)
        files.append(f)
    return files


def build(staging: Path, name: str, tiffs: list[Path], workers: int) -> Dataset:
    import cluster_tools

    ds = Dataset(staging, voxel_size=(650.0, 650.0, 650.0), name=name)
    with cluster_tools.get_executor("multiprocessing", max_workers=workers) as ex:
        layer = ds.add_layer_from_images(
            images=[str(t) for t in tiffs],
            layer_name=LAYER,
            category="color",
            data_format=DataFormat.Zarr3,
            compress=True,
            executor=ex,
        )
        layer.downsample(executor=ex)
    return ds


# ── the two phases ────────────────────────────────────────────────────────────

def phase_fresh(args, name: str, temp_base: Path) -> str:
    head("Phase 1: fresh upload")
    with TemporaryDirectory(dir=temp_base, prefix=f"{name}.src.") as src, \
         TemporaryDirectory(dir=temp_base, prefix=f"{name}.stage.") as stage:
        tiffs = write_stack(Path(src), value=11)
        say(f"  built {len(tiffs)} slices of {SIDE}x{SIDE}, fill value 11")
        ds = build(Path(stage), name, tiffs, args.workers)
        mags = sorted(str(m) for m in ds.get_layer(LAYER).mags)
        say(f"  converted, mags: {', '.join(mags)}")

        with webknossos_context(token=args.token, url=args.url):
            remote = ds.upload(
                new_dataset_name=name,
                transfer_mode=TransferMode.MOVE_AND_SYMLINK,
            )
    say(f"  uploaded: id={remote.dataset_id}")
    say(f"  url: {args.url}/datasets/{remote.dataset_id}")
    results["dataset_id_before"] = remote.dataset_id
    return remote.dataset_id


def inspect_storage(args, dataset_id: str, label: str) -> tuple[Path | None, int]:
    """Locate the dataset directory via the DIRECT_PATH mag paths and measure it."""
    with webknossos_context(token=args.token, url=args.url):
        remote = RemoteDataset.open(
            dataset_id=dataset_id,
            access_mode=RemoteAccessMode.DIRECT_PATH,
            read_only=True,
        )
        if LAYER not in remote.layers:
            say(f"  [{label}] no '{LAYER}' layer in properties")
            return None, 0
        layer = remote.get_layer(LAYER)
        paths = {str(m): str(layer.get_mag(m).path) for m in sorted(layer.mags)}

    for mag, p in paths.items():
        say(f"  [{label}] mag {mag}: {p}")
    any_path = Path(next(iter(paths.values())))
    dataset_dir = any_path.parent.parent  # <dataset>/<layer>/<mag>
    allocated, apparent = dir_bytes(dataset_dir) if dataset_dir.exists() else (0, 0)
    say(f"  [{label}] dataset dir: {dataset_dir}")
    say(f"  [{label}] size on disk: {gib(allocated)} allocated, {gib(apparent)} apparent")
    return dataset_dir, allocated


def phase_overwrite(args, dataset_id: str, name: str, temp_base: Path) -> bool:
    head("Phase 2: overwrite the layer (the sequence wbk_publish uses)")

    with TemporaryDirectory(dir=temp_base, prefix=f"{name}.src2.") as src, \
         TemporaryDirectory(dir=temp_base, prefix=f"{name}.stage2.") as stage:
        tiffs = write_stack(Path(src), value=222)
        say(f"  built replacement stack, fill value 222")
        local = build(Path(stage), name, tiffs, args.workers)
        local_layer = local.get_layer(LAYER)

        with webknossos_context(token=args.token, url=args.url):
            remote = RemoteDataset.open(
                dataset_id=dataset_id,
                access_mode=RemoteAccessMode.DIRECT_PATH,
                read_only=False,
            )

            old_paths = {
                str(m): Path(str(remote.get_layer(LAYER).get_mag(m).path))
                for m in sorted(remote.get_layer(LAYER).mags)
            }
            say(f"  old mag paths recorded: {len(old_paths)}")

            say("  calling RemoteDataset.delete_layer …")
            remote.delete_layer(LAYER)

            still_there = {m: p.exists() for m, p in old_paths.items()}
            results["data_after_delete_layer"] = still_there
            for m, exists in still_there.items():
                say(f"    old mag {m} still on disk after delete_layer: {exists}")

            say("  calling RemoteDataset.add_layer …")
            new_layer = remote.add_layer(
                LAYER,
                "color",
                dtype=local_layer.dtype_per_channel,
                num_channels=local_layer.num_channels,
                data_format=DataFormat.Zarr3,
                bounding_box=local_layer.bounding_box,
            )

            say("  transferring mags (MOVE_AND_SYMLINK) …")
            TransferMode.move_and_symlink = staticmethod(_spy)
            try:
                for mag in sorted(local_layer.mags):
                    say(f"  mag {mag}:")
                    new_layer.add_mag_as_copy(
                        local_layer.get_mag(mag),
                        transfer_mode=TransferMode.MOVE_AND_SYMLINK,
                        overwrite_pending=True,
                    )
                    say(f"    transferred ok")
            finally:
                TransferMode.move_and_symlink = staticmethod(_original_move)

    say("  overwrite completed without error")
    return True


def verify_content(args, dataset_id: str) -> None:
    head("Verification: id preserved and data actually replaced")
    with webknossos_context(token=args.token, url=args.url):
        remote = RemoteDataset.open(dataset_id=dataset_id)
        say(f"  dataset id: {remote.dataset_id}")
        results["dataset_id_after"] = remote.dataset_id
        data = remote.get_layer(LAYER).get_mag("1").read()
        values = np.unique(data)
        say(f"  distinct voxel values served: {values[:5]}")
        results["content_replaced"] = bool(222 in values)
        say(f"  contains the replacement value 222: {results['content_replaced']}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--token", default=os.environ.get("WK_TOKEN"))
    parser.add_argument(
        "--url", default=os.environ.get("WK_URL", "https://webknossos.embl-hamburg.de")
    )
    parser.add_argument("--temp-base", default=os.environ.get("WK_TEMP_BASE", "/morningstar/tmp"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--keep", action="store_true",
        help="Leave the local staging scratch in place for inspection",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Required. Confirms that a real dataset will be created on the server.",
    )
    args = parser.parse_args()

    if not args.token:
        token_file = Path(__file__).parent / ".config" / "auth_token.txt"
        if token_file.exists():
            args.token = token_file.read_text().strip()
    if not args.token:
        parser.error("No token. Use --token, $WK_TOKEN, or .config/auth_token.txt")

    temp_base = Path(args.temp_base)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"probe-overwrite-{stamp}"

    say("This probe creates a REAL dataset on " + args.url)
    say(f"  name:    {name}")
    say(f"  staging: {temp_base}")
    say("  It never deletes datasets — remove it from the UI afterwards.")
    if not args.yes:
        say()
        say("Re-run with --yes to proceed.")
        return 2

    if not temp_base.is_dir():
        say(f"Staging directory does not exist: {temp_base}")
        return 2

    dataset_id = None
    overwrite_ok = False
    failure = None

    try:
        dataset_id = phase_fresh(args, name, temp_base)

        head("Storage before the overwrite")
        _, size_before = inspect_storage(args, dataset_id, "before")
        results["size_before"] = size_before

        overwrite_ok = phase_overwrite(args, dataset_id, name, temp_base)
    except AssertionError as exc:
        failure = f"AssertionError: {exc}"
        say()
        say(f"  TRANSFER REFUSED: {exc}")
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not handle
        failure = f"{type(exc).__name__}: {exc}"
        say()
        say(f"  FAILED: {failure}")
        traceback.print_exc()

    if dataset_id and overwrite_ok:
        try:
            head("Storage after the overwrite")
            _, size_after = inspect_storage(args, dataset_id, "after")
            results["size_after"] = size_after
            verify_content(args, dataset_id)
        except Exception as exc:  # noqa: BLE001
            say(f"  post-check failed: {type(exc).__name__}: {exc}")

    # ── verdict ───────────────────────────────────────────────────────────────
    head("VERDICT")
    say(f"  dataset: {name}")
    if dataset_id:
        say(f"  url:     {args.url}/datasets/{dataset_id}")
    say()

    reserved = transfers[0] if transfers else None
    if reserved:
        say("Q2  Does the server reserve the path the old data occupies?")
        say(f"      reserved path: {reserved['dst']}")
        if reserved["dst_existed"]:
            say("      ANSWER: YES — the destination was already occupied.")
            say("              The overwrite path needs the destination cleared first.")
        else:
            say("      ANSWER: NO — the server handed back a free path.")
            say("              No collision. The current implementation is fine as written.")
    else:
        say("Q2  No transfer was attempted; the run failed earlier.")

    say()
    stale = results.get("data_after_delete_layer")
    if stale is not None:
        say("Q1  Does delete_layer free the old data?")
        leaked = [m for m, exists in stale.items() if exists]
        if leaked:
            say(f"      ANSWER: NO — mags still on disk immediately after: {', '.join(leaked)}")
            say("              Server-side GC may still be asynchronous — re-check the")
            say("              size below in a few hours before concluding it leaks.")
        else:
            say("      ANSWER: YES — the data was gone immediately after delete_layer.")

    if "size_before" in results and "size_after" in results:
        before, after = results["size_before"], results["size_after"]
        say()
        say(f"      dataset dir: {gib(before)} before, {gib(after)} after")
        if after > before * 1.5:
            say("      Roughly doubled — the old layer is still occupying disk.")
        else:
            say("      No significant growth — nothing obvious leaked.")

    say()
    if failure:
        say(f"  overwrite: FAILED — {failure}")
    elif overwrite_ok:
        idsame = results.get("dataset_id_before") == results.get("dataset_id_after")
        say(f"  overwrite: SUCCEEDED")
        say(f"  dataset id unchanged: {idsame}")
        say(f"  content replaced:     {results.get('content_replaced')}")

    say()
    if dataset_id:
        say(f"  Clean up: delete '{name}' in the WEBKNOSSOS UI.")
    else:
        say("  Nothing was created on the server; no cleanup needed.")
    if not args.keep:
        for leftover in temp_base.glob(f"{name}.*"):
            shutil.rmtree(leftover, ignore_errors=True)

    return 0 if overwrite_ok and not failure else 1


if __name__ == "__main__":
    sys.exit(main())
