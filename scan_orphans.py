#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos[all]>=4.0.0",
#     "requests",
# ]
# ///
"""Find storage that WEBKNOSSOS no longer references but never freed.

The datastore only deletes paths that literally `startsWith` a configured
`baseDirectories` entry; anything else is dropped silently — not deleted, not
queued for external deletion, no error. So when that prefix does not match,
storage is never reclaimed. Two things accumulate:

  1. Whole dataset directories whose dataset no longer exists in WEBKNOSSOS
     (deleted through the UI, data left behind).

  2. Stale mag directories inside datasets that DO still exist, left by layer
     overwrites. The server reserves a fresh suffixed path each time
     (`1__bhgirKI86DNZGmBv`) rather than reusing, so every re-reconstruction
     adds a full copy.

This script only reports. It deletes nothing. With --write-script it emits a
shell script of `rm -rf` commands for you to review and run yourself.

    ./scan_orphans.py
    ./scan_orphans.py --write-script /tmp/reclaim.sh
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

VIRTUAL_DIR = Path("/morningstar/webknossos/.published/EMBL_Hamburg/.virtual")
WK_URL = "https://webknossos.embl-hamburg.de"

# Published directories are named "<dataset name>-<24 hex dataset id>".
DIR_ID = re.compile(r"^(?P<name>.+)-(?P<id>[0-9a-f]{24})$")
# The server appends a random suffix when the plain mag name is taken.
MAG_DIR = re.compile(r"^(?P<mag>[0-9]+(-[0-9]+-[0-9]+)?)(__(?P<suffix>[A-Za-z0-9_-]+))?$")


def allocated_bytes(path: Path) -> int:
    """What the directory actually costs on disk, matching `du`."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_blocks * 512
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def dataset_exists(session: requests.Session, url: str, dataset_id: str) -> bool | None:
    """True/False, or None when the answer could not be determined."""
    try:
        r = session.get(f"{url}/api/datasets/{dataset_id}", timeout=20)
    except requests.RequestException as exc:
        print(f"  ! lookup failed for {dataset_id}: {exc}", file=sys.stderr)
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (400, 404):
        return False
    print(f"  ! unexpected status {r.status_code} for {dataset_id}", file=sys.stderr)
    return None


def live_mag_dirs(session: requests.Session, url: str, dataset_id: str) -> set[str] | None:
    """Directory names the dataset's properties currently point at."""
    try:
        r = session.get(f"{url}/api/datasets/{dataset_id}", timeout=20)
        r.raise_for_status()
        info = r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None

    source = info.get("dataSource") or {}
    layers = source.get("dataLayers")
    if layers is None:
        return None

    live = set()
    for layer in layers:
        for mag in layer.get("mags", []):
            path = mag.get("path")
            if path:
                live.add(Path(path).name)
    return live


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--virtual-dir", type=Path, default=VIRTUAL_DIR)
    parser.add_argument("--url", default=os.environ.get("WK_URL", WK_URL))
    parser.add_argument("--token", default=os.environ.get("WK_TOKEN"))
    parser.add_argument(
        "--write-script", type=Path,
        help="Write the rm -rf commands to this file for review. Never executed here.",
    )
    parser.add_argument(
        "--skip-stale-mags", action="store_true",
        help="Only look for fully orphaned datasets (much faster; no per-dataset API calls)",
    )
    args = parser.parse_args()

    if not args.token:
        token_file = Path(__file__).parent / ".config" / "auth_token.txt"
        if token_file.exists():
            args.token = token_file.read_text().strip()
    if not args.token:
        parser.error("No token. Use --token, $WK_TOKEN, or .config/auth_token.txt")

    if not args.virtual_dir.is_dir():
        print(f"Not a directory: {args.virtual_dir}")
        return 2

    session = requests.Session()
    session.headers["X-Auth-Token"] = args.token

    dirs = sorted(d for d in args.virtual_dir.iterdir() if d.is_dir())
    print(f"Scanning {len(dirs)} dataset directories in {args.virtual_dir}\n")

    orphan_datasets: list[tuple[Path, int]] = []
    stale_mags: list[tuple[Path, int]] = []
    unknown: list[Path] = []
    live_count = 0

    for i, d in enumerate(dirs, 1):
        match = DIR_ID.match(d.name)
        if not match:
            unknown.append(d)
            continue

        exists = dataset_exists(session, args.url, match.group("id"))
        if exists is None:
            unknown.append(d)
            continue

        if not exists:
            size = allocated_bytes(d)
            orphan_datasets.append((d, size))
            print(f"[{i}/{len(dirs)}] ORPHAN   {human(size):>12}  {d.name}")
            continue

        live_count += 1
        if args.skip_stale_mags:
            continue

        live = live_mag_dirs(session, args.url, match.group("id"))
        if live is None:
            continue

        for layer_dir in (p for p in d.iterdir() if p.is_dir()):
            for mag_dir in (p for p in layer_dir.iterdir() if p.is_dir()):
                if not MAG_DIR.match(mag_dir.name):
                    continue
                if mag_dir.name not in live:
                    size = allocated_bytes(mag_dir)
                    stale_mags.append((mag_dir, size))
                    print(
                        f"[{i}/{len(dirs)}] STALE    {human(size):>12}  "
                        f"{d.name}/{layer_dir.name}/{mag_dir.name}"
                    )

    orphan_total = sum(s for _, s in orphan_datasets)
    stale_total = sum(s for _, s in stale_mags)

    print("\n" + "─" * 78)
    print(f"  live datasets:            {live_count}")
    print(f"  orphaned dataset dirs:    {len(orphan_datasets):>5}  {human(orphan_total):>14}")
    if not args.skip_stale_mags:
        print(f"  stale mag dirs (live ds): {len(stale_mags):>5}  {human(stale_total):>14}")
    if unknown:
        print(f"  could not determine:      {len(unknown):>5}  (left alone)")
    print(f"  RECLAIMABLE TOTAL:              {human(orphan_total + stale_total):>14}")
    print("─" * 78)

    if unknown:
        print("\nNot classified (unrecognised name, or lookup failed):")
        for d in unknown[:20]:
            print(f"  {d.name}")
        if len(unknown) > 20:
            print(f"  … and {len(unknown) - 20} more")

    if args.write_script:
        lines = [
            "#!/bin/bash",
            "# Generated by scan_orphans.py — REVIEW BEFORE RUNNING.",
            "# Each path is either a dataset directory whose dataset no longer exists",
            "# in WEBKNOSSOS, or a mag directory the dataset no longer references.",
            "set -euo pipefail",
            "",
        ]
        for d, size in orphan_datasets:
            lines.append(f'rm -rf "{d}"  # orphan dataset, {human(size)}')
        for d, size in stale_mags:
            lines.append(f'rm -rf "{d}"  # stale mag, {human(size)}')
        args.write_script.write_text("\n".join(lines) + "\n")
        args.write_script.chmod(0o755)
        print(f"\nReview script written: {args.write_script}")
        print("Read it before running. Nothing has been deleted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
