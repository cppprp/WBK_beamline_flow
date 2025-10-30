#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos==2.5.0",
# ]
# ///
'''
Usage:
uv run wbk_convert_set_permissions.py \
    --input_dir /mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/recon_111_1/ \
    --output_name 20250921_p3l-miarnone_MA040_1 \
    --token <WBK_TOKEN> \
    --voxel_size '650,650,650' \
    --remote_host morningstar \
    --jobs 24
'''
import argparse
import json
import os
import subprocess
import sys

from webknossos import RemoteFolder, Dataset, webknossos_context, Team
from script_logger import setup_logger, log_section, log_dict

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
    'distances_m': 'Sample to Detector Distances (m)',
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


def run_remote_conversion(logger,  input_dir, output_path, remote_host, **kwargs):
    """Run conversion on remote host via SSH."""
    log_section(logger, "Converting", separator="-")

    # pull arguments here
    voxel_size = kwargs.get('voxel_size', '650,650,650')
    jobs = kwargs.get('jobs', 24)
    layer_name = kwargs.get('layer_name', 'xray')

    remote_cmd = (
        f'uvx --from "webknossos[all]" --python 3.12 webknossos convert '
        f'{input_dir} {output_path} '
        f'--compress --layer-name {layer_name} --jobs {jobs} '
        f'--voxel-size {voxel_size} --downsample'
    )

    ssh_cmd = ['ssh', '-t', remote_host, 'time', remote_cmd]

    # Log details (DEBUG - file only)
    logger.debug(f"Input: {input_dir}")
    logger.debug(f"Output: {output_path}")
    logger.debug(f"SSH command: {' '.join(ssh_cmd)}")

    try:
        process = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        # All conversion output goes to DEBUG (file only)
        for line in process.stdout:
            line = line.rstrip()
            if line:
                logger.debug(f"  {line}")

        return_code = process.wait()

        if return_code != 0:
            logger.error(f"Remote conversion failed with return code {return_code}")
            raise RuntimeError("Remote conversion failed")

        logger.info("✓ Conversion completed successfully")
        return True

    except Exception as e:
        logger.error(f"Remote conversion failed: {e}")
        raise


def set_permissions(logger, sample_name, input_dir, token):
    """Set permissions and metadata."""
    log_section(logger, "Setting permissions and attaching metadata", separator="-")

    run_number = sample_name.split('_').pop()
    p3l_account = sample_name.split('_').pop(1) # p3l-miarnone
    virtual_folder = sample_name.split('_').pop(1) # p3l-miarnone
    readable_name = '_'.join(sample_name.split('_')[2:]) #"20250921_p3l-miarnone_MA040_1" or "20250921_p3l-miarnone_MA040_stitching_1"
    dataset_path = os.path.dirname(input_dir)  # "/mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/"

    logger.debug(f"Dataset: {sample_name}")
    logger.debug(f"Team: {p3l_account}")
    logger.debug(f"Virtual folder: {virtual_folder}")
    logger.debug(f"Run: {run_number}")
    logger.debug(f"Readable name: {readable_name}")

    logger.info("Setting permissions and metadata...") #  <----  console

    try:
        with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):

            # try to open remote
            dataset = Dataset.open_remote(sample_name)
            logger.debug(f"Opened dataset: {dataset.name}")

            # update name to readable
            dataset.name = readable_name
            logger.debug(f"Updated name to: {readable_name}")

            # look for existing folder with p3l-user name, if doesn't exist -create
            folder_path = f"Datasets/{p3l_account}"
            try:
                folder = RemoteFolder.get_by_path(folder_path)
                logger.debug(f"Found folder: {folder_path}")
            except Exception:
                logger.debug(f"Creating folder: {folder_path}")
                folder = RemoteFolder.get_by_path("Datasets")
                folder.add_subfolder(p3l_account)
                folder = RemoteFolder.get_by_path(folder_path)
            # reassign dataset to p3l-user folder
            dataset.folder = folder

            # look for existing team with p3l-user name, if doesn'T exist - create
            try:
                team = Team.get_by_name(name=p3l_account)
                logger.debug(f"Found team: {p3l_account}")
            except Exception:
                logger.debug(f"Creating team: {p3l_account}")
                team = Team.add(p3l_account)
            # assign dataset to team
            dataset.allowed_teams = [team]

            # look for meta_<RUN_NUMBER>.json in the sample folder
            metadata_file = dataset_path / f'meta_{run_number}.json'
            if metadata_file.exists():
                with open(metadata_file, 'r') as f:
                    metadata = json.loads(f.read())
                    # WBK only accepts string lists, converting distance float list to strings
                    if 'distances_m' in metadata and isinstance(metadata['distances_m'], list):
                        metadata['distances_m'] = [str(d) for d in metadata['distances_m']]
                    # reassign the dict keys to prettier/ informative ones
                    readable_metadata = {key_mapping.get(k, k): v for k, v in metadata.items()}
                    #attach metadata
                    dataset.metadata = readable_metadata
                    logger.debug(f"Attached metadata")
            else:
                logger.debug(f"Metadata file not found: {metadata_file}")


        logger.info("✓ Permissions and metadata set successfully") # <----- console
        return True

    except Exception as e:
        logger.error(f"Failed to resolve context: {e}") # <----- console
        raise


def main():
    parser = argparse.ArgumentParser(description="Converting a tiff stack to a WBK dataset and editing permissions an metadata")

    # Required
    parser.add_argument('--input_dir', required=True, help='Path to the sample up to either /stitching_r or /recon_xyz_r')
    parser.add_argument('--sample_name', required=True, help='Name of the sample on Morningstar either YYYYDDMM_p3l-user_SampleName_R or YYYYDDMM_p3l-user_SampleName_stitching_R')
    parser.add_argument('--token', required=True, help='WBK access token')
    parser.add_argument('--remote_host', help='Remote host for conversion', default = 'morningstar')
    parser.add_argument('--layer_name',  help='Name of the image layer displayed', default='xray')
    parser.add_argument('--voxel_size', help='Voxel size in nanometers', default='650,650,650')
    parser.add_argument('--jobs', type=int, default=24, help='Number of jobs for the conversion')

    # Optional general
    parser.add_argument('--log_dir', default='./logs')
    parser.add_argument('--skip_conversion', action='store_true')
    parser.add_argument('--skip_permissions', action='store_true')

    args = parser.parse_args()



    # Setup logging with DEBUG to log file, INFO to console
    logger = setup_logger(
        f'wk_pipeline_{args.output_name}',
        args.log_dir,
        console_level=20,  # INFO (only major messages)
        file_level=10  # DEBUG (everything)
    )

    try:
        logger.debug(f"Input directory: {args.input_dir}")
        logger.debug(f"Output name: {args.output_name}")
        logger.debug(f"Jobs: {args.jobs}")
        logger.debug(f"Voxel size: {args.voxel_size}")
        # check input path
        if "recon_" in args.input_dir:
            stack = args.input_dir + "/tomo/"
        elif "stitching_" in args.input_dir:
            stack = args.input_dir + "/stack/"
        else:
            raise Exception(f"Bad input path {args.input_dir}")
        # conversion unless don't want to
        if not args.skip_conversion:
            output_path = f"/morningstar/webknossos/{args.output_name}"
            jobs = args.jobs
            run_remote_conversion(logger, stack, output_path, args.remote_host, layer_name=args.layer_name,
                                  voxel_size=args.voxel_size, jobs=jobs)

        # attach permissions and meta unless not
        if not args.skip_permissions:
            set_permissions(logger, args.output_name, args.input_dir, args.token)

        logger.info("-" * 60)
        logger.info("✓ Pipeline completed successfully")

    except Exception as e:
        logger.error(f"✗ Pipeline failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()