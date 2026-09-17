"""Publishing beamline tomography data to WEBKNOSSOS as zarr3 datasets.

Two entry points share one pipeline:

    python -m wbk_publish.beamline  — automatic, triggered after reconstruction.
                                      Derives everything from the input path.
    python -m wbk_publish.manual    — manual/batch, with explicit control over
                                      layer name, layer type, voxel size and
                                      project folder.

Re-running either one on the same input replaces the layer on the dataset it
published before, keeping the dataset id — see `publish.py`.
"""

from .threads import limit_thread_oversubscription

# Must happen before numpy is imported, i.e. before anything below this line.
limit_thread_oversubscription()

from .publish import PublishRequest, publish  # noqa: E402

__all__ = ["PublishRequest", "publish"]
