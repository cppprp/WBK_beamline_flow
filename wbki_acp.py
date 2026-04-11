#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos = 3.0.0",
# ]
# ///
'''
Usage:
uv run wbki_acp.py \
    --input_dir /mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/recon_111_1/ or
                /mnt/ximg/2025/p3l-miarnone/RECON/20250921/MA040/recon_111_1/\
    --sample_name 20250921_p3l-miarnone_MA040_1 \
    --token <WBK_TOKEN> \
    --voxel_size '650,650,650' \
    --jobs 24 \
    --project \
    >& /morningstar/logs/'${sample_name}'_convert.log &
'''

'''Need to assign the ssh key to morningstar before morningstar can log

ssh -t p3l-user@bcrunch rsync -e 'ssh -t' -avR .ssh/authorized_keys p3l-user@morningstar:
Then it will ask for a password twice'''
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
import warnings
from webknossos import RemoteFolder, Dataset, webknossos_context, Team

#warnings.simplefilter(action='ignore', category=FutureWarning)
#logging.captureWarnings(False)
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
    script_dir = Path(__file__).parent
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()

def announce_dataset(logger, ds, token):
    logger.info("Announcing dataset...")
    with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):
        ds_remote, _ =ds.upload(
            initial_team_ids=['ximg'],
            folder_id=None
        )
        logger.debug(f"Dataset ID: {ds_remote.dataset_id}")
        logger.info("✓ Dataset announced")
        return ds_remote


def run_remote_conversion(logger, input_dir, output_path, **kwargs):
    logger.info("-" * 60)
    logger.info("Starting remote conversion...")

    voxel_size = kwargs.get('voxel_size', '650,650,650')
    jobs = kwargs.get('jobs', 24)
    layer_name = kwargs.get('layer_name', 'xray')
    layer_type =kwargs.get ('layer_type', 'None')

    cmd = [
        'uvx', '--from', 'webknossos[all]', '--python', '3.12',
        'webknossos', 'convert',
        input_dir, output_path,
        '--compress', '--layer-name', layer_name,
        '--jobs', str(jobs),
        '--voxel-size', voxel_size,
        '--category', layer_type,
        '--downsample',
        '--overwrite-existing'
    ]

    logger.debug(f"Input: {input_dir}")
    logger.debug(f"Output: {output_path}")
    logger.debug(f"Converter command: {' '.join(cmd)}")
    start = time.time()
    skip_patterns = [
        'FutureWarning',
        'DeprecationWarning',
        'UserWarning'
        'site-packages',
        'plugin infrastructure',
        'return imread',
        'warnings.warn',
        'stacklevel=',
    ]
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )


        ''' for line in process.stdout:
            line = line.rstrip()
            if line:
                if any(pattern in line for pattern in skip_patterns):
                    continue
                logger.debug(f"  {line}")'''

        return_code = process.wait()

        if return_code != 0:
            logger.error(f"Remote conversion failed with return code {return_code}")
            raise RuntimeError("Remote conversion failed")
        end = time.time()
        logger.info(f"✓ Conversion completed successfully in {(end-start)/60.0}")
        return True

    except Exception as e:
        logger.error(f"Remote conversion failed: {e}")
        raise


def set_permissions(logger, sample_name, input_dir, subproject, token):
    """Set permissions and metadata.
    :param ds_remote: RemoteDataset()
    """
    logger.info("-" * 60)
    logger.info("Setting permissions and metadata...")


    # "20250921_p3l-miarnone_MA040_1" or "20250921_p3l-miarnone_MA040_stitching_1"
    dataset_path = os.path.dirname(input_dir)  # "/mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/"
    sample_name = sample_name  # "20250921_p3l-miarnone_MA040_1"
    run_number = sample_name.split('_').pop()
    p3l_account = sample_name.split('_').pop(1)  # p3l-miranone
    virtual_folder = sample_name.split('_').pop(1)  # p3l-miarnone

    readable_name = '_'.join(sample_name.split('_')[2:])  # MA040_1 or MA040_stitching_1


    dataset_path = Path(input_dir).parent

    logger.debug(f"Dataset: {sample_name}")
    logger.debug(f"Team: {p3l_account}")
    logger.debug(f"Run: {run_number}")
    logger.debug(f"Readable name: {readable_name}")
    logger.debug(f"Dataset path: {dataset_path}")

    try:
        with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):

            dataset = Dataset.open_remote(sample_name)
            logger.debug(f"Opened dataset: {dataset.name}")

            dataset.name = readable_name
            logger.debug(f"Updated name to: {readable_name}")

            # Handle folder
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
                        # Try to get existing subfolder
                        current_folder = RemoteFolder.get_by_path(f"{current_folder.name}/{part}")
                        logger.debug(f"Folder exists: {part}")
                    except Exception:
                        # Create it if it doesn't exist
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
            metadata_file = dataset_path / f'meta_{run_number}.json'
            if metadata_file.exists():
                with open(metadata_file, 'r') as f:
                    metadata = json.loads(f.read())

                    # convert distance floats to strings for WebKnossos
                    if 'distances_mm' in metadata and isinstance(metadata['distances_mm'], list):
                        metadata['distances_mm'] = [str(d) for d in metadata['distances_mm']]

                    # remap the order and the naming
                    readable_metadata = {}
                    for k in key_mapping.keys():
                        if k in metadata:
                            readable_metadata[key_mapping[k]] = metadata[k]

                    # Add any unexpected keys that weren't in key_mapping
                    for k, v in metadata.items():
                        if k not in key_mapping:
                            readable_metadata[k] = v
                            logger.debug(f"Found unmapped metadata key: {k}")

                    dataset.metadata = readable_metadata
                    logger.debug(f"Attached metadata from {metadata_file}")
            else:
                logger.debug(f"Metadata file not found: {metadata_file}")

        logger.info("✓ Permissions and metadata set successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to set permissions: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Converting a tiff stack to a WBK dataset and editing permissions and metadata")

    parser.add_argument('--input_dir', required=True,
                        help='Path to the sample up to either /stitching_r or /recon_xyz_r')
    parser.add_argument('--sample_name', required=True,
                        help='Name of the sample (e.g., YYYYDDMM_p3l-user_SampleName_R)')
    parser.add_argument('--token', required=False, help='WBK access token', default = None)
    #parser.add_argument('--remote_host', help='Remote host for conversion', default='morningstar')
    parser.add_argument('--layer_name', help='Name of the image layer displayed', default='xray')
    parser.add_argument('--voxel_size', help='Voxel size in nanometers', default='650,650,650')
    parser.add_argument('--jobs', type=int, default=24, help='Number of jobs for the conversion')
    parser.add_argument('--project', type=str, default=None, help='Optional sub project folder for the dataset')
    parser.add_argument('--layer_type', type=str, default='color', help='Specify layer type: "color" or "segmentation"')
    # Optional
    parser.add_argument('--skip_conversion', action='store_true')
    parser.add_argument('--skip_permissions', action='store_true')
    parser.add_argument('--skip_announce', action='store_false')

    args = parser.parse_args()
    #logging config
    logger = logging.getLogger(args.sample_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # everything to stdout
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    logger.addHandler(handler)
    # grab token
    if args.token is None:
        token = get_auth_token()
    else:
        token = args.token
    try:
        logger.info(f"Processing: {args.sample_name}")
        logger.debug(f"Input directory: {args.input_dir}")
        logger.debug(f"Jobs: {args.jobs}")
        logger.debug(f"Voxel size: {args.voxel_size}")

        # Check input path and see if single or stitch
        if "recon_" in args.input_dir:
            stack = args.input_dir + "/tomo/"
        elif "stitching" in args.input_dir:
            stack = args.input_dir + "/stack/"
        else:
            raise ValueError(f"Bad input path {args.input_dir} - must contain 'recon_' or 'stitching_'")

        logger.debug(f"Stack path: {stack}")

        output_path = f"/morningstar/webknossos/{args.sample_name}"

        # Announce dataset
        if not args.skip_announce:
            wbkDataset = Dataset(output_path, voxel_size=args.voxel_size)
            wbkDataset = announce_dataset(logger, wbkDataset, token)

        # Conversion
        if not args.skip_conversion:
            run_remote_conversion(logger, stack, output_path, layer_name=args.layer_name, voxel_size=args.voxel_size,
                                  jobs=args.jobs, layer_type=args.layer_type)
        time.sleep(5)
        # Permissions and metadata
        if not args.skip_permissions:
            set_permissions(logger, args.sample_name, args.input_dir, args.project, token)

        logger.info("=" * 60)
        logger.info("✓ Pipeline completed successfully")

    except Exception as e:
        logger.error(f"✗ Pipeline failed: {e}")
        logger.debug("Full traceback:", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()