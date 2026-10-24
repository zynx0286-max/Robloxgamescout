# Contributing

Practical guide for this checkout: how to run the tests, how to measure
performance, and the two contracts (bulk reads, SQLite connections) that
keep this codebase fast.

## Setup

```bash
py -m pip install -r Roblox-Game-Scout-Bot/requirements.txt
```

`DISCORD_TOKEN` is not needed for tests; everything runs against
throwaway temp databases.

## Tests

```bash
cd Roblox-Game-Scout-Bot
py -m pytest tests/ -q -p no:warnings
```

CI (`.github/workflows/ci.yml`, repo root) runs the same suite on every
push/PR, plus an import smoke test and a coverage report (uploaded as an
artifact). Run coverage locally with:

```bash
py -m pytest tests/ -q -p no:warnings --cov=. --cov-report=term-missing
```

## Coverage table

ARCHITECTURE.md carries a generated per-module coverage table (weakest
first, ⚠️ below 50%) inside a marked block. After a coverage run that
measurably changes module coverage, refresh it — do not hand-edit the block:

```bash
py -m pytest tests/ -q -p no:warnings --cov=. --cov-report=xml
py scripts/coverage_table.py         # rewrites the ARCHITECTURE.md block
py scripts/coverage_table.py --check # review-gate: exit 1 if stale
```

`tests/test_coverage_table.py` pins the renderer (filtering, ordering,
the star threshold, stable `--check` output) on synthetic XML, so
the script changes are tested without any pytest-cov run.

## The two contracts

**Bulk reads.** Any query executed per item in a loop is a bug. Enrichment
growth/velocity/chart lookups, snapshot writes, tracker snapshot updates,
scorecard collection, radar name resolution, portfolio API reads — all
have `*_bulk()` implementations with equivalence tests. When you touch a
per-item read path, bulkify it (or justify why per-item is correct, in
ARCHITECTURE.md). A self-relative performance guard
(`tests/test_perf_guard.py`) plus the committed phase ratios in
`tests/perf_baseline.json` with one in CI on every run.

**SQLite connections.** One shared file, WAL journal mode (set once at
`create_database`), and per-connection settings from
`sqlite_conn.tune()`: busy_timeout 30 s, `synchronous=NORMAL`,
`temp_store=MEMORY`. Modules open connections through their own
`_connect()` helpers (so tests stay monkeypatchable) that all call
`tune()`; the contract and the per-module audit live in ARCHITECTURE.md's
Persistence row. Do not open bare `sqlite3.connect()` calls on the shared
file in new code — use a helper or `sqlite_conn.connect()`.

## Benchmark

```bash
cd Roblox-Game-Scout-Bot
py scripts/bench_bulk_reads.py            # print per-phase speedups
py scripts/bench_bulk_reads.py --record   # commit as the guarded baseline
```

The bench builds a throwaway synthetic store (260 games x 6 snapshots,
~215 charted — the same shape the guard uses), times the per-game and
bulk read paths phase by phase (growth / velocity / chart_rank), and
prints `speedup[phase]: Nx` lines. `--record` writes
`tests/perf_baseline.json`; commit that file.

**Perf history.** Every run also appends a dated JSONL entry to
`data/perf_history.jsonl` (fail-soft; bounded to the newest ~100 runs;
`PERF_HISTORY_PATH` env overrides the location — CI/tests redirect it
so suite runs never write the committed file — and the read side
follows it too, so `/benchmark`'s trend check and `/perfhistory` judge
the file the runs actually went to). The file is runtime
evidence, not a test fixture: it lives in `data/`, and hosts upgrading
from the old `tests/` location have theirs copied there once
automatically (`migrate_legacy_history`). Entries carry **host
metadata** (`python` + `platform`, truncated) because ratios are
self-relative per machine — the absolute ratio a phase reaches depends
on the host. That is what separates a host switch from a code
regression, and the trend check enforces it structurally: it evaluates
PER HOST PLATFORM — each 📈 line counts only that host's own runs and
names it, so evidence never pools across hosts (a slow new host cannot
FAKE a trend out of another host's healthy runs) and one host's dips
fire from its own slice alone (no MASKing by another host's healthy
entries, and no vague "a host change may explain this" mixing warning).
The window is per machine too: a host is judged on ITS last 10 runs,
not on the fraction of a shared 10-entry window it happens to fill, so
an interleaved host gets the same evidence bar a lone machine gets and
an idle host keeps the record it built (see S10b below).
Only the newest entry's host speaks a VERDICT: another machine's still-open
alert is printed as an ℹ️ "separate alert, NOT this run's verdict" line
(naming both machines) that `route_of` never turns into an action, because
a run on B must not re-issue A's alert as B's instruction.
`/perfhistory` shows one host column per run and a `hosts:` caveat
line whenever the displayed runs span OS families. When the log holds
more than one machine it also prints a per-machine table — each host's
last run, the evidence (which phase, and how many of its OWN recent
runs were low) and the verdict it got THEN — because a host that has not run
since the runs moved elsewhere has no ⚠️ row and no line in the window,
and its open alert would otherwise read as resolved. It ends with the
same routed action `/benchmark` would post for the latest logged run —
"fix the regressed read path" (with "the alert is stuck" once the
window is saturated and the fix is not holding), "rerun, suspect
host noise first" or "rule the host out first (host-wide slide)" with the identical ⚠️/📈
messages beneath it, rebuilt from the file by `route_of` over the same
two functions — so the read-only view tells the operator what to do
without re-measuring.
The rest is the trend memory: `/benchmark` prints a 📈 line for any
phase below 25% of its committed baseline in ≥60% of its last 10
recorded runs — the minimum SCALES with the window
(`TREND_MIN_DRIFTS_FRACTION`, ceil, floor 2) — "repeat drift", which
is a signal to FIX the regression (reported as ONE host-wide line
instead when every phase drifted together on that host — the machine or
a shared path, not one read path), as opposed to one noisy run that
stays a lone ⚠️ and a short transient stretch that stays silent.
Re-recording is only for an intended change, never a response to a
warning: it writes the current ratios into the guarded baseline and
makes CI forget the regression.
`/perfhistory` reads the same file read-only (recent runs, per-phase
ratios vs baseline, trend lines) without re-running the bench. The file
is truncate-safe (corrupt lines are skipped) and safe to commit
alongside the baseline; delete it (and re-record the baseline) when
you switch hosts entirely — and never hand-edit entries, they're
measurements, not claims.

**Why the trend window is 10 runs at a 60% scaled threshold (fitted,
not vibes).** Evaluated against the real committed history with the
pure drift functions: normal run-to-run spread never approaches the
25% floor (nearest observed growth miss had a 1.9× margin), so flagged
runs always mean a regime change, not noise. The window and the share
are FITTED, not inherited: `scripts/trend_stress_test.py --refit`
grid-searches (window × share) under an AR(1) cluster-noise model — a
latent autocorrelated process whose dips arrive in persistent
stretches, the dependence a real host actually has — and picks by a
pre-declared objective: minimise the worst ordinary-noise false-fire
band (marginal dip rates ≤10%, rho 0.5/0.8) subject to a 6-regressed-
run detection budget. Window 10 at a 60% share (thr 6 of a full
10-run slice) wins that grid; the 40% share it replaced scores
worst-band 75% under the same model. Under the fitted rule a real
regression on a populated host fires on the 6th bad run of THAT host's
own slice — two runs later than the old policy, the deliberate price
of clustered-noise robustness. And because slices are per host, a NEW
host arms from its own runs only — three of its runs (HOST_MIN_SLICE)
are enough to speak, no full-window wait. Short transient stretches
within one host's slice stay silent (a 5-run slice needs 3 dips); a
sustained dip fires. The scaling — not the window — is the
burst-robustness lever: measured earlier, a longer window with a
fixed ≥2 floor still fired after 2 dips however close together.

**Stress-tested against injected random noise (reproducible).**
`scripts/trend_stress_test.py` replays synthetic histories through the
real `drift_trend_lines` — healthy runs bootstrap-resample the
committed history's observed growth ratios ±15% jitter (clamped so a
"healthy" draw can never reach the drift floor; the real corpus clears
it 1.94×), dips are transient noise only — iid, or AR(1) clustered
stretches whose marginal dip rate is exact while one persistence
parameter rho sets how long they last (the retired burst+idle model
remains reachable with `--bursty-levels` for comparison) — and the
truth is NO regression, so any 📈 line is a false fire. Seeds
are fixed; numbers change only when the noise model or the functions
under test change. The matrix, latency bullets and two-host tables
below are RENDERED by `scripts/render_stress_matrix.py` from the
measured rows (`--csv`) — never typed, so they cannot drift from the
run. CI re-runs the sweep and `render_stress_matrix.py --check`
fails the build when the committed block differs, printing the first
divergent lines; locally the same check exits 1.
false-fire rates by setting — under ordinary iid noise the current
default beats every fixed-thr2 setting by 60–93 points, and under
AR(1) clustering it is lowest in every band, beating the previous 40%
policy (shown as `w10 fixed thr4`) at the cost of 2 extra regressed
runs (6 vs 4):

<!-- stress-matrix: GENERATED - do not hand-edit. Regenerate with:
     py scripts/trend_stress_test.py --refit --csv /tmp/trend.csv
     py scripts/render_stress_matrix.py /tmp/trend.csv > /tmp/matrix.md
     (fixed seeds: defaults reproduce these numbers unless the noise model, sweep settings or the trend functions change) then replace this block. -->
| noise | `w3 scaled thr2` | `w5 scaled thr3` | `w5 fixed thr3` | `w10 scaled thr6  <- CURRENT` | `w10 fixed thr4` | `w10 fixed thr2` | `w20 fixed thr2` |
|---|---|---|---|---|---|---|---|
| iid 0.03 | 12% | 4% | 0% | 0% | 1% | 64% | 73% |
| iid 0.06 | 70% | 18% | 12% | 3% | 8% | 96% | 97% |
| iid 0.1 | 94% | 37% | 40% | 6% | 39% | 99% | 100% |
| iid 0.2 | 100% | 97% | 94% | 37% | 99% | 100% | 100% |
| ar1 rho0.5 0.05 | 81% | 35% | 42% | 5% | 29% | 89% | 94% |
| ar1 rho0.5 0.1 | 98% | 79% | 73% | 16% | 71% | 100% | 99% |
| ar1 rho0.5 0.2 | 100% | 99% | 100% | 86% | 97% | 100% | 100% |
| ar1 rho0.8 0.05 | 84% | 72% | 61% | 33% | 52% | 76% | 89% |
| ar1 rho0.8 0.1 | 96% | 84% | 90% | 66% | 79% | 95% | 97% |
| ar1 rho0.8 0.2 | 100% | 100% | 100% | 93% | 100% | 100% | 100% |

Detection latency for the sustained regression (regressed runs until the fire; 0 missed everywhere in this sweep unless the run says otherwise):
- `w3 scaled thr2`: 2/2.0/2 (30/30 fired, 0 missed)
- `w5 scaled thr3`: 3/3.0/3 (30/30 fired, 0 missed)
- `w5 fixed thr3`: 3/3.0/3 (30/30 fired, 0 missed)
- `w10 scaled thr6  <- CURRENT`: 6/6.0/6 (30/30 fired, 0 missed)
- `w10 fixed thr4`: 4/4.0/4 (30/30 fired, 0 missed)
- `w10 fixed thr2`: 2/2.0/2 (30/30 fired, 0 missed)
- `w20 fixed thr2`: 2/2.0/2 (30/30 fired, 0 missed)

Policy re-fit under the AR(1) model - grid over window x scaling share, each policy's false-fire rate per ordinary-noise band (heavy bands saturate and are excluded; a policy may wait at most `--fit-max-latency` regressed runs):
| window | share | thr | r0.5/0.05 | r0.5/0.1 | r0.8/0.05 | r0.8/0.1 | worst | latency | mark |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 0.20 | 2 | 86% | 95% | 74% | 98% | 98% | 2.0 |  |
| 5 | 0.25 | 2 | 86% | 95% | 74% | 98% | 98% | 2.0 |  |
| 5 | 0.30 | 2 | 86% | 95% | 74% | 98% | 98% | 2.0 |  |
| 5 | 0.40 | 2 | 86% | 95% | 74% | 98% | 98% | 2.0 |  |
| 5 | 0.50 | 3 | 46% | 69% | 59% | 85% | 85% | 3.0 |  |
| 5 | 0.60 | 3 | 46% | 69% | 59% | 85% | 85% | 3.0 |  |
| 8 | 0.20 | 2 | 81% | 98% | 76% | 98% | 98% | 2.0 |  |
| 8 | 0.25 | 2 | 81% | 98% | 76% | 98% | 98% | 2.0 |  |
| 8 | 0.30 | 3 | 38% | 85% | 57% | 92% | 92% | 3.0 |  |
| 8 | 0.40 | 4 | 10% | 57% | 40% | 80% | 80% | 4.0 |  |
| 8 | 0.50 | 4 | 9% | 55% | 40% | 80% | 80% | 4.0 |  |
| 8 | 0.60 | 5 | 2% | 34% | 25% | 71% | 71% | 5.0 |  |
| 10 | 0.20 | 2 | 79% | 98% | 75% | 92% | 98% | 2.0 |  |
| 10 | 0.25 | 3 | 51% | 91% | 61% | 86% | 91% | 3.0 |  |
| 10 | 0.30 | 3 | 51% | 90% | 61% | 86% | 90% | 3.0 |  |
| 10 | 0.40 | 4 | 26% | 66% | 48% | 75% | 75% | 4.0 |  |
| 10 | 0.50 | 5 | 12% | 39% | 39% | 61% | 61% | 5.0 |  |
| 10 | 0.60 | 6 | 4% | 19% | 26% | 46% | 46% | 6.0 | **current**, **fitted** |
| 12 | 0.20 | 3 | 60% | 94% | 54% | 84% | 94% | 3.0 |  |
| 12 | 0.25 | 3 | 59% | 94% | 52% | 84% | 94% | 3.0 |  |
| 12 | 0.30 | 4 | 31% | 75% | 41% | 72% | 75% | 4.0 |  |
| 12 | 0.40 | 5 | 21% | 48% | 31% | 60% | 60% | 5.0 |  |
| 12 | 0.50 | 6 | 10% | 25% | 21% | 48% | 48% | 6.0 |  |
| 12 | 0.60 | 8 | 5% | 4% | 6% | 38% | 38% | 8.0 | over latency budget |
| 15 | 0.20 | 3 | 66% | 95% | 57% | 90% | 95% | 3.0 |  |
| 15 | 0.25 | 4 | 36% | 84% | 54% | 80% | 84% | 4.0 |  |
| 15 | 0.30 | 5 | 15% | 65% | 41% | 68% | 68% | 5.0 |  |
| 15 | 0.40 | 6 | 8% | 45% | 32% | 52% | 52% | 6.0 |  |
| 15 | 0.50 | 8 | 6% | 16% | 19% | 38% | 38% | 8.0 | over latency budget |
| 15 | 0.60 | 9 | 1% | 9% | 14% | 34% | 34% | 9.0 | over latency budget |
| 20 | 0.20 | 4 | 45% | 85% | 41% | 88% | 88% | 4.0 |  |
| 20 | 0.25 | 5 | 22% | 71% | 30% | 81% | 81% | 5.0 |  |
| 20 | 0.30 | 6 | 14% | 49% | 29% | 68% | 68% | 6.0 |  |
| 20 | 0.40 | 8 | 5% | 21% | 19% | 45% | 45% | 8.0 | over latency budget |
| 20 | 0.50 | 10 | 5% | 8% | 16% | 29% | 29% | 10.0 | over latency budget |
| 20 | 0.60 | 12 | 0% | 6% | 9% | 15% | 15% | 12.0 | over latency budget |

Operator routing - truth x noise mix x concurrent host burst, routed by BREADTH (which phases slid): `one phase` = one phase sliding at that depth x baseline while the others hold (the code-regression signature; regression `none` is the negative control); `all phases` = every phase at 0.15x baseline together (a host-wide slide). The concurrent burst drops every phase for 3 runs. Each cell is where the last message set on that timeline sent the operator; `host` means it pointed at the machine/host rather than one read path, and the `--record` column counts messages that recommended re-recording (must stay 0 everywhere):
| truth | noise | regression | burst | trend fires | fix | host | rerun | none | `--record` |
|---|---|---|---|---|---|---|---|---|---|
| one phase | none | none | no | 0% | 0% | 0% | 0% | 100% | 0 |
| one phase | none | none | yes | 0% | 0% | 100% | 0% | 0% | 0 |
| one phase | iid 5% | none | no | 0% | 0% | 0% | 80% | 20% | 0 |
| one phase | iid 5% | none | yes | 0% | 0% | 35% | 65% | 0% | 0 |
| one phase | iid 10% | none | no | 0% | 0% | 0% | 100% | 0% | 0 |
| one phase | iid 10% | none | yes | 0% | 0% | 20% | 80% | 0% | 0 |
| one phase | ar1 10% rho0.8 | none | no | 0% | 0% | 0% | 35% | 65% | 0 |
| one phase | ar1 10% rho0.8 | none | yes | 15% | 15% | 35% | 50% | 0% | 0 |
| one phase | none | 0.20 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | none | 0.20 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.20 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.20 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.20 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.20 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.20 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.20 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | none | 0.12 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | none | 0.12 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.12 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.12 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.12 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.12 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.12 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.12 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | none | 0.06 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | none | 0.06 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.06 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 5% | 0.06 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.06 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | iid 10% | 0.06 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.06 | no | 100% | 100% | 0% | 0% | 0% | 0 |
| one phase | ar1 10% rho0.8 | 0.06 | yes | 100% | 100% | 0% | 0% | 0% | 0 |
| all phases | none | none | no | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | none | none | yes | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | iid 5% | none | no | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | iid 5% | none | yes | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | iid 10% | none | no | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | iid 10% | none | yes | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | ar1 10% rho0.8 | none | no | 100% | 0% | 100% | 0% | 0% | 0 |
| all phases | ar1 10% rho0.8 | none | yes | 100% | 0% | 100% | 0% | 0% | 0 |

Escalation on a regression that DEEPENS over time - one phase ramps from its committed ratio to 2% of it over 30 runs (geometric: shallow first, unmistakable later) while the others stay healthy (a narrow slide), replayed step by step through the REAL messages. `rerun run` / `fix run` are median tail runs after onset; `depth at fix` is the regression depth when the first fix posts; `on bar` is the share of trials whose FIRST fix lands exactly on the run where the fitted evidence bar is met; `premature` counts fixes posted before the bar (must be 0) and `--record` must stay 0:
| jitter | fix reached | rerun run | fix run | rerun->fix | depth at fix | on bar | premature | `--record` |
|---|---|---|---|---|---|---|---|---|
| 0.00 | 100% | 11 | 16 | 5 | 12% | 100% | 0 | 0 |
| 0.10 | 100% | 11 | 16 | 5 | 12% | 100% | 0 | 0 |
| 0.20 | 100% | 10 | 15 | 5 | 13% | 100% | 0 | 0 |

Recovery after the regression is fixed - a 24-run regression (saturated 10-run window) followed by healthy runs, replayed through the REAL messages and the /perfhistory verdict. `clear at` is the healthy run on which the warning/trend messages stop (median); `up on 1st` is the share still alerting on the first healthy run (the window is the memory, so the alert ages out instead of vanishing); `stayed clear` = no alert returned afterwards; `view none` = /perfhistory routes back to nothing; `--record` must stay 0:
| jitter | cleared | clear at | slowest | up on 1st | stayed clear | view none | `--record` |
|---|---|---|---|---|---|---|---|
| 0.00 | 100% | 5 | 5 | 100% | 100% | 100% | 0 |
| 0.15 | 100% | 5 | 5 | 100% | 100% | 100% | 0 |

Stall then relapse - a partial fix (one phase held at 0.35x baseline, clear of the 25% floor) for a full 10-run window, then a second regression at 0.10x. The route must age out while the window is clear, stay quiet, then re-escalate through rerun and reach fix on the fitted bar for the second regression. `stall clear` is the stall run the alert stops on (median); `quiet` = no alert for the rest of the stall; `rerun` / `re-fix` are relapse runs; `ends fix` and `view fix` are the final route and the /perfhistory verdict; `--record` must stay 0:
| jitter | cleared | stall clear | quiet | rerun | re-fix | ends fix | view fix | `--record` |
|---|---|---|---|---|---|---|---|---|
| 0.00 | 100% | 5 | 100% | 1 | 6 | 100% | 100% | 0 |
| 0.15 | 100% | 5 | 100% | 1 | 6 | 100% | 100% | 0 |

Stuck-alert escalation - a regression that lasts 24 runs, deep (0.12x baseline) or marginal (0.22x, inside the jitter to the 25% floor), then healthy runs. The stuck wording may only appear once the phase has been low for more than the whole window (11+ consecutive runs): `escalated` is the share of timelines that ever reach it; `onset` is the regression run it first posts on (median); `early` counts an escalation before that streak (must be 0); `up at end` is the share still alerting when the fix lands; `clear at` / `slowest` are healthy runs until it goes quiet (trials alerting at the end only). `--record` stays 0:
| shape | jitter | escalated | onset | early | up at end | clear at | slowest | `--record` |
|---|---|---|---|---|---|---|---|---|
| deep | 0.00 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| deep | 0.10 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| deep | 0.15 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| deep | 0.25 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| marginal | 0.00 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| marginal | 0.10 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| marginal | 0.15 | 100% | 11 | 0 | 100% | 5 | 5 | 0 |
| marginal | 0.25 | 50% | 14 | 0 | 100% | 3 | 5 | 0 |

Stuck alert across two hosts - a regression that runs on host B only (deep 0.12x or marginal 0.22x of baseline), interleaved 1:1 with a healthy host A, then B recovers. The stuck wording must NAME B (`named A` must be 0 - naming the healthy host would send the operator to the wrong machine), may only escalate once B's OWN streak passes the window (`early` must be 0), and must clear once B's healthy runs return (`onset`, `clear at` and `slowest` are in B's own runs; B's window is its own last 10 runs at the 6-of-10 bar, so 5 healthy own runs clear it, the same arithmetic a single machine gets). `pooled` is the same count without host scoping: the healthy host resets it every other run, so the pooled gate could never escalate this case. `--record` stays 0:
| shape | jitter | escalated | named B | named A | onset | early | pooled | up at end | clear at | slowest | `--record` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deep | 0.00 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| deep | 0.10 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| deep | 0.15 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| deep | 0.25 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| marginal | 0.00 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| marginal | 0.10 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| marginal | 0.15 | 100% | 100% | 0% | 11 | 0 | 1 | 100% | 5 | 5 | 0 |
| marginal | 0.25 | 20% | 20% | 0% | 20 | 0 | 1 | 100% | 3 | 5 | 0 |

Host-wide slide across two hosts - B slides EVERY phase on its own runs (0.12x or 0.20x of baseline), interleaved 1:1 with a healthy host A, then recovers. `sustained` is the share whose line claims persistence ("have slid together"); read POOLED, the same slide is `pooled ev` (5 of 10) and sits below the 6-of-10 bar, so `pooled sust` is 0% - the old rule called every interleaved machine-wide slide transient. The control is the same slide from a brand-new host: two own runs (`own ev` 2/2) must stay transient. Both shapes route to rule the host out (a machine is implicated either way), never to one read path (`route fix` 0%), and the line names B (`named A` must be 0). `clear at` is B's own healthy runs until the warning stops; `--record` stays 0:
| shape | depth | route host | route fix | sustained | pooled sust | named B | named A | own ev | pooled ev | clear at | `--record` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sustained | 0.12 | 100% | 0% | 100% | 0% | 100% | 0% | 10/10 | 5/10 | 5 | 0 |
| thin | 0.12 | 100% | 0% | 0% | 0% | 100% | 0% | 2/2 | 2/10 | - | 0 |
| sustained | 0.20 | 100% | 0% | 100% | 0% | 100% | 0% | 10/10 | 5/10 | 5 | 0 |
| thin | 0.20 | 100% | 0% | 0% | 0% | 100% | 0% | 2/2 | 2/10 | - | 0 |

Host switch mid-history - host A has been stuck for 24 of its own runs (long past saturation), then the runs move to B. Two truths: B healthy -> nothing to act on for B's first run (`clean 1st` 100%), and A's alert may be reported but never re-posted as B's verdict (`A leak` 0: the old code re-posted A's "unresolved REGRESSION" on every clean B run and routed it to "fix the regressed read path" on a machine that was fine); B slow -> the stuck wording is EARNED on B's own 11th low (`onset`, `early` 0) instead of inherited from A's streak. `ctx A` is how many of the 18 post-switch runs still carry A's separate alert (it drops out of the recent-window candidates after 10 runs elsewhere); `named B` is the share of post-switch runs whose verdict names B; `clear at` is B's healthy runs until nothing routes ("-" when B never had an alert to clear). `--record` stays 0:
| shape | clean 1st | A leak | stuck 1st | named B | ctx A | onset | early | clear at | `--record` |
|---|---|---|---|---|---|---|---|---|---|
| fixed by switch | 100% | 0% | 0% | 0% | 9 | - | 0 | - | 0 |
| new host slow | 0% | 0% | 0% | 89% | 9 | 11 | 0 | 5 | 0 |

Idle host returns - A is still stuck, the runs went to B for 12 runs, then A runs once. A's window is its OWN last 10 runs (`own ev` 10/10), so the saturated alert is back on that first run (`alert back` 100%), naming A and never B. The shared-window rule left `old slice` (1) of A's runs inside the window, below `HOST_MIN_SLICE`, so it had to wait for two more runs before it could say anything at all. `--record` stays 0:
| shape | alert back | named A | named B | own ev | old slice | `--record` |
|---|---|---|---|---|---|---|
| idle host | 100% | 100% | 0% | 10/10 | 1 | 0 |

Two-host scoping - a second host takes every 2nd run and is low on that share of ITS OWN runs (15/30/60/100%); the fitted bar asks 6 of a host's own last 10, so a flickering host is mostly not called. A fire naming the healthy host is a FALSE fire, a fire naming the slow host is a catch, `fires` is the share of timelines that ever fire and `med B-runs` the slow host's own runs until its first catch. The `pooled` rows are the pre-host-metadata counterfactual:
| B acting | setting | slice view | fires | named A (false) | named B (caught) | unlabeled | med B-runs |
|---|---|---|---|---|---|---|---|
| 15% | `w5 scaled thr3` | per-host | 55% | 0 | 55 | 0 | 36 |
| 15% | `w5 scaled thr3` | pooled | 17% | 0 | 0 | 17 | 45 |
| 15% | `w10 scaled thr6  <- CURRENT` | per-host | 5% | 0 | 5 | 0 | 46 |
| 15% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | 0 | 0 | 0 | - |
| 30% | `w5 scaled thr3` | per-host | 100% | 0 | 100 | 0 | 12.0 |
| 30% | `w5 scaled thr3` | pooled | 84% | 0 | 0 | 84 | 24.0 |
| 30% | `w10 scaled thr6  <- CURRENT` | per-host | 57% | 0 | 57 | 0 | 32 |
| 30% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | 0 | 0 | 0 | - |
| 60% | `w5 scaled thr3` | per-host | 100% | 0 | 100 | 0 | 5.0 |
| 60% | `w5 scaled thr3` | pooled | 100% | 0 | 0 | 100 | 6.5 |
| 60% | `w10 scaled thr6  <- CURRENT` | per-host | 100% | 0 | 100 | 0 | 10.0 |
| 60% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | 0 | 0 | 0 | - |
| 100% | `w5 scaled thr3` | per-host | 100% | 0 | 100 | 0 | 3.0 |
| 100% | `w5 scaled thr3` | pooled | 100% | 0 | 0 | 100 | 3.0 |
| 100% | `w10 scaled thr6  <- CURRENT` | per-host | 100% | 0 | 100 | 0 | 6.0 |
| 100% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | 0 | 0 | 0 | - |

Mask direction - a host regressed EVERY time it runs, at a fraction of the history (truth: real regression; zero catches here would be missed ones; latency in total runs and in the regressed host's own runs):
| A share | setting | slice view | fires | med total runs | med A's own runs | missed |
|---|---|---|---|---|---|---|
| 50% | `w5 scaled thr3` | per-host | 100% | 36.0 | 3.0 | 0 |
| 50% | `w5 scaled thr3` | pooled | 100% | 36.0 | 3.0 | 0 |
| 50% | `w10 scaled thr6  <- CURRENT` | per-host | 100% | 42.0 | 6.0 | 0 |
| 50% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | - | - | 100 |
| 25% | `w5 scaled thr3` | per-host | 100% | 42.0 | 3.0 | 0 |
| 25% | `w5 scaled thr3` | pooled | 0% | - | - | 100 |
| 25% | `w10 scaled thr6  <- CURRENT` | per-host | 100% | 54.0 | 6.0 | 0 |
| 25% | `w10 scaled thr6  <- CURRENT` | pooled | 0% | - | - | 100 |
<!-- end stress-matrix -->
Two-host reading: the fitted setting applies the SAME rule to every
machine — 60% of that host's own last 10 runs below the floor — so a
host whose own runs are low on 60% or all of them is caught in 100% of
runs within 10 of its own acting runs, at 30% in 57% (median 32 own
runs), and at 15% in 5%. That last row is the fitted policy's known
cost, not a scoping artifact: a host that flickers low on one run in
seven has not established a trend by the same 6-of-10 bar a single
machine is held to, and the earlier 5-entry interleaved slice (thr 3)
called it a trend in 61% of timelines — a more trigger-happy rule for
interleaved hosts that the fit never measured. Pooled
(pre-host-metadata) semantics catch it 0% of the time at the current
window, because the acting host's dips can never fill 6 of a 10-entry
shared window. No fire ever named the HEALTHY host in any cell, so
per-host naming adds attribution for free. The mask direction is the
mirror image: a host regressed EVERY time it runs is invisible to the
pooled slice at a 25% history share, while per-host catches it on its
6th own run (the 6-of-10 bar), 54 total runs in. Known symmetric cost:
the per-host 3-entry floor (`HOST_MIN_SLICE`) still bounds the sample —
a host with fewer than 3 of its own runs makes no trend claim — but
since each host's window is its own last 10 runs, a low history share
no longer shrinks its slice.

Reading the matrix: under ordinary iid noise (≤6% of runs) the fitted
threshold holds false fires at 0–3% where every fixed-thr2 alternative
sits at 64–97%; AR(1) clustering raises every setting's rate, but the
current default is lowest in every clustered band (5–33% at a 5%
marginal rate, 16–66% at 10%) and beats the previous 40% policy
everywhere — the rho 0.8/10% band at 66% is the honest limit of a
count-based policy that must also catch within 6 runs. At a 20%
marginal rate everything saturates (86–100%) and the takeaway flips to
"re-record, not investigate" — a re-recording-scope decision, not a
threshold tweak. Costs: the firmer threshold waits 6 regressed runs
(4 under the old 40% policy, 2 under fixed thr2) — and "thin" fires
(backed by the history's first window, where the per-host slice is
still short and the threshold sits at its floor-2 minimum) still
appear when clustered noise hits a brand-new history early (3–30% of
trials at the heavier noise levels);
`scripts/trend_stress_test.py --trials 25 --steps 60` reruns the
sweep in seconds, and `--refit` reruns the policy grid.

Routing (S3) is the other half of a useful alert: a warning that tells
an operator to re-record a live regression is worse than no warning —
it writes the regression into the guarded baseline and CI forgets it.
WHICH phases slid is what separates the two causes a slow run can have:
a host burst lands on every phase at once (same machine, same CPU/IO
weather), while a code change lives in ONE phase's read path. So a
flagged phase is routed by breadth AND persistence — both counted in
the run's OWN HOST's runs, so a second machine's healthy entries cannot
dilute a sustained slide into "rerun" — and the message names the
machine it measured. Every measured
phase below the floor together -> ONE host-wide line ("rule the host
out first: rerun /benchmark when the box is quiet and compare hosts in
/perfhistory; only if it survives a host change look for shared code"),
never N phase warnings and never a re-record. One phase below the floor
with the others at baseline -> the per-phase line ("only this phase
slid this run ..."): sustained routes to "find and fix that read path",
an isolated dip to "rerun, suspect host noise first". A run that
measured a single phase has no breadth evidence and makes no breadth
claim. The message also tracks how long the evidence has been at its
ceiling: once a phase has been low for 11+ of that host's own
consecutive runs (a saturated 10-run window plus at least one older
low), the same fix
route escalates from "find and fix" to a stuck alert — the window is
saturated, so this is not a new dip: a fix already applied is not
holding, and the advice becomes "check the change is deployed and is
the right cause". The route and the no-re-record invariant are
unchanged; only the advice is.

The harness simulates all three truths — a sustained one-phase
regression (0.20x, 0.12x, 0.06x of baseline), a host-wide slide (every
phase at 0.15x together), and neither — under every noise mix, with and
without a concurrent 3-run burst that drops every phase, and checks the
REAL messages rather than the detector. Results above: a one-phase
regression ends routed to fix in 100% of timelines in every mix, burst
or not, with the trend firing in 100% and re-record advice in none; a
host-wide slide ends routed to host in 100% and to fix never; a burst
with no regression underneath never routes to fix; plain iid noise
routes to rerun or nothing. The `--record` column of the matrix below
is the invariant that matters: it must stay 0 in every cell, regression
or not.

Two honest limits. (1) Breadth is deliberately all-or-nothing: with
three phases, two sliding together reads as two phase regressions, not
a host event — the phase still at baseline is direct evidence the
machine is not uniformly slow. (2) A regression in a path EVERY phase
goes through (the connection/query layer) is indistinguishable from a
host-wide slowdown in ratios alone, which is why a sustained shared
slide routes to "rule the host out first, then the shared path" rather
than asserting either. Heavy AR(1) clusters can still satisfy the
sustained bar while breadth stays narrow (the ar1 noise-only rows: 0%
of timelines without the onset burst, 15% with it in this fixed-seed
run) — still "investigate", never "re-record", and the next healthy
runs clear it.

Escalation is checked too (S4): a regression that DEEPENS over time
— one phase ramping from its committed ratio to 2% of it over 30 runs,
the others healthy — must not jump straight to "fix" or sit on
"rerun". Replayed step by step through the real messages, the route is
silent while the phase is above the floor, turns to "rerun, suspect
host noise" on the first below-floor run (an isolated dip), and turns
to "fix" exactly on the run the fitted bar is met — five lows later at
the 6-of-10 bar, at about 12% of baseline. All three jitter levels (0%,
10%, 20%) reach fix in 100% of timelines, 100% of first fixes land on
the bar, no fix precedes it, the route never falls back to rerun once
fixed, and no message recommends re-recording.

Recovery is checked as well (S5): after a 24-run regression — long
enough to saturate the 10-run window — healthy runs must clear the
alert. The window is the memory, so a fix does not cancel the alert:
it is still up on the first healthy run and goes quiet on the 5th
(10-window - 6-bar + 1), then stays quiet for the rest of the recovery
window, and `/perfhistory` routes back to "none — nothing below the
floor and no repeat drift". Both jitter levels clear in 100% of
timelines at exactly 5 healthy runs, and no message anywhere
recommends re-recording.

Relapses are covered too (S6): a partial fix — one phase held at 0.35x
baseline, above the floor — for a full window ages the alert out
(silent from the 5th stall run) and stays silent; a second regression
at 0.10x then re-arms the route through "rerun, suspect host noise"
and reaches "fix" on the 6th relapse run — the same bar — and
/perfhistory ends on the fix verdict. No cell recommends
re-recording.

Stuck-alert escalation is measured too (S7): a deep regression (0.12x)
escalates on the 11th consecutive low at every jitter level, while a
marginal one (0.22x, inside the jitter band around the floor) flickers
back above the floor — which resets the streak — and so escalates on
only 50% of timelines at a ±25% jitter, at a median of the 14th run.
It never escalates before the streak is real (`early` is 0 in every
cell), and once escalated it still ages out by the window arithmetic
(`clear at` = lows - bar + 1: 5 healthy runs from a saturated window,
3 when the marginal regression left two high runs inside it). No cell
recommends re-recording.

The alert is then put across TWO hosts (S8): the same 24-run regression,
but on host B only, interleaved 1:1 with a healthy host A, then B
recovers. That probe found a real gap rather than confirming the
single-host result: the per-run warning counted a POOLED streak, which
A's healthy entry resets every other run (`pooled` is 1 in every cell),
and the trend branch required the host's runs to fill the whole 10-entry
shared window, which a 1:1 host can never do (it supplies 5). An
interleaved host therefore escalated 0% of the time however long it
stayed low. The count and the escalation are now per host and the stuck
line names the machine: deep regressions escalate on B's 11th own run in
100% of timelines at every jitter, the marginal one flickers above the
floor and escalates in 20% of timelines at a ±25% jitter (median of B's
20th own run), and `early` is 0 in every cell. No cell names the healthy
host (`named A` = 0) — a stuck alert is an instruction to check a
deployment on one machine, so naming the wrong one is worse than saying
nothing. Every warning line, not just the stuck one, carries the same
`on <platform> (pyX)` clause — with two machines in the history "this
run" is ambiguous, and a fix instruction must not land on the wrong
box. The same rule governs persistence: the one-phase route, the
host-wide slide and the stuck streak all count this run's own host's
entries, so a second machine's healthy runs cannot turn a real
regression into "rerun". Clearing follows the same window arithmetic a
single machine gets, because B's window is its OWN last 10 runs: at
the 6-of-10 bar the alert is up for 4 healthy own runs and gone on the
5th. No cell recommends re-recording.

Host-wide slides get the same treatment (S9): B slides EVERY phase on
its own runs (0.12x and 0.20x), interleaved 1:1 with a healthy A. Read
pooled that is 5 lows of 10 — below the 6-of-10 bar — so the old rule
called every interleaved machine-wide slide transient ("slid together
this run … rerun when the box is quiet"), while the trend line,
already per host, could call
the same run's window saturated. Counted in B's own runs (10 of its own
last 10) the route is the sustained one in 100% of timelines at both
depths — "rule the host out first", naming B, never one read path — and
it clears after 5 healthy own runs. The control is the same slide on a
brand-new host: two runs of evidence (2 of 2) stay transient, which is
the honest answer — the machine is implicated, but two runs cannot
claim a streak. No cell names the healthy host and none recommends
re-recording.

Moving the RUNS to another machine (S10) was the last hole. Host A is
stuck for 24 of its own runs — past saturation — and then the runs move
to B. The per-run warning and the stuck alert already followed the
run's own host, but the trend line still evaluated every host's slice
in the shared window: on B's first, perfectly clean run it re-posted
A's "unresolved REGRESSION … has drifted in all 9 of its last recorded
runs on A", and the verdict for a healthy machine became "fix the
regressed read path". A posted line is a claim about the run that
carries it, so only the newest entry's host speaks one now; another
machine's open alert is reported once as an ℹ️ "separate alert, NOT
this run's verdict" line naming both machines, which `route_of`
ignores. Measured: after the switch a clean B run routes to nothing in
100% of timelines (`A leak` 0%), while A's alert stays visible on 9 of
the 18 post-switch runs before A drops out of the recent-window
candidates — reported, not dropped. A slow B earns its own stuck alert
at 11 of its OWN lows (`onset` 11, `early` 0, `stuck 1st` 0%) instead
of inheriting A's streak, and it clears after 5 healthy B runs. No cell
recommends re-recording.

The window itself is per MACHINE, which the returning host shows
(S10b): A is still stuck, the runs go to B for 12, then A runs once
more. Because A's window is its own last 10 runs (`own ev` 10/10), the
saturated alert is back on that very first run — 100% of timelines,
naming A and never B. Read through one shared window, only 1 of A's
runs was still inside it (below `HOST_MIN_SLICE`), so the old rule
could say nothing until A had run twice more, and would then have been
judging a 3-run sample. Making the window per host costs nothing in
false fires here (the B column stays 0) and is what keeps a machine's
record intact across another machine's busy week.

From Discord, `/benchmark` runs the same bench in a subprocess (its
module-level `database.DATABASE` rebind is why in-process is not safe)
and appends a drift check — any phase running below 25% of the committed
baseline prints a warning ROUTED by the run's breadth and the recent
record: every measured phase low together produces one host-wide line
(host burst / contention / a shared path — rule the host out first),
while one phase low with the others holding routes to "find and fix
that regression" when sustained or "rerun, suspect host noise first"
when isolated — plus the history trend check above. The run measures
ONE host, so when the log holds more than one machine the post ends
with the same per-machine table /perfhistory shows (last run, evidence
and verdict per host), otherwise an alert on a machine that has not run
recently would never reach the channel. GitHub-issue automation for that warning is
intentionally not implemented; it would need a repo token supplied as a
secret, which is the operator's call.

## Baselines

- Baselines are machine-relative ratios (old vs bulk timed back-to-back
  on the same synthetic data), never absolute seconds.
- Re-record when: you change a read/write implementation, switch host,
  or the CI guard catches a legitimately faster/slower change.
- Do not re-record to make a failing guard pass — that hides the
  regression the guard exists to catch. If the ratio legitimately
  changed, the `--record` commit is visible in review.
