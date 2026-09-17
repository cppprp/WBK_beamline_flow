"""Deployment configuration and the MX-Cube metadata key mapping.

Everything installation-specific lives here so the rest of the package stays
portable. Values can be overridden with environment variables, which is how the
staging instance is pointed at a different server without editing code.
"""

import os
from pathlib import Path

# ── WEBKNOSSOS instance ───────────────────────────────────────────────────────

WK_URL = os.environ.get("WK_URL", "https://webknossos.embl-hamburg.de")

# Team that gets read access to every dataset, in addition to the p3l account.
XIMG_TEAM = os.environ.get("WK_XIMG_TEAM", "ximg")

# ── Local filesystem ──────────────────────────────────────────────────────────

# Staging area for freshly converted layers. Must be on the same filesystem as
# the WEBKNOSSOS storage: layers are handed over with TransferMode.MOVE_AND_SYMLINK,
# which is an os.rename() and therefore cannot cross a mount point.
TEMP_BASE = Path(os.environ.get("WK_TEMP_BASE", "/morningstar/tmp"))

# Beamline data mount. Checked before any conversion work starts so a missing
# mount fails in seconds rather than halfway through a stack.
BEEGFS_MOUNT = Path(os.environ.get("WK_BEEGFS_MOUNT", "/mnt/beegfs7"))

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_LAYER_NAME = "xray"
DEFAULT_VOXEL_SIZE_NM = (650.0, 650.0, 650.0)

# Worker counts are not configured here — they are derived per run from the stack
# geometry and the machine's allocation. See resources.py.

# Sidecar written next to the input data. Carries both the MX-Cube metadata and
# the WEBKNOSSOS dataset identity, so a re-run can find the dataset it published
# last time and replace a layer in place instead of creating a duplicate.
SIDECAR_NAME = "dataset-metadata.json"

# ── MX-Cube metadata → WEBKNOSSOS display labels ──────────────────────────────
# Order matters: metadata is presented in the WEBKNOSSOS UI in this order.

KEY_MAPPING = {
    "user": "User",
    "operator": "Operator",
    "proposal_id": "Proposal ID",
    "timestamp": "Timestamp",
    "sample_name": "Sample Name",
    "sample_run": "Sample Run",
    "magnification": "Magnification",
    "pixelsize": "Pixel Size (µm)",
    "energy_kev": "Energy (keV)",
    "exposure_time_s": "Exposure Time (s)",
    "number_of_images": "Number of Projections",
    "angular_range_deg": "Angular Range (°)",
    "distances_mm": "Sample to Detector Distances (mm)",
    "betadelta": "Beta/Delta Ratio",
    "mode": "Acquisition Mode",
    "flux": "Beam Intensity (A)",
    "tiling_x_steps": "Tiling X Steps",
    "tiling_x_delta_mm": "Tiling X Delta (mm)",
    "tiling_y_steps": "Tiling Y Steps",
    "tiling_y_delta_mm": "Tiling Y Delta (mm)",
    "tiling_z_steps": "Tiling Z Steps",
    "tiling_z_delta_mm": "Tiling Z Delta (mm)",
    "file_path": "Path to Raw Data",
}


def get_auth_token(explicit: str | None = None) -> str:
    """Resolve the API token: argument, then $WK_TOKEN, then .config/auth_token.txt."""
    if explicit:
        return explicit

    from_env = os.environ.get("WK_TOKEN")
    if from_env:
        return from_env.strip()

    token_file = Path(__file__).resolve().parent.parent / ".config" / "auth_token.txt"
    if not token_file.exists():
        raise FileNotFoundError(
            "No WEBKNOSSOS token found. Pass --token, set $WK_TOKEN, "
            f"or create {token_file}"
        )
    return token_file.read_text().strip()
