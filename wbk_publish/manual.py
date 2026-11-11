"""Manual and batch publishing — the operator-driven path.

Same pipeline as the automatic beamline CLI, but everything the beamline path
fixes is adjustable here: layer name, layer type, voxel size, project folder,
and the dataset name, which is required rather than derived.

    python -m wbk_publish.manual --stack /mnt/ximg/2025/p3l-eduke/RECON/20250921/MA040/stitching_1/stack \
        --dataset-name 20250921_p3l-eduke_MA040_stitching_1 \
        --voxel-size 325 --project cardiac_study

    python -m wbk_publish.manual --stack /mnt/ximg/2026/preds/MA040/labels \
        --dataset-name 20250921_p3l-eduke_MA040_stitching_1 \
        --voxel-size 325 --layer-type segmentation

    python -m wbk_publish.manual --zarr /mnt/ximg/2026/p3l-eduke/exports/MA040.zarr \
        --dataset-name 20260115_p3l-eduke_MA040_1 --voxel-size 650

Two arguments carry it: --stack (or --zarr) says where the data is, and
--dataset-name says which dataset it belongs to. There is no second path to
give. The name is not optional, because for the archive and postprocessing
trees this CLI exists to serve, nothing reliable can be derived from the path —
and the name is also what finds an existing dataset, by an exact lookup on its
directory name, so a further layer needs neither a recorded id nor --dataset-id.

The publication record goes beside the stack as meta_<run>.json unless
--metadata-file says otherwise. Losing that write does not fail the publish; the
id is logged instead.

A zarr store (plain array or OME-Zarr, v2 or v3) is accepted in place of a TIFF
stack with --zarr. A store with several channels needs --channel.

Like the beamline CLI, re-running on the same dataset name replaces that layer
on the dataset published before and keeps the dataset id.
"""

import argparse
import sys
from pathlib import Path

from .config import LAYER_SUFFIXES, get_auth_token
from .logs import make_logger
from .naming import parse_dataset_name, validate_dataset_name
from .publish import PublishRequest, publish
from .resources import parse_memory_size
from .metadata import find_mx_cube_metadata


def parse_voxel_size(value: str) -> tuple[float, float, float]:
    """Accept '650' or '650,650,650' and return an (x, y, z) tuple in nm."""
    parts = [p.strip() for p in str(value).split(",") if p.strip()]

    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Voxel size '{value}' is not numeric. Use '650' or '650,650,650'."
        ) from None

    if len(numbers) == 1:
        return (numbers[0], numbers[0], numbers[0])
    if len(numbers) == 3:
        return (numbers[0], numbers[1], numbers[2])

    raise argparse.ArgumentTypeError(
        f"Voxel size '{value}' must have 1 or 3 components, got {len(numbers)}."
    )


def build_request(args: argparse.Namespace) -> PublishRequest:
    if args.zarr and args.stack:
        raise ValueError("--stack and --zarr both name the input; give one.")
    if not args.zarr and not args.stack:
        raise ValueError("--stack is required (or --zarr for a zarr store).")

    stack_path = Path(str(args.stack or args.zarr).rstrip("/"))
    dataset_name = args.dataset_name
    validate_dataset_name(dataset_name)
    parts = parse_dataset_name(dataset_name)

    folder_path = f"{parts.user}/{args.project}" if args.project else parts.user

    return PublishRequest(
        tiff_stack=stack_path,
        dataset_name=dataset_name,
        readable_name=parts.readable_name,
        # No run directory here, so the dataset name is the record key. It is
        # unique by construction, which the stack's directory name is not —
        # every prediction tree in the world has a directory called 'labels'.
        record_key=dataset_name,
        folder_path=folder_path,
        user_team=parts.user,
        metadata_file=find_mx_cube_metadata(
            stack_path,
            parts.run_number,
            explicit=Path(args.metadata_file) if args.metadata_file else None,
        ),
        voxel_size_nm=args.voxel_size,
        token=get_auth_token(args.token),
        layer_name=args.layer_name,
        category=args.layer_type,
        max_workers=args.jobs,
        max_memory=args.max_memory,
        sampling_mode=args.sampling_mode,
        dataset_id=args.dataset_id,
        replace_legacy=args.replace_legacy,
        new_dataset=args.new_dataset,
        channel=args.channel,
        dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.manual",
        description="Publish a TIFF stack or a zarr store to WEBKNOSSOS as a zarr3 layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Hyphens throughout, matching the beamline CLI, so one set of common
    # arguments applies to every line of a batch whichever CLI it routes to.
    parser.add_argument(
        "--stack",
        type=Path,
        help="Directory holding the TIFF slices. Required, unless --zarr is given.",
    )
    parser.add_argument(
        "--zarr",
        type=Path,
        help="Publish this zarr store (plain array or OME-Zarr, v2 or v3) instead of a "
        "TIFF stack.",
    )
    parser.add_argument(
        "--channel",
        type=int,
        help="Channel of a multi-channel zarr to publish (0-based). Required when it has several.",
    )
    parser.add_argument(
        "--dataset-name",
        required=True,
        help="Dataset name, YYYYMMDD_account_SAMPLE_RUN. The account may be a proposal "
        "(p3l-eduke) or a username (jdoe). Also what finds an existing dataset, so a "
        "further layer needs nothing else to attach itself to the right one.",
    )
    parser.add_argument(
        "--voxel-size",
        type=parse_voxel_size,
        default="650,650,650",
        help="Voxel size in nm, '650' or '650,650,650' (default: 650,650,650)",
    )
    parser.add_argument(
        "--jobs",
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
        "--project",
        help="Optional subfolder under the account, e.g. Datasets/p3l-eduke/<project>",
    )
    parser.add_argument(
        "--layer-name",
        default=None,
        help="Name of the layer to write. Defaults to the dataset's readable name plus "
             f"the layer type, so MA040_1 publishes as MA040_1_{LAYER_SUFFIXES['color']} "
             f"and its segmentation as MA040_1_{LAYER_SUFFIXES['segmentation']}.",
    )
    parser.add_argument(
        "--layer-type",
        choices=sorted(LAYER_SUFFIXES),
        default="color",
        help="Layer category (default: color)",
    )
    parser.add_argument(
        "--metadata-file",
        help="MX-Cube metadata JSON, and where the publication record is written. "
        "Defaults to meta_<run>.json beside the stack.",
    )
    parser.add_argument(
        "--sampling-mode",
        choices=["anisotropic", "isotropic", "constant_z"],
        default="anisotropic",
        help="Downsampling scheme for the magnification pyramid (default: anisotropic)",
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
        "--new-dataset",
        action="store_true",
        help="Confirm that a layer named something other than the default should "
             "start a NEW dataset. Without it, publishing a non-default layer when "
             "no existing dataset was found is refused, because it would leave the "
             "layer orphaned in a dataset of its own.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without converting or transferring anything",
    )
    args = parser.parse_args(argv)

    # argparse only runs `type` on strings from the command line, not defaults.
    if isinstance(args.voxel_size, str):
        args.voxel_size = parse_voxel_size(args.voxel_size)

    log = make_logger("wbk_publish")

    try:
        request = build_request(args)
        log.info(f"Publishing {request.dataset_name}")
        publish(request, log)
        return 0
    except Exception as exc:
        log.error(f"Pipeline failed: {type(exc).__name__}: {exc}")
        log.debug("Traceback:", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
