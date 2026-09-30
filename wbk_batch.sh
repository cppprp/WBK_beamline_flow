#!/bin/bash
#
# Submit a whole beamtime: one sbatch job per dataset.
#
# This script does no I/O of its own and runs on the login node. Every line of
# the list becomes an independent job, so one failure does not stop the rest,
# each gets its own log, and SLURM decides the order.
#
# Two steps, because publishing is hard to undo. First find what is there:
#
#   ./wbk_batch.sh --scan /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615
#
# That submits a short job which writes a list file and submits nothing else.
# Read it, delete the lines you do not want, then:
#
#   ./wbk_batch.sh beamtime.txt --voxel-size 650
#
# LIST FORMAT
#
# Each non-comment line is the argument list for one submission — exactly what
# you would type after wbk_publish_job.sh. Arguments given to this script are
# appended to every line, so the common ones are written once:
#
#   # tomograms, using the beamline CLI (the default)
#   /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1
#   /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB013/recon_111_1
#   /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB014/recon_111_1  --voxel-size 325
#
#   # segmentations, using the manual CLI — prefix the line with 'manual:'
#   manual: --input_dir /mnt/beegfs7/P14/2026/p3l-doejw/RECON/20260615/MB012/stitching_1 \
#           --stack_path /mnt/bigjbod6/2026/p3l-doejw/SEG/MB012/labels \
#           --layer_name predictions --layer_type segmentation
#
# Per-line arguments come after the common ones, so they win. Blank lines and
# lines starting with # are ignored. A trailing \ continues a line.
#
# Segmentations do not need --dataset-id: with --input_dir pointing at the run
# directory, the id is read from that run's record in meta_<run>.json.
#
# OPTIONS
#   --scan DIR      submit a discovery job that writes a list, then stop
#   --only PATTERN  with --scan: only runs matching, e.g. 'stitching_*'
#   --out FILE      with --scan: where to write the list
#   --check         print the submissions without submitting anything
#
# Environment: WBK_NODE, WBK_LOGS, WBK_HOME and SBATCH_ARGS behave as they do
# for wbk_publish_job.sh, and are passed through.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
JOB="$HERE/wbk_publish_job.sh"
UV="${UV:-/mx-beta/ximg-beta/uv/uv}"

fail() { echo "error: $*" >&2; exit 2; }
[[ -x "$JOB" ]] || fail "wbk_publish_job.sh not found beside this script"

if [[ $# -eq 0 ]]; then
    sed -n '3,50p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

# ── Discovery ─────────────────────────────────────────────────────────────────

if [[ "$1" == "--scan" ]]; then
    [[ $# -ge 2 ]] || fail "--scan needs a directory"
    root="$2"; shift 2
    out=""; only=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --out)  out="$2"; shift 2 ;;
            --only) only+=("--only" "$2"); shift 2 ;;
            *)      fail "unexpected argument after --scan: $1" ;;
        esac
    done
    # Default the list next to the checkout, named after the directory scanned,
    # so two scans of different beamtimes do not overwrite each other.
    [[ -n "$out" ]] || out="$HERE/$(basename "${root%/}").batch"

    echo "scanning $root"
    echo "the list will be written to $out"
    WBK_MODULE=wbk_publish.discover exec "$JOB" \
        --root "$root" --out "$out" ${only[@]+"${only[@]}"}
fi

# ── Submission ────────────────────────────────────────────────────────────────

LIST="$1"; shift
[[ -f "$LIST" ]] || fail "list file not found: $LIST"

CHECK=0
DRYRUN=0
common=()
for arg in "$@"; do
    case "$arg" in
        --check)   CHECK=1 ;;
        --dry-run) DRYRUN=1; common+=("$arg") ;;
        *)         common+=("$arg") ;;
    esac
done

# A dry run of a whole list goes into one job rather than one per line. The
# checks take seconds each; it is the exclusive allocation that is expensive,
# and forty of them would hold the node against everyone else all morning.
PLAN=""
if [[ $DRYRUN -eq 1 && $CHECK -eq 0 ]]; then
    PLAN="$LIST.plan"
    : > "$PLAN"
fi

# Sync once here rather than once per line: the wrapper does it at submit time,
# and forty identical syncs is forty seconds of nothing.
if [[ $CHECK -eq 0 ]]; then
    [[ -x "$UV" ]] || fail "uv not found at $UV. Set UV=/path/to/uv."
    echo "syncing environment in $HERE …"
    "$UV" sync --frozen --directory "$HERE" \
        || fail "uv sync failed; run 'uv lock' if you changed dependencies"
    export WBK_SKIP_SYNC=1
fi

MANIFEST="$LIST.submitted"
: > "$MANIFEST"

submitted=0
lineno=0
pending=""

submit_line() {
    local line="$1" module="wbk_publish.beamline"
    if [[ "$line" == manual:* ]]; then
        module="wbk_publish.manual"
        line="${line#manual:}"
    fi

    # Word-split deliberately: the line is an argument list, not one argument.
    # shellcheck disable=SC2206
    local args=($line)
    local all=(${common[@]+"${common[@]}"} "${args[@]}")

    # What to call this submission in the output and the manifest. A beamline
    # line starts with the path; a manual one names it after --input_dir, which
    # is the only part a human will recognise later.
    local label="${args[0]}"
    local i
    for ((i = 0; i < ${#args[@]} - 1; i++)); do
        if [[ "${args[i]}" == "--input_dir" || "${args[i]}" == "--input-dir" ]]; then
            label="${args[i + 1]}"
            break
        fi
    done

    if [[ $CHECK -eq 1 ]]; then
        echo "  WBK_MODULE=$module $JOB ${all[*]}"
        return
    fi

    # Dry runs are collected and handed to one job at the end.
    if [[ -n "$PLAN" ]]; then
        printf '%s' "$module" >> "$PLAN"
        printf '\t%s' "${all[@]}" >> "$PLAN"
        printf '\n' >> "$PLAN"
        echo "  will check  $label"
        submitted=$((submitted + 1))
        return
    fi

    local output
    output=$(WBK_MODULE="$module" "$JOB" "${all[@]}" 2>&1) || {
        echo "  FAILED TO SUBMIT (line $lineno): $label" >&2
        echo "$output" | sed 's/^/    /' >&2
        printf '%s\t%s\t%s\n' "SUBMIT-FAILED" "line $lineno" "$label" >> "$MANIFEST"
        return
    }
    local jobid
    jobid=$(echo "$output" | grep -oE 'Submitted batch job [0-9]+' | grep -oE '[0-9]+$' || true)
    echo "  ${jobid:-?}  $label"
    printf '%s\t%s\t%s\n' "${jobid:-?}" "${WBK_LOGS:-$HERE/logs}/wbk_${jobid:-unknown}.log" "$label" >> "$MANIFEST"
    submitted=$((submitted + 1))
}

echo "reading $LIST"
[[ ${#common[@]} -gt 0 ]] && echo "applying to every line: ${common[*]}"
echo

while IFS= read -r raw || [[ -n "$raw" ]]; do
    lineno=$((lineno + 1))
    line="$pending$raw"
    pending=""

    # Line continuation.
    if [[ "$line" == *\\ ]]; then
        pending="${line%\\} "
        continue
    fi

    # Comments and blanks.
    [[ -z "${line// }" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue

    submit_line "$line"
done < "$LIST"

echo
if [[ $CHECK -eq 1 ]]; then
    echo "--check: nothing was submitted"
    rm -f "$MANIFEST"
elif [[ -n "$PLAN" ]]; then
    rm -f "$MANIFEST"
    if [[ $submitted -eq 0 ]]; then
        echo "nothing to check"
        rm -f "$PLAN"
        exit 0
    fi
    echo
    echo "submitting one job to dry-run all $submitted of them"
    WBK_MODULE=wbk_publish.batch_check SBATCH_ARGS="${SBATCH_ARGS:-} --time=01:00:00" \
        exec "$JOB" --plan "$PLAN"
else
    echo "$submitted job(s) submitted; manifest written to $MANIFEST"
    echo "watch them with:  squeue -u \$USER"
fi
