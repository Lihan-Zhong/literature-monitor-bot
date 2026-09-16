#!/usr/bin/env bash
#SBATCH --partition=YOUR_PARTITION
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=0-2:00
#SBATCH --requeue
#SBATCH --job-name=lit_journals
#SBATCH --output=/PATH/TO/literatures/logs/slurm/journals-%j.out
#SBATCH --mail-type=NONE

set -uo pipefail
exec /PATH/TO/literatures/discord/run_journals.sh
