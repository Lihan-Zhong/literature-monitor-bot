#!/usr/bin/env bash
# Wrapper: fetch bioRxiv -> LLM judge -> push to Telegram + archive Markdown.
# Called either directly from cron or via run_biorxiv.sbatch. All paths are
# absolute so a stripped cron / sbatch environment doesn't matter.
# Logs append to logs/biorxiv-YYYY-MM-DD.log.

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
LOG="$LOG_DIR/biorxiv-$DATE.log"
CAND="$TMP_DIR/candidates-$STAMP.jsonl"
JUDGED="$TMP_DIR/judged-$STAMP.jsonl"
STDERR_FETCH="$TMP_DIR/fetch-$STAMP.stderr"

# Failure alerts go to Telegram: push_telegram.py --text sends a one-off message
# using the bot token + chat id in config/telegram.env.
alert() {
    local msg="$1"
    "$PY" "$ROOT/telegram/push_telegram.py" --text "⚠️ $msg" > /dev/null 2>&1 || true
}

# Trap any unexpected exit and notify the user.
trap 'rc=$?; if [ "$rc" -ne 0 ]; then alert "⚠️ lit-bot biorxiv run FAILED (exit $rc) at $(date -Iseconds). Check $LOG"; fi' EXIT

{
  echo "===== run @ $(date -Iseconds) ====="

  # 1) Fetch + Stage-1 category whitelist + keyword pre-filter (free, no LLM).
  # Default: rolling --days window. For targeted backfills set LIT_BIORXIV_FROM
  # (and optionally LIT_BIORXIV_TO); use SMALL windows — the details API 504s on
  # big ranges. cache dedup guarantees no duplicate pushes either way.
  FETCH_ARGS="--days ${LIT_BIORXIV_DAYS:-2}"
  if [ -n "${LIT_BIORXIV_FROM:-}" ]; then
    FETCH_ARGS="--from ${LIT_BIORXIV_FROM} --to ${LIT_BIORXIV_TO:-$(date +%Y-%m-%d)}"
  fi
  "$PY" "$SCRIPTS/fetch_biorxiv.py" $FETCH_ARGS \
        > "$CAND" 2> "$STDERR_FETCH"
  FETCH_EXIT=$?
  cat "$STDERR_FETCH"
  FETCH_NOTICE=""
  if [ "$FETCH_EXIT" -ne 0 ]; then
    # Fetch crashed (bioRxiv API down after retries). Do NOT present this as a
    # quiet day — push a ⚠️ banner and skip the LLM. Papers are re-fetched on the
    # next cron (nothing was cached), so no loss.
    : > "$CAND"
    FETCH_NOTICE="bioRxiv API 抓取失败（exit ${FETCH_EXIT}：504/超时/JSON），本次未扫，下次 cron 自动重试"
    echo "[run] FETCH FAILED (exit ${FETCH_EXIT}) — pushing warning, skipping LLM"
    alert "⚠️ lit-bot: bioRxiv fetch FAILED (exit ${FETCH_EXIT}) @ $(date -Iseconds). Retry next cron."
  fi
  N_CAND=$(wc -l < "$CAND" | tr -d ' ')
  SCANNED=$(grep -oE 'stats=\{[^}]*\}' "$STDERR_FETCH" | head -1 \
            | sed -E 's/.*"scanned":[[:space:]]*([0-9]+).*/\1/')
  SCANNED="${SCANNED:-0}"
  WIN_FROM=$(grep -oE 'querying biorxiv [0-9-]+ -> [0-9-]+' "$STDERR_FETCH" \
             | awk '{print $3}')
  WIN_TO=$(grep -oE 'querying biorxiv [0-9-]+ -> [0-9-]+' "$STDERR_FETCH" \
           | awk '{print $5}')
  echo "[run] candidates=$N_CAND scanned=$SCANNED window=$WIN_FROM..$WIN_TO"

  TRIAGED="$TMP_DIR/triaged-$STAMP.jsonl"
  QUOTA_HIT=0

  # 2) Stage-2 title triage (one batched LLM call, ~3K tokens for ~50 titles).
  if [ "$N_CAND" -gt 0 ]; then
    "$PY" "$SCRIPTS/triage_titles.py" --keep yes,maybe < "$CAND" \
          > "$TRIAGED" 2> "$TMP_DIR/triage-$STAMP.stderr"
    TRIAGE_EXIT=$?
    cat "$TMP_DIR/triage-$STAMP.stderr"
    if [ "$TRIAGE_EXIT" -eq 42 ]; then
      QUOTA_HIT=1
      echo "[run] quota exhausted in triage; bailing"
      alert "⚠️ lit-bot: quota exhausted at Stage 2 (triage) @ $(date -Iseconds). Will retry on next cron."
      : > "$JUDGED"
    fi
    N_TRIAGED=$(wc -l < "$TRIAGED" 2>/dev/null | tr -d ' ')
    N_TRIAGED="${N_TRIAGED:-0}"
    echo "[run] triage_passed=$N_TRIAGED (yes+maybe)"
  else
    : > "$TRIAGED"
    N_TRIAGED=0
  fi

  # 3) Stage-3 full abstract judge on the yes+maybe subset only.
  if [ "$QUOTA_HIT" -eq 0 ] && [ "$N_TRIAGED" -gt 0 ]; then
    "$PY" "$SCRIPTS/llm_judge.py" --batch 8 --keep-all < "$TRIAGED" \
          > "$JUDGED" 2> "$TMP_DIR/judge-$STAMP.stderr"
    JUDGE_EXIT=$?
    cat "$TMP_DIR/judge-$STAMP.stderr"
    if [ "$JUDGE_EXIT" -eq 42 ]; then
      QUOTA_HIT=1
      echo "[run] quota exhausted in judge; pushing what we have"
      alert "⚠️ lit-bot: quota exhausted at Stage 3 (judge) @ $(date -Iseconds). Already-judged papers pushed; rest retry next cron."
    fi
    N_JUDGED=$(wc -l < "$JUDGED" | tr -d ' ')
  else
    : > "$JUDGED"
    N_JUDGED=0
  fi
  echo "[run] judged=$N_JUDGED quota_hit=$QUOTA_HIT"

  # 4) Push to Telegram + archive (always sends, even if zero hits).
  "$PY" "$ROOT/telegram/push_telegram.py" \
        --source bioRxiv \
        --scanned "$SCANNED" \
        --keyword-pass "$N_CAND" \
        --triage-pass "$N_TRIAGED" \
        --quota-hit "$QUOTA_HIT" \
        --window-from "$WIN_FROM" \
        --window-to "$WIN_TO" \
        --notice "${FETCH_NOTICE:-}" \
        < "$JUDGED" 2>&1

  # 4) Cleanup tmp files older than 7 days (keep recent ones for debug).
  find "$TMP_DIR" -name '*.jsonl' -mtime +7 -delete 2>/dev/null || true
  find "$TMP_DIR" -name '*.stderr' -mtime +7 -delete 2>/dev/null || true

  echo "===== done @ $(date -Iseconds) ====="
  echo
} >> "$LOG" 2>&1
