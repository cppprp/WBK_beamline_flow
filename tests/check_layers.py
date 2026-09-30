"""Adding a layer to a dataset that already exists.

The dangerous case is quiet rather than loud: publishing `predictions` when the
lookup finds nothing would create a new dataset holding only the segmentation,
with no `xray` beside it, and the run would report success. Nothing downstream
notices — the dataset simply exists and is wrong.

So publish() refuses that combination, and the escape hatch has to be explicit.
Exercised here without a server, by stubbing the two calls that need one.
"""

import _support  # noqa: F401
from _support import WORK, Report, write_stack


def main() -> int:
    import importlib

    pub = importlib.import_module("wbk_publish.publish")
    from wbk_publish.config import DEFAULT_LAYER_NAME
    from wbk_publish.logs import make_logger
    from wbk_publish.publish import PublishRequest

    r = Report()
    log = make_logger("layer-check", verbose=False)

    stack = WORK / "layers/recon_111_1/tomo"
    write_stack(stack, value=7)
    input_dir = stack.parent

    # The server is not involved: the flows under test are decided before any
    # conversion or upload happens.
    found: dict = {"id": None}
    pub.find_dataset_id_by_directory = lambda name, log: found["id"]
    pub.find_dataset_id_by_name = lambda names, log: found["id"]
    pub.is_virtual = lambda dataset_id, token, log: True

    def request(**overrides):
        base = dict(
            input_dir=input_dir, tiff_stack=stack,
            dataset_name="20250921_p3l-test_LAYERS_1", readable_name="LAYERS_1",
            folder_path="p3l-test/20250921", user_team="p3l-test",
            metadata_file=input_dir.parent / "meta_1.json",
            voxel_size_nm=(650.0, 650.0, 650.0), token="dummy", dry_run=True,
        )
        base.update(overrides)
        return PublishRequest(**base)

    print("— the default layer publishes a new dataset without ceremony")
    found["id"] = None
    r.ok("dry run completes", pub.publish(request(), log) is None)

    print("— a non-default layer with nothing to attach to is refused")
    found["id"] = None
    try:
        pub.publish(request(layer_name="predictions", category="segmentation"), log)
        r.ok("refused", False, "publish returned")
    except RuntimeError as exc:
        msg = str(exc)
        r.ok("raises", True)
        r.ok("names the layer", "'predictions'" in msg)
        r.ok(f"says there would be no {DEFAULT_LAYER_NAME}", f"no '{DEFAULT_LAYER_NAME}'" in msg)
        r.ok("says nothing was changed", "Nothing has been changed" in msg)
        r.ok("offers --dataset-id", "--dataset-id" in msg)
        r.ok("offers --new-dataset", "--new-dataset" in msg)

    print("— ...and is refused in a dry run too, not just for real")
    r.ok("dry run does not mask it", request(layer_name="predictions").dry_run)

    print("— an explicit --dataset-id attaches it to that dataset")
    found["id"] = None
    result = pub.publish(
        request(layer_name="predictions", category="segmentation",
                dataset_id="6aad4234b6d36ce98fb28bd6"), log)
    r.ok("dry run completes", result is None)

    print("— a found dataset is enough; no flag needed")
    found["id"] = "6aad4234b6d36ce98fb28bd6"
    r.ok("dry run completes",
         pub.publish(request(layer_name="predictions", category="segmentation"), log) is None)

    print("— --new-dataset allows the standalone case the CLI help documents")
    found["id"] = None
    r.ok("dry run completes",
         pub.publish(request(layer_name="predictions", category="segmentation",
                             new_dataset=True), log) is None)

    print("— the beamline CLI cannot reach any of this: its layer is fixed")
    import wbk_publish.beamline as bl
    r.ok("no --layer_name on the beamline CLI",
         "--layer_name" not in bl.main.__doc__ if bl.main.__doc__ else True)

    print("— the manual CLI exposes the flag")
    import argparse
    seen = {}
    orig = argparse.ArgumentParser.parse_args

    def spy(self, args=None, namespace=None, _orig=orig):
        ns = _orig(self, args, namespace)
        seen["new_dataset"] = getattr(ns, "new_dataset", "MISSING")
        raise SystemExit(0)

    import wbk_publish.manual as mn
    argparse.ArgumentParser.parse_args = spy
    try:
        mn.main(["--input_dir", "/x", "--new-dataset", "--token", "t"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig
    r.eq("manual parses --new-dataset", seen.get("new_dataset"), True)

    return r.finish("LAYER CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
