#!/usr/bin/env bash
# Wrapper for the weekly journal scan: fetch (NCBI) → triage → judge → push.
# Mirrors run_biorxiv.sh but (a) no Stage-1 category whitelist (journals are
# pre-filtered by config/journals.json), (b) lookback = 8 days for weekly cron.

set -uo pipefail

ROOT="/PATH/TO/literatures"
PY="/PATH/TO/miniconda3/bin/python3"
CLAUDE="$HOME/.local/bin/claude"
SCRIPTS="$ROOT/scripts"
LOG_DIR="$ROOT/logs"
TMP_DIR="$ROOT/cache/tmp"

export PATH="$(dirname "$CLAUDE"):$PATH"
export HOME="${HOME:-$HOME}"

mkdir -p "$LOG_DIR" "$TMP_DIR"
# Run from ROOT so `claude -p` runs in this project's directory (keeps its
# transcripts / any cwd-scoped state tied to the project rather than $HOME).
cd "$ROOT"
DATE=$(date +%Y-%m-%d)
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/journals-$DATE.log"
CAND="$TMP_DIR/journals-cand-$STAMP.jsonl"
TRIAGED="$TMP_DIR/journals-triaged-$STAMP.jsonl"
JUDGED="$TMP_DIR/journals-judged-$STAMP.jsonl"
STDERR_FETCH="$TMP_DIR/journals-fetch-$STAMP.stderr"

# Failure alerts go to Telegram (push_telegram.py --text).
alert() {
    local msg="$1"
    "$PY" "$ROOT/telegram/push_telegram.py" --text "⚠️ $msg" > /dev/null 2>&1 || true
}

trap 'rc=$?; if [ "$rc" -ne 0 ]; then alert "⚠️ lit-bot journals run FAILED (exit $rc) at $(date -Iseconds). Check $LOG"; fi' EXIT

{
    echo "===== journals run @ $(date -Iseconds) ====="

    # Stage 1: fetch via NCBI E-utilities + keyword pre-filter.
    "$PY" "$SCRIPTS/fetch_journals.py" --days "${LIT_JOURNALS_DAYS:-8}" \
          > "$CAND" 2> "$STDERR_FETCH"
    # Vita (China-led, ISSN 2097-7468, DOI 10.15302) is not in PubMed → harvest
    # via its OAI-PMH feed (has abstracts) and APPEND to the candidate pool.
    # Non-fatal: a down feed never blocks the journals run.
    "$PY" "$SCRIPTS/fetch_vita.py" >> "$CAND" 2>> "$STDERR_FETCH" \
        || echo "[fetch_vita] non-fatal failure, skipping Vita" >> "$STDERR_FETCH"
    cat "$STDERR_FETCH"
    N_CAND=$(wc -l < "$CAND" | tr -d ' ')
    SCANNED=$(grep -oE 'stats=\{[^}]*\}' "$STDERR_FETCH" | head -1 \
              | sed -E 's/.*"scanned":[[:space:]]*([0-9]+).*/\1/')
    SCANNED="${SCANNED:-0}"
    WIN_FROM=$(grep -oE 'window [0-9/]+ . [0-9/]+' "$STDERR_FETCH" | awk '{print $2}')
    WIN_TO=$(grep -oE 'window [0-9/]+ . [0-9/]+' "$STDERR_FETCH" | awk '{print $4}')
    # Journal-coverage warning (feed-audit mode 7): a journal empty ≥2 runs.
    COV_WARN=$(grep -oE 'coverage_warning=.*' "$STDERR_FETCH" | head -1 | sed 's/coverage_warning=//')
    echo "[run] candidates=$N_CAND scanned=$SCANNED window=$WIN_FROM..$WIN_TO"
    [ -n "$COV_WARN" ] && echo "[run] coverage_warning: $COV_WARN"

    QUOTA_HIT=0

    # Stage 2: title triage.
    if [ "$N_CAND" -gt 0 ]; then
        "$PY" "$SCRIPTS/triage_titles.py" --keep yes,maybe < "$CAND" \
              > "$TRIAGED" 2> "$TMP_DIR/journals-triage-$STAMP.stderr"
        TRIAGE_EXIT=$?
        cat "$TMP_DIR/journals-triage-$STAMP.stderr"
        if [ "$TRIAGE_EXIT" -eq 42 ]; then
            QUOTA_HIT=1
            alert "⚠️ lit-bot journals: quota at Stage 2 @ $(date -Iseconds)."
            : > "$JUDGED"
        fi
        N_TRIAGED=$(wc -l < "$TRIAGED" 2>/dev/null | tr -d ' ')
        N_TRIAGED="${N_TRIAGED:-0}"
        echo "[run] triage_passed=$N_TRIAGED"
    else
        : > "$TRIAGED"; N_TRIAGED=0
    fi

    # Stage 3: full abstract judge.
    if [ "$QUOTA_HIT" -eq 0 ] && [ "$N_TRIAGED" -gt 0 ]; then
        "$PY" "$SCRIPTS/llm_judge.py" --batch 8 --keep-all < "$TRIAGED" \
              > "$JUDGED" 2> "$TMP_DIR/journals-judge-$STAMP.stderr"
        JUDGE_EXIT=$?
        cat "$TMP_DIR/journals-judge-$STAMP.stderr"
        if [ "$JUDGE_EXIT" -eq 42 ]; then
            QUOTA_HIT=1
            alert "⚠️ lit-bot journals: quota at Stage 3 @ $(date -Iseconds)."
        fi
        N_JUDGED=$(wc -l < "$JUDGED" | tr -d ' ')
    else
        : > "$JUDGED"; N_JUDGED=0
    fi
    echo "[run] judged=$N_JUDGED quota_hit=$QUOTA_HIT"

    # Stage 4: push to Telegram.
    "$PY" "$ROOT/telegram/push_telegram.py" \
          --source "Journals (CNS+sister)" \
          --scanned "$SCANNED" \
          --keyword-pass "$N_CAND" \
          --triage-pass "$N_TRIAGED" \
          --quota-hit "$QUOTA_HIT" \
          --window-from "$WIN_FROM" \
          --window-to "$WIN_TO" \
          --notice "${COV_WARN:-}" \
          < "$JUDGED" 2>&1

    find "$TMP_DIR" -name 'journals-*' -mtime +14 -delete 2>/dev/null || true
    echo "===== done @ $(date -Iseconds) ====="
    echo
} >> "$LOG" 2>&1
