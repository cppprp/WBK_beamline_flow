"""WEBKNOSSOS server-side operations: folders, teams, metadata, dataset lookup.

Every function here expects to be called inside an active `webknossos_context`.
"""

import logging

from webknossos import RemoteDataset, RemoteFolder, Team

from .config import WK_URL, XIMG_TEAM
from .metadata import to_display_metadata

ROOT_FOLDER = "Datasets"


def get_or_create_folder(folder_path: str, log: logging.Logger) -> RemoteFolder:
    """Resolve `Datasets/<folder_path>`, creating any missing levels.

    `folder_path` is relative to the Datasets root, e.g. "p3l-eduke/20250921".
    """
    current_full = ROOT_FOLDER
    folder = RemoteFolder.get_by_path(current_full)

    for part in folder_path.strip("/").split("/"):
        if not part:
            continue
        current_full = f"{current_full}/{part}"
        try:
            folder = RemoteFolder.get_by_path(current_full)
            log.debug(f"Folder exists: {current_full}")
        except Exception:
            log.debug(f"Creating folder: {current_full}")
            folder = folder.add_subfolder(part)

    return folder


def get_or_create_team(team_name: str, log: logging.Logger) -> Team:
    try:
        team = Team.get_by_name(team_name)
        log.debug(f"Found team: {team_name}")
    except Exception:
        log.info(f"Creating team: {team_name}")
        team = Team.add(team_name)
    return team


def resolve_teams(user_team_name: str, log: logging.Logger) -> list[Team]:
    """The p3l account team, plus ximg for facility-wide access.

    The user team is created on demand; ximg is expected to exist already and a
    missing one is a warning rather than a failure, so a first upload for a new
    account is never blocked by facility configuration.
    """
    teams = [get_or_create_team(user_team_name, log)]

    try:
        teams.append(Team.get_by_name(XIMG_TEAM))
    except Exception:
        log.warning(f"Team '{XIMG_TEAM}' not found — assigning the user team only")

    log.debug(f"Teams: {[t.name for t in teams]}")
    return teams


def find_dataset_id_by_directory(directory_name: str, log: logging.Logger) -> str | None:
    """Resolve the date-qualified directory name, which is unique per dataset.

    WEBKNOSSOS keeps two names: the `directory_name` a dataset was uploaded
    under (`20260827_p3l-eduke_pin_180_1`) and the display `name` shown in the
    UI, which this pipeline sets to the readable form (`pin_180_1`). Only the
    first carries the date, so only the first distinguishes this reconstruction
    from the same sample re-imaged on another day — and a display-name search
    cannot see it at all.

    Tried before the display-name search for exactly that reason. Returns None
    if nothing has that directory name, which is the common case for a dataset
    that was renamed by an older tool.
    """
    from webknossos import User
    from webknossos.client.context import _get_api_client

    try:
        client = _get_api_client()
        dataset_id = client.dataset_id_from_name(
            directory_name=directory_name,
            organization_id=User.get_current_user().organization_id,
        )
    except Exception as exc:  # noqa: BLE001 - absent is the normal outcome
        log.debug(f"No dataset with directory name '{directory_name}': {exc}")
        return None

    if dataset_id:
        log.info(f"Matched existing dataset by directory name '{directory_name}': {dataset_id}")
    return dataset_id or None


def find_dataset_id_by_name(names: list[str], log: logging.Logger) -> str | None:
    """Look up a dataset id by display name — the last resort before publishing fresh.

    Several names are tried because a dataset is uploaded under its full
    `20250921_p3l-eduke_MA040_1` name and then renamed to the readable
    `MA040_1`, so which one is live depends on how far a previous run got.

    Display names are not unique: the readable form drops the date, so a sample
    re-imaged months later produces a second dataset with the same one. That is
    why `find_dataset_id_by_directory` is tried first, and why an ambiguous
    result here stops the run rather than guessing.

    Returns None when nothing matches (the caller then publishes fresh) and
    also when the match is ambiguous — guessing would either duplicate a
    dataset or overwrite the wrong one, so the operator is asked to pass
    --dataset-id instead.
    """
    found: dict[str, str] = {}  # dataset_id → the name it matched
    errors: list[str] = []

    for name in names:
        try:
            matches = RemoteDataset.list(name=name)
        except Exception as exc:
            log.warning(f"Dataset lookup for '{name}' failed: {exc}")
            errors.append(f"{name}: {exc}")
            continue
        for dataset in matches.values():
            found[dataset.dataset_id] = name

    # A lookup that could not reach the server is not evidence that the dataset
    # is new. Treating it as new would publish a duplicate alongside the real
    # one, so an unreachable server has to stop the run instead.
    if errors and not found:
        raise RuntimeError(
            "Could not determine whether this dataset already exists — every "
            "lookup failed:\n  " + "\n  ".join(errors) + "\n"
            "Refusing to publish, as a fresh upload would duplicate an existing "
            "dataset. Fix connectivity, or pass --dataset-id to overwrite a known one."
        )

    if not found:
        return None

    if len(found) > 1:
        listed = "\n".join(
            f"    {WK_URL}/datasets/{i}   (matched '{n}')" for i, n in found.items()
        )
        raise RuntimeError(
            f"Found {len(found)} datasets sharing a display name with {names}:\n"
            f"{listed}\n"
            "The display name drops the acquisition date, so a sample imaged more\n"
            "than once has several. Open them and pick the one whose directory name\n"
            "carries this run's date, then re-run with --dataset-id <id>. Its id is\n"
            "recorded in the metadata file afterwards, so this only happens once."
        )

    dataset_id, matched_name = next(iter(found.items()))
    log.info(f"Matched existing dataset '{matched_name}' by name: {dataset_id}")
    return dataset_id


def is_virtual(dataset_id: str, token: str, log: logging.Logger) -> bool | None:
    """Whether the dataset is virtual. None if the API does not say.

    The Python client's dataset model does not carry this flag, so it is read
    from the raw JSON. Only virtual datasets can have layers replaced through
    the upload-to-paths API; anything the old convert-in-place system published
    is not virtual.
    """
    import httpx

    try:
        response = httpx.get(
            f"{WK_URL}/api/datasets/{dataset_id}",
            headers={"X-Auth-Token": token},
            timeout=30,
        )
        response.raise_for_status()
        flag = response.json().get("isVirtual")
    except Exception as exc:  # noqa: BLE001 - advisory; the caller has a backstop
        log.warning(f"Could not determine whether {dataset_id} is virtual: {exc}")
        return None

    if flag is None:
        log.debug("Dataset info carries no isVirtual flag")
        return None
    log.debug(f"Dataset {dataset_id} isVirtual={flag}")
    return bool(flag)


def attach_metadata(
    remote_ds: RemoteDataset, raw_metadata: dict, log: logging.Logger
) -> dict:
    """Publish the remapped MX-Cube metadata to the dataset. Returns what was set."""
    display = to_display_metadata(raw_metadata, log)
    if not display:
        log.info("No metadata to attach")
        return {}

    remote_ds.metadata = display
    log.info(f"Attached {len(display)} metadata fields")
    return display
