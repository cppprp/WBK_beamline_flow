#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
# ]
# ///
"""Generate a shareable zarr3 link for a published dataset.

/data/annotations/zarr3_experimental/<accessToken>/<layer>

That route serves the annotation's own layers *and* the underlying dataset's
layers, which is what makes it usable for plain image data: ask it for `xray`
and you get the published tomogram, as OME-NGFF 0.5 over zarr v3.

Private links belong to an annotation rather than a dataset, so a dataset with
no annotation gets a throwaway skeleton one — it holds no data and exists only
to hang the link on. Before creating anything the script looks for a link you
already made for this dataset and reuses it, so repeated runs hand out the same
URL instead of accumulating annotations and live credentials.

Links are per-person: whoever runs this creates them under their own token, and
only that person can revoke them. The token comes from --token or $WK_TOKEN and
nowhere else — there is no shared file to fall through to — and every run prints
the account it is acting as, so a link cannot quietly be made under the wrong
name.

The prefix is probed because it was renamed upstream from `zarr3_experimental`
to `zarr3`; our server still wants the old name, and this keeps working across
that upgrade.

    ./wbk_publish/streaming.py <datasetId>
    ./wbk_publish/streaming.py <datasetId> --expires-in-days 90
    ./wbk_publish/streaming.py --annotation <annotationId>

Anyone holding the printed URL can read the data until the link is deleted!
"""

import argparse
import os
import sys
import time

import httpx

# The instance these links live on. Hardcoded rather than configurable: a link is
# only meaningful on the server that issued it, and so are the dataset and
# annotation ids it is built from.
WK_URL = "https://webknossos.embl-hamburg.de"

# Renamed upstream once the routes left experimental status. Current name first,
# so a server upgrade needs no change here.
ZARR3_PREFIXES = ("zarr3", "zarr3_experimental")


def resolve_token(explicit: str | None) -> tuple[str, str]:
    """The API token and where it came from.

    Deliberately only two sources. A link is owned by the account that created
    it and only that account can revoke it, so there is no shared-file fallback
    that could quietly mint links under someone else's name.
    """
    if explicit:
        return explicit, "--token"

    from_env = os.environ.get("WK_TOKEN")
    if from_env:
        return from_env.strip(), "$WK_TOKEN"

    raise RuntimeError(
        "No token. Pass --token, or set $WK_TOKEN to your own WEBKNOSSOS API "
        "token (Account Settings → Auth Token)."
    )


def whoami(client: httpx.Client) -> str:
    """The account the token belongs to, for the record printed on every run."""
    response = client.get(f"{WK_URL}/api/user")
    response.raise_for_status()
    user = response.json()
    name = f"{user.get('firstName', '?')} {user.get('lastName', '?')}".strip()
    return f"{name} <{user.get('email', '?')}>"


def link_url(access_token: str, layer_name: str, prefix: str) -> str:
    """The URL to hand out. Open it in napari, zarr-python, or the NGFF validator."""
    return f"{WK_URL}/data/annotations/{prefix}/{access_token}/{layer_name}"


def create_annotation(client: httpx.Client, dataset_id: str) -> str:
    """Make a skeleton annotation to own the link, and return its id.

    Skeleton rather than volume: it allocates no data, and the link only needs
    something to belong to. The dataset's own layers are served through it
    regardless of what the annotation itself contains.
    """
    response = client.post(
        f"{WK_URL}/api/datasets/{dataset_id}/createExplorational",
        json=[{"typ": "Skeleton"}],
    )
    response.raise_for_status()
    return response.json()["id"]


def find_link(client: httpx.Client, annotation_id: str) -> str | None:
    """The annotation's existing access token, if it already has one."""
    response = client.get(f"{WK_URL}/api/zarrPrivateLinks/byAnnotation/{annotation_id}")
    response.raise_for_status()
    links = response.json()
    return links[0]["accessToken"] if links else None


def find_existing_link(client: httpx.Client, dataset_id: str) -> tuple[str, str] | None:
    """A link this user already made for this dataset, as (annotation id, token).

    Matching runs link-first rather than dataset-first because the annotation
    *listing* endpoints report only `dataSetName`, which our pipeline renames —
    so we take the links this account owns and ask each one's annotation which
    dataset it sits on, where the id is reported. That is a request per link,
    but a person holds a handful, and it keeps reuse working with no local state
    to synchronise, which matters when links are per-person and there is nowhere
    shared to record them.
    """
    response = client.get(f"{WK_URL}/api/zarrPrivateLinks")
    response.raise_for_status()

    now_ms = time.time() * 1000
    for link in response.json():
        expires = link.get("expirationDateTime")
        if expires is not None and expires <= now_ms:
            continue
        info = client.get(f"{WK_URL}/api/annotations/{link['annotation']}/info")
        if info.status_code != 200:
            continue  # annotation deleted out from under its link
        if info.json().get("datasetId") == dataset_id:
            return link["annotation"], link["accessToken"]

    return None


def create_link(
    client: httpx.Client, annotation_id: str, expires_in_days: int | None
) -> str:
    """Mint a new access token for the annotation.

    Omitting `expirationDateTime` means the link never expires; the server
    stores it as null and checks it only when set.
    """
    payload: dict = {"annotation": annotation_id}
    if expires_in_days is not None:
        payload["expirationDateTime"] = int(
            (time.time() + expires_in_days * 86400) * 1000
        )

    response = client.post(f"{WK_URL}/api/zarrPrivateLinks", json=payload)
    response.raise_for_status()
    return response.json()["accessToken"]


def resolve_layers(access_token: str) -> tuple[str, list[str]]:
    """Find the live zarr3 prefix and the layers the link exposes.

    Deliberately unauthenticated: this is the request a recipient's browser
    makes, so if it succeeds here the link really is self-contained. One
    request does both jobs, since datasource-properties.json exists only under
    a prefix the server routes and its body names the layers.
    """
    attempts = []
    with httpx.Client(timeout=30.0, follow_redirects=True) as anonymous:
        for prefix in ZARR3_PREFIXES:
            url = (
                f"{WK_URL}/data/annotations/{prefix}/{access_token}"
                "/datasource-properties.json"
            )
            response = anonymous.get(url)
            if response.status_code == 200:
                layers = [layer["name"] for layer in response.json()["dataLayers"]]
                return prefix, layers
            attempts.append(f"{prefix} → HTTP {response.status_code}")

    raise RuntimeError(f"No zarr3 route answered for the link ({'; '.join(attempts)})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a shareable OME-NGFF 0.5 zarr3 link for a published dataset."
    )
    parser.add_argument("dataset_id", nargs="?", help="Dataset id (see the webknossos block in meta_<run>.json)")
    parser.add_argument("--annotation", help="Use this annotation instead of making one")
    parser.add_argument(
        "--layer",
        help="Only print this layer's URL. Every layer is printed when omitted; the "
        "names come from the link itself, so there is nothing to guess.",
    )
    parser.add_argument(
        "--new", action="store_true", help="Mint a fresh link instead of reusing yours"
    )
    parser.add_argument(
        "--expires-in-days", type=int, help="Expire the link after N days (default: never)"
    )
    parser.add_argument("--token", help="API token; falls back to $WK_TOKEN")
    args = parser.parse_args(argv)

    if not args.dataset_id and not args.annotation:
        parser.error("give a dataset_id, or --annotation")

    try:
        token, source = resolve_token(args.token)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    headers = {"X-Auth-Token": token}

    with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
        try:
            print(f"Acting as  : {whoami(client)} (token from {source})")

            annotation_id: str | None = args.annotation
            access_token: str | None = None

            if args.new:
                pass  # forced fresh link; skip every lookup
            elif annotation_id is not None:
                access_token = find_link(client, annotation_id)
            else:
                found = find_existing_link(client, args.dataset_id)
                if found is not None:
                    annotation_id, access_token = found

            if annotation_id is None:
                annotation_id = create_annotation(client, args.dataset_id)
                print(f"Annotation : {annotation_id} (created)")
            else:
                print(f"Annotation : {annotation_id}")

            if access_token is None:
                access_token = create_link(client, annotation_id, args.expires_in_days)
                print(f"Link       : {access_token} (created)")
            else:
                print(f"Link       : {access_token} (reused)")

            prefix, layers = resolve_layers(access_token)
        except httpx.HTTPStatusError as exc:
            print(
                f"error: {exc.request.method} {exc.request.url} → HTTP "
                f"{exc.response.status_code}\n{exc.response.text[:300]}",
                file=sys.stderr,
            )
            return 2
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.layer:
        if args.layer not in layers:
            print(
                f"error: no layer {args.layer!r}; the link exposes {layers}", file=sys.stderr
            )
            return 2
        layers = [args.layer]

    print()
    for layer in layers:
        print(f"{layer}:")
        print(f"  {link_url(access_token, layer, prefix)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
