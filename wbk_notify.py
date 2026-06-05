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
  * * * * * /usr/bin/uv run /home/asvetlove/wbk_control/wbk_notify.py >> /home/asvetlove/wbk_control/logs/monitor.log 2>&1

.gitignore should include:
  .monitor_state.json
  logs/
"""

import json
import time
import shutil
import requests
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────

WK_URL          = "https://webknossos.embl-hamburg.de"
STATE_FILE      = Path("/home/asvetlove/wbk_control/.monitor_state.json")
NTFY_TOPIC      = "morningstar-p143mb1"   # change to your topic
RAM_THRESHOLD   = 80                   # percent — alert above this
DISK_PATH       = Path("/morningstar/webknossos/")
DISK_THRESHOLD  = 50                   # TB free — alert below this

# ── helpers ───────────────────────────────────────────────────────────────────
def get_auth_token():
    script_dir = Path(__file__).parent
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
    meminfo = Path("/proc/meminfo").read_text()
    values = {}
    for line in meminfo.splitlines():
        key, val = line.split(":")[0], line.split(":")[1].strip()
        values[key] = int(val.split()[0])

    total = values["MemTotal"]
    used  = values["AnonPages"] + values["Shmem"]
    percent = used / total * 100

    def to_gb(kb: int) -> str:
        return f"{kb / 1_048_576:.0f}GB"

    return percent, f"{to_gb(used)} / {to_gb(total)}"

def get_disk_free() -> tuple[float, str]:
    """Return (free_tb, human_readable_string) for the published data path."""
    usage = shutil.disk_usage(DISK_PATH)
    free_tb  = usage.free  / 1_099_511_627_776
    total_tb = usage.total / 1_099_511_627_776
    return free_tb, f"{free_tb:.1f}TB / {total_tb:.1f}TB free"


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
        text = STATE_FILE.read_text().strip()
        if text:
            return json.loads(text)
    return {"converting": {}, "start_times": {}, "ram_alert_active": False, "disk_alert_active": False}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    (STATE_FILE.parent / "logs").mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    state            = load_state()
    previous         = state.get("converting", {})
    start_times      = state.get("start_times", {})
    ram_was_alerting = state.get("ram_alert_active", False)
    disk_was_alerting = state.get("disk_alert_active", False)

    # ── dataset state changes ─────────────────────────────────────────────────
    current = get_converting_datasets()
    now     = time.time()

    started  = set(current)  - set(previous)
    finished = set(previous) - set(current)

    for name in sorted(started):
        start_times[name] = now
        print(f"started:  {name}")
        notify(f"Converting started: {name}", tags="arrows_counterclockwise")

    for name in sorted(finished):
        t0       = start_times.pop(name, None)
        duration = f" (~{(now - t0) / 60:.0f} min)" if t0 else ""
        print(f"finished: {name}{duration}")
        notify(f"Conversion finished{duration}: {name}", tags="white_check_mark")

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

    # ── disk check ────────────────────────────────────────────────────────────
    disk_free_tb, disk_str = get_disk_free()
    print(f"disk: {disk_str}")

    if disk_free_tb < DISK_THRESHOLD and not disk_was_alerting:
        notify(
            f"Disk space low: {disk_str}",
            priority="high",
            tags="warning",
        )
        state["disk_alert_active"] = True

    elif disk_free_tb >= DISK_THRESHOLD and disk_was_alerting:
        notify(
            f"Disk space restored: {disk_str}",
            tags="relieved",
        )
        state["disk_alert_active"] = False

    # ── save state ────────────────────────────────────────────────────────────
    state["converting"]        = current
    state["start_times"]       = start_times
    save_state(state)


if __name__ == "__main__":
    main()