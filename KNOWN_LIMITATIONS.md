# Known limitations

Each item gives what is missing, why, the risk, and a next step. Items are marked as either an **intentional scope
limit (S)** or a **known weakness or open issue (W)**. No known correctness bugs are open: the full test suite passes
and the simulator reports 0 policy problems.

## Agent and decisions

1. **(S) The mock model adds no decision value.** It mirrors the deterministic priority, so in mock mode the model
   only exercises the contract, budgets and failure paths. No live provider is shipped: keeping the pack local
   matters (personal data), and so does reproducibility.
   *Risk:* the "model chooses among evidenced goals" step is untested against a real LLM's behaviour.
   *Next:* add a provider behind `ModelProvider`, with a zero-retention endpoint and the same strict parser, then
   compare its choices offline against the priority on a held-out day before letting it act.
2. **(W) Goal priority is a fixed heuristic.** G07 (resume series) wins for about 59% of goals because many viewers
   sampled one episode of a 20-episode micro-drama.
   *Risk:* a viewer may be steered to a series they abandoned on purpose, and monetisation goals (G05) are rarely
   chosen. On this replay the G07 within-goal comparison is slightly negative (−1.35 pp, CI includes 0).
   *Next:* choose goals by estimated incremental value per goal, learned from randomized data (an exploration share
   inside the agent arm), never from served-vs-unserved data.
3. **(W) The G03 correction affected 0 goals in this replay.** The priority puts G01/G02 before G03 for new users, so
   the real-data evidence for "only affected goals are re-evaluated" is "0 affected, 576 untouched". The non-empty
   path is covered by a test only.
4. **(S) The confidence score is a transparent heuristic** (evidence completeness and conflicts), not a calibrated
   probability of success. It gates actions at 0.40 for a page and 0.60 for a message.
5. **(W) The next_episode row is rarely published.** It needs at least 3 unfinished series in the current dialect, so
   the unfinished series usually appears as the hero instead.
6. **(S) `dialect_name` in WhatsApp uses the catalogue code** (for example `d3`), because the pack contains no dialect
   names. A real deployment needs a name table owned by content ops.
7. **(S) Popularity is computed once per run** from the 14 days of history. It does not update with replay-day plays.

## Real time and state

8. **(S) A single process with a virtual-clock worker pool.** Concurrency safety is proven with threads and SQLite
   constraints, not with multiple processes or machines. SQLite has a single writer.
   *Next:* partition triggers by `user_id` on a log (Kafka or Kinesis), and use a store with conditional writes
   (ARCHITECTURE.md section 13).
9. **(S) Latency is part measured, part simulated.** Compute time is real wall-clock time on this machine. Model and
   store latencies are simulated (700 ms per mock call; the store model follows the control event's description).
   The 0-over-60 s result holds under those assumptions. A real LLM with multi-second latency would make load
   shedding matter much more.
10. **(W) Messages scheduled after the replay ends** (152 kept) were revalidated once, against the state at the end
    of the replay. In production a send-time worker re-runs the verifier at `send_at`, as the in-replay dispatcher
    already does (230 pushes and 20 WhatsApp messages sent, 8 cancelled).
11. **(S) The store failure policy is fail-closed** (1 decision). A smaller safe fallback, such as a generic popular
    page, was considered and rejected. Without state, the agent cannot check idempotency or caps, and a page without a
    goal is not a goal-directed decision. The trade-off is a few missed pages during an outage.
12. **(W) The verifier is logically independent, not physically.** It shares the IST helpers, event normalization and
    the SQLite store with the planner. A bug in `normalize_event` would affect both.
    *Next:* run the verifier as a separate service with its own data access, and add property tests for the shared
    helpers.

## Measurement

13. **(S) The evidence comes from the supplied simulator.** It models page and click effects with fixed multipliers,
    and its play choices ignore titles the agent recommends unless a message is clicked. This is why G07 ("play the
    same series") barely moves. Simulated uplift is not real-world uplift.
14. **(W) The study is underpowered.** About 870 users per arm, one replay day. The primary interval
    [−1.78, +3.31] pp cannot rule out zero or a small harm, and seed sensitivity spans −1.40 to +2.25 pp. No claim of
    impact is made.
15. **(S) The ITT population is the triggered users**, because the simulator only produces outcomes for them. This is
    a pre-treatment restriction, but it means non-visitors (about 65% of `arms.csv`) are outside the estimate.
16. **(W) Chance pre-treatment imbalance.** More holdout users had only stale triggers (no goal): 16.8% vs 13.2%,
    p = 0.04. This is reported, with a goal-holder sensitivity (+0.50 pp [−2.47, +3.46]). A covariate-adjusted
    estimator (for example stratifying by "has a live trigger") would be a sensible next step.
17. **(S) Goal-level rows are descriptive.** They are small, with no multiplicity correction.

## Engineering

18. **(W) Python versions.** Tested only on CPython 3.14.8 on Windows. The code targets 3.10+ (standard library only)
    but was not run on 3.10-3.13 or on Linux or macOS.
19. **(S) Instruction detection is regex-based.** It flags text for review. Safety does not depend on it, because
    text is never interpreted as an instruction.
20. **(S) No UI.** Auditability goes through `decisions.jsonl`, `viewer_state/`, `audit_log.jsonl` and `state.db`.
