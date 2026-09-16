#!/usr/bin/env bash
# Cron entry point: submit weekly journal scan via sbatch (Saturdays 20:00).
set -euo pipefail
export PATH="/usr/bin:/usr/local/bin:/bin:${PATH:-}"
ROOT="/PATH/TO/literatures"
LOG="$ROOT/logs/cron-journals.log"
mkdir -p "$(dirname "$LOG")"
{
    echo "===== cron submit @ $(date -Iseconds) ====="
    /usr/bin/sbatch "$ROOT/telegram/run_journals.sbatch"
    echo
} >> "$LOG" 2>&1
