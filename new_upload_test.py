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
from webknossos import webknossos_context
from pathlib import Path
import sys
def get_auth_token():
    """Read authentication token from config file."""
    script_dir = Path(__file__).parent
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()
def main():
    token = get_auth_token()
    stack_path= "/mnt/ximg/2025/p3l-eduke/RECON/20251210/pin360/recon_111_1/tomo"
    p3l_account = "p3l-eduke"
    sample_name = "20251210_p3l-eduke_pin360_1"
    readable_name = "pin360_1"
    folder = "p3l-eduke/20251210/"
    metadata = {
        'user': 'Angelika',
        'test': 'test'
    }

    with webknossos_context(token=token, url="https://webknossos.embl-hamburg.de"):
        print("entered context")
        with TemporaryDirectory() as tmp_dir:
            print("created temp dir")
            with cluster_tools.get_executor("multiprocessing", max_workers=6) as exec:
                ds = wk.Dataset.from_images(stack_path, f"{tmp_dir}/{sample_name}", voxel_size=(650,650,650), executor=exec, layer_name="xray", layer_category="color")
                print("created dataset")
                ds.downsample(executor=exec)
                print("finished downsampling staring upload")
            remote_ds = ds.upload(
                new_dataset_name=readable_name,
                initial_team_ids= ["ximg", p3l_account],
                transfer_mode=wk.TransferMode.MOVE_AND_SYMLINK,
                folder_id=wk.RemoteFolder.get_by_path(folder)
            )
            print("upload done, adding metadata")
            remote_ds.metadata = metadata
            print(remote_ds, remote_ds.metadata)


if __name__ == "__main__":
    main()
