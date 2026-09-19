# HPC partition auto-fallback (avoid stuck-PENDING scans)

Keeps a scheduled `cron → sbatch` scan **running promptly even when your priority partition
is full**, instead of sitting `PENDING` for hours and silently missing its run.

## The problem

Cron submits a tiny job (1 CPU / 2 GB / 1 h) to your preferred partition. When that
partition is fully allocated, the job just queues for hours:

```
JOBID   PARTITION  STATE     START_TIME            NODELIST(REASON)
6149365 <priority> PENDING   ...T13:02:35          (Resources)     ← submitted 08:00
```

**Root cause:** a SLURM *association* pins **one partition per account**, so a job that is
already queued **cannot migrate itself**; and its own code only runs *after* it starts. The
decision has to be made **at submit time**, by whatever runs `sbatch`.

## The fix

Prefer your priority partition **when it has a free CPU**; otherwise submit to whichever
candidate partition currently has idle capacity, so the job backfills in immediately. This
repo ships it as an **opt-in** feature driven by a config file.

### 1. `config/partitions.txt` — your candidate list (highest priority first)

Copy the example and edit for your cluster:

```bash
cp config/partitions.example.txt config/partitions.txt
```
```
# "<partition> <account>" per line, highest priority first. Account MUST match partition.
my_priority_partition   my_priority_account     # small/fast, preferred
my_big_fallback         my_fallback_account     # large, usually idle
```

`config/partitions.txt` is git-ignored (per-deployment). If you never create it, submission
falls back to the partition baked into the `#SBATCH` directives of `run_*.sbatch` — i.e. the
feature is off and nothing changes.

### 2. `scripts/pick_partition.sh` — the selector

Reads `config/partitions.txt`, sums idle CPUs per partition via `sinfo`, and echoes the
first `"<partition> <account>"` that has a free CPU (else the first line). Tunables:
`LIT_PARTITION_MIN_IDLE` (default 1), `LIT_PARTITIONS_FILE`.

### 3. Wiring (already done in this repo)

- **`<channel>/cron_submit_*.sh`** — if `config/partitions.txt` exists, it picks a partition
  and passes `--partition/--account` (these override the `#SBATCH` directives); otherwise it
  submits plain.
- **`<channel>/run_biorxiv.sh` auto-retry** — the self-scheduled retry (`sbatch --begin`)
  picks a partition the same way, so the retry doesn't stall either.

## Testing

```bash
bash -n scripts/pick_partition.sh
scripts/pick_partition.sh                                  # what it picks now
LIT_PARTITION_MIN_IDLE=999999 scripts/pick_partition.sh    # simulate "all full" → first line
# simulate priority-full: set MIN_IDLE just above its idle count → expect a fallback line
```

## Gotchas

- **Decide at submit, not in the job** — a `PENDING` job can't move itself.
- **`sinfo`/`sbatch` must be on `PATH`** — cron's env is minimal; set
  `PATH="/usr/bin:/usr/local/bin:/bin:$PATH"`.
- **`sbatch --test-only` start-time estimates are pessimistic** — trust idle-CPU counts, not
  the estimate; a 1-CPU job backfills into an idle partition in seconds.
- **Account must match the partition** (association pins one partition per account) or the
  submit is rejected.
- **Compute-node submission (for the retry).** If a running job re-submits, confirm your
  cluster allows it (`srun … bash -c 'sbatch --test-only …'` → `Job N to start at …`); the
  code degrades gracefully if not.
- **Priority vs immediacy.** Falling back loses priority, but for a time-sensitive scan,
  running now on an idle fallback beats waiting hours. Keep your priority partition first.
