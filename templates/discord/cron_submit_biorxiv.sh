#!/usr/bin/env bash
# Cron entry point: submit the bioRxiv lit-bot via sbatch.
# Cron's environment is minimal, so we set PATH explicitly and use absolute
# paths. This script is what crontab calls; it returns immediately after the
# sbatch submission (the actual work runs on a compute node).
set -euo pipefail
export PATH="/usr/bin:/usr/local/bin:/bin:${PATH:-}"
ROOT="/PATH/TO/literatures"
LOG="$ROOT/logs/cron-biorxiv.log"
mkdir -p "$(dirname "$LOG")"
{
    echo "===== cron submit @ $(date -Iseconds) ====="
    # Prefer a partition with free capacity NOW (see config/partitions.txt) so the
    # job doesn't sit PENDING when your priority partition is full. If that file
    # isn't configured, fall back to the partition baked into the .sbatch file.
    if [ -f "$ROOT/config/partitions.txt" ] && [ -x "$ROOT/scripts/pick_partition.sh" ]; then
        read -r PART ACCT _ < <("$ROOT/scripts/pick_partition.sh")
        echo "[cron] selected partition=$PART account=$ACCT"
        /usr/bin/sbatch --partition="$PART" --account="$ACCT" "$ROOT/discord/run_biorxiv.sbatch"
    else
        /usr/bin/sbatch "$ROOT/discord/run_biorxiv.sbatch"
    fi
    echo
} >> "$LOG" 2>&1
