"""Manual and batch publishing — the operator-driven path.

Same pipeline as the automatic beamline CLI, but everything the beamline path
fixes is adjustable here: layer name, layer type, voxel size, project folder,
and the dataset name itself for archive data that predates the naming
convention.

    python -m wbk_publish.manual --input_dir /mnt/ximg/2025/p3l-eduke/RECON/20250921/MA040/stitching_1 \
        --voxel_size 325,325,325 --project cardiac_study

    python -m wbk_publish.manual --input_dir /archive/2019/batch_5 \
        --stack_path /archive/2019/batch_5/tiffs \
        --sample_name 20190501_p3l-archive_exp5_1 \
        --layer_type segmentation --layer_name predictions --new-dataset

Like the beamline CLI, re-running on the same input replaces that layer on the
dataset published before and keeps the dataset id.
"""

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_LAYER_SUFFIX, get_auth_token
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
    input_dir = Path(str(args.input_dir).rstrip("/"))

    # An explicit name is the escape hatch for paths that do not follow the
    # beamline layout; otherwise derive it the same way the beamline CLI does.
    if args.sample_name:
        dataset_name = args.sample_name
    else:
        try:
            dataset_name = dataset_name_from_path(input_dir)
        except ValueError as exc:
            # Most often this is someone pointing --input_dir at a directory
            # that is not a run directory — a segmentation output, say. The
            # derivation cannot work there, but --sample_name makes it moot.
            raise ValueError(
                f"{exc}\n"
                "  Pass --sample_name to state the dataset name yourself, in the\n"
                "  form YYYYMMDD_p3l-account_SAMPLE_RUN. That is what it is for:\n"
                "  --input_dir only has to follow the beamline layout when the\n"
                "  name is being derived from it."
            ) from exc
    validate_dataset_name(dataset_name)
    parts = parse_dataset_name(dataset_name)

    stack_path = Path(args.stack_path) if args.stack_path else resolve_tiff_stack(input_dir)

    folder_path = f"{parts.user}/{args.project}" if args.project else parts.user

    return PublishRequest(
        input_dir=input_dir,
        tiff_stack=stack_path,
        dataset_name=dataset_name,
        readable_name=parts.readable_name,
        folder_path=folder_path,
        user_team=parts.user,
        metadata_file=find_mx_cube_metadata(
            input_dir,
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
        dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.manual",
        description="Publish a TIFF stack to WEBKNOSSOS as a zarr3 layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Both spellings are accepted for every flag. The underscore forms came
    # first and existing triggers use them; the hyphenated ones match the
    # beamline CLI, so a batch can apply one set of common arguments to lines
    # of either kind.
    parser.add_argument(
        "--input_dir", "--input-dir",
        required=True,
        type=Path,
        help="The recon or stitching directory. Its name keys the publication record in the metadata file.",
    )
    parser.add_argument(
        "--stack_path", "--stack-path",
        help="Directory holding the TIFF slices. Derived from --input_dir when omitted.",
    )
    parser.add_argument(
        "--sample_name", "--sample-name",
        help="Dataset name (YYYYMMDD_p3l-account_SAMPLE_RUN). Derived from the path when omitted.",
    )
    parser.add_argument(
        "--voxel_size", "--voxel-size",
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
        "--layer_name", "--layer-name",
        default=None,
        help="Name of the layer to write. Defaults to the dataset's readable name "
             f"followed by '_{DEFAULT_LAYER_SUFFIX}', so MA040_1 publishes as "
             f"MA040_1_{DEFAULT_LAYER_SUFFIX}. Give a name only for a further layer, "
             "such as a segmentation.",
    )
    parser.add_argument(
        "--layer_type", "--layer-type",
        choices=["color", "segmentation"],
        default="color",
        help="Layer category (default: color)",
    )
    parser.add_argument(
        "--metadata_file", "--metadata-file",
        help="MX-Cube metadata JSON. Defaults to meta_<run>.json beside the sample directory.",
    )
    parser.add_argument(
        "--sampling_mode", "--sampling-mode",
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
