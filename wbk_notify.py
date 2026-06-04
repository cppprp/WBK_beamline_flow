#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///

"""
P14 pipeline monitor
--------------------
Polls WebKnossos for dataset conversion status and RAM usage.
Posts to ntfy when datasets start/finish converting, and when
RAM crosses a threshold.

Cron usage (every minute):
  * * * * * /usr/bin/uv run /opt/p14-pipeline/wbk_monitor.py >> /opt/p14-pipeline/logs/monitor.log 2>&1
"""

import json
import requests
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────

WK_URL         = "https://webknossos.embl-hamburg.de"
STATE_FILE     = Path("/opt/p14-status/monitor_state.json")
NTFY_TOPIC     = "morningstar-p143mb1"
RAM_THRESHOLD  = 80

# ── helpers ───────────────────────────────────────────────────────────────────
def get_auth_token():
    script_dir = "/home/asvetlove/wbk_control"
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()
def get_converting_datasets() -> dict[str, str]:
    """Return {name: status} for datasets that are mid-conversion.
    Excludes known orphans, UI-deleted datasets, and unavailable datasets."""
    token = get_auth_token()
    r = requests.get(
        f"{WK_URL}/api/datasets",
        headers={"X-Auth-Token": token},
        params={"isActive": "false"},
        timeout=10,
    )
    r.raise_for_status()

    skip_statuses = {
        "Deleted by user.",
        "No longer available on datastore.",
    }

    converting = {}
    for ds in r.json():
        src = ds["dataSource"]
        status = src.get("status", "")
        if ds.get("isUnreported") or status in skip_statuses:
            continue
        converting[ds["name"]] = status

    return converting


def get_ram_percent() -> tuple[float, str]:
    """Return (percent_used, human_readable_string) e.g. (37.2, '187GB / 503GB')."""
    meminfo = Path("/proc/meminfo").read_text()
    values = {}
    for line in meminfo.splitlines():
        key, val = line.split(":")[0], line.split(":")[1].strip()
        values[key] = int(val.split()[0])  # all values in kB

    total     = values["MemTotal"]
    available = values["MemAvailable"]
    used      = total - available
    percent   = used / total * 100

    def to_gb(kb: int) -> str:
        return f"{kb / 1_048_576:.0f}GB"

    return percent, f"{to_gb(used)} / {to_gb(total)}"


def notify(message: str, priority: str = "default", tags: str = "") -> None:
    headers = {"Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        print(f"ntfy post failed: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"converting": {}, "ram_alert_active": False}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    state    = load_state()
    previous = state.get("converting", {})
    ram_was_alerting = state.get("ram_alert_active", False)

    # ── dataset state changes ─────────────────────────────────────────────────
    current = get_converting_datasets()

    started  = set(current)  - set(previous)
    finished = set(previous) - set(current)

    for name in sorted(started):
        print(f"started:  {name}")
        notify(f"Converting started: {name}", tags="arrows_counterclockwise")

    for name in sorted(finished):
        print(f"finished: {name}")
        notify(f"Conversion finished: {name}", tags="white_check_mark")

    if not started and not finished:
        print(f"no change — {len(current)} converting")

    # ── RAM check ─────────────────────────────────────────────────────────────
    ram_pct, ram_str = get_ram_percent()
    print(f"RAM: {ram_str} ({ram_pct:.1f}%)")

    if ram_pct >= RAM_THRESHOLD and not ram_was_alerting:
        notify(
            f"RAM high: {ram_str} ({ram_pct:.0f}%)",
            priority="high",
            tags="warning",
        )
        state["ram_alert_active"] = True

    elif ram_pct < RAM_THRESHOLD and ram_was_alerting:
        notify(
            f"RAM back to normal: {ram_str} ({ram_pct:.0f}%)",
            tags="relieved",
        )
        state["ram_alert_active"] = False

    # ── save state ────────────────────────────────────────────────────────────
    state["converting"] = current
    save_state(state)


if __name__ == "__main__":
    main()