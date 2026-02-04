#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "webknossos[all]==3.1.0",
#      "zarr"
# ]
# ///
import webknossos as wk
import cluster_tools
from tempfile import TemporaryDirectory
import argparse
from webknossos import webknossos_context, Team
from pathlib import Path
import sys
from glob import glob
import warnings
warnings.filterwarnings('ignore', category=FutureWarning, module='pims')
def get_auth_token():
    """Read authentication token from config file."""
    script_dir = Path(__file__).parent
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()
def main():
    token = get_auth_token()
    landing_path ="/morningstar/webknossos/.published/EMBL_Hamburg/.virtual/"
    stack_path= "/mnt/ximg/2025/p3l-eduke/RECON/20251210/pin360/recon_111_1/tomo"
    p3l_account = "p3l-eduke"
    sample_name = "20251210_p3l-eduke_pin360_1"
    readable_name = "full_name_pin360_1"
    folder = "Datasets/p3l-eduke/20251210/"
    metadata = {
        'user': 'Angelika',
        'test': 'test'

    }
    overwrite = True
    with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):
        print("entered context")
        '''team = Team.get_by_name(name=p3l_account)
        with TemporaryDirectory(dir="/morningstar/tmp") as tmp_dir:
            print("created temp dir")
            with cluster_tools.get_executor("multiprocessing", max_workers=6) as exec:
                ds = wk.Dataset.from_images(stack_path, f"{tmp_dir}/{sample_name}", voxel_size=(650,650,650), executor=exec, layer_name="xray", layer_category="color")
                print("created dataset")
                ds.downsample(executor=exec)
                print("finished downsampling staring upload")
            remote_ds = ds.upload(
                #new_dataset_name=readable_name,
                initial_team_ids= ["691317520100003802eda8a2", team.id],
                transfer_mode=wk.TransferMode.MOVE_AND_SYMLINK,
                folder_id=wk.RemoteFolder.get_by_path(folder)
            )
            print("upload done, adding metadata")
            remote_ds.metadata = metadata
            remote_ds.display_name = readable_name

            print(remote_ds, remote_ds.metadata)'''

    morningstar_path = list(glob(landing_path+sample_name+"*"))
    if len(morningstar_path) > 1:
        print("More than one match found to the dataset, cleanup or be more specific")
    print(morningstar_path[0])
    id_str = morningstar_path[0].split('-')[-1]
    print(id_str)
if __name__ == "__main__":
    main()
