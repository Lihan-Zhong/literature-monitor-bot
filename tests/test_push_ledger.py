"""Offline test of push_discord's judge-failure ledger (no network, temp
files only): a paper the judge could not score is NOT cached on its first
failed run (so the next cron retries it), is cached as score -1
("unjudgeable") once it has failed MAX_JUDGE_FAIL_RUNS runs, and a later
successful score clears its ledger entry."""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo: templates/*, deployed project: scripts/
for _d in ("templates/scripts", "templates/discord", "scripts"):
    if (ROOT / _d).is_dir():
        sys.path.insert(0, str(ROOT / _d))
import push_discord as pd  # noqa: E402

tmp = Path(tempfile.mkdtemp())
pd.JUDGE_FAIL_LEDGER = tmp / "judge_failures.json"
pd.CACHE_DB = tmp / "seen.sqlite"
bad = {"doi": "10.0/bad", "version": "1", "title": "Bad paper",
       "biorxiv_url": "https://example.org/bad", "judge_error": "unjudgeable: x", "score": 0}
good = {"doi": "10.0/good", "version": "1", "title": "Good", "score": 4, "relevant": True}
expected = {
    1: (1, 0, [("10.0/good", 4.0)]),
    2: (2, 1, [("10.0/bad", -1.0), ("10.0/good", 4.0)]),
}
fails = 0
for run in (1, 2):
    led = pd.load_fail_ledger()
    errored, scored = [dict(bad)], [dict(good)]
    capped = pd.apply_fail_ledger(led, errored, scored)
    pd.save_fail_ledger(led)
    pd.update_cache(scored + [{**p, "relevant": False, "score": -1} for p in capped])
    rows = sqlite3.connect(pd.CACHE_DB).execute(
        "select doi, score from seen order by doi").fetchall()
    got = (errored[0]["_fail_runs"], len(capped), [(d, float(s)) for d, s in rows])
    ok = got == expected[run]
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} run {run}: fail_runs={got[0]} capped={got[1]} seen={got[2]}")
led = pd.load_fail_ledger()
pd.apply_fail_ledger(led, [], [{"doi": "10.0/bad", "version": "1"}])
ok = list(led) == []
fails += not ok
print(f"{'PASS' if ok else 'FAIL'} a later success clears the ledger entry: {list(led)}")
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
