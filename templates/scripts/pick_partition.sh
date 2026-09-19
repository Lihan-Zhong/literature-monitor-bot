#!/usr/bin/env bash
# Echo "<partition> <account>" for the best SLURM partition to submit to RIGHT NOW.
#
# Why: a job pinned to a saturated partition sits PENDING for hours — a SLURM
# association pins ONE partition per account, so a queued job can't migrate
# itself. Decide at SUBMIT time instead: prefer the highest-priority partition
# that has a free CPU; otherwise fall back to one with idle capacity so the job
# runs promptly. If nothing is idle, use the first (highest-priority) candidate.
#
# Candidates come from config/partitions.txt (see config/partitions.example.txt):
# one "<partition> <account>" per line, HIGHEST PRIORITY FIRST. The account must
# match the partition. Tunables: LIT_PARTITION_MIN_IDLE (default 1),
# LIT_PARTITIONS_FILE (override the config path).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${LIT_PARTITIONS_FILE:-$ROOT/config/partitions.txt}"
MIN_IDLE="${LIT_PARTITION_MIN_IDLE:-1}"

idle_cpus() {   # sinfo %C = "Alloc/Idle/Other/Total"; sum the Idle field
  sinfo -h -p "$1" -o "%C" 2>/dev/null | awk -F/ '{s+=$2} END{print s+0}'
}

first=""
if [ -f "$CONF" ]; then
  while read -r part acct _; do
    case "$part" in ""|\#*) continue ;; esac
    [ -z "$first" ] && first="$part $acct"
    n="$(idle_cpus "$part")"
    [ "${n:-0}" -ge "$MIN_IDLE" ] && { echo "$part $acct"; exit 0; }
  done < "$CONF"
fi
# Nothing idle (or no config file yet): fall back to the first configured pair.
echo "${first:-YOUR_PARTITION YOUR_ACCOUNT}"
