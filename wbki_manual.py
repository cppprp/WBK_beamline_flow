#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos==3.0.4",
# ]
# ///
'''
Manual WebKnossos converter for datasets with non-standard paths.
Allows explicit specification of all parameters without path validation.

Sample Name Construction:
The script can extract sample names from standard path structures OR accept them explicitly.

Path extraction looks for these components:
  - p3l-<account> (e.g., p3l-eduke)
  - YYYYMMDD date (8 digits)
  - Sample directory name
  - Run directory (stitching_N or recon_XYZ_N)

Example path → sample name:
  /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack/
  → 20251130_p3l-eduke_tree_2x2x10_stitching_1

  /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/recon_111_1/tomo/
  → 20251130_p3l-eduke_tree_2x2x10_1  (extracts just final number from recon)

Usage:
# Auto-extract from path
uv run wbki_manual.py \
    --stack_path /mnt/ximg/2025/p3l-eduke/RECON/20251130/sample/stitching_1/stack \
     >& /morningstar/logs/'${sample_name}'_convert.log &

# Explicit sample name (for non-standard paths)
uv run wbki_manual.py \
    --stack_path /weird/backup/old_data/tiffs \
    --sample_name 20250921_p3l-user_sample_1 \
    --voxel_size '650,650,650' \
    --jobs 24
    | & tee /morningstar/logs/sample_name_convert.log

Optional:
    --token <WBK_TOKEN> (reads from .config/auth_token.txt if not provided)
    --project <subfolder> (creates subfolder under p3l_account)
    --layer_name xray (default: xray)
    --layer_type color (default: color, options: color, segmentation)
    --metadata_file /path/to/meta.json (optional metadata file)
    --skip_conversion (skip conversion step)
    --skip_permissions (skip permissions/metadata step)
    --skip_announce (skip dataset announcement)
'''
import os
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['NUMEXPR_NUM_THREADS'] = '1'

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
import warnings
from webknossos import RemoteFolder, Dataset, webknossos_context, Team

warnings.simplefilter(action='ignore', category=FutureWarning)
logging.captureWarnings(False)

key_mapping = {
    'user': 'User',
    'operator': 'Operator',
    'proposal_id': 'Proposal ID',
    'timestamp': 'Timestamp',
    'sample_name': 'Sample Name',
    'sample_run': 'Sample Run',
    'magnification': 'Magnification',
    'pixelsize': 'Pixel Size (µm)',
    'energy_kev': 'Energy (keV)',
    'exposure_time_s': 'Exposure Time (s)',
    'number_of_images': 'Number of Projections',
    'angular_range_deg': 'Angular Range (°)',
    'distances_mm': 'Sample to Detector Distances (m)',
    'betadelta': 'Beta/Delta Ratio',
    'mode': 'Acquisition Mode',
    'flux': 'Beam Intensity (A)',
    'tiling_x_steps': 'Tiling X Steps',
    'tiling_x_delta_mm': 'Tiling X Delta (mm)',
    'tiling_y_steps': 'Tiling Y Steps',
    'tiling_y_delta_mm': 'Tiling Y Delta (mm)',
    'tiling_z_steps': 'Tiling Z Steps',
    'tiling_z_delta_mm': 'Tiling Z Delta (mm)',
    'file_path': 'Path to Raw Data'
}


def get_auth_token():
    """Read authentication token from config file."""
    script_dir = Path(__file__).parent
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()


def validate_sample_name(sample_name):
    """
    Validate sample name format and extract components.

    Expected format: YYYYMMDD_p3l-account_SampleName_RunIdentifier
    Examples:
        20250921_p3l-miarnone_MA040_1
        20250921_p3l-miarnone_MA040_stitching_1

    Returns:
        tuple: (date, p3l_account, readable_name, run_number) or None if invalid
    """
    # Pattern: YYYYMMDD_p3l-account_anything
    # Must have at least 3 underscore-separated components
    parts = sample_name.split('_')
    if len(parts) < 3:
        return None

    # First part must be 8-digit date
    if not re.match(r'^\d{8}$', parts[0]):
        return None

    # Second part must be p3l-account
    if not re.match(r'^p3l-[^_]+$', parts[1]):
        return None

    date = parts[0]
    p3l_account = parts[1]

    # Readable name is everything from index 2 onwards
    readable_name = '_'.join(parts[2:])

    # Run identifier is the last component
    run_identifier = parts[-1]

    return (date, p3l_account, readable_name, run_identifier)


def extract_sample_name_from_path(path):
    """
    Extract sample name from path by finding and combining components.
    Mirrors the bash extract_sample_name function logic.

    Path structure expected:
        /some/base/path/p3l-account/.../YYYYMMDD/sample_dir/run_dir/...

    Examples:
        /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack/
        -> 20251130_p3l-eduke_tree_2x2x10_stitching_1

        /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/recon_111_1/tomo/
        -> 20251130_p3l-eduke_tree_2x2x10_1

    Returns:
        str: Constructed sample_name or None if extraction fails
    """
    path = str(path).rstrip('/')
    parts = Path(path).parts

    # Find p3l-account
    p3l_account = None
    for part in parts:
        if re.match(r'^p3l-[^/]+$', part):
            p3l_account = part
            break

    if not p3l_account:
        return None

    # Find date directory (8 digits)
    date_dir = None
    for part in parts:
        if re.match(r'^\d{8}$', part):
            date_dir = part
            break

    if not date_dir:
        return None

    # Find the run directory and sample directory
    # Run dir should be something like "stitching_1" or "recon_111_1"
    # Sample dir is the one before run_dir
    run_dir = None
    sample_dir = None

    for i, part in enumerate(parts):
        # Check if this looks like a run directory
        if re.match(r'^(stitching_\d+|recon_\d+_\d+)$', part):
            run_dir = part
            # Sample dir should be the one before this (if exists and after date)
            if i > 0:
                potential_sample = parts[i - 1]
                # Make sure we're after the date directory
                try:
                    date_idx = parts.index(date_dir)
                    if i > date_idx:
                        sample_dir = potential_sample
                except ValueError:
                    pass
            break

    if not run_dir or not sample_dir:
        return None

    # Handle run_identifier based on type (recon vs stitching)
    # For recon_111_1, extract just the final number (1)
    # For stitching_1, keep the whole thing (stitching_1)
    recon_match = re.match(r'^recon_.*_(\d+)$', run_dir)
    if recon_match:
        run_identifier = recon_match.group(1)
    else:
        run_identifier = run_dir

    # Construct sample name
    sample_name = f"{date_dir}_{p3l_account}_{sample_dir}_{run_identifier}"

    return sample_name

def is_zarr_directory(p) -> bool:
    """Check if path is a Zarr directory."""
    path = Path(p)
    return (path / '.zarray').exists() or (path / 'zarr.json').exists()


def extract_run_number(sample_name):
    """
    Extract run number (single integer) from sample name for metadata lookup.

    Run number is ALWAYS a single integer, regardless of acquisition type.
    - stitching_1 → run number: 1
    - recon_111_1 → run number: 1
    - sample_5 → run number: 5

    Metadata files are named: meta_{run_number}.json (e.g., meta_1.json)

    Examples:
        20251130_p3l-eduke_tree_2x2x10_stitching_1 → "1"
        20250101_p3l-user_sample_1 → "1"
        20250921_p3l-user_test_5 → "5"

    Returns:
        str: Run number (single integer) or None if cannot extract
    """
    parts = sample_name.split('_')
    if len(parts) < 3:
        return None

    # Last component should contain the run number
    last_part = parts[-1]

    # Try to extract integer from last part
    # Could be: "1", "5", "stitching_1", etc.
    match = re.search(r'(\d+)$', last_part)
    if match:
        return match.group(1)

    return None

# additing random comment
def announce_dataset(logger, ds, token):
    """Announce dataset to WebKnossos."""
    logger.info("Announcing dataset...")
    with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):
        ds_remote, _ = ds.upload(
            initial_team_ids=['ximg'],
            folder_id=None
        )
        logger.debug(f"Dataset ID: {ds_remote.dataset_id}")
        logger.info("✓ Dataset announced")
        return ds_remote


def run_remote_conversion(logger, stack_path, output_path, **kwargs):
    """Run the WebKnossos conversion."""
    logger.info("-" * 60)
    logger.info("Starting remote conversion...")

    voxel_size = kwargs.get('voxel_size', '650,650,650')
    jobs = kwargs.get('jobs', 24)
    layer_name = kwargs.get('layer_name', 'xray')
    layer_type = kwargs.get('layer_type', 'color')
    # attempt to find the current python interpreter
    python_exe = sys.executable
    if is_zarr_directory(stack_path):
        cmd = [
            #'uvx', '--from', 'webknossos[all]', '--python', '3.12',
            'webknossos', 'convert-zarr',
            stack_path, output_path,
            '--voxel-size', voxel_size,
            '--layer-name', layer_name,
            '--jobs', str(jobs),
            '--is-segmentation-layer' if layer_type == 'segmentation' else '',
            '--compress',
            '--downsample',
            '--overwrite-existing',
            '--sampling-mode', 'isotropic']
    else:
        cmd = [
            #'uvx', '--from', 'webknossos[all]', '--python', '3.12',
            'webknossos', 'convert',
            stack_path, output_path,
            '--compress', '--layer-name', layer_name,
            '--jobs', str(jobs),
            '--voxel-size', voxel_size,
            '--category', layer_type,
            '--downsample',
            '--overwrite-existing',
            '--sampling-mode', 'isotropic'
        ]

    logger.debug(f"Stack path: {stack_path}")
    logger.debug(f"Output: {output_path}")
    logger.debug(f"Converter command: {' '.join(cmd)}")

    start = time.time()
    skip_patterns = [
        'FutureWarning',
        'DeprecationWarning',
        'UserWarning',
        'site-packages',
        'plugin infrastructure',
        'return imread',
        'warnings.warn',
        'stacklevel=',
    ]
    subprocess_env = os.environ.copy()
    subprocess_env.update({
        'OPENBLAS_NUM_THREADS': '1',
        'MKL_NUM_THREADS': '1',
        'OMP_NUM_THREADS': '1',
        'NUMEXPR_NUM_THREADS': '1',
        'VECLIB_MAXIMUM_THREADS': '1',
        'NUMBA_NUM_THREADS': '1',
    })

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env =subprocess_env
        )

        for line in process.stdout:
            line = line.rstrip()
            if line:
                if any(pattern in line for pattern in skip_patterns):
                    continue
                logger.debug(f"  {line}")

        return_code = process.wait()

        if return_code != 0:
            logger.error(f"Remote conversion failed with return code {return_code}")
            raise RuntimeError("Remote conversion failed")

        end = time.time()
        logger.info(f"✓ Conversion completed successfully in {(end - start) / 60.0:.1f} minutes")
        return True

    except Exception as e:
        logger.error(f"Remote conversion failed: {e}")
        raise


def set_permissions(logger, sample_name, sample_info, stack_path, subproject, metadata_file, token):
    """
    Set permissions and metadata for the dataset.

    Args:
        sample_info: tuple of (date, p3l_account, readable_name, run_identifier)
        stack_path: Path to stack (used to find metadata if not explicitly provided)
        metadata_file: Explicit metadata path or None (will auto-construct)
    """
    logger.info("-" * 60)
    logger.info("Setting permissions and metadata...")

    date, p3l_account, readable_name, run_identifier = sample_info

    # Extract run number (single integer) for metadata lookup
    run_number = extract_run_number(sample_name)

    logger.debug(f"Dataset: {sample_name}")
    logger.debug(f"Team: {p3l_account}")
    logger.debug(f"Run identifier (in name): {run_identifier}")
    logger.debug(f"Run number (for metadata): {run_number}")
    logger.debug(f"Readable name: {readable_name}")

    try:
        with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):
            dataset = Dataset.open_remote(sample_name)
            logger.debug(f"Opened dataset: {dataset.name}")

            dataset.name = readable_name
            logger.debug(f"Updated name to: {readable_name}")

            # Handle folder creation
            if subproject is None:
                folder_path = f"{p3l_account}"
            else:
                folder_path = f"{p3l_account}/{subproject}"

            try:
                folder = RemoteFolder.get_by_path(f"Datasets/{folder_path}")
                logger.debug(f"Found folder: Datasets/{folder_path}")
            except Exception:
                logger.debug(f"Creating folder path: {folder_path}")

                # Start from Datasets folder
                current_folder = RemoteFolder.get_by_path("Datasets")

                # Create each level one at a time
                for part in folder_path.split('/'):
                    try:
                        current_folder = RemoteFolder.get_by_path(f"{current_folder.name}/{part}")
                        logger.debug(f"Folder exists: {part}")
                    except Exception:
                        logger.debug(f"Creating subfolder: {part}")
                        current_folder = current_folder.add_subfolder(part)

                folder = current_folder

            dataset.folder = folder

            # Handle team
            try:
                team = Team.get_by_name(name=p3l_account)
                logger.debug(f"Found team: {p3l_account}")
            except Exception:
                logger.debug(f"Creating team: {p3l_account}")
                team = Team.add(p3l_account)

            dataset.allowed_teams = [team]

            # Handle metadata
            # If no explicit metadata file provided, try to construct from stack_path
            if metadata_file is None and run_number:
                # Construct path: parent of stack_path + meta_{run_number}.json
                dataset_path = Path(stack_path).parent
                constructed_metadata = dataset_path / f'meta_{run_number}.json'
                if constructed_metadata.exists():
                    metadata_file = str(constructed_metadata)
                    logger.debug(f"Auto-constructed metadata path: {metadata_file}")

            if metadata_file and Path(metadata_file).exists():
                with open(metadata_file, 'r') as f:
                    metadata = json.loads(f.read())

                    # Convert distance floats to strings for WebKnossos
                    if 'distances_mm' in metadata and isinstance(metadata['distances_mm'], list):
                        metadata['distances_mm'] = [str(d) for d in metadata['distances_mm']]

                    # Try to remap using key_mapping (for beamline format)
                    readable_metadata = {}
                    keys_remapped = 0
                    for k in key_mapping.keys():
                        if k in metadata:
                            readable_metadata[key_mapping[k]] = metadata[k]
                            keys_remapped += 1

                    # Add any unexpected keys that weren't in key_mapping
                    for k, v in metadata.items():
                        if k not in key_mapping:
                            readable_metadata[k] = v

                    # If no keys matched key_mapping, use original metadata as-is
                    # (this means it's not in beamline format)
                    if keys_remapped == 0:
                        logger.debug("Metadata doesn't match beamline format, using as-is")
                        dataset.metadata = metadata
                    else:
                        logger.debug(f"Metadata remapped {keys_remapped} keys using key_mapping")
                        dataset.metadata = readable_metadata

                    logger.debug(f"Attached metadata from {metadata_file}")
            else:
                if metadata_file:
                    logger.debug(f"Metadata file not found: {metadata_file}")
                else:
                    logger.debug("No metadata file found")

        logger.info("✓ Permissions and metadata set successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to set permissions: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Manual WebKnossos converter - flexible for non-standard paths",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:

  # Auto-extract from standard path structure
  uv run wbki_manual.py \\
      --stack_path /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack \\
      --voxel_size 650,650,650
  # Extracts: 20251130_p3l-eduke_tree_2x2x10_stitching_1

  # Auto-extract from recon path (extracts just final number)
  uv run wbki_manual.py \\
      --stack_path /mnt/ximg/2025/p3l-user/RECON/20250101/sample/recon_111_1/tomo \\
      --voxel_size 650,650,650
  # Extracts: 20250101_p3l-user_sample_1

  # Explicit sample name for non-standard paths
  uv run wbki_manual.py \\
      --stack_path /weird/backup/old_data/tiffs \\
      --sample_name 20250921_p3l-user_sample_1 \\
      --voxel_size 650,650,650

  # With metadata and project folder
  uv run wbki_manual.py \\
      --stack_path /archive/data/tiffs \\
      --sample_name 20180920_p3l-archive_bone_1 \\
      --voxel_size 650,650,650 \\
      --project cardiac_archive \\
      --metadata_file /archive/meta.json

Path extraction looks for:
  - p3l-<account> component
  - YYYYMMDD date (8 digits)  
  - Sample directory
  - Run directory (stitching_N or recon_XYZ_N)

Sample name format: YYYYMMDD_p3l-account_SampleName_RunIdentifier
        """
    )

    # Required arguments
    parser.add_argument('--stack_path', required=True,
                        help='Direct path to the tiff stack (no path validation or appending)')

    # Sample name - can be auto-extracted
    parser.add_argument('--sample_name', default=None,
                        help='Full sample name in format YYYYMMDD_p3l-account_SampleName_R (auto-extracted from path if not provided)')

    # Optional with defaults
    parser.add_argument('--voxel_size', default='650,650,650',
                        help='Voxel size in nanometers (default: 650,650,650)')
    parser.add_argument('--token', default=None,
                        help='WebKnossos access token (reads from .config/auth_token.txt if not provided)')
    parser.add_argument('--layer_name', default='xray',
                        help='Name of the image layer (default: xray)')
    parser.add_argument('--layer_type', default='color',
                        help='Layer type: "color" or "segmentation" (default: color)')
    parser.add_argument('--jobs', type=int, default=24,
                        help='Number of parallel jobs for conversion (default: 24)')
    parser.add_argument('--project', default=None,
                        help='Optional sub-project folder under p3l_account')
    parser.add_argument('--metadata_file', default=None,
                        help='Optional path to metadata JSON file (auto-constructed if not provided)')

    # Skip flags
    parser.add_argument('--skip_conversion', action='store_true',
                        help='Skip the conversion step')
    parser.add_argument('--skip_permissions', action='store_true',
                        help='Skip the permissions/metadata step')
    parser.add_argument('--skip_announce', action='store_false',
                        help='Skip the dataset announcement step')

    args = parser.parse_args()

    # Validate stack path exists
    if not Path(args.stack_path).exists():
        print(f"Error: Stack path does not exist: {args.stack_path}")
        sys.exit(1)

    # Get or extract sample name
    if args.sample_name:
        sample_name = args.sample_name
        print(f"Using provided sample name: {sample_name}")
    else:
        print(f"Attempting to extract sample name from path: {args.stack_path}")
        sample_name = extract_sample_name_from_path(args.stack_path)
        if not sample_name:
            print(f"\nError: Could not extract sample name from path: {args.stack_path}")
            print("\nExpected path structure should contain:")
            print("  - p3l-<account> (e.g., p3l-eduke)")
            print("  - YYYYMMDD date directory (8 digits)")
            print("  - Sample directory (e.g., tree_2x2x10)")
            print("  - Run directory (e.g., stitching_1 or recon_111_1)")
            print("\nExample path:")
            print("  /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack/")
            print("  Would extract: 20251130_p3l-eduke_tree_2x2x10_stitching_1")
            print("\nPlease provide --sample_name explicitly in format:")
            print("  YYYYMMDD_p3l-account_SampleName_RunIdentifier")
            sys.exit(1)
        print(f"Extracted sample name: {sample_name}")

    # Validate sample name format
    sample_info = validate_sample_name(sample_name)
    if not sample_info:
        print(f"\nError: Sample name does not match required format: {sample_name}")
        print("\nExpected format: YYYYMMDD_p3l-account_SampleName_RunIdentifier")
        print("Examples:")
        print("  - 20250921_p3l-miarnone_MA040_1")
        print("  - 20250921_p3l-user_test_stitching_1")
        print("\nComponents:")
        print("  YYYYMMDD: Date (8 digits)")
        print("  p3l-account: P3L account identifier (no underscores)")
        print("  SampleName_RunIdentifier: Everything else (at least one more component)")
        sys.exit(1)

    date, p3l_account, readable_name, run_number = sample_info

    # Setup logging
    logger = logging.getLogger(sample_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)

    # Get token
    if args.token is None:
        token = get_auth_token()
    else:
        token = args.token

    try:
        logger.info(f"Processing: {sample_name}")
        logger.debug(f"Date: {date}")
        logger.debug(f"P3L account: {p3l_account}")
        logger.debug(f"Readable name: {readable_name}")
        logger.debug(f"Run number: {run_number}")
        logger.debug(f"Stack path: {args.stack_path}")
        logger.debug(f"Jobs: {args.jobs}")
        logger.debug(f"Voxel size: {args.voxel_size}")
        logger.debug(f"Layer type: {args.layer_type}")
        if args.project:
            logger.debug(f"Project folder: {args.project}")
        if args.metadata_file:
            logger.debug(f"Metadata file: {args.metadata_file}")

        output_path = f"/morningstar/webknossos/{sample_name}"

        # Announce dataset
        if not args.skip_announce:
            wbkDataset = Dataset(output_path, voxel_size=args.voxel_size)
            wbkDataset = announce_dataset(logger, wbkDataset, token)

        # Conversion
        if not args.skip_conversion:
            run_remote_conversion(
                logger,
                args.stack_path,
                output_path,
                layer_name=args.layer_name,
                voxel_size=args.voxel_size,
                jobs=args.jobs,
                layer_type=args.layer_type
            )

        time.sleep(5)

        # Permissions and metadata
        if not args.skip_permissions:
            set_permissions(
                logger,
                sample_name,
                sample_info,
                args.stack_path,
                args.project,
                args.metadata_file,
                token
            )

        logger.info("=" * 60)
        logger.info("✓ Pipeline completed successfully")

    except Exception as e:
        logger.error(f"✗ Pipeline failed: {e}")
        logger.debug("Full traceback:", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()