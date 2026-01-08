#!/bin/bash
#SBATCH --job-name=wbk_test
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=48:00:00
#SBATCH --output=wbk_test_%j.log

# Simple test of WebKnossos SLURM distribution
# This controller job will spawn worker jobs automatically

python3 test_slurm_convert.py "$@"