"""Beamline path conventions: deriving and validating dataset names.

The auto-triggered pipeline never accepts a dataset name as an argument — it
derives one from the path and refuses to publish if the result does not match
the convention. That way a misfiring trigger cannot publish a mislabelled
dataset. The manual CLI can supply a name explicitly for archive data that
predates the convention.
"""

import re
from dataclasses import dataclass
from pathlib import Path

# YYYYMMDD_p3l-account_SAMPLE_RUN
DATASET_NAME_PATTERN = re.compile(r"^\d{8}_p3l-[\w]+_.+$")

_P3L_ACCOUNT = re.compile(r"^p3l-[^/]+$")
_DATE_DIR = re.compile(r"^\d{8}$")
_RUN_DIR = re.compile(r"^(stitching_\d+|recon_\d+_\d+)$")
_RECON_RUN = re.compile(r"^recon_.*_(\d+)$")


@dataclass(frozen=True)
class DatasetNameParts:
    """The pieces of a dataset name that drive folder placement and metadata lookup."""

    imaging_date: str  # 20250921
    user: str  # p3l-eduke
    readable_name: str  # MA040_1        — shown in the WEBKNOSSOS UI
    run_number: str  # 1              — selects meta_<run>.json


def dataset_name_from_path(path: Path) -> str:
    """Construct the dataset name from a beamline path.

    Expected structure: .../p3l-account/.../YYYYMMDD/<sample>/<run>/

        .../p3l-eduke/RECON/20250921/MA040/recon_111_1/
            → 20250921_p3l-eduke_MA040_1
        .../p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/
            → 20251130_p3l-eduke_tree_2x2x10_stitching_1
    """
    parts = Path(str(path).rstrip("/")).parts

    p3l_account = next((p for p in parts if _P3L_ACCOUNT.match(p)), None)
    if not p3l_account:
        raise ValueError(f"Cannot find p3l-account in path: {path}")

    date_dir = next((p for p in parts if _DATE_DIR.match(p)), None)
    if not date_dir:
        raise ValueError(f"Cannot find YYYYMMDD date component in path: {path}")

    # The run directory, and the sample directory immediately preceding it.
    date_idx = parts.index(date_dir)
    run_dir = sample_dir = None
    for i, part in enumerate(parts):
        if i > date_idx and _RUN_DIR.match(part):
            run_dir, sample_dir = part, parts[i - 1]
            break

    if not run_dir or not sample_dir:
        raise ValueError(
            f"Cannot find run directory (recon_XYZ_N or stitching_N) after the date in: {path}"
        )

    # recon_111_1 contributes only its trailing run number; stitching_1 is kept whole.
    recon_match = _RECON_RUN.match(run_dir)
    run_identifier = recon_match.group(1) if recon_match else run_dir

    return f"{date_dir}_{p3l_account}_{sample_dir}_{run_identifier}"


def validate_dataset_name(dataset_name: str) -> None:
    if not DATASET_NAME_PATTERN.match(dataset_name):
        raise ValueError(
            f"Dataset name '{dataset_name}' does not match the expected convention.\n"
            "  Expected: YYYYMMDD_p3l-account_SAMPLE_RUN\n"
            "  Check that the input path follows the beamline structure."
        )


def parse_dataset_name(dataset_name: str) -> DatasetNameParts:
    """Split a dataset name into its components.

    20250921_p3l-eduke_MA040_1            → readable_name=MA040_1, run_number=1
    20251130_p3l-eduke_tree_stitching_1   → readable_name=tree_stitching_1, run_number=1
    """
    parts = dataset_name.split("_")
    if len(parts) < 3:
        raise ValueError(f"Dataset name '{dataset_name}' has too few components")

    return DatasetNameParts(
        imaging_date=parts[0],
        user=parts[1],
        readable_name="_".join(parts[2:]),
        run_number=parts[-1],
    )


def resolve_tiff_stack(input_dir: Path) -> Path:
    """Locate the image stack inside a recon/stitching directory.

    recon_*     → input_dir/tomo/
    stitching_* → input_dir/stack/
    """
    name = input_dir.name
    if "recon" in name:
        return input_dir / "tomo"
    if "stitching" in name:
        return input_dir / "stack"
    raise ValueError(
        f"Cannot determine the image stack location from: {input_dir}\n"
        "The directory name must contain 'recon' or 'stitching'."
    )
