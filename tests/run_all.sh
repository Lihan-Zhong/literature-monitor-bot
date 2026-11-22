#!/usr/bin/env bash
# Offline tests for the LLM-stage (triage + judge) failure handling. No LLM call, no network,
# temp files only. Usage: bash tests/run_all.sh [python3]
set -u
PY="${1:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1  # keep __pycache__ out of the tree
rc=0
for t in test_judge_recovery.py test_call_claude.py test_fallback.py test_push_ledger.py \
         test_triage_refusal.py; do
    echo "== $t"
    "$PY" "$HERE/$t" 2>/dev/null || rc=1
done
echo "== test_classify_llm_fail.sh"
bash "$HERE/test_classify_llm_fail.sh" || rc=1
[ "$rc" -eq 0 ] && echo "== ALL TEST FILES PASS" || echo "== SOME TESTS FAILED"
exit "$rc"
