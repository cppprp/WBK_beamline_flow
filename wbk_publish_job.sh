#!/bin/bash
#SBATCH --job-name=wbk_publish
#SBATCH --nodes=1
#SBATCH --time=08:00:00
#
# Publish a dataset to WEBKNOSSOS as an sbatch job on the cluster.
#
# The job runs on a cluster node; morningstar is only the storage it writes to.
# The WEBKNOSSOS server hands the client paths under /morningstar/... to write
# into, so the node must reach morningstar at exactly that path — either mounted
# there, or via a symlink /morningstar -> wherever it is mounted. A mount at a
# different path cannot work, and nothing on the client side can remap it.
#
# Run it directly and it submits itself; inside the allocation it runs the
# conversion from the project's own environment. One command, no separate step:
#
#   ./wbk_publish_job.sh /mnt/beegfs7/P14/2025/p3l-eduke/RECON/20250921/MA040/recon_111_1/ --voxel-size 650
#
# Pin it to a node:
#
#   WBK_NODE=node042 ./wbk_publish_job.sh <input_dir> --voxel-size 650
#
# Manual/batch path:
#
#   WBK_MODULE=wbk_publish.manual ./wbk_publish_job.sh \
#       --input_dir /mnt/ximg/2025/p3l-eduke/RECON/20250921/MA040/stitching_1 \
#       --voxel_size 325 --project cardiac_study
#
# Check a path before spending an allocation. A dry run converts nothing, but it
# still goes through sbatch: the login node has neither the input filesystems nor
# morningstar, and the plan it prints is only right when produced on the node the
# real run will use. It is given a 15-minute limit rather than the usual eight
# hours, so it cannot sit on a node.
#
#   ./wbk_publish_job.sh <input_dir> --voxel-size 650 --dry-run
#
# Extra sbatch flags go through SBATCH_ARGS, e.g. a known-long stitch:
#
#   SBATCH_ARGS="--time=24:00:00" ./wbk_publish_job.sh <input_dir> --voxel-size 650
#
# Environment (all optional):
#   WBK_HOME    checkout containing wbk_publish/   default: the directory this script is in
#   WBK_MODULE  wbk_publish.beamline | .manual     default wbk_publish.beamline
#   WBK_NODE    a specific node to reserve         default: let SLURM choose
#   WBK_LOGS    where job logs go                  default $WBK_HOME/logs
#   WBK_LOCAL   run here instead of queueing      debugging escape hatch; warns
#   UV          the uv binary                      default /mx-beta/ximg-beta/uv/uv
#
# Anything else in the submitting environment (WK_TOKEN, WK_TIMEOUT, ...) reaches
# the job too; sbatch exports the environment by default.
#
# --exclusive is deliberate. A co-tenant job competing for cores and page cache is
# exactly the contention that makes thread oversubscription hurt, and it would
# invalidate the memory budget this job sizes itself against. Parallelism inside
# the job is chosen by wbk_publish/resources.py from the stack and the allocation.
#
# Run one dataset per node. Two datasets on two nodes drains a queue faster than
# one dataset striped across both: conversion parallelism is capped at
# ceil(n_slices / 32) tasks, and I/O saturates well before one dataset can use
# a second node.

set -euo pipefail

WBK_MODULE="${WBK_MODULE:-wbk_publish.beamline}"
WBK_NODE="${WBK_NODE:-}"
UV="${UV:-/mx-beta/ximg-beta/uv/uv}"

# uv-managed Python interpreters go on shared storage next to uv itself, so the
# project venv resolves on every node. The default is under $HOME, which may not
# be shared between the login node and the compute nodes.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(dirname "$UV")/python}"

if [[ $# -eq 0 ]]; then
    sed -n '7,55p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

fail() { echo "error: $*" >&2; exit 2; }

# ── Submit ────────────────────────────────────────────────────────────────────
# Outside an allocation (and not asked to run in place), hand ourselves to sbatch.
# WBK_HOME and the log location are resolved *here*, where $0 still points at the
# real checkout — inside the job, sbatch has copied the script to its spool
# directory and $0 is useless. Exporting them is what carries them into the job.
#
# The environment is synced here too, on the login node, so that the compute
# node finds a ready venv and two jobs starting at once never race to build it.

if [[ -z "${SLURM_JOB_ID:-}" && -z "${WBK_LOCAL:-}" ]]; then
    export WBK_HOME="${WBK_HOME:-$(cd "$(dirname "$0")" && pwd -P)}"
    export WBK_LOGS="${WBK_LOGS:-$WBK_HOME/logs}"
    [[ -d "$WBK_HOME/wbk_publish" ]] || fail "no wbk_publish package under WBK_HOME=$WBK_HOME"
    mkdir -p "$WBK_LOGS"

    # wbk_batch.sh syncs once before submitting a whole list; forty identical
    # syncs would be forty seconds of nothing. uv is only needed for the sync,
    # so skipping it removes the dependency here too — the job itself runs
    # .venv/bin/python directly.
    if [[ -z "${WBK_SKIP_SYNC:-}" ]]; then
        [[ -x "$UV" ]] || fail "uv not found at $UV. Set UV=/path/to/uv."
        echo "syncing environment in $WBK_HOME …"
        "$UV" sync --frozen --directory "$WBK_HOME" \
            || fail "uv sync failed; the lockfile and pyproject.toml must agree (run 'uv lock' if you changed dependencies)"
    fi

    submit=("--output=$WBK_LOGS/wbk_%j.log")
    [[ -n "$WBK_NODE" ]] && submit+=("--nodelist=$WBK_NODE")

    # A dry run needs the same allocation shape as the real thing — the plan it
    # prints is derived from the node's cores and memory, so sizing it down
    # would report a plan that no real run will ever use. It only needs the
    # node for seconds, though, so cap the time rather than the resources.
    if [[ "$WBK_MODULE" == "wbk_publish.discover" ]]; then
        # A directory walk. It does not size itself from the allocation, so it
        # has no reason to hold a node others are queuing for.
        submit+=("--time=00:30:00" "--cpus-per-task=2" "--mem=8G" "--job-name=wbk_scan")
    elif [[ "$WBK_MODULE" == "wbk_publish.batch_check" ]]; then
        # Reports the same plan a real run would, so it needs the same
        # allocation; SBATCH_ARGS carries the longer time limit.
        submit+=("--exclusive" "--job-name=wbk_batchcheck")
    elif [[ "$WBK_MODULE" == "wbk_publish.batch_run" ]]; then
        # Real publishes, one after another, on one exclusive node. The time
        # limit is the default unless SBATCH_ARGS raises it for a long batch.
        submit+=("--exclusive" "--job-name=wbk_sequence")
    elif [[ " $* " == *" --dry-run "* || " $* " == *" --dry_run "* ]]; then
        submit+=("--exclusive" "--time=00:15:00" "--job-name=wbk_dryrun")
    else
        submit+=("--exclusive")
    fi
    # shellcheck disable=SC2206  # SBATCH_ARGS is deliberately word-split
    [[ -n "${SBATCH_ARGS:-}" ]] && submit+=(${SBATCH_ARGS})

    echo "submitting: $WBK_MODULE $*"
    echo "checkout:   $WBK_HOME"
    echo "logs:       $WBK_LOGS/wbk_<jobid>.log"
    [[ -n "$WBK_NODE" ]] && echo "node:       $WBK_NODE"
    exec sbatch "${submit[@]}" "$0" "$@"
fi

# ── Inside the allocation ─────────────────────────────────────────────────────

if [[ -n "${WBK_LOCAL:-}" ]]; then
    echo "warning: WBK_LOCAL is set, so this is running outside SLURM." >&2
    echo "         Conversions belong in an sbatch job: they need a whole node's" >&2
    echo "         cores and memory, and sizing reads the SLURM allocation to" >&2
    echo "         decide how many workers fit. Run without WBK_LOCAL unless you" >&2
    echo "         are deliberately debugging on a host you have to yourself." >&2
fi

WBK_HOME="${WBK_HOME:-$(cd "$(dirname "$0")" && pwd -P)}"   # WBK_LOCAL path
[[ -d "$WBK_HOME/wbk_publish" ]] || fail "no wbk_publish package under WBK_HOME=$WBK_HOME"

# The storage mounts. Checked here, before Python starts, because a missing
# /morningstar would otherwise let the pipeline create a local /morningstar/tmp
# on the node and stage a conversion into it. readlink -f so that a symlink to
# the real mount point passes too.
# Only /morningstar is checked here, and only because its failure is dangerous:
# without the mount, the pipeline's staging directory would be created on the
# node's own disk and a conversion staged into it. Input filesystems vary
# (beegfs, ximg, the big JBODs) and are checked in Python against the path
# actually given, which produces a better message than a hardcoded list could.
for path in /morningstar; do
    [[ -e "$path" ]] && mountpoint -q "$(readlink -f "$path")" \
        || fail "$path is not reachable on $(hostname). It must be mounted there, or be a symlink to where it is mounted."
done

# The venv was synced at submit time. All we need on the node is that its
# interpreter resolves — which it will not if it points into an unshared $HOME.
PYTHON="$WBK_HOME/.venv/bin/python"
[[ -x "$PYTHON" ]] || fail "no venv at $WBK_HOME/.venv. Run '$UV sync --directory $WBK_HOME' on the login node."
"$PYTHON" -c "import webknossos" 2>/dev/null \
    || fail "the venv at $WBK_HOME/.venv does not work on $(hostname).
Its interpreter is probably under a \$HOME that this node does not share.
Rebuild it on the login node with the interpreter on shared storage:
  rm -rf $WBK_HOME/.venv
  UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR $UV sync --directory $WBK_HOME"

echo "host:       $(hostname)"
echo "job:        ${SLURM_JOB_ID:-<local>}"
echo "cpus:       ${SLURM_CPUS_ON_NODE:-<unset>}"
echo "mem (MB):   ${SLURM_MEM_PER_NODE:-<unset>}"
echo "module:     $WBK_MODULE"
echo "checkout:   $WBK_HOME"
echo "python:     $("$PYTHON" -c 'import sys; print(sys.version.split()[0])') ($PYTHON)"
echo "webknossos: $("$PYTHON" -c 'import webknossos; print(webknossos.current_version)')"
# Limits that turn a healthy allocation into std::bad_alloc: an address-space
# ulimit, or strict overcommit. Printed so a memory failure can be read off the log.
echo "ulimit -v:  $(ulimit -v)   overcommit: $(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo '?')"
echo

cd "$WBK_HOME"
exec "$PYTHON" -m "$WBK_MODULE" "$@"
