"""Every webknossos/cluster_tools API wbk_publish depends on, checked by signature.

Run after any dependency upgrade. A failure here means the pipeline is broken
against the installed library, whether or not the unit tests still pass.

Everything lives inside main(): cluster_tools spawns its workers, which re-import
this module, and any work at import time would run again in every child.
"""

import _support  # noqa: F401 - sets sys.path and env
import inspect

fails = []


def ok(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f": {detail}" if detail else ""))
    if not condition:
        fails.append(label)


def needs(label, fn, expected):
    missing = expected - set(inspect.signature(fn).parameters)
    ok(label, not missing, "missing " + ", ".join(sorted(missing)) if missing else "")


def main():
    import attr as _attr
    import cluster_tools
    import webknossos as wk
    from webknossos.dataset.layer import Layer, RemoteLayer

    print(f"— webknossos {wk.current_version}")

    print("— top-level names")
    for name in (
        "Dataset", "RemoteDataset", "RemoteFolder", "Team", "DataFormat",
        "TransferMode", "RemoteAccessMode", "SamplingModes", "webknossos_context",
    ):
        ok(f"wk.{name}", hasattr(wk, name))

    print("— enum members the pipeline names explicitly")
    ok("DataFormat.Zarr3", hasattr(wk.DataFormat, "Zarr3"))
    ok("TransferMode.MOVE_AND_SYMLINK", hasattr(wk.TransferMode, "MOVE_AND_SYMLINK"))
    ok("RemoteAccessMode.DIRECT_PATH", hasattr(wk.RemoteAccessMode, "DIRECT_PATH"))
    ok("SamplingModes.parse('anisotropic')",
       wk.SamplingModes.parse("anisotropic") is wk.SamplingModes.ANISOTROPIC)

    print("— shard default that sets the task granularity")
    from webknossos.dataset.defaults import DEFAULT_SHARD_SHAPE_FROM_IMAGES as shard
    ok("DEFAULT_SHARD_SHAPE_FROM_IMAGES importable", True, str(shard))
    ok("shard z is still 32", shard.z == 32, f"z={shard.z}")

    print("— conversion")
    needs("Dataset.add_layer_from_images", wk.Dataset.add_layer_from_images,
          {"images", "layer_name", "category", "data_format", "compress", "executor"})
    needs("Dataset.__init__", wk.Dataset.__init__, {"dataset_path", "voxel_size", "name"})

    print("— fresh upload")
    needs("Dataset.upload", wk.Dataset.upload,
          {"new_dataset_name", "folder", "initial_team_ids", "transfer_mode"})

    print("— layer overwrite (the id-preserving path)")
    needs("RemoteDataset.open", wk.RemoteDataset.open,
          {"dataset_id", "access_mode", "read_only"})
    ok("RemoteDataset.delete_layer", callable(getattr(wk.RemoteDataset, "delete_layer", None)))
    needs("RemoteDataset.add_layer", wk.RemoteDataset.add_layer,
          {"layer_name", "category", "dtype", "num_channels", "data_format", "bounding_box"})
    needs("RemoteLayer.add_mag_as_copy", RemoteLayer.add_mag_as_copy,
          {"foreign_mag_view_or_path", "transfer_mode", "overwrite_pending"})

    print("— layer properties read from the staged layer")
    for attr in ("dtype_per_channel", "num_channels", "bounding_box", "mags", "name"):
        ok(f"Layer.{attr}", hasattr(Layer, attr))
    ok("Layer.as_segmentation_layer", callable(getattr(Layer, "as_segmentation_layer", None)))

    print("— folders, teams, metadata, lookup")
    needs("RemoteFolder.get_by_path", wk.RemoteFolder.get_by_path, {"path"})
    ok("RemoteFolder.add_subfolder", callable(getattr(wk.RemoteFolder, "add_subfolder", None)))
    ok("Team.get_by_name", callable(getattr(wk.Team, "get_by_name", None)))
    ok("Team.add", callable(getattr(wk.Team, "add", None)))
    ok("Team.id field", "id" in {f.name for f in _attr.fields(wk.Team)})
    needs("RemoteDataset.list", wk.RemoteDataset.list, {"name"})
    for prop in ("metadata", "name", "folder", "allowed_teams"):
        attr_obj = getattr(wk.RemoteDataset, prop, None)
        ok(f"RemoteDataset.{prop} is settable",
           isinstance(attr_obj, property) and attr_obj.fset is not None)
    ok("RemoteDataset.dataset_id", hasattr(wk.RemoteDataset, "dataset_id"))

    # The date-qualified directory name is the only unique handle on a dataset
    # whose display name has been changed, so resolving it is load-bearing for
    # re-published samples. It is a private API; pin it so an upgrade that moves
    # it fails here rather than silently falling back to an ambiguous search.
    from webknossos.client.api_client.wk_api_client import WkApiClient
    from webknossos.client.context import _get_api_client  # noqa: F401
    ok("client.context._get_api_client", callable(_get_api_client))
    needs("WkApiClient.dataset_id_from_name", WkApiClient.dataset_id_from_name,
          {"directory_name", "organization_id"})
    ok("User.organization_id", hasattr(wk.User, "organization_id"))

    print("— insegt/: adding layers to an existing dataset over HTTPS")
    ok("TransferMode.HTTP", hasattr(wk.TransferMode, "HTTP"))
    needs("RemoteDataset.add_layer_as_copy", wk.RemoteDataset.add_layer_as_copy,
          {"foreign_layer", "transfer_mode"})
    from webknossos.dataset.layer.view.mag_view import MagView
    needs("MagView.write", MagView.write, {"data", "absolute_offset", "allow_unaligned"})
    needs("Layer.add_mag", Layer.add_mag, {"mag", "chunk_shape", "shard_shape", "compress"})
    needs("Layer.downsample", Layer.downsample,
          {"from_mag", "sampling_mode", "shard_shape", "executor"})
    for attr in ("first_name", "last_name", "email"):
        ok(f"User.{attr}", hasattr(wk.User, attr))

    print("— cluster_tools: spawn is what makes the thread cap reach workers")
    ex = cluster_tools.get_executor("multiprocessing", max_workers=1)
    method = ex._mp_context.get_start_method()
    ex.shutdown()
    ok("start method is spawn", method == "spawn", method)

    print()
    print("FAILURES:" if fails else "API SURFACE INTACT")
    for f in fails:
        print(" -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
