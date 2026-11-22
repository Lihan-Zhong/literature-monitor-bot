#!/usr/bin/env bash
# Offline test of classify_llm_fail, which picks the ⚠️ banner reason
# (oauth / quota / overload / generic) after a Stage 2 or 3 exit 42. The cases
# are stderr shapes from real failures. Our own text must not decide the label:
# the "__QUOTA_EXHAUSTED__" / "QUOTA EXHAUSTED" markers, "quota_skipped=N",
# "SKIPPED (quota)" lines, and DOIs. Matching the whole stderr made every
# non-oauth failure read as quota. Tests every run_*.sh that defines it.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mapfile -t SRCS < <(grep -l "^classify_llm_fail()" "$ROOT"/scripts/run_*.sh "$ROOT"/templates/*/run_*.sh 2>/dev/null)
[ "${#SRCS[@]}" -gt 0 ] || { echo "FAIL no run_*.sh defines classify_llm_fail"; exit 1; }
fails=0

check() {  # check <case-name> <expected-label>  (stderr text on stdin)
    local name="$1" want="$2" src got bad=""
    cat > "$TMP/stderr"
    for src in "${SRCS[@]}"; do
        got=$(bash -c "$(sed -n '/^classify_llm_fail()/,/^}/p' "$src")"'
classify_llm_fail "$1"' _ "$TMP/stderr")
        [ "$got" = "$want" ] || bad+=" ${src#"$ROOT"/}->'$got'"
    done
    if [ -z "$bad" ]; then printf "PASS %-24s -> '%s'\n" "$name" "$want"
    else printf "FAIL %-24s want '%s':%s\n" "$name" "$want" "$bad"; fails=$((fails+1)); fi
}

check triage_529 overload <<'EOF'
__QUOTA_EXHAUSTED__ [hard failure] claude exit 1: 'API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment. If it persists, check https://status.claude.com.'
EOF
check triage_timeout "" <<'EOF'
[triage] 40 candidates → batching titles in chunks of 200
__QUOTA_EXHAUSTED__ [timeout after 300s]
EOF
check triage_weekly quota <<'EOF'
__QUOTA_EXHAUSTED__ [hard failure] claude exit 1: "You've hit your weekly limit · resets 1am (America/New_York)"
EOF
check triage_oauth oauth <<'EOF'
__QUOTA_EXHAUSTED__ [hard failure] claude exit 1: 'Failed to authenticate: OAuth session expired and could not be refreshed'
EOF
check judge_hard_failure "" <<'EOF'
[llm_judge] 12 candidates, batch_size=8
[llm_judge] QUOTA EXHAUSTED on batch 1: claude exit 1 [hard failure]: API Error: Connection error.
__QUOTA_EXHAUSTED__
[llm_judge] 10.64898/2026.11.14.766429 SKIPPED (quota)
[llm_judge] candidates=12 judged=0 kept=0 errored=0 quota_skipped=12
EOF
check judge_quota_signal quota <<'EOF'
[llm_judge] batch 1 (8 papers, 21.0s) scores=[1,2,1,3,4,2,0,1]
[llm_judge] QUOTA EXHAUSTED on batch 2: claude exit 1 [quota signal]: You've hit your usage limit · resets 3pm
__QUOTA_EXHAUSTED__
[llm_judge] candidates=16 judged=8 kept=8 errored=0 quota_skipped=8
EOF
check judge_rate_limit_note quota <<'EOF'
[llm_judge] QUOTA EXHAUSTED on batch 1: claude exit 1 [quota signal]: rate_limit rejected (seven_day)
__QUOTA_EXHAUSTED__
EOF
check judge_oauth_multiline oauth <<'EOF'
[llm_judge] QUOTA EXHAUSTED on batch 1: claude exit 1 [hard failure]: Note: this sign-in ends soon
Failed to authenticate. API Error: 401 authentication_error
__QUOTA_EXHAUSTED__
EOF
check judge_529_doi_429 overload <<'EOF'
[llm_judge] sonnet rescue OK for 10.64898/2026.11.14.766429: score 2
[llm_judge] QUOTA EXHAUSTED on batch 2: claude exit 1 [hard failure]: API Error: 529 Overloaded.
__QUOTA_EXHAUSTED__
EOF

[ "$fails" -eq 0 ] && echo "ALL PASS (${#SRCS[@]} run scripts)" || echo "$fails FAILED"
exit $(( fails > 0 ))
