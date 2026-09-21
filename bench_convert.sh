#!/bin/bash
#SBATCH --job-name=wbk_bench
#SBATCH --nodes=1
#SBATCH --exclusive
#SBATCH --time=02:00:00
#
# Time the plain `webknossos convert` CLI on a cluster node — the command the old
# wbki_acp.py system ran — with a chosen library version and target directory.
#
# This exists to answer one question: why a conversion that took 4 minutes on
# morningstar takes 14 on a node writing to /morningstar over NFS. The pipeline
# itself is not involved, so the two variables are isolated cleanly:
#
#   version   the webknossos release (the old system ran 3.0.0, the new one 4.0.0)
#   target    where the zarr is written (NFS to morningstar, or beegfs)
#
# Three runs cover it:
#
#   ./bench_convert.sh <stack> 4.0.0 /morningstar/tmp/bench_A        # new lib, NFS
#   ./bench_convert.sh <stack> 3.0.0 /morningstar/tmp/bench_B        # old lib, NFS
#   ./bench_convert.sh <stack> 4.0.0 /mnt/beegfs7/<scratch>/bench_C  # new lib, beegfs
#
# Each log ends with the phase timings, the bytes that crossed each network
# interface during the run, NFS client counters, and the size of what was
# written — enough to compute effective MB/s per phase. The target directory is
# left in place for inspection; remove it yourself.
#
# Run it directly and it submits itself, like wbk_publish_job.sh.
#
# Environment (all optional):
#   BENCH_JOBS   --jobs for the CLI          default 24 (what the old system used)
#   BENCH_NODE   a specific node to reserve  default: let SLURM choose
#   BENCH_LOGS   where job logs go           default ./logs next to this script
#   UV           the uv binary               default /mx-beta/ximg-beta/uv/uv

set -euo pipefail

UV="${UV:-/mx-beta/ximg-beta/uv/uv}"
BENCH_JOBS="${BENCH_JOBS:-24}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(dirname "$UV")/python}"

if [[ $# -lt 3 ]]; then
    sed -n '7,34p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

STACK="$1"; VERSION="$2"; TARGET="$3"

fail() { echo "error: $*" >&2; exit 2; }

# ── Submit ────────────────────────────────────────────────────────────────────

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
    BENCH_LOGS="${BENCH_LOGS:-$(cd "$(dirname "$0")" && pwd -P)/logs}"
    [[ -x "$UV" ]] || fail "uv not found at $UV. Set UV=/path/to/uv."
    mkdir -p "$BENCH_LOGS"

    submit=("--output=$BENCH_LOGS/bench_%j.log")
    [[ -n "${BENCH_NODE:-}" ]] && submit+=("--nodelist=$BENCH_NODE")

    echo "submitting: webknossos $VERSION, $BENCH_JOBS jobs, $STACK -> $TARGET"
    echo "log:        $BENCH_LOGS/bench_<jobid>.log"
    exec sbatch "${submit[@]}" "$0" "$@"
fi

# ── Inside the allocation ─────────────────────────────────────────────────────

# The login node has neither mount, so the inputs can only be checked here.
for path in /morningstar /mnt/beegfs7; do
    [[ -e "$path" ]] && mountpoint -q "$(readlink -f "$path")" \
        || fail "$path is not reachable on $(hostname)"
done
[[ -d "$STACK" ]] || fail "stack directory not found: $STACK"
[[ -e "$TARGET" ]] && fail "target already exists: $TARGET (pick a fresh directory so the write is measured, not an overwrite)"

echo "host:       $(hostname)"
echo "job:        $SLURM_JOB_ID"
echo "cpus:       ${SLURM_CPUS_ON_NODE:-<unset>}"
echo "webknossos: $VERSION"
echo "jobs:       $BENCH_JOBS"
echo "stack:      $STACK ($(find "$STACK" -maxdepth 1 -name '*.tif*' | wc -l) tiffs)"
echo "target:     $TARGET"
echo "target fs:  $(df -hT "$(dirname "$TARGET")" | tail -1)"
echo

# Link speed and NFS mount options: what the storage path is capable of.
echo "── network interfaces ──"
for iface in /sys/class/net/*; do
    name=$(basename "$iface")
    [[ "$name" == lo ]] && continue
    speed=$(cat "$iface/speed" 2>/dev/null || echo "?")
    echo "  $name: ${speed} Mbit/s, $(cat "$iface/operstate")"
done
echo "── morningstar mount ──"
findmnt -T /morningstar -o SOURCE,FSTYPE,OPTIONS | tail -1 | sed 's/^/  /'
echo

net_snapshot() { awk 'NR>2 && $1 != "lo:" {gsub(":","",$1); print $1, $2, $10}' /proc/net/dev; }
NET_BEFORE=$(net_snapshot)
NFS_BEFORE=$(nfsstat -c -l 2>/dev/null || true)

# Timestamp what the CLI prints. Progress bars redraw with \r; each redraw
# becomes a line, and the awk keeps only the first one per distinct message so
# a phase boundary gets a time without thousands of percent ticks around it.
stamp() {
    tr '\r' '\n' | awk '{ key=$0; gsub(/[0-9━╸%.: ]+/, "", key); if (key != last) { print; last=key } }' \
        | while IFS= read -r line; do printf '%(%T)T  %s\n' -1 "$line"; done
}

echo "── conversion ──"
T0=$(date +%s)
set +e
"$UV" tool run --from "webknossos[all]==$VERSION" --python 3.12 \
    webknossos convert "$STACK" "$TARGET" \
    --compress --layer-name xray --category color \
    --jobs "$BENCH_JOBS" --voxel-size 650,650,650 --downsample \
    2>&1 | stamp
STATUS=${PIPESTATUS[0]}
set -e
T1=$(date +%s)
echo

NET_AFTER=$(net_snapshot)
NFS_AFTER=$(nfsstat -c -l 2>/dev/null || true)

echo "── result ──"
echo "exit status:  $STATUS"
echo "wall time:    $(( (T1 - T0) / 60 ))m$(( (T1 - T0) % 60 ))s"
echo "written:"
du -sh "$TARGET"/xray/* 2>/dev/null | sed 's/^/  /' || true
du -sh "$TARGET" | sed 's/^/  /'

# Phase timing from the files themselves, independent of what the CLI printed:
# a mag's zarr.json is written when the mag is initialised (phase start), and
# its newest shard when the last job finished (phase end).
echo "phases (from file times):"
for mag in "$TARGET"/xray/*/; do
    [[ -f "$mag/zarr.json" ]] || continue
    start=$(stat -c %Y "$mag/zarr.json")
    end=$(find "$mag" -type f -printf '%T@\n' | sort -n | tail -1 | cut -d. -f1)
    printf '  mag %-4s %s → %s  (%dm%02ds)\n' "$(basename "$mag")" \
        "$(date -d @"$start" +%T)" "$(date -d @"$end" +%T)" $(( (end - start) / 60 )) $(( (end - start) % 60 ))
done

echo "network bytes during the run (rx / tx, GiB):"
join <(echo "$NET_BEFORE" | sort) <(echo "$NET_AFTER" | sort) \
    | awk '{ rx=($4-$2)/2^30; tx=($5-$3)/2^30; if (rx > 0.1 || tx > 0.1) printf "  %-12s rx %7.1f   tx %7.1f\n", $1, rx, tx }'

if [[ -n "$NFS_AFTER" ]]; then
    echo "nfs client calls during the run:"
    # `nfsstat -c -l` prints one counter per line: "nfs v4 client  read: 1234".
    for op in read write commit; do
        b=$(echo "$NFS_BEFORE" | awk -v op="$op:" '$4==op{print $5; exit}')
        a=$(echo "$NFS_AFTER"  | awk -v op="$op:" '$4==op{print $5; exit}')
        [[ -n "$a" && -n "$b" ]] && printf "  %-8s %d\n" "$op" $((a - b))
    done
fi

exit "$STATUS"
