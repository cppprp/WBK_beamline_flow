"""Beamline path conventions: deriving and validating dataset names.

The auto-triggered pipeline never accepts a dataset name as an argument — it
derives one from the path and refuses to publish if the result does not match
the convention. That way a misfiring trigger cannot publish a mislabelled
dataset. The manual CLI supplies a name explicitly instead.

The account component is usually a proposal (`p3l-eduke`) but may be a bare
username for staff data (`kdenisova`). Both validate; only the `p3l-` form can
be *located* in a path with certainty, so derivation prefers it and falls back
to position — see `dataset_name_from_path`.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import LAYER_SUFFIXES, STRUCTURAL_DIRS

# YYYYMMDD_account_SAMPLE_RUN, where account is a proposal (p3l-eduke) or a
# bare username (kdenisova). The date is not negotiable: it is what makes the
# directory name unique, and dataset lookup keys on it.
DATASET_NAME_PATTERN = re.compile(r"^\d{8}_[A-Za-z0-9][\w.-]*_.+$")

_P3L_ACCOUNT = re.compile(r"^p3l-[^/]+$")
# What a component has to look like to be taken for an account name by position.
# Rules out the filesystem root, which is otherwise neither structural nor a date.
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*$")
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


def account_from_parts(
    parts: tuple[str, ...], date_idx: int, path: Path
) -> tuple[str, bool]:
    """The account component of a path: a p3l- proposal, or a staff username.

    Two rules, in order, because they differ in how much they can be trusted:

      1. A component matching `p3l-*` anywhere in the path. Unambiguous — no
         other directory is named that way — and position-independent, which
         matters because the account sits two components before the date under
         RECON but immediately before it in a prediction tree.

      2. Otherwise, the nearest component before the date that is not a
         structural directory (RECON, PROC, ...). Staff data has no marker to
         look for, so position is all there is.

    Returns the account and whether rule 2 was used. Rule 2 can be wrong in a
    way rule 1 cannot, and a wrong account means a wrong team and the wrong
    people seeing the data, so the caller logs when it was. Nothing is guessed
    past the date: a path with no date component never reaches here.
    """
    p3l = next((p for p in parts if _P3L_ACCOUNT.match(p)), None)
    if p3l:
        return p3l, False

    for part in reversed(parts[:date_idx]):
        if (
            _ACCOUNT_NAME.match(part)
            and part.lower() not in STRUCTURAL_DIRS
            and not _DATE_DIR.match(part)
        ):
            return part, True

    raise ValueError(
        f"Cannot find an account component in path: {path}\n"
        "  Expected a p3l-account directory, or a username directory before the\n"
        f"  date. Everything before {parts[date_idx]!r} is a structural name.\n"
        "  Pass --dataset-name to state the dataset name yourself."
    )


def dataset_name_from_path(path: Path, log: logging.Logger | None = None) -> str:
    """Construct the dataset name from a beamline path.

    Expected structure: .../p3l-account/.../YYYYMMDD/<sample>/<run>/

        .../p3l-eduke/RECON/20250921/MA040/recon_111_1/
            → 20250921_p3l-eduke_MA040_1
        .../p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/
            → 20251130_p3l-eduke_tree_2x2x10_stitching_1
        .../2025/kdenisova/20250826/NBG25/recon_111_1/
            → 20250826_kdenisova_NBG25_1        (account found by position)

    Finding the account is the only guesswork here, and it is kept to a
    minimum. A `p3l-` component is unmistakable wherever it sits, and its
    position varies in practice — two components before the date under RECON,
    one before it in a prediction tree — so it is matched by prefix, not
    position. Only a path with no such component falls back to position, and
    that fallback is announced in the log, because an account name decides the
    team and therefore who can see the dataset.
    """
    parts = Path(str(path).rstrip("/")).parts

    date_dir = next((p for p in parts if _DATE_DIR.match(p)), None)
    if not date_dir:
        raise ValueError(f"Cannot find YYYYMMDD date component in path: {path}")

    date_idx = parts.index(date_dir)
    account, by_position = account_from_parts(parts, date_idx, path)
    if by_position and log is not None:
        log.info(
            f"Account taken by position (no p3l- component in the path): {account}\n"
            "  It becomes the team name, which decides who can open the dataset — "
            "check it."
        )

    # The run directory, and the sample directory immediately preceding it.
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

    return f"{date_dir}_{account}_{sample_dir}_{run_identifier}"


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


def default_layer_name(readable_name: str, category: str) -> str:
    """The layer name used when none is given: the dataset's name, then the type.

        MA040_1  + color         → MA040_1_xray
        MA040_1  + segmentation  → MA040_1_segmentation

    A layer named after its dataset says which sample it is wherever it appears
    on its own — in the layer list of a dataset that has several, in a shared
    URL, in a figure panel. A bare 'xray' says only what modality it was.

    The type has to be in there too. Without it a segmentation published with no
    explicit name inherited the tomogram's word and became `MA040_1_xray`, which
    the orphan-layer guard then waved through because it *was* the default.
    """
    return f"{readable_name}_{LAYER_SUFFIXES.get(category, category)}"


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
