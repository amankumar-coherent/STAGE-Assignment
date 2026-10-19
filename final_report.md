# Final report: One User, One Agent

All numbers come from the sample run (`python run.py`, simulator seed `oua-practice`, mock model, no network).
The generated `sample_run/final_report.md` and `sample_run/impact_report.md` are the authoritative versions. This
page summarises them.

## 1. What was built

A per-viewer agent that, for each trigger, resolves facts with provenance, keeps or picks one measurable goal,
proposes a proportionate action or none, passes the proposal through an independent verifier, and publishes
idempotently. It reacts to live purchases, opt-outs, title withdrawals, rule corrections and outages. Afterwards it
judges goals from simulator events and measures impact against the randomized holdout. The stack is standard-library
Python, SQLite for durable per-viewer state, and a deterministic mock model with replayed faults. See ARCHITECTURE.md.

Approach, in order:

1. Inspect the brief, configs, fixtures and simulator.
2. Measure the data problems.
3. Build the trusted timing model and fact precedence.
4. Build goals (measurability first), then the planner, then the verifier.
5. Build the replay harness with the virtual clock.
6. Build the judge and ITT reporting.
7. Write the tests, run mutation checks and profiling.
8. Do a fresh-run verification.

## 2. Data quality (measured; DATA_QUALITY.md)

* 2,176 redelivered history events and 90 redelivered triggers.
* 15,651 offline-buffered history events and 432 stale triggers (up to 216 h old).
* 4,386 events with wrong phone clocks.
* 30 users with contradictory duplicate profiles; 103 triggered users without a profile.
* 7,299 history events with a dialect outside the catalogue (`None`, `NA`, `d7`).
* 60 three-way dialect conflicts on triggers.
* 514 language-variant slug pairs.
* 2 catalogue descriptions containing instructions (T0002, T0522).
* 2 unmeasurable goals (G10 null, G11 `tv_app_open`), and 2 underspecified rules (G06 "recently", G02 "never
  completed"), each interpreted conservatively.

## 3. Replay execution (2,769 trigger deliveries, 1,746 users)

| | Count |
|---|---|
| PUBLISHED | 906 (893 trigger decisions + 13 withdrawal repairs) |
| HOLDOUT (goal recorded, nothing published) | 1,325 |
| ABSTAINED (agent arm) | 461: stale 191, page already live 166, dialect unknown or without content 91, no measurable goal 13 |
| DUPLICATE_IGNORED | 90 |
| Actions | 893 pages (+13 repairs; 13 superseded), 385 push, 25 WhatsApp, 9 in-app; 8 pushes cancelled by the T0001 withdrawal; 152 messages still scheduled at the end of the replay, revalidated |

Goals opened (agent / holdout): G07 438/440, G05 130/114, G01 59/51, G08 34/42, G12 30/38, G02 24/23, G09 23/16,
G06 10/11, G04 1/1, G03 1/0. G10 and G11 were never selected.

## 4. Latency (virtual clock: queue + measured compute + simulated dependency time)

| | median | p95 | p99 | max | > 60 s | > 5 min |
|---|---|---|---|---|---|---|
| all 2,769 decisions | 18 ms | 723 ms | 729 ms | 6,019 ms | 0 | 0 |
| published | 717 ms | 726 ms | 731 ms | 2,512 ms | 0 | 0 |

Real compute per decision: median 2.1 ms, max 29 ms. There was no queueing at 8 workers. Load shedding and
fresh-first priority were exercised in a 1-worker burst test. The maximum (6.0 s) is the 45-s model-timeout fault,
capped at 2 × 3 s.

## 5. Safety and policy

* **0 actions on holdout or unknown users.** The simulator ignored 0 holdout actions.
* **Simulator `policy_problem_count` = 0.** The independent post-hoc audit of the 1,304 exported actions found 0
  problems: quiet hours, cap, dialect, availability, slugs, rows, templates, lengths, deeplinks and quarantine.
* **Verifier.** 0 failed checks on the planner's proposals in the replay. Its ability to block is proven by 17 policy
  tests, and mutation checks confirmed the tests fail if consent, the IST day cap, holdout, staleness or claim
  validation are broken.
* **Surprise events:**
  * buffered burst: stale triggers abstained and were served last;
  * purchase 2 min after the decision: price messaging cancelled, goal premise re-checked and recorded, later
    decisions use `active` from the event;
  * three-way dialect conflict: the app setting is used and the others are listed;
  * G03 correction: `G03-r2`, 0 of 576 open goals affected;
  * unsupported model claim: rejected, deterministic fallback;
  * instructions in the catalogue: flagged and quarantined;
  * model 503 and store degradation: circuit breaker, bounded retries, 1 fail-closed abstention;
  * evening peak: no queueing at 8 workers.

## 6. Tests

`python -m unittest discover -s tests -v`: **48 tests, all passing**, about 15 s, stable over repeated runs. The suite
covers every scenario in the brief: duplicates, staleness, clock skew, conflicts, precedence, missing and duplicate
profiles, model timeout, broken JSON, unsupported claims, outages, store failure, rule correction, holdout,
consent/opt-out, quiet hours, the IST cap across UTC midnight, dialect, withdrawal, licence expiry within the TTL,
slugs, short rows, injection, copy rules, verifier fail-closed, purchase re-planning, unmeasurable goals, judging
windows, finalized outcomes, Newcombe known values, ITT denominators, pilot audit, output schema and evidence
resolution, burst handling, and idempotent replay after a restart.

Not tested: a live LLM provider, multiple processes, and Python other than 3.14.

## 7. Goal outcomes (simulator seed `oua-practice`)

* 1,486 goals judged: 137 succeeded, 1,349 failed, 0 inconclusive (all windows end inside the 14-day horizon).
* Agent arm: 71 succeeded, 679 failed. Holdout: 66 succeeded, 670 failed.
* Of the 71 agent-arm successes, 65 followed an observed exposure to the agent's own page or message, and 6 had none.

## 8. Impact: randomized ITT (impact_report.md)

| Outcome | Agent | Holdout | Difference | 95% CI (Newcombe) |
|---|---|---|---|---|
| **First goal reached (primary)** | 71/863 = 8.2% | 66/883 = 7.5% | **+0.75 pp** (relative +10%) | **[−1.78, +3.31]** |
| Goal reached, users with a goal | 71/749 = 9.5% | 66/735 = 9.0% | +0.50 pp | [−2.47, +3.46] |
| Any play in 14 days | 77.1% | 75.8% | +1.29 pp | [−2.69, +5.27] |
| Any episode completed | 56.5% | 56.9% | −0.30 pp | [−4.94, +4.33] |
| Paid purchase | 4.1% | 4.1% | −0.02 pp | [−1.91, +1.88] |
| Opt-out (harm) | 0.0% | 0.0% | 0.00 pp | [−0.43, +0.44] |

Simulator seed sensitivity (same actions): +0.75, +0.87, +2.25, +0.53, −1.40 pp.

**Interpretation.** The point estimates are slightly positive, but every interval includes zero and one seed is
negative. At about 870 users per arm, the data **do not show that the agent changed outcomes**. The agent caused no
harm signal (0 opt-outs, 0 policy problems). The largest within-goal estimates (G02 +16 pp, G12 +12 pp, G01 +8 pp) are
small, uncorrected subgroups, so they suggest where to look and prove nothing.

## 9. Pilot audit

* The report's method, served (217) vs everyone else (3,674), reproduces **40.1% vs 17.7% (+22.4 pp)**.
* The randomized comparison, treatment (1,912) vs holdout (1,979), gives **18.3% vs 19.6%: −1.30 pp
  [−3.76, +1.16]**, with no detectable effect.
* **Why the headline misleads.** Serving was not random: 105 of the 217 served users had G09, a goal with a 67%
  baseline in the holdout. Holdout users with the served users' goal mix succeed 34.4% of the time without any agent.
  "Everyone else" also mixes the unserved treatment users (15.5%) with the holdout. The +22 pp is selection, not
  effect, and the recommendation to roll out does not follow.

## 10. Limitations (KNOWN_LIMITATIONS.md)

* The mock model adds no decision value.
* The goal priority is a fixed heuristic, and G07 dominates.
* The real-data evidence for the G03 correction is "0 affected".
* Latency is partly simulated.
* The study is underpowered and relies on simulator evidence.
* The process is single, and the verifier shares some helpers with the planner.
* Testing was on CPython 3.14 only.

## 11. Reproduce

```
python run.py                                  # replay + simulator (5 seeds) + judging + all reports, about 80 s
python run.py --seeds <seed1>,<seed2>          # evaluator seeds
python run.py --pack <other_pack>              # unseen replay day
python -m unittest discover -s tests -v        # 48 tests
```
