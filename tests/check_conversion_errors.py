"""Conversion failures must say which slice failed and what state things are in.

webknossos 4.0 raises typed `ImageConversionError` subclasses carrying the
offending path. Before that, a bad slice four thousand frames into a six-hour
conversion surfaced as a reader-specific error with no context.
"""

import re
import shutil

from _support import WORK, Report, write_stack


def main() -> int:
    from upath import UPath
    from webknossos import (
        CorruptImageError,
        UnsupportedImageDataError,
        UnsupportedImageFormatError,
    )

    from wbk_publish.convert import ConversionFailed, _conversion_message, build_layer
    from wbk_publish.logs import make_logger
    from wbk_publish.resources import plan_for_stack

    r = Report()
    log = make_logger("conversion-errors", verbose=False)

    stack = WORK / "convfail/stack"
    shutil.rmtree(stack.parent, ignore_errors=True)
    tiffs = write_stack(stack, value=11)

    print("— a corrupt slice is located in the stack")
    bad = tiffs[32]
    msg = _conversion_message(CorruptImageError("bad header", path=UPath(str(bad))), tiffs)
    r.ok("names the slice position", f"slice 33 of {len(tiffs)}" in msg)
    r.ok("names the file", bad.name in msg)
    r.ok("says nothing was published", "Nothing was published" in msg)
    r.ok("suggests comparing file sizes", "smaller than its neighbours" in msg)
    r.ok("says a re-run is safe", "sidecar records the dataset id" in msg)

    print("— an unplaceable path still reports cleanly")
    msg = _conversion_message(CorruptImageError("bad", path=UPath("/elsewhere/x.tif")), tiffs)
    r.ok("no bogus slice number", re.search(r"slice \d+ of \d+", msg) is None)
    r.ok("still names the file", "/elsewhere/x.tif" in msg)

    print("— a missing optional dependency blames the environment, not the data")
    msg = _conversion_message(
        UnsupportedImageFormatError("no reader", path=UPath("/x/a.ims"), missing_extras=("ims",)),
        tiffs,
    )
    r.ok("names the extra", "webknossos[ims]" in msg)
    r.ok("points at the environment", "uv run" in msg)

    print("— an unsupported format lists what is supported")
    msg = _conversion_message(
        UnsupportedImageFormatError(
            "no reader", path=UPath("/x/a.zzz"), supported_file_extensions=(".tif", ".png")
        ),
        tiffs,
    )
    r.ok("says no reader handles it", "No reader handles this format" in msg)
    r.ok("lists supported extensions", ".tif" in msg)

    print("— unsupported data explains the usual causes")
    msg = _conversion_message(UnsupportedImageDataError("float into segmentation"), tiffs)
    r.ok("mentions the segmentation case", "--layer_type color" in msg)
    r.ok("mentions the multi-channel case", "multi-channel" in msg)
    r.ok("tolerates a missing path", "None" not in msg.splitlines()[1])

    print("— end to end: a genuinely corrupt slice mid-stack")
    bad.write_bytes(b"\x00" * 128)  # was a valid TIFF, now is not
    out = WORK / "convfail/out"
    shutil.rmtree(out, ignore_errors=True)
    plan = plan_for_stack(tiffs, log, max_workers=2)

    try:
        build_layer(
            tiffs,
            out,
            dataset_name="20250921_p3l-test_SAMPLE_1",
            voxel_size_nm=(650.0, 650.0, 650.0),
            layer_name="xray",
            category="color",
            plan=plan,
            sampling_mode="anisotropic",
            log=log,
        )
        r.ok("raises on a corrupt slice", False, "conversion succeeded unexpectedly")
    except ConversionFailed as exc:
        r.ok("raises ConversionFailed", True)
        text = str(exc)
        r.ok("names the corrupt file", bad.name in text, text.splitlines()[0])
        r.ok("says nothing was published", "Nothing was published" in text)
    except Exception as exc:  # noqa: BLE001 - the point is to see what it was
        r.ok(
            "raises ConversionFailed",
            False,
            f"got {type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
        )

    return r.finish("CONVERSION ERROR CHECKS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
