#!/usr/bin/env bash
# Wrapper: fetch bioRxiv -> LLM judge -> push to Discord + archive Markdown.
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

# Failure alerts go to the Discord webhook: push_discord.py --text resolves the
# webhook from state/discord_webhook.txt and handles JSON/429 retries.
alert() {
    local msg="$1"
    "$PY" "$ROOT/discord/push_discord.py" --text "⚠️ $msg" > /dev/null 2>&1 || true
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
    # quiet day — push a ⚠️ banner and skip the LLM. Papers are re-fetched later
    # (nothing was cached), so no loss.
    : > "$CAND"
    echo "[run] FETCH FAILED (exit ${FETCH_EXIT}) — pushing warning, skipping LLM"
    # Self-healing auto-retry: bioRxiv's API outages are usually transient
    # (minutes–an hour). Rather than wait ~12h for the next cron, schedule a
    # delayed retry via `sbatch --begin`. LIT_FETCH_RETRY is a counter carried
    # into the retry job so it can't loop forever; a retry that succeeds just
    # completes normally. Tunables: LIT_MAX_FETCH_RETRY (default 2),
    # LIT_FETCH_RETRY_DELAY_MIN (default 30).
    FETCH_RETRY="${LIT_FETCH_RETRY:-0}"
    MAX_FETCH_RETRY="${LIT_MAX_FETCH_RETRY:-2}"
    RETRY_DELAY_MIN="${LIT_FETCH_RETRY_DELAY_MIN:-30}"
    if [ "$FETCH_RETRY" -ge "$MAX_FETCH_RETRY" ]; then
      FETCH_NOTICE="bioRxiv API 抓取失败（exit ${FETCH_EXIT}）；已连续自动重试 ${FETCH_RETRY} 次仍失败，等下一趟 cron"
      echo "[run] auto-retry cap reached (${FETCH_RETRY}/${MAX_FETCH_RETRY})"
      alert "⚠️ lit-bot: bioRxiv fetch FAILED; auto-retry cap (${FETCH_RETRY}) reached, waiting for next cron."
    elif command -v sbatch >/dev/null 2>&1 && \
         sbatch --begin="now+${RETRY_DELAY_MIN}minutes" \
                --export=ALL,LIT_FETCH_RETRY=$((FETCH_RETRY+1)),LIT_BIORXIV_DAYS=${LIT_BIORXIV_DAYS:-2} \
                "$ROOT/discord/run_biorxiv.sbatch" >/dev/null 2>&1; then
      FETCH_NOTICE="bioRxiv API 抓取失败（exit ${FETCH_EXIT}）；已安排 ${RETRY_DELAY_MIN} 分钟后自动重试（第 $((FETCH_RETRY+1))/${MAX_FETCH_RETRY} 次）"
      echo "[run] scheduled auto-retry #$((FETCH_RETRY+1)) in ${RETRY_DELAY_MIN}min via sbatch --begin"
      alert "⚠️ lit-bot: bioRxiv fetch FAILED (exit ${FETCH_EXIT}) @ $(date -Iseconds); auto-retry #$((FETCH_RETRY+1)) in ${RETRY_DELAY_MIN}min."
    else
      FETCH_NOTICE="bioRxiv API 抓取失败（exit ${FETCH_EXIT}）；自动重试调度失败，等下一趟 cron"
      echo "[run] could not schedule auto-retry (sbatch unavailable?)"
      alert "⚠️ lit-bot: bioRxiv fetch FAILED (exit ${FETCH_EXIT}) @ $(date -Iseconds); could not schedule auto-retry, waiting for next cron."
    fi
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

  # 4) Push to Discord + archive (always sends, even if zero hits).
  "$PY" "$ROOT/discord/push_discord.py" \
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
