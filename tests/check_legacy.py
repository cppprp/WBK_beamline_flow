"""Legacy (non-virtual) datasets are recognised before any conversion happens.

A dataset published by the old convert-in-place system cannot be updated
through the upload-to-paths API. The check has to run before the 12-minute
conversion, refuse clearly by default, and publish fresh with --replace-legacy.

This is the path that was broken by a half-committed edit: publish() calling a
function that did not exist. Nothing else exercises a *matched* dataset without
a server, so this does, with the API lookup stubbed out.
"""

import _support  # noqa: F401
from _support import WORK, Report, write_stack


def main() -> int:
    from pathlib import Path

    import importlib

    # The package re-exports the publish() function, which shadows the module.
    pub = importlib.import_module("wbk_publish.publish")
    from wbk_publish.logs import make_logger
    from wbk_publish.publish import LegacyDatasetError, PublishRequest, _delete_layer

    r = Report()
    log = make_logger("legacy-check", verbose=False)

    stack = WORK / "legacy/recon_111_1/tomo"
    write_stack(stack, value=11)
    input_dir = stack.parent

    def request(**overrides):
        base = dict(
            input_dir=input_dir, tiff_stack=stack,
            dataset_name="20250921_p3l-test_LEGACY_1", readable_name="LEGACY_1",
            folder_path="p3l-test/20250921", user_team="p3l-test",
            metadata_file=input_dir.parent / "meta_1.json",
            voxel_size_nm=(650.0, 650.0, 650.0), token="dummy",
            dataset_id="6a90baa72358dcbef5d8bcdd",  # skips the name lookup
            dry_run=True,
        )
        base.update(overrides)
        return PublishRequest(**base)

    # Stub the API: the only thing publish() needs from the server here.
    answers = {}
    pub.is_virtual = lambda dataset_id, token, log: answers.get("virtual")

    print("— a non-virtual match is refused, before conversion, with guidance")
    answers["virtual"] = False
    try:
        pub.publish(request(), log)
        r.ok("refused", False, "publish returned")
    except LegacyDatasetError as exc:
        msg = str(exc)
        r.ok("raises LegacyDatasetError", True)
        r.ok("names the dataset id", "6a90baa72358dcbef5d8bcdd" in msg)
        r.ok("says nothing was changed", "Nothing has been changed" in msg)
        r.ok("points at --replace-legacy", "--replace-legacy" in msg)
        r.ok("links the old dataset", "/datasets/6a90baa72358dcbef5d8bcdd" in msg)
    r.ok("no staging was created", not any((WORK / "tmp").glob("20250921_p3l-test_LEGACY_1.*")))

    print("— --replace-legacy turns it into a fresh upload")
    answers["virtual"] = False
    result = pub.publish(request(replace_legacy=True), log)   # dry run: reports, returns None
    r.ok("dry run completes", result is None)

    print("— a virtual match proceeds to the overwrite path")
    answers["virtual"] = True
    result = pub.publish(request(), log)
    r.ok("dry run completes", result is None)

    print("— an undeterminable flag does not block the run")
    answers["virtual"] = None
    result = pub.publish(request(), log)
    r.ok("dry run completes", result is None)

    print("— the backstop translates the server's refusal")
    class Refuses:
        def delete_layer(self, name):
            raise RuntimeError('Failed RPC 6483: PUT https://x/data/datasets/abc. Response: 400 \'{"messages":[{"error":"No layers."}]}\'')

    try:
        _delete_layer(Refuses(), "xray", "abc", log)
        r.ok("backstop fires", False, "no exception")
    except LegacyDatasetError as exc:
        r.ok("backstop fires", True)
        r.ok("explains it is not virtual", "not virtual" in str(exc))
        r.ok("points at --replace-legacy", "--replace-legacy" in str(exc))

    class OtherError:
        def delete_layer(self, name):
            raise RuntimeError("status 403 forbidden")

    try:
        _delete_layer(OtherError(), "xray", "abc", log)
    except LegacyDatasetError:
        r.ok("unrelated errors are not mistaken for legacy", False)
    except RuntimeError:
        r.ok("unrelated errors are not mistaken for legacy", True)

    print("— the CLIs expose the flag")
    import wbk_publish.beamline as bl, wbk_publish.manual as mn
    for mod, argv in ((bl, ["/x", "--voxel-size", "650"]), (mn, ["--input_dir", "/x"])):
        import argparse
        parser = argparse.ArgumentParser()
        # Cheap way to prove the flag parses: the modules' own parsers are built
        # inside main(); reuse them by intercepting parse_args.
        seen = {}
        orig = argparse.ArgumentParser.parse_args
        def spy(self, args=None, namespace=None, _orig=orig):
            ns = _orig(self, args, namespace)
            seen["replace_legacy"] = getattr(ns, "replace_legacy", "MISSING")
            raise SystemExit(0)
        argparse.ArgumentParser.parse_args = spy
        try:
            mod.main(argv + ["--replace-legacy", "--token", "t"])
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = orig
        r.eq(f"{mod.__name__.split('.')[-1]} parses --replace-legacy", seen.get("replace_legacy"), True)

    return r.finish("LEGACY CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
