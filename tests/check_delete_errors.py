"""Error handling around delete_layer.

A timeout here is ambiguous, not fatal — the server is probably still deleting.
The point of the handler is that the operator can tell that apart from a real
failure without reading the source.
"""

import os
from pathlib import Path

from _support import WORK as SC  # noqa: E402 - sets sys.path and env

import httpx
from webknossos.client.api_client.errors import UnexpectedStatusError

from wbk_publish.logs import make_logger
from wbk_publish.publish import LayerDeletionTimeout, _delete_layer

log = make_logger("delete-errors", verbose=False)
fails = []
DS_ID = "6a958832b88c452c5c0069e7"


def ok(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f": {detail}" if detail else ""))
    if not condition:
        fails.append(label)


class FakeRemote:
    def __init__(self, exc=None):
        self.exc = exc
        self.called = False

    def delete_layer(self, name):
        self.called = True
        if self.exc is not None:
            raise self.exc


def status_error(code: int) -> UnexpectedStatusError:
    request = httpx.Request("PATCH", f"https://wk.example/api/datasets/{DS_ID}/updatePartial")
    return UnexpectedStatusError(httpx.Response(code, request=request, content=b"gateway timeout"))


def expect_timeout(label, exc):
    remote = FakeRemote(exc)
    try:
        _delete_layer(remote, "xray", DS_ID, log)
    except LayerDeletionTimeout as raised:
        msg = str(raised)
        ok(f"{label}: raises LayerDeletionTimeout", True)
        for needle, what in [
            (DS_ID, "the dataset id"),
            ("NOT corrupt", "reassurance about dataset state"),
            ("--dataset-id", "the safe re-run instruction"),
            ("WK_TIMEOUT", "the client timeout knob"),
            ("proxy_read_timeout", "the proxy timeout knob"),
            (type(exc).__name__, "the original exception type"),
        ]:
            ok(f"{label}: names {what}", needle in msg)
        return msg
    except Exception as other:
        ok(f"{label}: raises LayerDeletionTimeout", False, f"got {type(other).__name__}")
        return ""
    ok(f"{label}: raises LayerDeletionTimeout", False, "no exception raised")
    return ""


print("— client-side timeout")
msg = expect_timeout("read timeout", httpx.ReadTimeout("timed out"))
ok("says the client timed out", "the client timed out" in msg)

print("— proxy gateway errors")
for code in (502, 503, 504):
    m = expect_timeout(f"HTTP {code}", status_error(code))
    ok(f"HTTP {code}: blames the proxy", "proxy in front of WEBKNOSSOS" in m)

print("— unrelated failures must pass through untouched")
for exc in (IndexError("no such layer"), PermissionError("denied"), status_error(403)):
    remote = FakeRemote(exc)
    try:
        _delete_layer(remote, "xray", DS_ID, log)
        ok(f"{type(exc).__name__} propagates", False, "no exception raised")
    except LayerDeletionTimeout:
        ok(f"{type(exc).__name__} propagates", False, "wrongly treated as a timeout")
    except Exception as raised:
        ok(f"{type(exc).__name__} propagates unchanged", raised is exc)

print("— success path")
remote = FakeRemote()
try:
    _delete_layer(remote, "xray", DS_ID, log)
    ok("no exception", True)
    ok("delete_layer actually called", remote.called)
except Exception as exc:
    ok("no exception", False, f"{type(exc).__name__}: {exc}")

print("— a 403 must not be mistaken for a gateway error")
remote = FakeRemote(status_error(403))
try:
    _delete_layer(remote, "xray", DS_ID, log)
except LayerDeletionTimeout:
    ok("403 not treated as timeout", False)
except UnexpectedStatusError:
    ok("403 not treated as timeout", True)

print()
print("FAILURES:" if fails else "DELETE ERROR-HANDLING CHECKS PASSED")
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
