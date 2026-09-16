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
    /usr/bin/sbatch "$ROOT/telegram/run_biorxiv.sbatch"
    echo
} >> "$LOG" 2>&1
