#!/usr/bin/env bash
#SBATCH --partition=YOUR_PARTITION
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=0-1:00
#SBATCH --requeue
#SBATCH --job-name=lit_biorxiv
#SBATCH --output=/PATH/TO/literatures/logs/slurm/biorxiv-%j.out
#SBATCH --mail-type=NONE
#
# Production cron entry submits this. fetch -> judge -> push -> archive.
# Set --partition / --account above to a queue you have PRIORITY on (a lab
# condo/allocation is ideal). Production lesson: if the chosen partition
# saturates, the job sits PENDING for hours and silently never runs (no push) —
# pick a partition with headroom and watch `squeue`. Your SLURM association may
# pin a single partition (no multi-partition fallback list).

set -uo pipefail
exec /PATH/TO/literatures/telegram/run_biorxiv.sh
