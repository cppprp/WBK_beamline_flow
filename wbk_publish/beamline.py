"""Automatic beamline publishing — triggered over SSH after recon/stitching.

Nothing about the dataset's identity is a free parameter: the name is derived
from the input path and validated against the beamline convention, and the
layer is always 'xray'. A trigger firing with a bad argument therefore cannot
publish a mislabelled dataset. Use the manual CLI when you need control over
those things.

    python -m wbk_publish.beamline /mnt/beegfs7/P14/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1/ \
        --voxel-size 650

Re-running on the same input replaces the xray layer and keeps the dataset id,
so URLs and existing annotations stay valid.
"""

import argparse
import logging
import sys
from pathlib import Path

from .config import get_auth_token
from .logs import make_logger
from .naming import (
    dataset_name_from_path,
    parse_dataset_name,
    resolve_tiff_stack,
    validate_dataset_name,
)
from .publish import PublishRequest, publish
from .resources import parse_memory_size
from .metadata import find_mx_cube_metadata


def build_request(args: argparse.Namespace, log: logging.Logger) -> PublishRequest:
    input_dir = Path(str(args.input_dir).rstrip("/"))

    dataset_name = dataset_name_from_path(input_dir, log)
    validate_dataset_name(dataset_name)
    parts = parse_dataset_name(dataset_name)

    voxel = float(args.voxel_size_nm)

    return PublishRequest(
        input_dir=input_dir,
        # One meta_<run>.json can describe a recon and a stitching with the same
        # run number, so the record is keyed by the directory, not the run.
        record_key=input_dir.name,
        tiff_stack=resolve_tiff_stack(input_dir),
        dataset_name=dataset_name,
        readable_name=parts.readable_name,
        # Beamline datasets are filed by acquisition date under the account.
        folder_path=f"{parts.user}/{parts.imaging_date}",
        user_team=parts.user,
        metadata_file=find_mx_cube_metadata(
            input_dir,
            parts.run_number,
            explicit=Path(args.metadata_file) if args.metadata_file else None,
        ),
        voxel_size_nm=(voxel, voxel, voxel),
        token=get_auth_token(args.token),
        # No layer_name: a tomogram takes the dataset's own default,
        # <readable name>_xray.
        category="color",
        max_workers=args.cpus,
        max_memory=args.max_memory,
        dataset_id=args.dataset_id,
        replace_legacy=args.replace_legacy,
        dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.beamline",
        description="Publish a beamline TIFF stack to WEBKNOSSOS as a zarr3 xray layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="The recon or stitching directory, e.g. .../recon_111_1/ or .../stitching_1/",
    )
    parser.add_argument(
        "--metadata-file",
        help="MX-Cube metadata JSON to read, and to record the publication in. "
             "Defaults to meta_<run>.json beside the sample directory. Give it "
             "explicitly when the acquisition metadata lives somewhere else, or "
             "when a stitching has no meta file of its own.",
    )
    parser.add_argument(
        "--voxel-size",
        dest="voxel_size_nm",
        type=int,
        choices=[325, 650],
        required=True,
        help="Voxel size in nm, set by the magnification used",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=None,
        help="Upper bound on worker processes. By default the worker count is "
        "derived from the stack size and the machine's CPU and memory allocation.",
    )
    parser.add_argument(
        "--max-memory",
        type=parse_memory_size,
        default=None,
        help="Memory this run may use, e.g. '400G'. Read from SLURM or the machine "
        "when omitted.",
    )
    parser.add_argument(
        "--token",
        help="WEBKNOSSOS API token. Defaults to $WK_TOKEN, then .config/auth_token.txt",
    )
    parser.add_argument(
        "--dataset-id",
        help="Overwrite this specific dataset, skipping the recorded id and the name lookup",
    )
    parser.add_argument(
        "--replace-legacy",
        action="store_true",
        help="If the matched dataset was published by the old system and cannot be "
        "updated in place, publish a new dataset alongside it instead of refusing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without converting or transferring anything",
    )
    args = parser.parse_args(argv)

    log = make_logger("wbk_publish")

    try:
        request = build_request(args, log)
        log.info(f"Publishing {request.dataset_name}")
        publish(request, log)
        return 0
    except Exception as exc:
        log.error(f"Pipeline failed: {type(exc).__name__}: {exc}")
        log.debug("Traceback:", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
