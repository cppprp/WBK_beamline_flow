import os
from importlib.metadata import metadata

import webknossos as wk
from webknossos import RemoteFolder, Dataset, webknossos_context, Team
import json
import fnmatch

dataset_path = "/mnt/beegfs7/P14/2025/p3l-miarnone/RECON/20250921/MA040/"
sample_name = "20250921_p3l-miarnone_MA040_1"
terms = dataset_path.split('/')
readable_sample_name = '_'.join(sample_name.split('_')[2:])
#TODO figure out where to pull the metadata from and under which criteria
virtual_folder = terms[5]
p3l_account = terms[5]
fnmatch.filter(os.listdir('.'), 'meta*.json')
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
        #folder_id = folder.id
        print(f"✓ Found existing folder: {folder_path}")
    except Exception as e:
        print(f"⚠ Folder not found: {folder_path}, creating...")
        folder = RemoteFolder.get_by_path("Datasets")
        folder.add_subfolder(p3l_account)
        folder = RemoteFolder.get_by_path(folder_path)
        #folder_id = folder.id
    # move data to the correct p3l folder
    dataset.folder = folder

    #existing_teams = Team.get_list()
    #print(existing_teams)
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

    with open('data.json', 'r') as file:
        data = file.read()
    # Parse JSON data into a dictionary
    parsed_data = json.loads(data)
    test_metadata =  {
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
    }
    # WBK only accepts string lists, converting distance float list to strings
    test_metadata['distances_m'] = [str(d) for d in test_metadata['distances_m']]

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
        'flux': 'Beam Intensity (A)',  # Adjust unit if needed
        'tiling_x_steps': 'Tiling X Steps',
        'tiling_x_delta_mm': 'Tiling X Delta (mm)',
        'tiling_y_steps': 'Tiling Y Steps',
        'tiling_y_delta_mm': 'Tiling Y Delta (mm)',
        'tiling_z_steps': 'Tiling Z Steps',
        'tiling_z_delta_mm': 'Tiling Z Delta (mm)',
        'file_path': 'Path to Raw Data'
    }
    # remap metadata to readable
    readable_metadata = {key_mapping[k]: v for k, v in test_metadata.items()}
    # add metadata to the dataset
    dataset.metadata = readable_metadata
