# AI collaboration log

## How AI was used

The implementation, tests and documents were produced in one working session with **Claude Code** (model: Claude
Opus 5.5) in VS Code. I set the brief: a detailed engineering specification covering lifecycle, safety boundary,
ITT measurement, tests and honesty rules. The assistant then:

* read the assignment PDF, the pack README, every config and fixture file and the simulator source *before* designing
  anything;
* measured the data problems with throwaway scripts (counts in DATA_QUALITY.md);
* wrote the package, the tests and the docs;
* ran everything and iterated on failures.

No data was sent to any external service. The default run uses a local mock model.

How AI output was checked:

* **Execution, not inspection.** Every claim in the reports is computed by code from run artifacts
  (`impact_report.md` and `final_report.md` under `sample_run/` are generated). The pipeline was run end to end
  several times; two fresh runs gave identical numbers.
* **48 behavioural tests** on synthetic packs, including every scenario in the brief.
* **Mutation checks.** Bugs were deliberately injected to prove the tests can fail (below).
* **Cross-checking written claims against data.** Statements in the docs were re-queried before publishing, and
  several were wrong and corrected (below).

## Concrete examples of AI output that was inspected, tested, corrected or rejected

These all happened in this session and are reproducible from the code and tests.

1. **Bug caught by a test: cancellations missing from the audit trail.** The first version of the purchase handler
   closed the goal (which cancelled the viewer's in-app price message) and *then* ran the purchase cancellation, which
   found nothing left. The action was cancelled, but `purchase_after_decision` recorded `cancelled: []`, a misleading
   audit entry. `test_purchase_after_decision_cancels_and_replans` failed (`'A-…' not found in []`). Both cancellation
   paths now report into the same list.
2. **Gap found by inspecting a real viewer: purchase without a recorded re-plan.** For U-04744, a purchase arrived
   2 minutes after the decision. The kept goal (G07) does not depend on subscription status, so keeping it was right,
   but nothing showed that the agent had re-checked. A `_replan_after_purchase` step was added: it cancels goals whose
   premise the purchase invalidates, and otherwise records why the goal stands. A new test covers the invalidating case
   (a trial purchase cancels G05, and the next trigger selects G04).
3. **Flaky test traced to the fixture, not the code.** `test_prompt_injection_ignored` failed on 1 of 3 runs. The
   synthetic history gave the injected title and another title the same play count, so "most popular" depended on
   random UUID order. The fixture was made strict, and all fixture IDs became deterministic (`uuid5`). The suite was
   then run 4 times without failure.
4. **A wrong test expectation, rejected.** The first burst test asserted that every fresh trigger is dispatched
   before every stale one. It failed because the first stale trigger arrived while a worker was idle and nothing else
   was waiting. Serving it immediately is correct (a work-conserving scheduler). The assertion was rewritten to the
   real property (no stale trigger is served while a fresh one is waiting) instead of changing the scheduler to make
   the test pass.
5. **Mutation testing found weak harness patches and one real robustness gap.** Five bugs were injected:
   * consent always true;
   * cap computed on UTC days;
   * holdout ignored;
   * staleness disabled;
   * model claims trusted.

   The first three were caught at once. The other two initially "survived", but because the mutation never took
   effect: a class attribute on a frozen dataclass is ignored by instances, and claim validation lives in
   `ModelClient`, not in `parse_advice`. With effective patches, both were caught. The trusted-claim mutation also
   showed that the agent would *crash* (`StopIteration`) if an unevidenced goal ever reached it. A defensive guard
   now falls back to the deterministic priority instead.
6. **Performance claim checked with a profiler.** The first replay took 118 s. `cProfile` showed two full-table
   scans, not slow decision logic: `delivery_recorded` had no index (32 s), and `dispatch_due` loaded every scheduled
   message on every tick (40 s). After adding indexes and a `send_at <= now` query, the replay loop takes about 19 s.
   One ingest took 125 s instead of 16 s. That was re-timed in isolation and attributed to transient disk/antivirus
   I/O, not the code, and is documented as such rather than "optimised".
7. **Statements in the docs corrected after re-querying the data.**
   * The walkthrough first said viewer U-02354 had two eligible goals. The decision record shows four (G01, G02, G03,
     G05).
   * "Most buffered triggers arrive in the 10:15 burst" was changed to the measured "223 of 432".
   * The inventory table referenced a test name (`test_impact_known_dataset`) that did not exist.
8. **Suggestions rejected on policy or honesty grounds.**
   * Using `wa_new_episode_v2` for resume-series goals: rejected, because the pack has no episode release dates, so
     "naya episode aa gaya" would be an invented claim.
   * Inventing a human-readable dialect name for `dialect_name`: rejected, the catalogue code is used.
   * LLM-written push copy: rejected for deterministic templates the verifier can check.
   * A "generic popular page" fallback when the data store is down: rejected (fail closed), because idempotency and
     caps cannot be verified without state.
   * Forcing a model call on the two stale triggers that carried unsupported-claim faults so that the fixture "fires":
     rejected. The decision correctly skips planning, and the report says the fault was not exercised.
9. **Analysis choices reviewed rather than accepted.**
   * The headline is the randomized ITT over every triggered user, not served vs unserved.
   * The goal priority was fixed before the first simulator run and was **not** tuned after seeing results.
   * When the agent/holdout "no goal" counts differed (114 vs 148), it was traced to stale-only users fixed by the
     fixture (a pre-treatment chance imbalance, p = 0.04), not hidden. A balance row and a goal-holder sensitivity
     were added to the report.
   * The final estimate (+0.75 pp, CI includes 0) is reported as "no detectable effect".

## Candidate's own review

> **[TO BE COMPLETED BY THE CANDIDATE]** Add your own first-hand notes here before submitting. For example: which parts
> you read line by line, design decisions you changed or overrode, anything you disagree with, and your answer to "What
> did an AI coding assistant suggest that you chose not to accept?" The items above document what happened inside the
> AI session. Only you can attest to your own review.
