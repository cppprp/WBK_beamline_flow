#!/usr/bin/env python3
"""
Simple test of WebKnossos SLURM distribution strategy.
Submit this as a controller job - it will spawn workers automatically.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        description="Test WebKnossos SLURM distribution"
    )
    parser.add_argument("input_path", help="Path to TIFF stack")
    parser.add_argument("output_path", help="Path for WebKnossos dataset")
    parser.add_argument("voxel_size", help="Voxel size (e.g., 11.24,11.24,11.24)")
    parser.add_argument(
        "--category",
        choices=["color", "segmentation"],
        default="color",
        help="Layer category (default: color)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=2,
        help="Number of worker jobs (default: 3)",
    )
    parser.add_argument(
        "--worker-mem",
        default="150G",
        help="Memory per worker (default: 150G)",
    )
    parser.add_argument(
        "--worker-time",
        default="24:00:00",
        help="Time per worker (default: 24:00:00)",
    )

    args = parser.parse_args()

    # Job resources for SLURM workers
    job_resources = {
        "mem": args.worker_mem,
        "time": args.worker_time,
        "cpus_per_task": 32,
    }

    # Build command
    cmd = [
        '/mx-beta/ximg-beta/uv/uvx', '--from', 'webknossos[all]', '--python', '3.12',
        'webknossos', 'convert',
        args.input_path,
        args.output_path,
        "--voxel-size", args.voxel_size,
        "--overwrite-existing",
        "--downsample",
        "--category", args.category,
        "--distribution-strategy", "slurm",
        "--jobs", str(args.jobs),
        "--job-resources", json.dumps(job_resources),
    ]

    print("=" * 70)
    print("WebKnossos SLURM Distribution Test")
    print("=" * 70)
    print(f"Started:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input:          {args.input_path}")
    print(f"Output:         {args.output_path}")
    print(f"Voxel size:     {args.voxel_size}")
    print(f"Category:       {args.category}")
    print(f"Worker jobs:    {args.jobs}")
    print(f"Worker memory:  {args.worker_mem}")
    print(f"Worker time:    {args.worker_time}")
    print("=" * 70)
    print()
    print("WebKnossos will now spawn worker jobs via sbatch...")
    print("Watch with: squeue -u $USER")
    print()
    print("=" * 70)
    print()

    # Run conversion
    result = subprocess.run(cmd)

    print()
    print("=" * 70)
    print(f"Finished:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Exit code:      {result.returncode}")
    print("=" * 70)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())