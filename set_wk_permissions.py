#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos==2.5.0",
# ]
# ///
"""
Assign WebKnossos permissions and attach metadata to datasets.

Usage:
    uv run set_wk_permissions.py \
        --input_dir /path/to/sample/recon_xyz_r \
        --mstar_name 20250921_p3l-miarnone_MA040_1
"""
import argparse
import os
from webknossos import RemoteFolder, Dataset, webknossos_context, Team
import json


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


'''test_metadata =  {
        'user': "p3l-miarnone",
        'operator': ' Anzhelika ASVETLOVE',
        'proposal_id': 'XIMG17',
        'timestamp' : '2025-09-21 12:53:57',
        'sample_name': 'MA040',
        'sample_run': 1,
        'magnification': '10x',
        'pixelsize' : 0.650,
        'energy_kev': 18.0,
        'exposure_time_s': 0.01,
        'number_of_images': 1800,
        'angular_range_deg': 0.1,
        'distances_m': [0.073, 0.077, 0.083, 0.092],
        'betadelta' : 0.1,
        'flux': 7.83384509979e-07,
        'tiling_x_steps': 0,
        'tiling_x_delta_mm': 0.0,
        'tiling_y_steps': 0,
        'tiling_y_delta_mm': 0.0,
        'tiling_z_steps': 0,
        'tiling_z_delta_mm': 0.0,
        'file_path': "/mnt/beegfs7/P14/2025/p3l-miarnone/20250921/RAW_DATA"
    }'''

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Assign WBK permissions and attach metadata")
    parser.add_argument('--input_dir', required=True, type=str, help='Path to the sample with either /stitching_r or /recon_xyz_r')
    parser.add_argument('--mstar_name', required=True, type=str,
                        help='Name of the sample on Morningstar either YYYYDDMM_p3l-user_SampleName_R or YYYYDDMM_p3l-user_SampleName_stitching_R')
    args = parser.parse_args()

    dataset_path = os.path.dirname(args.input_dir)  #"/mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/"
    sample_name =  args.mstar_name            #"20250921_p3l-miarnone_MA040_1"
    run_number = sample_name.split('_').pop()
    p3l_account = sample_name.split('_').pop(1) #p3l-miranone
    virtual_folder = sample_name.split('_').pop(1) # p3l-miarnone

    readable_sample_name = '_'.join(sample_name.split('_')[2:]) # MA040_1 or MA040_stitching_1

    with webknossos_context(token="sometoken", url="https://webknossos.embl-hamburg.de"):
        dataset = Dataset.open_remote(sample_name)
        print(f"Current dataset name: {dataset.name}")
        print(f"Current dataset folder: {dataset.folder}")
        # edit name to readable
        dataset.name = readable_sample_name
        # try to look for the p3l-user folder, if doesn't exist - create
        folder_path = f"Datasets/{p3l_account}"
        try:
            print(f"Looking for folder: {folder_path}")
            folder = RemoteFolder.get_by_path(folder_path)
            # folder_id = folder.id
            print(f"✓ Found existing folder: {folder_path}")
        except Exception as e:
            print(f"⚠ Folder not found: {folder_path}, creating...")
            folder = RemoteFolder.get_by_path("Datasets")
            folder.add_subfolder(p3l_account)
            folder = RemoteFolder.get_by_path(folder_path)
            # folder_id = folder.id
        # move data to the correct p3l folder
        dataset.folder = folder

        # try to look for the p3l-user team, if not found - create
        try:
            print(f"Looking for team: {p3l_account}")
            team = Team.get_by_name(name=p3l_account)
            print(f"✓ Found existing team: {p3l_account}")
        except Exception as e:
            print(f"⚠ Team not found: {p3l_account}, creating...")
            team = Team.add(p3l_account)
        # assign to the correct team
        dataset.allowed_teams = [team]
        try:
            with open(f'{dataset_path}/meta_{run_number}.json', 'r') as file:
                _md = file.read()
                # Parse JSON data into a dictionary
                pulled_metadata = json.loads(_md)
                # WBK only accepts string lists, converting distance float list to strings
                pulled_metadata['distances_m'] = [str(d) for d in pulled_metadata['distances_m']]
                readable_metadata = {key_mapping.get(k, k): v for k, v in pulled_metadata.items()}
                # add metadata to the dataset
                dataset.metadata = readable_metadata
        except Exception as e:
            print(f"⚠ Metadata not found: meta_{run_number}.json, skipping...")

